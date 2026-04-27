"""
Tests for IP-based onboarding rate limiting and temporary blacklist.

Strategy: drive the throttle classes directly with synthesised request
objects instead of going through the full Django test client. This keeps
the tests fast and avoids the long-standing pytest-django TRUNCATE-FK
teardown issue triggered by tests that create tenants. The service-level
behaviour (per-registration lockout, code expiry, etc.) is covered in
``test_onboarding.py``; this file focuses purely on the throttle layer.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from django.core.cache import cache
from django.test import override_settings
from rest_framework.exceptions import Throttled

from apps.tenants.onboarding_throttles import (
    GENERIC_BLOCKED_MESSAGE,
    IPBlacklist,
    OnboardingCompleteThrottle,
    OnboardingResendCodeThrottle,
    OnboardingStartThrottle,
    OnboardingVerifyEmailThrottle,
    parse_extended_rate,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_cache():
    """Throttle and blacklist state lives in the default cache. Wipe between
    tests so counters don't leak."""
    cache.clear()
    yield
    cache.clear()


def _request(ip: str = '203.0.113.10', path: str = '/api/v1/platform/onboarding/start/'):
    """Build a minimal request-like object with the attributes the throttle
    inspects: ``audit_ip`` (preferred) and ``path``."""
    return SimpleNamespace(audit_ip=ip, path=path, META={})


# ---------------------------------------------------------------------------
# Rate parser
# ---------------------------------------------------------------------------

class TestParseExtendedRate:

    @pytest.mark.parametrize('rate,expected', [
        ('5/10m', (5, 600)),
        ('10/10m', (10, 600)),
        ('3/h', (3, 3600)),
        ('1/30s', (1, 30)),
        ('100/d', (100, 86400)),
        (None, (None, None)),
        ('', (None, None)),
    ])
    def test_valid(self, rate, expected):
        assert parse_extended_rate(rate) == expected

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_extended_rate('5/minute')


# ---------------------------------------------------------------------------
# Per-endpoint throttle
# ---------------------------------------------------------------------------

class TestIPRateLimit:

    def test_under_limit_allows(self):
        with override_settings(ONBOARDING_RATE_LIMITS={'start': '3/10m'}):
            throttle = OnboardingStartThrottle()
            assert throttle.allow_request(_request(), None) is True
            assert throttle.allow_request(_request(), None) is True

    def test_over_limit_raises_throttled_with_generic_message(self):
        with override_settings(
            ONBOARDING_RATE_LIMITS={'start': '2/10m'},
            ONBOARDING_IP_BLACKLIST_THRESHOLD=99,  # take blacklist out of the picture
        ):
            throttle = OnboardingStartThrottle()
            assert throttle.allow_request(_request(), None) is True
            assert throttle.allow_request(_request(), None) is True
            with pytest.raises(Throttled) as exc:
                throttle.allow_request(_request(), None)
            assert str(exc.value.detail) == GENERIC_BLOCKED_MESSAGE

    def test_limits_are_per_ip(self):
        """An IP at its limit shouldn't affect a different IP."""
        with override_settings(
            ONBOARDING_RATE_LIMITS={'start': '1/10m'},
            ONBOARDING_IP_BLACKLIST_THRESHOLD=99,
        ):
            throttle = OnboardingStartThrottle()
            assert throttle.allow_request(_request(ip='203.0.113.1'), None) is True
            with pytest.raises(Throttled):
                throttle.allow_request(_request(ip='203.0.113.1'), None)
            # Different IP — fresh budget.
            assert throttle.allow_request(_request(ip='203.0.113.2'), None) is True

    def test_each_endpoint_has_its_own_budget(self):
        """start/verify/resend/complete use independent counters even from
        the same IP, mirroring the per-scope cache key."""
        with override_settings(
            ONBOARDING_RATE_LIMITS={
                'start': '1/10m',
                'verify_email': '10/10m',
                'resend_code': '3/10m',
                'complete': '5/10m',
            },
            ONBOARDING_IP_BLACKLIST_THRESHOLD=99,
        ):
            ip = '203.0.113.7'
            assert OnboardingStartThrottle().allow_request(_request(ip=ip), None) is True
            with pytest.raises(Throttled):
                OnboardingStartThrottle().allow_request(_request(ip=ip), None)
            # verify-email / resend / complete are still wide open.
            assert OnboardingVerifyEmailThrottle().allow_request(_request(ip=ip), None) is True
            assert OnboardingResendCodeThrottle().allow_request(_request(ip=ip), None) is True
            assert OnboardingCompleteThrottle().allow_request(_request(ip=ip), None) is True

    def test_no_rate_configured_means_no_throttling(self):
        with override_settings(ONBOARDING_RATE_LIMITS={}):
            throttle = OnboardingStartThrottle()
            for _ in range(50):
                assert throttle.allow_request(_request(), None) is True


