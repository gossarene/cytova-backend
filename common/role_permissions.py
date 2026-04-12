"""
Cytova -- Role-Permission Mapping

Static, code-defined mapping from each tenant role to its default
set of permission codes. This is the single source of truth for
what each role can do out of the box.

User-level overrides (grant / revoke) are stored in the DB and
applied on top of these defaults by the PermissionChecker.

Usage:
    from common.role_permissions import ROLE_PERMISSIONS

    perms = ROLE_PERMISSIONS['LAB_ADMIN']  # frozenset of codes
"""
from common.permissions_registry import PermissionRegistry

# ---------------------------------------------------------------------------
# Tenant-level role permissions
# ---------------------------------------------------------------------------

# LAB_ADMIN gets every registered permission — dynamically resolved
# so new permissions are automatically included.
_ALL = PermissionRegistry.codes

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {

    # --- Full access ---
    'LAB_ADMIN': _ALL(),

    # --- Senior technical role ---
    'BIOLOGIST': frozenset({
        'patients.view', 'patients.create', 'patients.update',
        'catalog.view', 'pricing.view',
        'requests.view', 'requests.create', 'requests.update', 'requests.confirm',
        'requests.finalize_validation',
        'results.view', 'results.create', 'results.update', 'results.submit',
        'results.validate', 'results.reject', 'results.publish',
        'partners.view',
        'stock.view',
        'suppliers.view', 'procurement.view',
        'alerts.view', 'alerts.acknowledge',
        'users.view',
        'audit.view',
        'dashboard.view',
        'files.view', 'files.upload',
    }),

    # --- Technical operations ---
    'TECHNICIAN': frozenset({
        'patients.view',
        'catalog.view',
        'requests.view',
        'results.view', 'results.create', 'results.update', 'results.submit',
        'partners.view',
        'stock.view', 'stock.manage',
        'suppliers.view', 'procurement.view',
        'alerts.view', 'alerts.acknowledge',
        'users.view',
        'dashboard.view',
        'files.view', 'files.upload',
    }),

    # --- Patient-facing / front-desk ---
    'RECEPTIONIST': frozenset({
        'patients.view', 'patients.create', 'patients.update',
        'patients.manage_portal',
        'catalog.view', 'pricing.view',
        'requests.view', 'requests.create', 'requests.update', 'requests.confirm',
        'results.view',
        'partners.view',
        'users.view',
        'billing.view',
        'dashboard.view',
        'files.view',
    }),

    # --- Financial / billing ---
    'BILLING_OFFICER': frozenset({
        'patients.view',
        'catalog.view', 'pricing.view', 'pricing.manage',
        'requests.view',
        'results.view',
        'partners.view', 'partners.manage',
        'users.view',
        'billing.view', 'billing.manage',
        'dashboard.view',
        'files.view',
    }),

    # --- Stock & supply chain ---
    'INVENTORY_MANAGER': frozenset({
        'catalog.view',
        'stock.view', 'stock.manage',
        'suppliers.view', 'suppliers.manage',
        'procurement.view', 'procurement.manage',
        'alerts.view', 'alerts.acknowledge',
        'users.view',
        'inventory.reports',
        'dashboard.view',
        'files.view',
    }),

    # --- Read-only + audit ---
    'VIEWER_AUDITOR': frozenset({
        'patients.view',
        'catalog.view', 'pricing.view',
        'requests.view',
        'results.view',
        'partners.view',
        'stock.view',
        'suppliers.view', 'procurement.view',
        'alerts.view',
        'users.view',
        'audit.view',
        'dashboard.view',
        'files.view',
        'settings.view',
    }),
}


def get_role_permissions(role: str) -> frozenset[str]:
    """
    Return the default permissions for a given role.

    For LAB_ADMIN, returns the current full set of registered permissions
    (dynamically resolved so new permissions are auto-included).
    """
    if role == 'LAB_ADMIN':
        return _ALL()
    return ROLE_PERMISSIONS.get(role, frozenset())
