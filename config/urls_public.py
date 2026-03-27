"""
Cytova — Public Schema URL Configuration
Served on the platform admin origin: admin.cytova.io
Only PLATFORM_ADMIN authenticated users may access these endpoints.
"""
from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),

    # Platform admin API — tenant (laboratory) management
    path('api/v1/platform/', include('apps.tenants.urls')),

    # Health check
    path('health/', include('common.urls')),
]
