"""
Cytova — RBAC Unit Tests

Tests for the permission registry, role-permission mapping,
permission checker, and service-level RBAC logic.
"""
import pytest

from django.core.exceptions import ValidationError

from apps.users.models import (
    StaffUser, Role, UserPermissionOverride, OverrideType,
)
from apps.audit.models import AuditLog, AuditAction
from common.permissions_registry import PermissionRegistry
from common.role_permissions import ROLE_PERMISSIONS, get_role_permissions
from common.permission_checker import PermissionChecker
from apps.users.services import UserService


# ---------------------------------------------------------------------------
# Permission Registry
# ---------------------------------------------------------------------------

class TestPermissionRegistry:

    def test_all_codes_use_module_action_format(self):
        """Every registered permission code must contain a dot."""
        for code in PermissionRegistry.codes():
            assert '.' in code, f'{code} does not use module.action format'

    def test_registry_has_permissions(self):
        """Sanity check: at least 20 permissions are registered."""
        assert len(PermissionRegistry.codes()) >= 20

    def test_is_valid_returns_true_for_known(self):
        assert PermissionRegistry.is_valid('patients.view') is True

    def test_is_valid_returns_false_for_unknown(self):
        assert PermissionRegistry.is_valid('nonexistent.perm') is False

    def test_by_module_groups_correctly(self):
        by_module = PermissionRegistry.by_module()
        assert 'patients' in by_module
        assert 'results' in by_module
        patient_codes = {p.code for p in by_module['patients']}
        assert 'patients.view' in patient_codes
        assert 'patients.create' in patient_codes

    def test_get_returns_permission_object(self):
        perm = PermissionRegistry.get('results.publish')
        assert perm.code == 'results.publish'
        assert perm.module == 'results'
        assert perm.description != ''

    def test_get_raises_for_unknown(self):
        with pytest.raises(KeyError):
            PermissionRegistry.get('fake.permission')


# ---------------------------------------------------------------------------
# Role-Permission Mapping
# ---------------------------------------------------------------------------

class TestRolePermissions:

    def test_all_roles_have_mapping(self):
        """Every Role choice must have an entry in ROLE_PERMISSIONS."""
        for role_value, _ in Role.choices:
            assert role_value in ROLE_PERMISSIONS, f'{role_value} missing from ROLE_PERMISSIONS'

    def test_no_unknown_roles_in_mapping(self):
        """ROLE_PERMISSIONS should not contain roles that don't exist in Role."""
        valid = {r.value for r in Role}
        for key in ROLE_PERMISSIONS:
            assert key in valid, f'{key} is not a valid Role'

    def test_no_unknown_permissions_in_mapping(self):
        """No role should reference a permission code not in the registry."""
        all_codes = PermissionRegistry.codes()
        for role, perms in ROLE_PERMISSIONS.items():
            unknown = perms - all_codes
            assert not unknown, f'{role} references unknown permissions: {unknown}'

    def test_lab_admin_has_all_permissions(self):
        """LAB_ADMIN must have every registered permission."""
        all_codes = PermissionRegistry.codes()
        admin_perms = get_role_permissions('LAB_ADMIN')
        assert admin_perms == all_codes

    def test_viewer_auditor_has_only_view_and_audit(self):
        """VIEWER_AUDITOR should have no write/manage/publish permissions."""
        perms = get_role_permissions('VIEWER_AUDITOR')
        write_actions = {'create', 'update', 'manage', 'publish', 'validate',
                         'deactivate', 'activate', 'assign_role', 'manage_permissions',
                         'confirm', 'cancel', 'acknowledge', 'upload', 'reports'}
        for code in perms:
            action = code.split('.', 1)[1]
            assert action not in write_actions, (
                f'VIEWER_AUDITOR should not have write permission: {code}'
            )

    def test_get_role_permissions_unknown_role(self):
        """Unknown role returns empty set."""
        assert get_role_permissions('NONEXISTENT') == frozenset()

    def test_biologist_can_publish_results(self):
        perms = get_role_permissions('BIOLOGIST')
        assert 'results.publish' in perms
        assert 'results.validate' in perms

    def test_technician_cannot_publish_results(self):
        perms = get_role_permissions('TECHNICIAN')
        assert 'results.publish' not in perms
        assert 'results.validate' not in perms

    def test_receptionist_can_create_patients(self):
        perms = get_role_permissions('RECEPTIONIST')
        assert 'patients.create' in perms
        assert 'patients.manage_portal' in perms

    def test_billing_officer_has_billing_manage(self):
        perms = get_role_permissions('BILLING_OFFICER')
        assert 'billing.manage' in perms
        assert 'billing.view' in perms
        assert 'pricing.manage' in perms

    def test_inventory_manager_has_stock_manage(self):
        perms = get_role_permissions('INVENTORY_MANAGER')
        assert 'stock.manage' in perms
        assert 'procurement.manage' in perms
        assert 'inventory.reports' in perms


