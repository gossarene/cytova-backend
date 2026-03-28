"""
Cytova — Public Schema URL Configuration
Served on the platform admin origin: admin.cytova.io

Two API sections:
1. /api/v1/platform/    — PLATFORM_ADMIN authenticated (tenant CRUD)
2. /api/v1/onboarding/  — Public (self-service laboratory signup)
"""
from django.contrib import admin
from django.urls import path, include

from apps.tenants.onboarding_views import LaboratorySignupView, SlugAvailabilityView

urlpatterns = [
    path('admin/', admin.site.urls),

    # Platform admin API — tenant (laboratory) management
    path('api/v1/platform/', include('apps.tenants.urls')),

    # Public onboarding API — self-service laboratory signup
    path('api/v1/onboarding/', include([
        path('signup/', LaboratorySignupView.as_view(), name='laboratory-signup'),
        path('check-slug/', SlugAvailabilityView.as_view(), name='check-slug'),
    ])),

    # Health check
    path('health/', include('common.urls')),
]
