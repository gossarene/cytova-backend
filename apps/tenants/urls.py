"""
Platform URL conf — mounted under /api/v1/platform/ in urls_public.py.

Two surfaces share this prefix:
  - PLATFORM_ADMIN-authenticated tenant management (auth/login, tenants/, plans/, subscriptions/)
  - Public self-service onboarding (onboarding/...) — no authentication
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import TenantViewSet, PlatformAdminLoginView, PlatformDashboardView
from .subscription_views import SubscriptionPlanViewSet, SubscriptionViewSet
from .onboarding_views import (
    OnboardingStartView,
    OnboardingVerifyEmailView,
    OnboardingResendCodeView,
    OnboardingCompleteView,
    SlugAvailabilityView,
)

router = DefaultRouter(trailing_slash=True)
router.register('tenants', TenantViewSet, basename='platform-tenants')
router.register('plans', SubscriptionPlanViewSet, basename='platform-plans')
router.register('subscriptions', SubscriptionViewSet, basename='platform-subscriptions')

urlpatterns = [
    # Platform admin (authenticated)
    path('auth/login/', PlatformAdminLoginView.as_view(), name='platform-admin-login'),
    path('dashboard/', PlatformDashboardView.as_view(), name='platform-dashboard'),

    # Public onboarding (no authentication; tenants are NOT created until /complete/)
    path('onboarding/', include([
        path('start/',         OnboardingStartView.as_view(),       name='onboarding-start'),
        path('verify-email/',  OnboardingVerifyEmailView.as_view(), name='onboarding-verify-email'),
        path('resend-code/',   OnboardingResendCodeView.as_view(),  name='onboarding-resend-code'),
        path('complete/',      OnboardingCompleteView.as_view(),    name='onboarding-complete'),
        path('check-slug/',    SlugAvailabilityView.as_view(),      name='onboarding-check-slug'),
    ])),

    path('', include(router.urls)),
]
