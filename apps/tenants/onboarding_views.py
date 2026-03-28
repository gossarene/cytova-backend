"""
Cytova — Laboratory Onboarding Views (Public API)

POST /api/v1/onboarding/signup/    — self-service laboratory signup

This endpoint is public (no authentication required) and rate-limited
to prevent abuse. It creates a fully provisioned tenant with an admin
user in a single request.
"""
import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from .onboarding_serializers import LaboratorySignupSerializer
from .onboarding_service import OnboardingService

logger = logging.getLogger(__name__)


class SignupRateThrottle(AnonRateThrottle):
    """Strict rate limit for signup: 5 per hour per IP."""
    rate = '5/hour'


class LaboratorySignupView(APIView):
    """
    POST /api/v1/onboarding/signup/

    Public endpoint for laboratory self-registration.
    Creates a Tenant, Domain, and initial LAB_ADMIN user.

    No authentication required. Rate-limited to 5 requests/hour per IP.
    """
    authentication_classes = []
    permission_classes = []
    throttle_classes = [SignupRateThrottle]

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
    Returns { "available": true/false, "slug": "normalized-slug" }.
    """
    authentication_classes = []
    permission_classes = []
    throttle_classes = [AnonRateThrottle]

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
