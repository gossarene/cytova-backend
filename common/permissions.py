"""
Cytova — RBAC Permission Classes

Usage in views:
    permission_classes = [IsLabAdmin]
    permission_classes = [IsBiologistOrAbove]
    permission_classes = [IsAnyStaff]
    permission_classes = [IsLabAdminOrReadOnly]

These classes enforce role-level (Layer 2) access control.
Object-level (Layer 3) checks are applied in has_object_permission()
on individual view classes.
"""
from rest_framework.permissions import BasePermission, SAFE_METHODS
from apps.users.models import Role


class IsLabAdmin(BasePermission):
    """Full access for LAB_ADMIN only."""
    message = 'Lab Admin role required.'

    def has_permission(self, request, view):
        return (
            bool(request.user and request.user.is_authenticated)
            and request.user.role == Role.LAB_ADMIN
        )


class IsBiologistOrAbove(BasePermission):
    """Access for BIOLOGIST and LAB_ADMIN."""
    message = 'Biologist or Lab Admin role required.'

    _ALLOWED = frozenset({Role.BIOLOGIST, Role.LAB_ADMIN})

    def has_permission(self, request, view):
        return (
            bool(request.user and request.user.is_authenticated)
            and request.user.role in self._ALLOWED
        )


class IsTechnicianOrAbove(BasePermission):
    """Access for TECHNICIAN, BIOLOGIST, and LAB_ADMIN."""
    message = 'Technician role or above required.'

    _ALLOWED = frozenset({Role.TECHNICIAN, Role.BIOLOGIST, Role.LAB_ADMIN})

    def has_permission(self, request, view):
        return (
            bool(request.user and request.user.is_authenticated)
            and request.user.role in self._ALLOWED
        )


class IsReceptionistOrAbove(BasePermission):
    """Access for RECEPTIONIST, TECHNICIAN, BIOLOGIST, and LAB_ADMIN."""
    message = 'Receptionist role or above required.'

    _ALLOWED = frozenset({
        Role.RECEPTIONIST, Role.TECHNICIAN, Role.BIOLOGIST, Role.LAB_ADMIN
    })

    def has_permission(self, request, view):
        return (
            bool(request.user and request.user.is_authenticated)
            and request.user.role in self._ALLOWED
        )


class IsAnyStaff(BasePermission):
    """
    Access for any authenticated staff user (all roles, including VIEWER).
    Equivalent to IsAuthenticated but semantically clearer in staff contexts.
    """
    message = 'Staff authentication required.'

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)


class IsReceptionistOrLabAdmin(BasePermission):
    """
    Write access for RECEPTIONIST and LAB_ADMIN only.
    Used for patient registration, analysis request creation, and portal
    account management — actions that do NOT extend to TECHNICIAN or BIOLOGIST.
    """
    message = 'Receptionist or Lab Admin role required.'

    _ALLOWED = frozenset({Role.RECEPTIONIST, Role.LAB_ADMIN})

    def has_permission(self, request, view):
        return (
            bool(request.user and request.user.is_authenticated)
            and request.user.role in self._ALLOWED
        )


class IsLabAdminOrReadOnly(BasePermission):
    """
    Write access (POST, PATCH, DELETE) for LAB_ADMIN only.
    Read access (GET, HEAD, OPTIONS) for any authenticated staff.
    """

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.method in SAFE_METHODS:
            return True
        return request.user.role == Role.LAB_ADMIN


# ---------------------------------------------------------------------------
# Fine-grained permission classes (module.action based)
# ---------------------------------------------------------------------------

class HasPermission(BasePermission):
    """
    DRF permission class that checks one or more permission codes.

    Grants access if the user has *any* of the specified permissions.

    Usage:
        permission_classes = [HasPermission('results.publish')]
        permission_classes = [HasPermission('results.validate', 'results.publish')]
    """

    def __init__(self, *required_codes: str):
        self.required_codes = required_codes

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        from common.permission_checker import PermissionChecker
        return PermissionChecker.has_any_permission(request.user, *self.required_codes)


def RequiresPermission(*codes: str):
    """
    Factory that returns a BasePermission subclass for the given codes.

    Grants access if the user has *any* of the specified permissions.
    Designed for use in get_permissions() where you need to instantiate:

        def get_permissions(self):
            if self.action == 'publish':
                return [RequiresPermission('results.publish')()]
            return [RequiresPermission('results.view')()]
    """
    class _PermissionCheck(BasePermission):
        message = f'Required permission: {", ".join(codes)}'

        def has_permission(self, request, view):
            if not (request.user and request.user.is_authenticated):
                return False
            from common.permission_checker import PermissionChecker
            return PermissionChecker.has_any_permission(request.user, *codes)

    _PermissionCheck.__name__ = f'Requires_{"__".join(codes)}'
    _PermissionCheck.__qualname__ = _PermissionCheck.__name__
    return _PermissionCheck
