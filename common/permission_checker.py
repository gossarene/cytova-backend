"""
Cytova -- Permission Checker

Resolves a user's effective permissions by combining role defaults
with per-user overrides (grant / revoke) stored in the database.

Resolution order:
    1. Start with get_role_permissions(user.role)
    2. Add all GRANT overrides from UserPermissionOverride
    3. Remove all REVOKE overrides from UserPermissionOverride
    4. Cache result on the user instance for the request lifetime

Usage:
    from common.permission_checker import PermissionChecker

    PermissionChecker.has_permission(user, 'results.publish')
    PermissionChecker.has_any_permission(user, 'results.validate', 'results.publish')
    effective = PermissionChecker.get_effective_permissions(user)
"""


class PermissionChecker:
    """Stateless checker — all methods are static, state lives on user instances."""

    _CACHE_ATTR = '_effective_permissions_cache'

    @staticmethod
    def get_effective_permissions(user) -> frozenset[str]:
        """
        Return the full set of permission codes for this user.

        Results are cached on the user instance. Call `invalidate_cache(user)`
        after changing roles or overrides within the same request.
        """
        cached = getattr(user, PermissionChecker._CACHE_ATTR, None)
        if cached is not None:
            return cached

        from common.role_permissions import get_role_permissions
        base = set(get_role_permissions(user.role))

        # Apply per-user overrides.
        # If permission_overrides was prefetched on the queryset, .all()
        # reads from the prefetch cache with zero DB queries. If not
        # prefetched, this triggers a single DB query (then cached on
        # the user instance via _CACHE_ATTR for the request lifetime).
        try:
            for override in user.permission_overrides.all():
                if override.override_type == 'GRANT':
                    base.add(override.permission_code)
                elif override.override_type == 'REVOKE':
                    base.discard(override.permission_code)
        except Exception:
            pass  # Defensive: anonymous user or missing relation

        result = frozenset(base)
        setattr(user, PermissionChecker._CACHE_ATTR, result)
        return result

    @staticmethod
    def has_permission(user, permission_code: str) -> bool:
        """Check if the user has a specific permission."""
        return permission_code in PermissionChecker.get_effective_permissions(user)

    @staticmethod
    def has_any_permission(user, *codes: str) -> bool:
        """Check if the user has at least one of the given permissions."""
        effective = PermissionChecker.get_effective_permissions(user)
        return bool(effective & set(codes))

    @staticmethod
    def has_all_permissions(user, *codes: str) -> bool:
        """Check if the user has all of the given permissions."""
        effective = PermissionChecker.get_effective_permissions(user)
        return set(codes).issubset(effective)

    @staticmethod
    def invalidate_cache(user):
        """Clear the cached permissions after a role or override change."""
        try:
            delattr(user, PermissionChecker._CACHE_ATTR)
        except AttributeError:
            pass
