"""
Cytova — Laboratory Onboarding Views (Public API)

POST /api/v1/platform/onboarding/start/         — start: identity → email code
POST /api/v1/platform/onboarding/verify-email/  — submit 6-digit code
POST /api/v1/platform/onboarding/resend-code/   — resend verification code (cooldown enforced)
POST /api/v1/platform/onboarding/complete/      — provision tenant + admin + trial
GET  /api/v1/platform/onboarding/check-slug/    — check workspace identifier availability

All endpoints are public (no authentication) and rate-limited via DRF
throttle scopes:
    auth_signup  — start / resend
    slug_check   — slug availability checks

Tenant creation only happens in the complete view; everything else mutates
only the public-schema OnboardingRegistration row.

Email enumeration defence: the start endpoint always returns the same
shape regardless of whether the email already had a registration — never
expose "this email is already registered" to unauthenticated callers.
"""
import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .onboarding_serializers import (
    OnboardingCompleteSerializer,
    OnboardingRegistrationSerializer,
    OnboardingResendCodeSerializer,
    OnboardingStartSerializer,
    OnboardingVerifyEmailSerializer,
)
from .onboarding_throttles import (
    OnboardingCompleteThrottle,
    OnboardingResendCodeThrottle,
    OnboardingStartThrottle,
    OnboardingVerifyEmailThrottle,
)
from .onboarding_service import (
    EmailDeliveryError,
    InvalidVerificationCode,
    OnboardingError,
    OnboardingNotFound,
    OnboardingNotReady,
    OnboardingService,
    ResendCooldown,
    VerificationCodeExpired,
    VerificationLocked,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _envelope(data=None, errors=None, http_status=status.HTTP_200_OK):
    return Response(
        {'data': data, 'meta': None, 'errors': errors or []},
        status=http_status,
    )


def _error_response(err: OnboardingError, http_status: int):
    detail: dict = {}
    if isinstance(err, InvalidVerificationCode):
        detail['attempts_remaining'] = err.attempts_remaining
    elif isinstance(err, VerificationLocked):
        detail['locked_until'] = err.locked_until.isoformat() if err.locked_until else None
    elif isinstance(err, ResendCooldown):
        detail['retry_after_seconds'] = err.retry_after_seconds
    return _envelope(
        errors=[{
            'code': err.code,
            'message': err.message,
            'field': None,
            'detail': detail,
        }],
        http_status=http_status,
    )


def _registration_payload(registration):
    return OnboardingRegistrationSerializer(registration).data


# ---------------------------------------------------------------------------
# Step 1 — start
# ---------------------------------------------------------------------------

class OnboardingStartView(APIView):
    authentication_classes = []
    permission_classes = []
    # Two layers: scoped (deployment-wide auth_signup limit) + IP-keyed
    # (per-endpoint, blacklist-aware). Both must allow the request.
    throttle_classes = [OnboardingStartThrottle, ScopedRateThrottle]
    throttle_scope = 'auth_signup'

    def post(self, request):
        serializer = OnboardingStartSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            registration = OnboardingService.start(**serializer.validated_data)
        except EmailDeliveryError as e:
            # Provider rejected the send (e.g. unverified Brevo sender,
            # invalid API key, network outage). Surface as a distinct
            # error code so the operator can fix configuration without
            # mistaking it for a generic crash.
            return _error_response(e, status.HTTP_503_SERVICE_UNAVAILABLE)
        except OnboardingError as e:
            return _error_response(e, status.HTTP_400_BAD_REQUEST)
        except Exception:
            logger.exception('Onboarding start failed')
            return _envelope(
                errors=[{
                    'code': 'INTERNAL_ERROR',
                    'message': 'Could not start onboarding. Please try again.',
                    'field': None,
                    'detail': {},
                }],
                http_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return _envelope(_registration_payload(registration), http_status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Step 2 — verify email
# ---------------------------------------------------------------------------

class OnboardingVerifyEmailView(APIView):
    authentication_classes = []
    permission_classes = []
    throttle_classes = [OnboardingVerifyEmailThrottle, ScopedRateThrottle]
    throttle_scope = 'auth_login'  # tighter throttle since this is a code-guessing surface

    def post(self, request):
        serializer = OnboardingVerifyEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            registration = OnboardingService.verify_email(**serializer.validated_data)
        except OnboardingNotFound as e:
            return _error_response(e, status.HTTP_404_NOT_FOUND)
        except VerificationLocked as e:
            return _error_response(e, status.HTTP_429_TOO_MANY_REQUESTS)
        except VerificationCodeExpired as e:
            return _error_response(e, status.HTTP_400_BAD_REQUEST)
        except InvalidVerificationCode as e:
            return _error_response(e, status.HTTP_400_BAD_REQUEST)
        return _envelope(_registration_payload(registration))


# ---------------------------------------------------------------------------
# Resend code
# ---------------------------------------------------------------------------

class OnboardingResendCodeView(APIView):
    authentication_classes = []
    permission_classes = []
    throttle_classes = [OnboardingResendCodeThrottle, ScopedRateThrottle]
    throttle_scope = 'auth_signup'

    def post(self, request):
        serializer = OnboardingResendCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            registration = OnboardingService.resend_code(**serializer.validated_data)
        except OnboardingNotFound as e:
            return _error_response(e, status.HTTP_404_NOT_FOUND)
        except VerificationLocked as e:
            return _error_response(e, status.HTTP_429_TOO_MANY_REQUESTS)
        except ResendCooldown as e:
            return _error_response(e, status.HTTP_429_TOO_MANY_REQUESTS)
        except EmailDeliveryError as e:
            return _error_response(e, status.HTTP_503_SERVICE_UNAVAILABLE)
        return _envelope(_registration_payload(registration))


# ---------------------------------------------------------------------------
# Step 4 — complete (tenant provisioning happens here, NOT before)
# ---------------------------------------------------------------------------

class OnboardingCompleteView(APIView):
    authentication_classes = []
    permission_classes = []
    throttle_classes = [OnboardingCompleteThrottle, ScopedRateThrottle]
    throttle_scope = 'auth_signup'

    def post(self, request):
        serializer = OnboardingCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = OnboardingService.complete(**serializer.validated_data)
        except OnboardingNotFound as e:
            return _error_response(e, status.HTTP_404_NOT_FOUND)
        except OnboardingNotReady as e:
            return _error_response(e, status.HTTP_409_CONFLICT)
        except Exception:
            logger.exception('Onboarding completion failed')
            return _envelope(
                errors=[{
                    'code': 'INTERNAL_ERROR',
                    'message': 'An error occurred during laboratory setup. Please try again.',
                    'field': None,
                    'detail': {},
                }],
                http_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        subscription = result.subscription
        trial_end = subscription.trial_end_date if subscription else None
        trial_days = (
            subscription.plan.trial_duration_days
            if subscription and subscription.plan else None
        )

        return _envelope(
            {
                'tenant_id': str(result.tenant.id),
                'laboratory_name': result.tenant.name,
                'slug': result.tenant.subdomain,
                'domain': result.domain,
                'admin_email': result.admin_user.email,
                'trial_end_date': trial_end.isoformat() if trial_end else None,
                'trial_duration_days': trial_days,
            },
            http_status=status.HTTP_201_CREATED,
        )


# ---------------------------------------------------------------------------
# Slug availability (unchanged)
# ---------------------------------------------------------------------------

class SlugAvailabilityView(APIView):
    """
    GET /api/v1/platform/onboarding/check-slug/?slug=my-lab
    """
    authentication_classes = []
    permission_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'slug_check'

    def get(self, request):
        from apps.tenants.models import Tenant
        from .onboarding_serializers import RESERVED_SLUGS, SLUG_RE

        raw_slug = request.query_params.get('slug', '').lower().strip()

        if not raw_slug or len(raw_slug) < 3:
            return _envelope({'available': False, 'slug': raw_slug, 'reason': 'Too short (min 3 characters).'})

        if not SLUG_RE.match(raw_slug):
            return _envelope({'available': False, 'slug': raw_slug, 'reason': 'Invalid format.'})

        if raw_slug in RESERVED_SLUGS:
            return _envelope({'available': False, 'slug': raw_slug, 'reason': 'Reserved name.'})

        exists = Tenant.objects.filter(subdomain=raw_slug).exists()

        return _envelope({
            'available': not exists,
            'slug': raw_slug,
            'reason': 'Already taken.' if exists else None,
        })
