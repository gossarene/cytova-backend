"""
Cytova — Laboratory Onboarding Views (Public API)

POST /api/v1/onboarding/signup/      — self-service laboratory signup
GET  /api/v1/onboarding/check-slug/  — slug availability check

Both endpoints are public (no authentication) and rate-limited via
DRF throttle scopes configured in REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']:
    auth_signup  — signup requests per IP
    slug_check   — slug availability checks per IP
"""
import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .onboarding_serializers import LaboratorySignupSerializer
from .onboarding_service import OnboardingService

logger = logging.getLogger(__name__)


class LaboratorySignupView(APIView):
    """
    POST /api/v1/onboarding/signup/

    Public endpoint for laboratory self-registration.
    Creates a Tenant, Domain, trial Subscription, and initial LAB_ADMIN user.

    Rate limited via scope 'auth_signup' (configured in settings).
    """
    authentication_classes = []
    permission_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth_signup'

    def post(self, request):
        serializer = LaboratorySignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            result = OnboardingService.signup(serializer.validated_data)
        except Exception:
            logger.exception('Laboratory onboarding failed')
            return Response(
                {
                    'data': None,
                    'meta': None,
                    'errors': [{
                        'code': 'INTERNAL_ERROR',
                        'message': 'An error occurred during laboratory setup. Please try again.',
                        'field': None,
                        'detail': {},
                    }],
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                'data': {
                    'tenant_id': str(result.tenant.id),
                    'laboratory_name': result.tenant.name,
                    'slug': result.tenant.subdomain,
                    'domain': result.domain,
                    'admin_email': result.admin_user.email,
                },
                'meta': None,
                'errors': [],
            },
            status=status.HTTP_201_CREATED,
        )


class SlugAvailabilityView(APIView):
    """
    GET /api/v1/onboarding/check-slug/?slug=my-lab

    Public endpoint to check if a subdomain slug is available.
    Rate limited via scope 'slug_check' (configured in settings).
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
            return Response({
                'data': {'available': False, 'slug': raw_slug, 'reason': 'Too short (min 3 characters).'},
                'meta': None,
                'errors': [],
            })

        if not SLUG_RE.match(raw_slug):
            return Response({
                'data': {'available': False, 'slug': raw_slug, 'reason': 'Invalid format.'},
                'meta': None,
                'errors': [],
            })

        if raw_slug in RESERVED_SLUGS:
            return Response({
                'data': {'available': False, 'slug': raw_slug, 'reason': 'Reserved name.'},
                'meta': None,
                'errors': [],
            })

        exists = Tenant.objects.filter(subdomain=raw_slug).exists()

        return Response({
            'data': {
                'available': not exists,
                'slug': raw_slug,
                'reason': 'Already taken.' if exists else None,
            },
            'meta': None,
            'errors': [],
        })
