"""
Cytova — Tenant URL Configuration
Served on all tenant subdomains: laba.cytova.io, labb.cytova.io, etc.
"""
from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    # Django admin (accessible from tenant context in development)
    path('admin/', admin.site.urls),

    path('api/v1/', include([

        # ------------------------------------------------------------------
        # Authentication — custom views with audit logging + role claims
        # ------------------------------------------------------------------
        path('auth/', include('apps.authentication.urls')),

        # ------------------------------------------------------------------
        # Staff users + RBAC
        # ------------------------------------------------------------------
        path('users/', include('apps.users.urls')),

        # ------------------------------------------------------------------
        # API schema & docs (restrict access in production via settings)
        # ------------------------------------------------------------------
        path('schema/', SpectacularAPIView.as_view(), name='schema'),
        path('docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),

        # ------------------------------------------------------------------
        # Domain modules
        # ------------------------------------------------------------------
        path('patients/',  include('apps.patients.urls')),
        path('catalog/',   include('apps.catalog.urls')),
        path('requests/',  include('apps.requests.urls')),
        path('results/',   include('apps.results.urls')),
        path('stock/',     include('apps.stock.urls')),
        path('suppliers/',    include('apps.suppliers.urls')),
        # path('procurement/',  include('apps.procurement.urls')),
        path('alerts/',       include('apps.alerts.urls')),
        path('dashboard/',    include('apps.dashboard.urls')),
        # path('audit/',        include('apps.audit.urls')),
        # path('files/',        include('apps.files.urls')),
        # path('portal/',       include('apps.portal.urls')),

    ])),

    # Health check — returns {"status": "ok"}, no internal detail exposed
    path('health/', include('common.urls')),
]
