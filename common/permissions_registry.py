"""
Cytova -- Permission Registry

Single source of truth for all permission codes in the system.
Permissions follow the `module.action` naming convention.

These are code-defined constants — not stored in the database,
not editable per-tenant. The registry is populated at import time.

Usage:
    from common.permissions_registry import PermissionRegistry

    # Check if a code is valid
    'patients.view' in PermissionRegistry.codes()

    # Get all permissions grouped by module
    PermissionRegistry.by_module()
"""


class Permission:
    """A named permission with module.action convention."""

    __slots__ = ('code', 'description', 'module')

    def __init__(self, code: str, description: str, module: str):
        self.code = code
        self.description = description
        self.module = module

    def __repr__(self):
        return f"Permission('{self.code}')"

    def __eq__(self, other):
        if isinstance(other, str):
            return self.code == other
        return isinstance(other, Permission) and self.code == other.code

    def __hash__(self):
        return hash(self.code)


class PermissionRegistry:
    """
    Central registry of all system permissions.

    Class-level singleton — all instances share the same `_permissions` dict.
    Permissions are registered at module import time via `register()`.
    """

    _permissions: dict[str, Permission] = {}

    @classmethod
    def register(cls, code: str, description: str) -> str:
        """Register a permission and return its code string."""
        if '.' not in code:
            raise ValueError(f"Permission code must use 'module.action' format: {code}")
        module = code.split('.', 1)[0]
        perm = Permission(code, description, module)
        cls._permissions[code] = perm
        return code

    @classmethod
    def get(cls, code: str) -> Permission:
        """Get a Permission object by its code. Raises KeyError if not found."""
        return cls._permissions[code]

    @classmethod
    def all(cls) -> dict[str, Permission]:
        """Return a copy of all registered permissions."""
        return dict(cls._permissions)

    @classmethod
    def codes(cls) -> frozenset[str]:
        """Return all registered permission codes as a frozenset."""
        return frozenset(cls._permissions.keys())

    @classmethod
    def by_module(cls) -> dict[str, list[Permission]]:
        """Return permissions grouped by module name."""
        result: dict[str, list[Permission]] = {}
        for perm in cls._permissions.values():
            result.setdefault(perm.module, []).append(perm)
        return result

    @classmethod
    def is_valid(cls, code: str) -> bool:
        """Check if a permission code is registered."""
        return code in cls._permissions


# ---------------------------------------------------------------------------
# Register all permissions
# ---------------------------------------------------------------------------
_r = PermissionRegistry.register

# -- Patients --
PATIENTS_VIEW = _r('patients.view', 'View patient records')
PATIENTS_CREATE = _r('patients.create', 'Register new patients')
PATIENTS_UPDATE = _r('patients.update', 'Update patient information')
PATIENTS_DEACTIVATE = _r('patients.deactivate', 'Deactivate patient records')
PATIENTS_MANAGE_PORTAL = _r('patients.manage_portal', 'Create/remove patient portal accounts')

# -- Catalog --
CATALOG_VIEW = _r('catalog.view', 'View exam categories and definitions')
CATALOG_MANAGE = _r('catalog.manage', 'Create/update exam categories and definitions')

# -- Pricing --
PRICING_VIEW = _r('pricing.view', 'View pricing rules')
PRICING_MANAGE = _r('pricing.manage', 'Create/update/delete pricing rules')

# -- Analysis Requests --
REQUESTS_VIEW = _r('requests.view', 'View analysis requests')
REQUESTS_CREATE = _r('requests.create', 'Create analysis requests')
REQUESTS_UPDATE = _r('requests.update', 'Update analysis requests')
REQUESTS_CONFIRM = _r('requests.confirm', 'Confirm analysis requests')
REQUESTS_CANCEL = _r('requests.cancel', 'Cancel analysis requests')

# -- Results --
RESULTS_VIEW = _r('results.view', 'View exam results')
RESULTS_CREATE = _r('results.create', 'Enter exam results')
RESULTS_UPDATE = _r('results.update', 'Update exam results')
RESULTS_VALIDATE = _r('results.validate', 'Validate exam results')
RESULTS_PUBLISH = _r('results.publish', 'Publish exam results (irreversible)')

# -- Partners --
PARTNERS_VIEW = _r('partners.view', 'View partner organizations')
PARTNERS_MANAGE = _r('partners.manage', 'Create/update partner organizations')

# -- Stock --
STOCK_VIEW = _r('stock.view', 'View stock items, lots, and movements')
STOCK_MANAGE = _r('stock.manage', 'Manage stock items, lots, record movements')

# -- Suppliers --
SUPPLIERS_VIEW = _r('suppliers.view', 'View suppliers')
SUPPLIERS_MANAGE = _r('suppliers.manage', 'Create/update suppliers')

# -- Procurement --
PROCUREMENT_VIEW = _r('procurement.view', 'View purchase orders and receptions')
PROCUREMENT_MANAGE = _r('procurement.manage', 'Create/manage purchase orders and receptions')

# -- Alerts --
ALERTS_VIEW = _r('alerts.view', 'View inventory and system alerts')
ALERTS_ACKNOWLEDGE = _r('alerts.acknowledge', 'Acknowledge alerts')

# -- Users --
USERS_VIEW = _r('users.view', 'View staff user list and details')
USERS_CREATE = _r('users.create', 'Create new staff users')
USERS_UPDATE = _r('users.update', 'Update staff user profiles')
USERS_DEACTIVATE = _r('users.deactivate', 'Deactivate staff users')
USERS_ACTIVATE = _r('users.activate', 'Reactivate staff users')
USERS_ASSIGN_ROLE = _r('users.assign_role', 'Assign or change roles on staff users')
USERS_MANAGE_PERMISSIONS = _r('users.manage_permissions', 'Grant/revoke per-user permission overrides')

# -- Audit --
AUDIT_VIEW = _r('audit.view', 'View audit logs')

# -- Dashboard --
DASHBOARD_VIEW = _r('dashboard.view', 'View dashboard KPIs and summaries')

# -- Files --
FILES_VIEW = _r('files.view', 'View and download files via signed URLs')
FILES_UPLOAD = _r('files.upload', 'Upload files')

# -- Billing --
BILLING_VIEW = _r('billing.view', 'View billing and invoices')
BILLING_MANAGE = _r('billing.manage', 'Manage billing settings and invoices')

# -- Inventory Reports --
INVENTORY_REPORTS = _r('inventory.reports', 'Generate inventory reports')

# -- Settings --
SETTINGS_VIEW = _r('settings.view', 'View tenant settings')
SETTINGS_MANAGE = _r('settings.manage', 'Manage tenant settings')

del _r  # Clean up module namespace
