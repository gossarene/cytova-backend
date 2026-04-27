"""
Tests for the tenant-aware password-reset flow.

Coverage:
  - token created only for existing active users (no leak otherwise)
  - email goes through EmailService (provider abstraction respected)
  - token persisted as SHA-256 hash, never plaintext
  - 30-minute TTL
  - generated reset link uses the request host (tenant subdomain)
  - confirm: valid token resets password and marks token used
  - confirm: expired / used / unknown tokens rejected
  - confirm: previously unused tokens for same user are invalidated on success
  - per-IP throttle on request + confirm

The autouse fixture `_in_tenant_schema` (root conftest) puts every test
inside the test tenant schema, so StaffUser / PasswordResetToken queries
all hit the per-tenant tables — no cross-tenant leakage is possible.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.exceptions import Throttled

from apps.authentication.services import AuthService, PASSWORD_RESET_TTL_MINUTES
from apps.authentication.throttles import (
    PasswordResetConfirmThrottle,
    PasswordResetRequestThrottle,
)
from apps.users.models import PasswordResetToken
from common.email.providers.base import EmailMessage, EmailResult
from common.utils.crypto import hash_token


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _request(host: str = 'testlab.localhost', ip: str = '203.0.113.1', secure: bool = False):
    """Minimal request stand-in with the attributes the service touches."""
    req = SimpleNamespace()
    req.audit_ip = ip
    req.get_host = lambda: host
    req.is_secure = lambda: secure
    req.path = '/api/v1/auth/password-reset/request/'
    req.META = {}
    return req


@pytest.fixture
def email_capture(monkeypatch):
    """Patch the EmailService factory used by AuthService so password-reset
    emails are captured rather than dispatched. Returns a list — the most
    recent send is `captured[-1]` (an EmailMessage)."""
    captured: list[EmailMessage] = []

    class _StubProvider:
        name = 'stub'

        def send(self, message: EmailMessage) -> EmailResult:
            captured.append(message)
            return EmailResult(ok=True)

    from common.email import EmailService

    monkeypatch.setattr(
        'apps.authentication.services.get_email_service',
        lambda: EmailService(provider=_StubProvider()),
    )
    return captured


@pytest.fixture(autouse=True)
def _clean_throttle_cache():
    """Throttle counters live in the default cache; wipe between tests."""
    cache.clear()
    yield
    cache.clear()


# ---------------------------------------------------------------------------
# request_password_reset
# ---------------------------------------------------------------------------

class TestRequestPasswordReset:

    def test_creates_token_only_for_existing_user(self, lab_admin, email_capture):
        AuthService.request_password_reset(lab_admin.email, _request())
        assert PasswordResetToken.objects.filter(user=lab_admin).count() == 1
        assert len(email_capture) == 1

    def test_unknown_email_creates_no_token_and_sends_no_email(self, email_capture):
        AuthService.request_password_reset('not-a-user@example.com', _request())
        assert PasswordResetToken.objects.count() == 0
        assert email_capture == []
        # Service returns None either way — same response shape protects
        # against email-existence enumeration at the view layer.

    def test_inactive_user_treated_as_unknown(self, lab_admin, email_capture):
        lab_admin.is_active = False
        lab_admin.save()
        AuthService.request_password_reset(lab_admin.email, _request())
        assert PasswordResetToken.objects.count() == 0
        assert email_capture == []

    def test_token_stored_as_sha256_hash_not_plaintext(self, lab_admin, email_capture):
        AuthService.request_password_reset(lab_admin.email, _request())
        token = PasswordResetToken.objects.get(user=lab_admin)
        # 64 hex chars (SHA-256), no slashes, no equals — clearly not a base64 token.
        assert len(token.token_hash) == 64
        assert all(c in '0123456789abcdef' for c in token.token_hash)
        # The plaintext token reaches only the email body — never the row.
        msg = email_capture[-1]
        import re
        m = re.search(r'token=([A-Za-z0-9_\-]+)', msg.text)
        assert m is not None
        plaintext = m.group(1)
        assert plaintext != token.token_hash
        assert hash_token(plaintext) == token.token_hash

    def test_ttl_is_30_minutes(self, lab_admin, email_capture):
        before = timezone.now()
        AuthService.request_password_reset(lab_admin.email, _request())
        after = timezone.now()
        token = PasswordResetToken.objects.get(user=lab_admin)
        # Allow a small window around `before`/`after` to absorb call latency.
        assert before + timedelta(minutes=29, seconds=55) <= token.expires_at
        assert token.expires_at <= after + timedelta(minutes=PASSWORD_RESET_TTL_MINUTES, seconds=5)

    def test_creating_a_new_token_invalidates_previous_unused_ones(self, lab_admin, email_capture):
        AuthService.request_password_reset(lab_admin.email, _request())
        AuthService.request_password_reset(lab_admin.email, _request())
        # Two rows total, but only the latest is_used=False.
        assert PasswordResetToken.objects.filter(user=lab_admin).count() == 2
        active = PasswordResetToken.objects.filter(user=lab_admin, is_used=False)
        assert active.count() == 1

    @override_settings(DEBUG=True, CYTOVA_DEV_FRONTEND_PORT=3000)
    def test_reset_link_uses_request_host_in_dev(self, lab_admin, email_capture):
        AuthService.request_password_reset(
            lab_admin.email, _request(host='veno-lab.cytova.io:8000'),
        )
        msg = email_capture[-1]
        # Tenant subdomain preserved; backend port (8000) replaced by frontend port (3000) in dev.
        assert 'http://veno-lab.cytova.io:3000/reset-password?token=' in msg.text
        # Link must NOT carry the backend port or any other tenant subdomain.
        assert ':8000' not in msg.text
        assert 'localhost' not in msg.text

    @override_settings(DEBUG=False)
    def test_reset_link_uses_https_same_origin_in_prod(self, lab_admin, email_capture):
        # Same-origin proxy in prod: scheme=https (request.is_secure → True), no port.
        AuthService.request_password_reset(
            lab_admin.email, _request(host='veno-lab.cytova.io', secure=True),
        )
        msg = email_capture[-1]
        assert 'https://veno-lab.cytova.io/reset-password?token=' in msg.text
        assert ':3000' not in msg.text
        assert ':8000' not in msg.text

    def test_token_records_requester_ip(self, lab_admin, email_capture):
        AuthService.request_password_reset(lab_admin.email, _request(ip='198.51.100.42'))
        token = PasswordResetToken.objects.get(user=lab_admin)
        assert token.created_by_ip == '198.51.100.42'

    def test_email_send_failure_does_not_raise(self, lab_admin, monkeypatch):
        """Provider failure must not propagate — generic 200 is the contract."""
        from common.email import EmailService

        class _FailingProvider:
            name = 'failing'

            def send(self, message):
                return EmailResult(ok=False, error='http_400')

        monkeypatch.setattr(
            'apps.authentication.services.get_email_service',
            lambda: EmailService(provider=_FailingProvider()),
        )
        # Must not raise.
        AuthService.request_password_reset(lab_admin.email, _request())
        # Token is still persisted — operator can investigate via logs.
        assert PasswordResetToken.objects.filter(user=lab_admin).count() == 1


# ---------------------------------------------------------------------------
# confirm_password_reset
# ---------------------------------------------------------------------------

class TestConfirmPasswordReset:

    def _issue_token(self, user, email_capture, host='testlab.localhost'):
        AuthService.request_password_reset(user.email, _request(host=host))
        msg = email_capture[-1]
        import re
        return re.search(r'token=([A-Za-z0-9_\-]+)', msg.text).group(1)

    def test_valid_token_resets_password(self, lab_admin, email_capture):
        token = self._issue_token(lab_admin, email_capture)
        new_password = 'N3wStr0ng!Pass#2026'

        ok = AuthService.confirm_password_reset(token, new_password, _request())
        assert ok is True
        lab_admin.refresh_from_db()
        assert lab_admin.check_password(new_password)
        # Token consumed.
        row = PasswordResetToken.objects.get(user=lab_admin)
        assert row.is_used is True
        assert row.used_at is not None

    def test_expired_token_rejected(self, lab_admin, email_capture):
        token = self._issue_token(lab_admin, email_capture)
        PasswordResetToken.objects.filter(user=lab_admin).update(
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        ok = AuthService.confirm_password_reset(token, 'N3wStr0ng!Pass#2026', _request())
        assert ok is False

    def test_used_token_rejected_on_second_attempt(self, lab_admin, email_capture):
        token = self._issue_token(lab_admin, email_capture)
        AuthService.confirm_password_reset(token, 'N3wStr0ng!Pass#2026', _request())
        ok = AuthService.confirm_password_reset(token, 'AnotherStr0ng!Pass', _request())
        assert ok is False

    def test_unknown_token_rejected(self):
        ok = AuthService.confirm_password_reset('totally-fake-token', 'N3wStr0ng!Pass', _request())
        assert ok is False

    def test_other_outstanding_tokens_invalidated_on_success(self, lab_admin, email_capture):
        # Create two consecutive tokens — first is auto-invalidated on second
        # request, second is the live one. Capture the second's plaintext.
        AuthService.request_password_reset(lab_admin.email, _request())
        token = self._issue_token(lab_admin, email_capture)

        # Confirm the live token — there shouldn't be any unused tokens left.
        AuthService.confirm_password_reset(token, 'N3wStr0ng!Pass#2026', _request())
        assert PasswordResetToken.objects.filter(
            user=lab_admin, is_used=False,
        ).count() == 0


# ---------------------------------------------------------------------------
# Throttling
# ---------------------------------------------------------------------------

class TestPasswordResetThrottling:

    def test_request_throttle_kicks_in_at_limit(self):
        with override_settings(PASSWORD_RESET_RATE_LIMITS={'request': '2/10m'}):
            t = PasswordResetRequestThrottle()
            req = _request(ip='203.0.113.99')
            assert t.allow_request(req, None) is True
            assert t.allow_request(req, None) is True
            with pytest.raises(Throttled):
                t.allow_request(req, None)

    def test_confirm_throttle_kicks_in_at_limit(self):
        with override_settings(PASSWORD_RESET_RATE_LIMITS={'confirm': '2/10m'}):
            t = PasswordResetConfirmThrottle()
            req = _request(ip='203.0.113.100')
            assert t.allow_request(req, None) is True
            assert t.allow_request(req, None) is True
            with pytest.raises(Throttled):
                t.allow_request(req, None)

    def test_request_and_confirm_have_independent_counters(self):
        with override_settings(PASSWORD_RESET_RATE_LIMITS={'request': '1/10m', 'confirm': '5/10m'}):
            req = _request(ip='203.0.113.101')
            PasswordResetRequestThrottle().allow_request(req, None)
            with pytest.raises(Throttled):
                PasswordResetRequestThrottle().allow_request(req, None)
            # Confirm scope unaffected.
            assert PasswordResetConfirmThrottle().allow_request(req, None) is True

    def test_per_ip_isolation(self):
        with override_settings(PASSWORD_RESET_RATE_LIMITS={'request': '1/10m'}):
            t = PasswordResetRequestThrottle()
            t.allow_request(_request(ip='1.1.1.1'), None)
            with pytest.raises(Throttled):
                t.allow_request(_request(ip='1.1.1.1'), None)
            # Second IP is fresh.
            assert t.allow_request(_request(ip='2.2.2.2'), None) is True


# ---------------------------------------------------------------------------
# Logging hygiene — no token / no password ever logged
# ---------------------------------------------------------------------------

class TestNoSecretsInLogs:

    def test_no_token_in_logs_on_success(self, lab_admin, email_capture, caplog):
        AuthService.request_password_reset(lab_admin.email, _request())
        msg = email_capture[-1]
        import re
        plaintext = re.search(r'token=([A-Za-z0-9_\-]+)', msg.text).group(1)

        with caplog.at_level('INFO', logger='apps.authentication.services'):
            AuthService.confirm_password_reset(plaintext, 'N3wStr0ng!Pass#2026', _request())

        joined = ' '.join(rec.getMessage() for rec in caplog.records)
        assert plaintext not in joined
        assert 'N3wStr0ng' not in joined  # no password fragment either

    def test_no_token_in_logs_on_invalid_attempt(self, caplog):
        with caplog.at_level('WARNING', logger='apps.authentication.services'):
            AuthService.confirm_password_reset('plaintext-fake-token-xyz', 'N3wStr0ng', _request())
        joined = ' '.join(rec.getMessage() for rec in caplog.records)
        assert 'plaintext-fake-token-xyz' not in joined
