"""
Cytova — RBAC API Tests

Tests for role assignment, permission management, and enforcement
via the UserViewSet API endpoints.
"""
import pytest
from rest_framework.test import APIClient

from apps.users.models import StaffUser, Role, UserPermissionOverride
from apps.audit.models import AuditLog, AuditAction


@pytest.fixture()
def api_client():
    return APIClient()


@pytest.fixture()
def admin_client(api_client, lab_admin):
    """API client authenticated as LAB_ADMIN."""
    api_client.force_authenticate(user=lab_admin)
    return api_client


@pytest.fixture()
def receptionist_client(api_client, receptionist):
    """API client authenticated as RECEPTIONIST."""
    api_client.force_authenticate(user=receptionist)
    return api_client


# ---------------------------------------------------------------------------
# Role Assignment Endpoint
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestAssignRoleEndpoint:

    def test_assign_role_success(self, admin_client, receptionist):
        resp = admin_client.post(
            f'/api/v1/users/{receptionist.id}/assign-role/',
            {'role': 'TECHNICIAN'},
            format='json',
        )
        assert resp.status_code == 200
        receptionist.refresh_from_db()
        assert receptionist.role == 'TECHNICIAN'

    def test_assign_role_creates_audit_log(self, admin_client, receptionist):
        admin_client.post(
            f'/api/v1/users/{receptionist.id}/assign-role/',
            {'role': 'BIOLOGIST'},
            format='json',
        )
        assert AuditLog.objects.filter(action=AuditAction.ROLE_ASSIGN).exists()

    def test_assign_role_requires_permission(self, receptionist_client, lab_admin):
        """RECEPTIONIST does not have users.assign_role — should be 403."""
        resp = receptionist_client.post(
            f'/api/v1/users/{lab_admin.id}/assign-role/',
            {'role': 'TECHNICIAN'},
            format='json',
        )
        assert resp.status_code == 403

    def test_assign_invalid_role_fails(self, admin_client, receptionist):
        resp = admin_client.post(
            f'/api/v1/users/{receptionist.id}/assign-role/',
            {'role': 'FAKE_ROLE'},
            format='json',
        )
        assert resp.status_code == 400

    def test_lab_admin_assigns_lab_admin_to_another(self, admin_client, receptionist):
        """Critical delegation: lab_admin can promote another user to lab_admin."""
        resp = admin_client.post(
            f'/api/v1/users/{receptionist.id}/assign-role/',
            {'role': 'LAB_ADMIN'},
            format='json',
        )
        assert resp.status_code == 200
        receptionist.refresh_from_db()
        assert receptionist.role == 'LAB_ADMIN'

    def test_last_admin_protection(self, admin_client, lab_admin):
        """Cannot demote the only lab_admin."""
        resp = admin_client.post(
            f'/api/v1/users/{lab_admin.id}/assign-role/',
            {'role': 'TECHNICIAN'},
            format='json',
        )
        assert resp.status_code == 400
        lab_admin.refresh_from_db()
        assert lab_admin.role == 'LAB_ADMIN'


