"""
Platform admin URL conf — mounted under /api/v1/platform/ in urls_public.py
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import TenantViewSet, PlatformAdminLoginView

router = DefaultRouter(trailing_slash=True)
router.register('tenants', TenantViewSet, basename='platform-tenants')

urlpatterns = [
    path('auth/login/', PlatformAdminLoginView.as_view(), name='platform-admin-login'),
    path('', include(router.urls)),
]
