"""Reusable per-IP rate-throttle primitives.

Apps that need IP-keyed rate limiting (onboarding, password reset, future
public endpoints) inherit ``IPRateThrottleBase`` and supply:
  - ``scope`` — a short identifier used in the cache key
  - ``get_rate()`` — returns ``'N/Xunit'`` or ``None`` (no throttling)

Subclasses can also override ``cache_format`` to namespace their counters
away from the generic ``throttle_ip_*`` keys, and ``on_throttle(request, ip)``
to record violations into a blacklist or other side-channel.

The extended rate format (``'5/10m'`` style) is shared via
``parse_extended_rate`` so different layers stay consistent.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

from rest_framework.exceptions import Throttled
from rest_framework.throttling import SimpleRateThrottle


# ---------------------------------------------------------------------------
# Rate parsing
# ---------------------------------------------------------------------------

_RATE_RE = re.compile(r'^\s*(\d+)\s*/\s*(\d+)?\s*([smhd])\s*$')
_UNIT_SECONDS = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}


def parse_extended_rate(rate: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    """Parse rates like ``'5/10m'`` (5 req per 10 min) → ``(5, 600)``.
    Returns ``(None, None)`` for empty/None input.
    """
    if not rate:
        return (None, None)
    match = _RATE_RE.match(rate)
    if not match:
        raise ValueError(f'Invalid rate format: {rate!r}. Expected e.g. "5/10m".')
    num = int(match.group(1))
    multiplier = int(match.group(2)) if match.group(2) else 1
    unit = match.group(3)
    return (num, multiplier * _UNIT_SECONDS[unit])


# ---------------------------------------------------------------------------
# Client IP resolution
# ---------------------------------------------------------------------------

def client_ip(request) -> str:
    """Resolve the client IP using the project-wide proxy-aware logic.
    Prefers ``request.audit_ip`` set by AuditContextMiddleware, falling
    back to the middleware's helper for requests that haven't reached
    that middleware yet (e.g. throttle errors during preflight)."""
    cached = getattr(request, 'audit_ip', None)
    if cached:
        return cached
    from common.middleware import AuditContextMiddleware
    return AuditContextMiddleware._get_client_ip(request) or 'unknown'


# ---------------------------------------------------------------------------
# Generic IP-keyed rate throttle
# ---------------------------------------------------------------------------

GENERIC_THROTTLED_MESSAGE = 'Too many requests. Please try again later.'


class IPRateThrottleBase(SimpleRateThrottle):
    """Per-IP throttle. Subclasses set ``scope`` and override ``get_rate``."""

    scope: str = ''
    cache_format = 'throttle_ip_%(scope)s_%(ident)s'

    def __init__(self):
        # Bypass DRF's __init__ which raises if scope isn't in
        # DEFAULT_THROTTLE_RATES; rates come from the subclass instead.
        self.rate = self.get_rate()
        self.num_requests, self.duration = self.parse_rate(self.rate)

    def get_rate(self) -> Optional[str]:  # pragma: no cover — abstract
        raise NotImplementedError

    def parse_rate(self, rate):  # type: ignore[override]
        return parse_extended_rate(rate)

    def get_cache_key(self, request, view):
        return self.cache_format % {
            'scope': self.scope,
            'ident': client_ip(request),
        }

    def allow_request(self, request, view):
        if self.rate is None or self.duration is None:
            return True
        allowed = super().allow_request(request, view)
        if not allowed:
            self.on_throttle(request, client_ip(request))
            raise Throttled(detail=self.throttled_message())
        return True

    # ----- Subclass hooks ---------------------------------------------

    def on_throttle(self, request, ip: str) -> None:
        """Override to record violations / blacklist / log structured detail."""
        pass

    def throttled_message(self) -> str:
        return GENERIC_THROTTLED_MESSAGE