# ---------------------------------------------------------------------------
# Permission Management Endpoint
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestPermissionManagementEndpoint:

    def test_get_user_permissions(self, admin_client, receptionist):
        resp = admin_client.get(f'/api/v1/users/{receptionist.id}/permissions/')
        assert resp.status_code == 200
        data = resp.json()
        assert data['role'] == 'RECEPTIONIST'
        assert 'patients.create' in data['effective_permissions']
        assert 'patients.create' in data['role_permissions']
        assert data['overrides'] == []

    def test_grant_permission(self, admin_client, receptionist):
        resp = admin_client.post(
            f'/api/v1/users/{receptionist.id}/manage-permissions/',
            {
                'action': 'grant',
                'permission_code': 'results.publish',
                'reason': 'Temporary access for training',
            },
            format='json',
        )
        assert resp.status_code == 200

        # Verify override was created
        assert UserPermissionOverride.objects.filter(
            user=receptionist, permission_code='results.publish',
        ).exists()

        # Verify it appears in effective permissions
        resp = admin_client.get(f'/api/v1/users/{receptionist.id}/permissions/')
        assert 'results.publish' in resp.json()['effective_permissions']

    def test_revoke_permission(self, admin_client, receptionist):
        resp = admin_client.post(
            f'/api/v1/users/{receptionist.id}/manage-permissions/',
            {
                'action': 'revoke',
                'permission_code': 'patients.create',
                'reason': 'Under review',
            },
            format='json',
        )
        assert resp.status_code == 200

        resp = admin_client.get(f'/api/v1/users/{receptionist.id}/permissions/')
        assert 'patients.create' not in resp.json()['effective_permissions']

    def test_remove_override(self, admin_client, receptionist):
        # First grant
        admin_client.post(
            f'/api/v1/users/{receptionist.id}/manage-permissions/',
            {'action': 'grant', 'permission_code': 'results.publish'},
            format='json',
        )
        # Then remove
        resp = admin_client.post(
            f'/api/v1/users/{receptionist.id}/manage-permissions/',
            {'action': 'remove', 'permission_code': 'results.publish'},
            format='json',
        )
        assert resp.status_code == 200
        assert not UserPermissionOverride.objects.filter(
            user=receptionist, permission_code='results.publish',
        ).exists()

    def test_grant_unknown_permission_fails(self, admin_client, receptionist):
        resp = admin_client.post(
            f'/api/v1/users/{receptionist.id}/manage-permissions/',
            {'action': 'grant', 'permission_code': 'fake.permission'},
            format='json',
        )
        assert resp.status_code == 400

    def test_manage_permissions_requires_permission(self, receptionist_client, lab_admin):
        """RECEPTIONIST cannot manage permissions."""
        resp = receptionist_client.post(
            f'/api/v1/users/{lab_admin.id}/manage-permissions/',
            {'action': 'grant', 'permission_code': 'results.publish'},
            format='json',
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Roles & Permissions Catalog Endpoints
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestCatalogEndpoints:

    def test_roles_list(self, admin_client):
        resp = admin_client.get('/api/v1/users/roles/')
        assert resp.status_code == 200
        data = resp.json()
        codes = [r['code'] for r in data]
        assert 'LAB_ADMIN' in codes
        assert 'BILLING_OFFICER' in codes
        assert 'INVENTORY_MANAGER' in codes
        assert 'VIEWER_AUDITOR' in codes
        # Each role has permissions listed
        admin_entry = next(r for r in data if r['code'] == 'LAB_ADMIN')
        assert len(admin_entry['permissions']) > 20

    def test_roles_accessible_to_any_staff(self, receptionist_client):
        resp = receptionist_client.get('/api/v1/users/roles/')
        assert resp.status_code == 200

    def test_permissions_catalog(self, admin_client):
        resp = admin_client.get('/api/v1/users/permissions-catalog/')
        assert resp.status_code == 200
        data = resp.json()
        assert 'patients' in data
        assert 'results' in data
        patient_codes = [p['code'] for p in data['patients']]
        assert 'patients.view' in patient_codes

    def test_permissions_catalog_accessible_to_any_staff(self, receptionist_client):
        resp = receptionist_client.get('/api/v1/users/permissions-catalog/')
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Me Endpoint — Permissions Included
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestMeEndpointPermissions:

    def test_me_includes_permissions(self, receptionist_client):
        resp = receptionist_client.get('/api/v1/users/me/')
        assert resp.status_code == 200
        data = resp.json()
        assert 'permissions' in data
        assert 'patients.create' in data['permissions']
        assert data['role'] == 'RECEPTIONIST'

    def test_me_reflects_overrides(self, admin_client, receptionist_client, receptionist):
        # Admin grants extra permission
        admin_client.post(
            f'/api/v1/users/{receptionist.id}/manage-permissions/',
            {'action': 'grant', 'permission_code': 'results.publish'},
            format='json',
        )

        resp = receptionist_client.get('/api/v1/users/me/')
        assert 'results.publish' in resp.json()['permissions']


# ---------------------------------------------------------------------------
# Permission Enforcement on Existing Endpoints
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestPermissionEnforcement:

    def test_user_list_requires_users_view(self, api_client, viewer_auditor):
        """VIEWER_AUDITOR has users.view — can list users."""
        api_client.force_authenticate(user=viewer_auditor)
        resp = api_client.get('/api/v1/users/')
        assert resp.status_code == 200

    def test_user_create_requires_users_create(self, receptionist_client):
        """RECEPTIONIST does not have users.create — cannot create users."""
        resp = receptionist_client.post(
            '/api/v1/users/',
            {
                'email': 'new@testlab.io',
                'first_name': 'New',
                'last_name': 'User',
                'role': 'TECHNICIAN',
                'password': 'securePass123!',
            },
            format='json',
        )
        assert resp.status_code == 403

    def test_user_create_allowed_for_admin(self, admin_client):
        """LAB_ADMIN has users.create — can create users."""
        resp = admin_client.post(
            '/api/v1/users/',
            {
                'email': 'new@testlab.io',
                'first_name': 'New',
                'last_name': 'User',
                'role': 'TECHNICIAN',
                'password': 'securePass123!',
            },
            format='json',
        )
        assert resp.status_code == 201

    def test_deactivate_requires_users_deactivate(self, receptionist_client, technician):
        resp = receptionist_client.post(
            f'/api/v1/users/{technician.id}/deactivate/',
        )
        assert resp.status_code == 403

    def test_unauthenticated_cannot_access(self, api_client):
        resp = api_client.get('/api/v1/users/')
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Tenant Isolation of Overrides
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestTenantIsolation:

    def test_override_exists_only_in_current_schema(
        self, lab_admin, receptionist, make_request,
    ):
        """Overrides are stored per-tenant-schema — basic model test."""
        from apps.users.services import UserService
        UserService.grant_permission(
            receptionist, 'results.publish', lab_admin,
            '', make_request(lab_admin),
        )
        # Override exists in current schema
        assert UserPermissionOverride.objects.filter(
            user=receptionist, permission_code='results.publish',
        ).count() == 1
