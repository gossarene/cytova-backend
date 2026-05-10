"""
Cytova — Public Schema URL Configuration
Served on the platform admin origin: admin.cytova.io

API sections:
  /api/v1/platform/                  — PLATFORM_ADMIN authenticated (tenant CRUD)
  /api/v1/platform/onboarding/...    — Public self-service onboarding (4-step flow)

Tenants are NOT created until POST /api/v1/platform/onboarding/complete/.
The legacy single-call /api/v1/onboarding/signup/ endpoint has been removed —
it created tenants before email verification, which left orphan schemas.
"""
from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),

    # Platform admin + public onboarding (mounted in apps.tenants.urls)
    path('api/v1/platform/', include('apps.tenants.urls')),

    # Platform-admin back-office identity (auth/login + auth/me).
    # Distinct mount point from the legacy ``/api/v1/platform/auth/``
    # so the new auth surface coexists with the older tenant-CRUD
    # surface during the foundation phase. Public schema only —
    # explicitly NOT included in the tenant URL conf.
    path('api/v1/platform-admin/', include('apps.platform_admin.urls')),

    # Global Cytova patient signup (PatientAccount/Profile/Consent live in
    # the public schema). Same routes are also mounted on the tenant URL
    # conf — see ``config/urls.py`` — so the endpoint is reachable from
    # any host the deployment serves without the caller having to know
    # which subdomain hits which urlconf.
    path('api/v1/patient-portal/', include('apps.patient_portal.urls')),

    # Health check
    path('health/', include('common.urls')),
]
