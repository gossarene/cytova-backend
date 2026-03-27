"""
Cytova — Middleware

CytovaTenantMiddleware  — Tenant resolution (wraps django-tenants)
AuditContextMiddleware  — Request metadata capture for audit logging
"""
import logging
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