# ---------------------------------------------------------------------------
# Permission Checker
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestPermissionChecker:

    def test_base_role_permissions(self, receptionist):
        """User gets permissions from their role defaults."""
        assert PermissionChecker.has_permission(receptionist, 'patients.create')
        assert not PermissionChecker.has_permission(receptionist, 'results.publish')

    def test_grant_override_adds_permission(self, receptionist, lab_admin, make_request):
        """Granting a permission gives it to the user beyond role defaults."""
        assert not PermissionChecker.has_permission(receptionist, 'results.publish')

        UserService.grant_permission(
            receptionist, 'results.publish', lab_admin,
            'Temporary access', make_request(lab_admin),
        )

        assert PermissionChecker.has_permission(receptionist, 'results.publish')

    def test_revoke_override_removes_permission(self, receptionist, lab_admin, make_request):
        """Revoking a permission removes it from role defaults."""
        assert PermissionChecker.has_permission(receptionist, 'patients.create')

        UserService.revoke_permission(
            receptionist, 'patients.create', lab_admin,
            'Restricted', make_request(lab_admin),
        )

        assert not PermissionChecker.has_permission(receptionist, 'patients.create')

    def test_remove_override_restores_default(self, receptionist, lab_admin, make_request):
        """Removing an override restores the role default."""
        req = make_request(lab_admin)

        UserService.revoke_permission(
            receptionist, 'patients.create', lab_admin, '', req,
        )
        assert not PermissionChecker.has_permission(receptionist, 'patients.create')

        UserService.remove_permission_override(
            receptionist, 'patients.create', lab_admin, req,
        )
        assert PermissionChecker.has_permission(receptionist, 'patients.create')

    def test_has_any_permission(self, technician):
        assert PermissionChecker.has_any_permission(
            technician, 'results.publish', 'results.create',
        )
        assert not PermissionChecker.has_any_permission(
            technician, 'results.publish', 'billing.manage',
        )

    def test_has_all_permissions(self, technician):
        assert PermissionChecker.has_all_permissions(
            technician, 'results.create', 'results.update',
        )
        assert not PermissionChecker.has_all_permissions(
            technician, 'results.create', 'results.publish',
        )

    def test_caching(self, receptionist):
        """Second call uses cached value — no extra DB queries."""
        PermissionChecker.get_effective_permissions(receptionist)
        # Cache should be set
        assert hasattr(receptionist, '_effective_permissions_cache')
        cached = receptionist._effective_permissions_cache
        result = PermissionChecker.get_effective_permissions(receptionist)
        assert result is cached

    def test_invalidate_cache(self, receptionist):
        PermissionChecker.get_effective_permissions(receptionist)
        PermissionChecker.invalidate_cache(receptionist)
        assert not hasattr(receptionist, '_effective_permissions_cache')

    def test_lab_admin_has_all(self, lab_admin):
        """LAB_ADMIN has every permission."""
        effective = PermissionChecker.get_effective_permissions(lab_admin)
        assert effective == PermissionRegistry.codes()

    def test_has_perm_code_method(self, receptionist):
        """The convenience method on StaffUser works."""
        assert receptionist.has_perm_code('patients.create')
        assert not receptionist.has_perm_code('results.publish')

    def test_grant_unknown_permission_fails(self, receptionist, lab_admin, make_request):
        with pytest.raises(ValidationError, match='Unknown permission'):
            UserService.grant_permission(
                receptionist, 'fake.permission', lab_admin,
                '', make_request(lab_admin),
            )

    def test_revoke_unknown_permission_fails(self, receptionist, lab_admin, make_request):
        with pytest.raises(ValidationError, match='Unknown permission'):
            UserService.revoke_permission(
                receptionist, 'fake.permission', lab_admin,
                '', make_request(lab_admin),
            )

    def test_remove_nonexistent_override_fails(self, receptionist, lab_admin, make_request):
        with pytest.raises(ValidationError, match='No override found'):
            UserService.remove_permission_override(
                receptionist, 'results.publish', lab_admin,
                make_request(lab_admin),
            )


