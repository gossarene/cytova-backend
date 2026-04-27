"""
Cytova — Onboarding IP throttles + temporary blacklist

Two-layer abuse protection at the IP level:

  1. ``OnboardingIPThrottleBase`` — DRF throttle keyed on the client IP
     and configurable per onboarding endpoint via ``ONBOARDING_RATE_LIMITS``.

  2. ``IPBlacklist`` — cache-backed counter that promotes an IP to a
     temporary blacklist after a configurable number of rate-limit hits
     within an observation window. Blacklisted IPs are denied for a
     configurable duration, regardless of which onboarding endpoint they
     hit. Independent from per-registration lockout in OnboardingService;
     both layers are evaluated.

The reusable rate-parsing + IP throttle base were extracted to
``common.throttling`` so other apps (password reset, future public
endpoints) share the same primitives. This module re-exports
``parse_extended_rate`` for backward compatibility with importers that
were already wired against it here.

This module never logs verification codes, passwords, request bodies, or
any data outside ``ip / scope / path / counter`` — see logging calls.
"""
from __future__ import annotations

import logging
from typing import Optional

from django.conf import settings
from django.core.cache import cache
from rest_framework.exceptions import Throttled

from common.throttling import (
    GENERIC_THROTTLED_MESSAGE as GENERIC_BLOCKED_MESSAGE,
    IPRateThrottleBase,
    client_ip as _client_ip,
    parse_extended_rate,  # re-export for backward compat
)

__all__ = [
    'GENERIC_BLOCKED_MESSAGE',
    'IPBlacklist',
    'OnboardingIPThrottleBase',
    'OnboardingStartThrottle',
    'OnboardingVerifyEmailThrottle',
    'OnboardingResendCodeThrottle',
    'OnboardingCompleteThrottle',
    'parse_extended_rate',
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Temporary IP blacklist
# ---------------------------------------------------------------------------

class IPBlacklist:
    """Cache-backed temporary IP blacklist with two keys per IP:

      ``onboarding:ip-violations:<ip>`` — incremented on each rate-limit
        hit. TTL = ``ONBOARDING_IP_BLACKLIST_WINDOW_SECONDS`` so old
        violations don't accumulate forever.

      ``onboarding:ip-blocked:<ip>`` — set when violation count crosses
        the configured threshold. TTL = ``ONBOARDING_IP_BLACKLIST_DURATION_SECONDS``.
    """

    @staticmethod
    def _violation_key(ip: str) -> str:
        return f'onboarding:ip-violations:{ip}'

    @staticmethod
    def _block_key(ip: str) -> str:
        return f'onboarding:ip-blocked:{ip}'

    # ----- Configuration accessors (read each call so settings overrides
    # via @override_settings in tests are picked up) ---------------------

    @staticmethod
    def _threshold() -> int:
        return int(getattr(settings, 'ONBOARDING_IP_BLACKLIST_THRESHOLD', 3))

    @staticmethod
    def _window() -> int:
        return int(getattr(settings, 'ONBOARDING_IP_BLACKLIST_WINDOW_SECONDS', 3600))

    @staticmethod
    def _duration() -> int:
        return int(getattr(settings, 'ONBOARDING_IP_BLACKLIST_DURATION_SECONDS', 1800))

    # ----- Public API --------------------------------------------------

    @classmethod
    def is_blacklisted(cls, ip: str) -> bool:
        if not ip or ip == 'unknown':
            return False
        return cache.get(cls._block_key(ip)) is not None

    @classmethod
    def blacklist(cls, ip: str, duration_seconds: Optional[int] = None) -> None:
        """Force-blacklist an IP (used by record_violation; exposed for tests)."""
        if not ip:
            return
        cache.set(cls._block_key(ip), True, duration_seconds or cls._duration())

    @classmethod
    def record_violation(cls, ip: str, scope: str) -> int:
        """Increment the violation counter for ``ip`` (auto-expires after
        the observation window) and promote to blacklist if the threshold
        is crossed. Returns the new violation count."""
        if not ip or ip == 'unknown':
            return 0
        key = cls._violation_key(ip)
        cache.add(key, 0, cls._window())
        try:
            count = cache.incr(key)
        except ValueError:
            cache.set(key, 1, cls._window())
            count = 1

        if count >= cls._threshold():
            cls.blacklist(ip)
            logger.warning(
                'Onboarding IP blacklisted: ip=%s violations=%d trigger_scope=%s duration=%ds',
                ip, count, scope, cls._duration(),
            )
        return count


# ---------------------------------------------------------------------------
# Onboarding-specific throttle (adds blacklist enforcement)
# ---------------------------------------------------------------------------

class OnboardingIPThrottleBase(IPRateThrottleBase):
    """Per-IP throttle for an onboarding endpoint.

    Adds two behaviours on top of the generic IP throttle base:
      - blacklist precedence (blacklisted IPs are denied before any rate counting)
      - rate-limit hits feed the IPBlacklist violation counter
    """

    cache_format = 'throttle_onboarding_%(scope)s_%(ident)s'

    def get_rate(self) -> Optional[str]:
        rates = getattr(settings, 'ONBOARDING_RATE_LIMITS', {}) or {}
        return rates.get(self.scope)

    def allow_request(self, request, view):
        ip = _client_ip(request)

        if IPBlacklist.is_blacklisted(ip):
            logger.warning(
                'Onboarding request blocked (IP blacklisted): scope=%s ip=%s path=%s',
                self.scope, ip, request.path,
            )
            raise Throttled(detail=GENERIC_BLOCKED_MESSAGE)

        return super().allow_request(request, view)

    def on_throttle(self, request, ip: str) -> None:
        IPBlacklist.record_violation(ip, self.scope)
        logger.warning(
            'Onboarding rate limit exceeded: scope=%s ip=%s path=%s limit=%s',
            self.scope, ip, request.path, self.rate,
        )


class OnboardingStartThrottle(OnboardingIPThrottleBase):
    scope = 'start'


class OnboardingVerifyEmailThrottle(OnboardingIPThrottleBase):
    scope = 'verify_email'


class OnboardingResendCodeThrottle(OnboardingIPThrottleBase):
    scope = 'resend_code'


class OnboardingCompleteThrottle(OnboardingIPThrottleBase):
    scope = 'complete'
