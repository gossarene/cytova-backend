"""Per-IP rate throttles for password reset endpoints.

Configured via ``settings.PASSWORD_RESET_RATE_LIMITS`` — same extended-rate
format as onboarding (``'5/10m'`` etc.). Independent counters from
onboarding (different cache key prefix) so abuse on one surface doesn't
spill into the other.
"""
from __future__ import annotations

import logging
from typing import Optional

from django.conf import settings

from common.throttling import IPRateThrottleBase, client_ip

logger = logging.getLogger(__name__)


class PasswordResetThrottleBase(IPRateThrottleBase):
    cache_format = 'throttle_password_reset_%(scope)s_%(ident)s'

    def get_rate(self) -> Optional[str]:
        rates = getattr(settings, 'PASSWORD_RESET_RATE_LIMITS', {}) or {}
        return rates.get(self.scope)

    def on_throttle(self, request, ip: str) -> None:
        logger.warning(
            'Password reset rate limit exceeded: scope=%s ip=%s path=%s limit=%s',
            self.scope, ip, request.path, self.rate,
        )


class PasswordResetRequestThrottle(PasswordResetThrottleBase):
    """Throttle for /auth/password-reset/request/ — limits how often a
    single IP can ask for new reset emails. Acts as the IP-side complement
    to per-account token invalidation."""
    scope = 'request'


class PasswordResetConfirmThrottle(PasswordResetThrottleBase):
    """Throttle for /auth/password-reset/confirm/ — limits brute-force
    guessing of the random token, even though the token itself is
    cryptographically secure."""
    scope = 'confirm'
