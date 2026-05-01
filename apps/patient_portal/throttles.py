"""
Cytova — Patient Portal IP throttles.

Mirrors the per-IP pattern used by the laboratory onboarding endpoints
(``apps/tenants/onboarding_throttles.py``) but without the temporary
blacklist layer — patient signup is a much lower-traffic surface and
the simple rate cap is enough for the foundation. The reusable
``IPRateThrottleBase`` from ``common.throttling`` handles parsing,
cache keys, and the IP-resolution helper.

Rates are read from ``settings.PATIENT_PORTAL_RATE_LIMITS`` so test
suites and ops can tune them without code changes.
"""
from __future__ import annotations

from typing import Optional

from django.conf import settings

from common.throttling import IPRateThrottleBase


class PatientPortalIPThrottleBase(IPRateThrottleBase):
    """Per-IP throttle for a patient-portal endpoint. Subclasses set
    ``scope`` matching a key in ``PATIENT_PORTAL_RATE_LIMITS``."""

    cache_format = 'throttle_patient_portal_%(scope)s_%(ident)s'

    def get_rate(self) -> Optional[str]:
        rates = getattr(settings, 'PATIENT_PORTAL_RATE_LIMITS', {}) or {}
        return rates.get(self.scope)


class PatientSignupThrottle(PatientPortalIPThrottleBase):
    scope = 'signup'


class PatientLoginThrottle(PatientPortalIPThrottleBase):
    scope = 'login'


class PatientVerifyEmailThrottle(PatientPortalIPThrottleBase):
    scope = 'verify_email'
