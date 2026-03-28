"""
Platform admin URL conf — mounted under /api/v1/platform/ in urls_public.py
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import TenantViewSet, PlatformAdminLoginView, PlatformDashboardView
from .subscription_views import SubscriptionPlanViewSet, SubscriptionViewSet

router = DefaultRouter(trailing_slash=True)
router.register('tenants', TenantViewSet, basename='platform-tenants')
router.register('plans', SubscriptionPlanViewSet, basename='platform-plans')
router.register('subscriptions', SubscriptionViewSet, basename='platform-subscriptions')

urlpatterns = [
    path('auth/login/', PlatformAdminLoginView.as_view(), name='platform-admin-login'),
    path('dashboard/', PlatformDashboardView.as_view(), name='platform-dashboard'),
    path('', include(router.urls)),
]
