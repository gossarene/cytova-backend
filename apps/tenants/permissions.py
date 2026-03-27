"""
Cytova — Platform Admin Permission

Used exclusively on the public-schema (admin.cytova.io) API.
Requires the request to be authenticated via PlatformAdminJWTAuthentication
so that request.user is a PlatformAdmin instance.
"""
from rest_framework.permissions import BasePermission
from apps.tenants.models import PlatformAdmin


class IsPlatformAdmin(BasePermission):
    message = 'Platform admin access required.'

    def has_permission(self, request, view):
        return (
            request.user is not None
            and request.user.is_authenticated
            and isinstance(request.user, PlatformAdmin)
            and request.user.is_active
        )
