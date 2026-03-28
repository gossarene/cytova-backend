"""
Tests for platform admin endpoint protection and audit logging.
"""
import json

import pytest
from django.test import RequestFactory

from apps.tenants.models import (
    Domain,
    PlatformAdmin,
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
    Tenant,
)
from apps.tenants.platform_audit import PlatformAuditLog, PlatformAction
from apps.tenants.subscription_service import SubscriptionService
from apps.tenants.tokens import PlatformAdminAccessToken


@pytest.fixture(autouse=True)
def _in_tenant_schema():
    yield


@pytest.fixture()
def platform_admin():
    return PlatformAdmin.objects.create(
        email='superadmin@cytova.io',
        password='pbkdf2_test',  # won't use check_password in these tests
        is_active=True,
    )


@pytest.fixture()
def starter_plan():
    return SubscriptionPlan.objects.create(
        code='STARTER', name='Starter', is_trial=True, trial_duration_days=14,
    )


@pytest.fixture()
def sample_tenant(starter_plan):
    tenant = Tenant(name='Admin Test Lab', subdomain='admin-test', schema_name='schema_admin_test')
    tenant.save()
    Domain.objects.create(domain='admin-test.localhost', tenant=tenant, is_primary=True)
    SubscriptionService.create_trial(tenant, starter_plan)
    return tenant


# ---------------------------------------------------------------------------
# Platform endpoint protection
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestPlatformEndpointProtection:

    def test_unauthenticated_request_rejected(self, client):
        """Platform endpoints reject requests without auth."""
        response = client.get('/api/v1/platform/tenants/')
        # Should be 401 or 403 — not 200
        assert response.status_code in (401, 403)

    def test_tenant_staff_token_rejected(self, client, sample_tenant):
        """A per-tenant StaffUser JWT cannot access platform endpoints."""
        from django_tenants.utils import schema_context
        from apps.users.models import StaffUser, Role

        with schema_context(sample_tenant.schema_name):
            user = StaffUser.objects.create_user(
                email='staff@admin-test.io',
                password='testpass123!',
                first_name='Staff',
                last_name='User',
                role=Role.LAB_ADMIN,
            )

        # Generate a tenant-scoped token (not a platform admin token)
        from rest_framework_simplejwt.tokens import AccessToken
        token = AccessToken.for_user(user)

        response = client.get(
            '/api/v1/platform/tenants/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        # PlatformAdminJWTAuthentication rejects non-PLATFORM_ADMIN tokens
        assert response.status_code in (401, 403)

    def test_platform_admin_token_accepted(self, client, platform_admin):
        """A valid platform admin token grants access."""
        token = PlatformAdminAccessToken.for_user(platform_admin)

        response = client.get(
            '/api/v1/platform/tenants/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Platform audit logging
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestPlatformAuditLogging:

    def test_tenant_creation_audited(self, client, platform_admin):
        token = PlatformAdminAccessToken.for_user(platform_admin)

        response = client.post(
            '/api/v1/platform/tenants/',
            data=json.dumps({
                'name': 'Audited Lab',
                'subdomain': 'audited-lab',
                'plan': 'STARTER',
            }),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        assert response.status_code == 201

        log = PlatformAuditLog.objects.filter(
            entity_type='Tenant',
            action=PlatformAction.CREATE,
        ).first()
        assert log is not None
        assert log.actor_email == platform_admin.email
        assert log.diff['after']['subdomain'] == 'audited-lab'

    def test_tenant_suspend_audited(self, client, platform_admin, sample_tenant):
        token = PlatformAdminAccessToken.for_user(platform_admin)

        response = client.post(
            f'/api/v1/platform/tenants/{sample_tenant.id}/suspend/',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        assert response.status_code == 200

        log = PlatformAuditLog.objects.filter(
            entity_type='Tenant',
            entity_id=sample_tenant.id,
            action=PlatformAction.SUSPEND,
        ).first()
        assert log is not None

    def test_subscription_activate_audited(self, client, platform_admin, sample_tenant):
        token = PlatformAdminAccessToken.for_user(platform_admin)
        sub = sample_tenant.subscriptions.first()

        response = client.post(
            f'/api/v1/platform/subscriptions/{sub.id}/activate/',
            data=json.dumps({'period_months': 12}),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        assert response.status_code == 200

        log = PlatformAuditLog.objects.filter(
            entity_type='Subscription',
            entity_id=sub.id,
            action=PlatformAction.ACTIVATE,
        ).first()
        assert log is not None
        assert log.diff['after']['status'] == 'ACTIVE'

    def test_plan_creation_audited(self, client, platform_admin):
        token = PlatformAdminAccessToken.for_user(platform_admin)

        response = client.post(
            '/api/v1/platform/plans/',
            data=json.dumps({
                'code': 'ENTERPRISE',
                'name': 'Enterprise',
                'monthly_price': '499.00',
            }),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        assert response.status_code == 201

        log = PlatformAuditLog.objects.filter(
            entity_type='SubscriptionPlan',
            action=PlatformAction.CREATE,
        ).first()
        assert log is not None
        assert log.diff['after']['code'] == 'ENTERPRISE'

    def test_plan_change_audited(self, client, platform_admin, sample_tenant):
        token = PlatformAdminAccessToken.for_user(platform_admin)
        sub = sample_tenant.subscriptions.first()

        # Create a pro plan to switch to
        pro = SubscriptionPlan.objects.create(code='PRO', name='Pro', trial_duration_days=None)

        response = client.post(
            f'/api/v1/platform/subscriptions/{sub.id}/change-plan/',
            data=json.dumps({'plan_id': str(pro.id)}),
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        assert response.status_code == 200

        log = PlatformAuditLog.objects.filter(
            entity_type='Subscription',
            entity_id=sub.id,
            action=PlatformAction.PLAN_CHANGE,
        ).first()
        assert log is not None
        assert log.diff['before']['plan'] == 'STARTER'
        assert log.diff['after']['plan'] == 'PRO'


# ---------------------------------------------------------------------------
# Audit log immutability
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestAuditLogImmutability:

    def test_platform_audit_log_no_update(self):
        log = PlatformAuditLog.objects.create(
            actor_email='test@cytova.io',
            action=PlatformAction.CREATE,
            entity_type='Test',
            entity_id=None,
            diff={},
        )
        log.action = PlatformAction.UPDATE
        with pytest.raises(PermissionError):
            log.save()

    def test_platform_audit_log_no_delete(self):
        log = PlatformAuditLog.objects.create(
            actor_email='test@cytova.io',
            action=PlatformAction.CREATE,
            entity_type='Test',
            entity_id=None,
            diff={},
        )
        with pytest.raises(PermissionError):
            log.delete()
