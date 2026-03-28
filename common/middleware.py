"""
Cytova — Middleware

CytovaTenantMiddleware              — Tenant resolution (wraps django-tenants)
SubscriptionEnforcementMiddleware   — Block access when subscription is not usable
AuditContextMiddleware              — Request metadata capture for audit logging
"""
import json
import logging

from django.http import JsonResponse
from django_tenants.middleware.main import TenantMainMiddleware

logger = logging.getLogger(__name__)


class CytovaTenantMiddleware(TenantMainMiddleware):
    """
    Extends django-tenants' TenantMainMiddleware with:

    1. request.tenant_schema  — convenience shorthand for the active schema name.
    2. Debug-level logging of tenant resolution (useful during development).

    django-tenants' middleware already:
    - Parses the Host header to extract the subdomain
    - Looks up the matching Domain/Tenant record
    - Calls connection.set_tenant() to switch the DB search_path
    - Sets request.tenant to the resolved Tenant instance
    """

    def process_request(self, request):
        super().process_request(request)

        if hasattr(request, 'tenant'):
            request.tenant_schema = request.tenant.schema_name
            logger.debug(
                'Tenant resolved: schema=%s name=%s path=%s',
                request.tenant_schema,
                request.tenant.name,
                request.path,
            )
        else:
            # Public schema (no tenant matched) — e.g. admin.cytova.io
            request.tenant_schema = 'public'


class SubscriptionEnforcementMiddleware:
    """
    Blocks tenant API requests when the tenant has no usable subscription
    (i.e. status is EXPIRED, SUSPENDED, or CANCELLED).

    Placement: immediately after CytovaTenantMiddleware in MIDDLEWARE.

    Skipped for:
    - Public schema requests (platform admin, onboarding, health checks)
    - Paths listed in SUBSCRIPTION_EXEMPT_PATHS (e.g. health, auth)

    Returns a 403 JSON response in the standard Cytova error envelope with
    a machine-readable code that the frontend can use to show the right UI
    (upgrade prompt, suspension notice, etc.).

    Error codes:
        SUBSCRIPTION_EXPIRED    — trial or billing period ended
        SUBSCRIPTION_SUSPENDED  — admin or payment suspension
        SUBSCRIPTION_CANCELLED  — permanently cancelled
        SUBSCRIPTION_MISSING    — no subscription record at all
    """

    # Exempt paths loaded from settings at first use.
    _exempt_prefixes = None

    _STATUS_TO_CODE = None  # Lazy-loaded to avoid import at module level

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Skip for public schema (platform admin, onboarding, health)
        if getattr(request, 'tenant_schema', 'public') == 'public':
            return self.get_response(request)

        # Skip exempt paths (auth endpoints needed to retrieve tokens)
        if any(request.path.startswith(p) for p in self._get_exempt_prefixes()):
            return self.get_response(request)

        tenant = getattr(request, 'tenant', None)
        if tenant is None:
            return self.get_response(request)

        # Check subscription
        subscription = tenant.active_subscription
        if subscription is not None and subscription.is_usable:
            # Attach subscription to request for downstream use
            request.subscription = subscription
            return self.get_response(request)

        # No usable subscription — determine the specific reason
        error_code, message, detail = self._resolve_error(tenant)

        return JsonResponse(
            {
                'data': None,
                'meta': None,
                'errors': [{
                    'code': error_code,
                    'message': message,
                    'field': None,
                    'detail': detail,
                }],
            },
            status=403,
        )

    @classmethod
    def _get_exempt_prefixes(cls):
        if cls._exempt_prefixes is None:
            from django.conf import settings
            cls._exempt_prefixes = tuple(
                getattr(settings, 'SUBSCRIPTION_EXEMPT_PATH_PREFIXES', [
                    '/health/',
                    '/api/v1/auth/',
                ])
            )
        return cls._exempt_prefixes

    def _resolve_error(self, tenant):
        """Determine the specific subscription error for the tenant."""
        from apps.tenants.models import Subscription, SubscriptionStatus

        # Get the most recent subscription regardless of status
        latest = (
            Subscription.objects
            .filter(tenant=tenant)
            .order_by('-created_at')
            .first()
        )

        if latest is None:
            return (
                'SUBSCRIPTION_MISSING',
                'No subscription found for this laboratory. Please contact support.',
                {},
            )

        status_map = {
            SubscriptionStatus.EXPIRED: (
                'SUBSCRIPTION_EXPIRED',
                'Your subscription has expired. Please renew to continue.',
            ),
            SubscriptionStatus.SUSPENDED: (
                'SUBSCRIPTION_SUSPENDED',
                'Your subscription has been suspended. Please contact support.',
            ),
            SubscriptionStatus.CANCELLED: (
                'SUBSCRIPTION_CANCELLED',
                'Your subscription has been cancelled.',
            ),
        }

        code, message = status_map.get(
            latest.status,
            ('SUBSCRIPTION_INACTIVE', 'Your subscription is not active.'),
        )

        return code, message, {'subscription_status': latest.status}


class AuditContextMiddleware:
    """
    Captures per-request metadata and attaches it to the request object.
    This data is consumed by the audit logging layer (signals / view hooks)
    without requiring repeated extraction.

    Attaches:
        request.audit_ip          — real client IP (proxy-aware)
        request.audit_user_agent  — truncated User-Agent string
        request.audit_request_id  — client-supplied or empty string

    Also echoes X-Request-ID in the response when provided by the client.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.audit_ip = self._get_client_ip(request)
        request.audit_user_agent = request.META.get('HTTP_USER_AGENT', '')[:500]
        request.audit_request_id = request.META.get('HTTP_X_REQUEST_ID', '')

        response = self.get_response(request)

        if request.audit_request_id:
            response['X-Request-ID'] = request.audit_request_id

        return response

    @staticmethod
    def _get_client_ip(request) -> str:
        """
        Extract the real client IP, accounting for reverse proxies.
        Trusts the first IP in X-Forwarded-For if present.
        In production, ensure the proxy is configured to set this header reliably.
        """
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR', '')
        if x_forwarded_for:
            return x_forwarded_for.split(',')[0].strip()
        return request.META.get('REMOTE_ADDR', '')