# ---------------------------------------------------------------------------
# Role Assignment
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestRoleAssignment:

    def test_assign_role_success(self, lab_admin, receptionist, make_request):
        assert receptionist.role == Role.RECEPTIONIST

        user = UserService.assign_role(
            receptionist, Role.TECHNICIAN, lab_admin, make_request(lab_admin),
        )

        assert user.role == Role.TECHNICIAN
        receptionist.refresh_from_db()
        assert receptionist.role == Role.TECHNICIAN

    def test_assign_role_creates_audit_log(self, lab_admin, receptionist, make_request):
        UserService.assign_role(
            receptionist, Role.BIOLOGIST, lab_admin, make_request(lab_admin),
        )

        log = AuditLog.objects.filter(
            action=AuditAction.ROLE_ASSIGN,
            entity_id=receptionist.id,
        ).first()

        assert log is not None
        assert log.diff['before']['role'] == 'RECEPTIONIST'
        assert log.diff['after']['role'] == 'BIOLOGIST'
        assert log.actor_id == lab_admin.id

    def test_assign_same_role_is_noop(self, lab_admin, receptionist, make_request):
        user = UserService.assign_role(
            receptionist, Role.RECEPTIONIST, lab_admin, make_request(lab_admin),
        )
        assert user.role == Role.RECEPTIONIST
        assert not AuditLog.objects.filter(action=AuditAction.ROLE_ASSIGN).exists()

    def test_role_change_clears_overrides(self, lab_admin, receptionist, make_request):
        """Changing role deletes all permission overrides."""
        req = make_request(lab_admin)
        UserService.grant_permission(
            receptionist, 'results.publish', lab_admin, '', req,
        )
        assert UserPermissionOverride.objects.filter(user=receptionist).count() == 1

        UserService.assign_role(receptionist, Role.TECHNICIAN, lab_admin, req)
        assert UserPermissionOverride.objects.filter(user=receptionist).count() == 0

    def test_lab_admin_can_assign_lab_admin(self, lab_admin, receptionist, make_request):
        """A lab_admin can assign the lab_admin role to another user."""
        UserService.assign_role(
            receptionist, Role.LAB_ADMIN, lab_admin, make_request(lab_admin),
        )
        receptionist.refresh_from_db()
        assert receptionist.role == Role.LAB_ADMIN


# ---------------------------------------------------------------------------
# Last-Admin Protection
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestLastAdminProtection:

    def test_cannot_demote_last_admin(self, lab_admin, make_request):
        """Cannot change the role of the only active LAB_ADMIN."""
        with pytest.raises(ValidationError, match='last active Lab Admin'):
            UserService.assign_role(
                lab_admin, Role.TECHNICIAN, lab_admin, make_request(lab_admin),
            )

    def test_can_demote_admin_when_another_exists(self, lab_admin, make_request):
        """If there's more than one admin, demotion is allowed."""
        second_admin = StaffUser.objects.create_user(
            email='admin2@testlab.io',
            password='testpass123!',
            first_name='Second',
            last_name='Admin',
            role=Role.LAB_ADMIN,
        )
        UserService.assign_role(
            lab_admin, Role.TECHNICIAN, second_admin, make_request(second_admin),
        )
        lab_admin.refresh_from_db()
        assert lab_admin.role == Role.TECHNICIAN

    def test_cannot_deactivate_last_admin(self, lab_admin, make_request):
        """Cannot deactivate the only active LAB_ADMIN."""
        # Create another user to do the deactivation
        other = StaffUser.objects.create_user(
            email='other@testlab.io',
            password='testpass123!',
            first_name='Other',
            last_name='User',
            role=Role.TECHNICIAN,
        )
        with pytest.raises(ValidationError, match='last active Lab Admin'):
            UserService.deactivate_user(lab_admin, other, make_request(other))

    def test_can_deactivate_admin_when_another_exists(self, lab_admin, make_request):
        """If there's another active admin, deactivation is allowed."""
        second_admin = StaffUser.objects.create_user(
            email='admin2@testlab.io',
            password='testpass123!',
            first_name='Second',
            last_name='Admin',
            role=Role.LAB_ADMIN,
        )
        UserService.deactivate_user(lab_admin, second_admin, make_request(second_admin))
        lab_admin.refresh_from_db()
        assert lab_admin.is_active is False


# ---------------------------------------------------------------------------
# Permission Override Audit Logging
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestPermissionOverrideAudit:

    def test_grant_creates_audit_log(self, lab_admin, receptionist, make_request):
        UserService.grant_permission(
            receptionist, 'results.publish', lab_admin,
            'Temporary', make_request(lab_admin),
        )

        log = AuditLog.objects.filter(
            action=AuditAction.PERMISSION_OVERRIDE,
        ).first()
        assert log is not None
        assert log.diff['permission'] == 'results.publish'
        assert log.diff['type'] == 'GRANT'
        assert log.diff['target_email'] == receptionist.email

    def test_revoke_creates_audit_log(self, lab_admin, receptionist, make_request):
        UserService.revoke_permission(
            receptionist, 'patients.create', lab_admin,
            'Restricted', make_request(lab_admin),
        )

        log = AuditLog.objects.filter(
            action=AuditAction.PERMISSION_OVERRIDE,
        ).first()
        assert log is not None
        assert log.diff['type'] == 'REVOKE'

    def test_remove_creates_audit_log(self, lab_admin, receptionist, make_request):
        req = make_request(lab_admin)
        UserService.grant_permission(
            receptionist, 'results.publish', lab_admin, '', req,
        )
        UserService.remove_permission_override(
            receptionist, 'results.publish', lab_admin, req,
        )

        logs = AuditLog.objects.filter(
            action=AuditAction.PERMISSION_OVERRIDE,
        )
        assert logs.count() == 2
        diff_types = {log.diff['type'] for log in logs}
        assert 'GRANT' in diff_types
        assert 'REMOVED' in diff_types
        removed_log = [l for l in logs if l.diff['type'] == 'REMOVED'][0]
        assert removed_log.diff['previous_override'] == 'GRANT'
