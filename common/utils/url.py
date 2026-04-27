"""URL construction helpers.

The frontend listens on a different port than the backend in development
(8000 vs 3000). In production both share an origin behind a reverse proxy.
This helper produces a tenant-aware absolute URL pointing at the frontend
without hardcoding the tenant slug — it preserves whatever subdomain the
incoming request was made against.
"""
from __future__ import annotations

from django.conf import settings


def build_tenant_frontend_url(request, path: str) -> str:
    """Return an absolute frontend URL on the same tenant subdomain as
    ``request``. Always honours the request host — never a globally
    configured domain — so tenant-isolation is preserved end-to-end
    (links generated for tenant A can never point at tenant B).

    Examples (DEBUG=True, CYTOVA_DEV_FRONTEND_PORT=3000):
        request.host = "veno-lab.cytova.io:8000"
        path         = "/reset-password?token=abc"
        →            "http://veno-lab.cytova.io:3000/reset-password?token=abc"

    Examples (DEBUG=False):
        request.host = "veno-lab.cytova.io"
        path         = "/reset-password?token=abc"
        →            "https://veno-lab.cytova.io/reset-password?token=abc"
    """
    host_with_port = request.get_host()
    host = host_with_port.split(':', 1)[0]

    if settings.DEBUG:
        port = int(getattr(settings, 'CYTOVA_DEV_FRONTEND_PORT', 3000))
        scheme = 'http'
        netloc = f'{host}:{port}'
    else:
        scheme = 'https' if request.is_secure() else 'http'
        netloc = host

    if not path.startswith('/'):
        path = '/' + path
    return f'{scheme}://{netloc}{path}'
