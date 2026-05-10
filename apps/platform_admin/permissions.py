"""
DRF permission classes for the platform-admin API.

All three classes share the same ground rules: the request must
already be authenticated through ``PlatformAdminJWTAuthentication``
(so ``request.user`` is a ``PlatformAdminUser`` instance) and the
account must be active. Role-level fan-out happens on top of that
common base.
"""
from __future__ import annotations

from rest_framework.permissions import BasePermission

from .models import PlatformAdminRole, PlatformAdminUser


def _is_active_platform_admin(request) -> bool:
    user = getattr(request, 'user', None)
    return (
        user is not None
        and user.is_authenticated
        and isinstance(user, PlatformAdminUser)
        and user.is_active
    )


class IsPlatformAdmin(BasePermission):
    """Any active platform admin, regardless of role.

    The base gate. Use it on endpoints whose authorisation is purely
    "platform admin or not" — finer role-based access control is
    layered on top via ``IsPlatformSuperAdmin`` or ``HasPlatformRole``.
    """
    message = 'Platform admin authentication required.'

    def has_permission(self, request, view) -> bool:
        return _is_active_platform_admin(request)


class IsPlatformSuperAdmin(BasePermission):
    """Only ``SUPER_ADMIN`` role.

    Reserved for actions that affect other platform admins (creating,
    suspending, role changes) or for irreversible platform-wide
    operations.
    """
    message = 'Platform super-admin authentication required.'

    def has_permission(self, request, view) -> bool:
        return (
            _is_active_platform_admin(request)
            and request.user.role == PlatformAdminRole.SUPER_ADMIN
        )


class HasPlatformRole(BasePermission):
    """Generic role-list gate.

    The view declares the allowed roles via ``required_platform_roles``
    (an iterable of ``PlatformAdminRole`` values). The check passes
    iff the authenticated admin's role is in that list.

    Example::

        class TenantSuspendView(APIView):
            permission_classes = [HasPlatformRole]
            required_platform_roles = [
                PlatformAdminRole.SUPER_ADMIN,
                PlatformAdminRole.SUPPORT,
            ]

    The class is deliberately data-driven (rather than one subclass
    per role combination) so adding a new endpoint with a new role
    mix doesn't require a new permission class.
    """
    message = 'Insufficient platform admin role.'

    def has_permission(self, request, view) -> bool:
        if not _is_active_platform_admin(request):
            return False
        required = getattr(view, 'required_platform_roles', None) or []
        if not required:
            # No role list declared — fall back to "any active admin"
            # rather than silently denying. The view author opted in
            # to this class; the role list defaults to "all".
            return True
        return request.user.role in {
            r.value if hasattr(r, 'value') else r for r in required
        }
