"""
Cytova — Platform Admin Permission

Used exclusively on the public-schema (admin.cytova.io) API.
Requires the request to be authenticated via PlatformAdminJWTAuthentication
so that request.user is a PlatformAdmin instance.
"""
from rest_framework.permissions import BasePermission
from apps.tenants.models import PlatformAdmin, PlatformRole


class IsPlatformAdmin(BasePermission):
    """Access for any active platform admin (owner or staff)."""
    message = 'Platform admin access required.'

    def has_permission(self, request, view):
        return (
            request.user is not None
            and request.user.is_authenticated
            and isinstance(request.user, PlatformAdmin)
            and request.user.is_active
        )


class IsPlatformOwner(BasePermission):
    """Access for platform owners only. Used for destructive operations."""
    message = 'Platform owner access required.'

    def has_permission(self, request, view):
        return (
            request.user is not None
            and request.user.is_authenticated
            and isinstance(request.user, PlatformAdmin)
            and request.user.is_active
            and request.user.role == PlatformRole.PLATFORM_OWNER
        )