# ---------------------------------------------------------------------------
# Temporary IP blacklist
# ---------------------------------------------------------------------------

class TestIPBlacklist:

    def test_record_violation_increments_counter(self):
        IPBlacklist.record_violation('203.0.113.20', 'start')
        IPBlacklist.record_violation('203.0.113.20', 'start')
        assert cache.get(IPBlacklist._violation_key('203.0.113.20')) == 2

    def test_blacklist_kicks_in_at_threshold(self):
        with override_settings(ONBOARDING_IP_BLACKLIST_THRESHOLD=2):
            IPBlacklist.record_violation('203.0.113.30', 'start')
            assert not IPBlacklist.is_blacklisted('203.0.113.30')
            IPBlacklist.record_violation('203.0.113.30', 'start')
            assert IPBlacklist.is_blacklisted('203.0.113.30')

    def test_blacklisted_ip_blocked_on_every_endpoint(self):
        """Blacklist applies across all onboarding endpoints, not only the
        scope that triggered it."""
        IPBlacklist.blacklist('203.0.113.40')
        for ThrottleCls in [
            OnboardingStartThrottle,
            OnboardingVerifyEmailThrottle,
            OnboardingResendCodeThrottle,
            OnboardingCompleteThrottle,
        ]:
            throttle = ThrottleCls()
            with pytest.raises(Throttled) as exc:
                throttle.allow_request(_request(ip='203.0.113.40'), None)
            assert str(exc.value.detail) == GENERIC_BLOCKED_MESSAGE

    def test_blacklist_promotes_after_repeated_rate_limit_hits(self):
        """End-to-end: rate-limit an IP enough times that it crosses the
        blacklist threshold via the throttle's record_violation hook, then
        confirm subsequent unrelated endpoints are blocked."""
        with override_settings(
            ONBOARDING_RATE_LIMITS={'start': '1/10m', 'verify_email': '10/10m'},
            ONBOARDING_IP_BLACKLIST_THRESHOLD=2,
        ):
            ip = '203.0.113.50'

            # Each iteration: 1 success + 1 throttled (= 1 violation recorded).
            for _ in range(2):
                throttle = OnboardingStartThrottle()
                throttle.allow_request(_request(ip=ip), None)  # success
                with pytest.raises(Throttled):
                    OnboardingStartThrottle().allow_request(_request(ip=ip), None)
                cache.delete(throttle.get_cache_key(_request(ip=ip), None))  # reset rate window for next iter

            # IP now has 2 violations → blacklisted → verify-email also blocks.
            assert IPBlacklist.is_blacklisted(ip)
            with pytest.raises(Throttled):
                OnboardingVerifyEmailThrottle().allow_request(_request(ip=ip), None)

    def test_blacklist_expires(self):
        IPBlacklist.blacklist('203.0.113.60', duration_seconds=1)
        assert IPBlacklist.is_blacklisted('203.0.113.60')
        time.sleep(1.1)
        assert not IPBlacklist.is_blacklisted('203.0.113.60')

    def test_unknown_ip_is_never_blacklisted(self):
        # Defensive: don't propagate a blacklist for a missing IP.
        IPBlacklist.record_violation('', 'start')
        IPBlacklist.record_violation('unknown', 'start')
        assert not IPBlacklist.is_blacklisted('')
        assert not IPBlacklist.is_blacklisted('unknown')


# ---------------------------------------------------------------------------
# Independence from per-registration lockout
# ---------------------------------------------------------------------------

class TestIndependentFromRegistrationLockout:
    """The IP throttle is purely additive — it does not read or mutate the
    per-registration ``failed_attempts`` / ``locked_until`` counters set by
    OnboardingService.verify_email. Confirm by direct inspection of
    ``OnboardingRegistration`` after a throttled request."""

    @pytest.mark.django_db
    def test_throttling_does_not_touch_registration_state(self):
        from apps.tenants.models import OnboardingRegistration, OnboardingStatus

        # Fabricate a registration row directly — no need to spin up the
        # full service to test the orthogonality guarantee.
        reg = OnboardingRegistration.objects.create(
            first_name='A', last_name='B', email='unique-throttle@example.com',
            phone='+33 1', verification_code_hash='deadbeef' * 8,
            status=OnboardingStatus.PENDING_EMAIL,
        )
        before_failed = reg.failed_attempts
        before_locked = reg.locked_until

        with override_settings(
            ONBOARDING_RATE_LIMITS={'verify_email': '1/10m'},
            ONBOARDING_IP_BLACKLIST_THRESHOLD=99,
        ):
            throttle = OnboardingVerifyEmailThrottle()
            throttle.allow_request(_request(ip='203.0.113.70'), None)
            with pytest.raises(Throttled):
                throttle.allow_request(_request(ip='203.0.113.70'), None)

        reg.refresh_from_db()
        assert reg.failed_attempts == before_failed
        assert reg.locked_until == before_locked
