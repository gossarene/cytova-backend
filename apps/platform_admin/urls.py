"""URL conf for the platform-admin API.

Mounted under ``/api/v1/platform-admin/`` from ``urls_public.py``.
The full paths are:

  POST /api/v1/platform-admin/auth/login/
  GET  /api/v1/platform-admin/auth/me/
  GET  /api/v1/platform-admin/tenants/
  GET  /api/v1/platform-admin/tenants/{id}/
  GET  /api/v1/platform-admin/patients/
  GET  /api/v1/platform-admin/patients/{id}/
  POST /api/v1/platform-admin/patients/{id}/deactivate/
  POST /api/v1/platform-admin/patients/{id}/reactivate/
  GET  /api/v1/platform-admin/dashboard/

The mount happens only on the public-schema URL conf so the routes
literally do not exist on tenant subdomains. Lab staff hitting
``<lab>.cytova.io/api/v1/platform-admin/...`` get a 404 from the
routing layer, before authentication runs.
"""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .dashboard_views import PlatformDashboardView
from .patient_views import PlatformPatientAccountViewSet
from .tenant_views import PlatformTenantViewSet
from .views import PlatformAdminLoginView, PlatformAdminMeView


router = DefaultRouter(trailing_slash=True)
router.register('tenants', PlatformTenantViewSet, basename='platform-admin-tenants')
router.register(
    'patients', PlatformPatientAccountViewSet, basename='platform-admin-patients',
)


urlpatterns = [
    path('auth/login/', PlatformAdminLoginView.as_view(), name='platform-admin-login'),
    path('auth/me/',    PlatformAdminMeView.as_view(),    name='platform-admin-me'),

    path('dashboard/',  PlatformDashboardView.as_view(),  name='platform-admin-dashboard'),

    path('', include(router.urls)),
]
