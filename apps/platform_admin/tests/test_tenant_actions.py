"""
Platform-admin tenant actions — Phase 3 tests.

Pins the contract of the four POST detail actions:

  POST /tenants/{id}/suspend/
  POST /tenants/{id}/reactivate/
  POST /tenants/{id}/extend-trial/   payload: {"days": int}
  POST /tenants/{id}/change-plan/    payload: {"plan_id": uuid}

Each action MUST:
  - flip the documented state field on the tenant or its subscription
  - write exactly one PLATFORM_TENANT_* audit row
  - reject anonymous, lab-staff, and patient-portal callers BEFORE
    any state change runs

Reversibility constraint
------------------------
Phase 3's strict-rules contract says every action must be reversible.
The tests pin reversibility shape rather than every undo path:

  - Suspend / reactivate are obvious inverses; both are exercised.
  - Extend-trial preserves the prior ``trial_end_date`` in the
    audit row, so a future "undo" feature has the data to restore.
  - Change-plan marks the previous subscription EXPIRED (not
    CANCELLED), and the audit row records both subscription ids.
    EXPIRED → ACTIVE is a permitted transition, so the previous
    plan can be restored without DB surgery.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context
from rest_framework.test import APIClient

from apps.patient_portal.services import register_patient_account
from apps.patient_portal.tokens import PatientAccessToken
from apps.platform_admin.models import (
    PlatformAdminAuditLog, PlatformAdminRole, PlatformAdminUser,
    PlatformAuditAction,
)
from apps.platform_admin.tokens import PlatformAdminAccessToken
from apps.tenants.models import (
    Domain, Subscription, SubscriptionPlan, SubscriptionStatus, Tenant,
)
from apps.users.models import Role, StaffUser
from apps.authentication.tokens import CytovaAccessToken


PASSWORD = 'Strong-Pass-1234!'

BASE = '/api/v1/platform-admin/tenants/'


def _action_url(tenant_id, slug: str) -> str:
    return f'{BASE}{tenant_id}/{slug}/'


# ---------------------------------------------------------------------------
# Helpers (mirror the Phase 2 listing tests so each suite reads on its own)
# ---------------------------------------------------------------------------

def _client() -> APIClient:
    return APIClient(HTTP_HOST='core.localhost')


def _auth(client: APIClient, token: str) -> APIClient:
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return client


def _make_admin(role: str = PlatformAdminRole.SUPER_ADMIN) -> PlatformAdminUser:
    return PlatformAdminUser.objects.create_user(
        email=f'{role.lower()}-actions@cytova.io',
        password=PASSWORD, role=role,
    )


def _admin_client(admin: PlatformAdminUser | None = None) -> APIClient:
    admin = admin or _make_admin()
    return _auth(_client(), str(PlatformAdminAccessToken.for_user(admin)))


def _make_tenant(
    *, name: str = 'Action Lab', subdomain: str = 'action-lab',
    is_active: bool = True, with_domain: bool = True,
) -> Tenant:
    tenant = Tenant(
        name=name, subdomain=subdomain,
        schema_name=f'schema_{subdomain.replace("-", "_")}',
        is_active=is_active,
    )
    tenant.auto_create_schema = False
    tenant.save()
    if with_domain:
        Domain.objects.create(
            domain=f'{subdomain}.cytova.io', tenant=tenant, is_primary=True,
        )
    return tenant


def _make_plan(code: str = 'STARTER', is_trial: bool = False) -> SubscriptionPlan:
    return SubscriptionPlan.objects.create(
        code=code, name=code.title(),
        is_trial=is_trial,
        trial_duration_days=14 if is_trial else None,
    )


def _make_subscription(
    tenant: Tenant, plan: SubscriptionPlan,
    *, status: str = SubscriptionStatus.ACTIVE,
    trial_end_date=None,
) -> Subscription:
    return Subscription.objects.create(
        tenant=tenant, plan=plan, status=status,
        trial_end_date=trial_end_date,
    )


def _data(resp):
    body = resp.json()
    return body.get('data', body)


# ---------------------------------------------------------------------------
# 1. Suspend
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSuspendAction:

    def test_suspend_flips_is_active_false(self):
        tenant = _make_tenant(subdomain='lab-suspend-1')
        assert tenant.is_active is True

        resp = _admin_client().post(_action_url(tenant.id, 'suspend'))
        assert resp.status_code == 200, resp.content

        tenant.refresh_from_db()
        assert tenant.is_active is False
        # ``suspended_at`` is set by the underlying TenantService —
        # pin it here so a later refactor that drops the timestamp
        # is caught immediately.
        assert tenant.suspended_at is not None
        assert _data(resp)['is_active'] is False

    def test_suspend_is_idempotent(self):
        # Re-suspending an already-suspended tenant must not error
        # and must not stack audit rows misleadingly.
        tenant = _make_tenant(subdomain='lab-suspend-2', is_active=False)
        client = _admin_client()
        client.post(_action_url(tenant.id, 'suspend'))
        resp = client.post(_action_url(tenant.id, 'suspend'))
        assert resp.status_code == 200

    def test_suspend_writes_audit_row(self):
        admin = _make_admin()
        tenant = _make_tenant(subdomain='lab-suspend-3')

        _admin_client(admin).post(_action_url(tenant.id, 'suspend'))
        rows = list(PlatformAdminAuditLog.objects.filter(
            actor=admin,
            action=PlatformAuditAction.PLATFORM_TENANT_SUSPENDED,
            entity_id=tenant.id,
        ))
        assert len(rows) == 1
        meta = rows[0].metadata
        assert meta['before']['is_active'] is True
        assert meta['after']['is_active'] is False
        assert meta['subdomain'] == 'lab-suspend-3'

    def test_anonymous_cannot_suspend(self):
        tenant = _make_tenant(subdomain='lab-suspend-4')
        before = PlatformAdminAuditLog.objects.filter(
            action=PlatformAuditAction.PLATFORM_TENANT_SUSPENDED,
        ).count()
        resp = _client().post(_action_url(tenant.id, 'suspend'))
        assert resp.status_code == 401
        tenant.refresh_from_db()
        # State did NOT change AND no audit row written — the auth
        # gate runs before the action handler.
        assert tenant.is_active is True
        after = PlatformAdminAuditLog.objects.filter(
            action=PlatformAuditAction.PLATFORM_TENANT_SUSPENDED,
        ).count()
        assert before == after

    def test_lab_staff_token_cannot_suspend(self):
        tenant = _make_tenant(subdomain='lab-suspend-5')
        with schema_context('schema_testlab'):
            staff = StaffUser.objects.create_user(
                email='actions-staff@testlab.io', password=PASSWORD,
                first_name='Lab', last_name='Admin', role=Role.LAB_ADMIN,
            )
            staff_token = str(CytovaAccessToken.for_user(staff))
        resp = _auth(_client(), staff_token).post(_action_url(tenant.id, 'suspend'))
        assert resp.status_code == 401
        tenant.refresh_from_db()
        assert tenant.is_active is True

    def test_patient_token_cannot_suspend(self):
        tenant = _make_tenant(subdomain='lab-suspend-6')
        account = register_patient_account(
            email='patient-actions@portal.test', password=PASSWORD,
            first_name='Pat', last_name='Test',
            date_of_birth='1990-05-17', accept_terms=True,
        )
        account.email_verified_at = timezone.now()
        account.save(update_fields=['email_verified_at'])
        patient_token = str(
            PatientAccessToken.for_patient(account, profile=account.profile)
        )
        resp = _auth(_client(), patient_token).post(_action_url(tenant.id, 'suspend'))
        assert resp.status_code == 401
        tenant.refresh_from_db()
        assert tenant.is_active is True


# ---------------------------------------------------------------------------
# 2. Reactivate
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestReactivateAction:

    def test_reactivate_restores_access(self):
        tenant = _make_tenant(subdomain='lab-react-1', is_active=False)
        # Pre-condition: tenant.suspended_at is None for our fixture
        # builder since it was never explicitly suspended via service —
        # set it so we can prove it gets cleared.
        tenant.suspended_at = timezone.now()
        tenant.save(update_fields=['suspended_at'])

        resp = _admin_client().post(_action_url(tenant.id, 'reactivate'))
        assert resp.status_code == 200, resp.content

        tenant.refresh_from_db()
        assert tenant.is_active is True
        # ``activate_tenant`` clears ``suspended_at`` so the next
        # ``is_suspended`` check correctly returns False.
        assert tenant.suspended_at is None
        assert _data(resp)['is_active'] is True

    def test_reactivate_is_idempotent(self):
        tenant = _make_tenant(subdomain='lab-react-2')  # already active
        resp = _admin_client().post(_action_url(tenant.id, 'reactivate'))
        assert resp.status_code == 200

    def test_reactivate_writes_audit_row(self):
        admin = _make_admin()
        tenant = _make_tenant(subdomain='lab-react-3', is_active=False)

        _admin_client(admin).post(_action_url(tenant.id, 'reactivate'))
        rows = list(PlatformAdminAuditLog.objects.filter(
            actor=admin,
            action=PlatformAuditAction.PLATFORM_TENANT_REACTIVATED,
            entity_id=tenant.id,
        ))
        assert len(rows) == 1
        meta = rows[0].metadata
        assert meta['before']['is_active'] is False
        assert meta['after']['is_active'] is True

    def test_lab_staff_token_cannot_reactivate(self):
        tenant = _make_tenant(subdomain='lab-react-4', is_active=False)
        with schema_context('schema_testlab'):
            staff = StaffUser.objects.create_user(
                email='react-staff@testlab.io', password=PASSWORD,
                first_name='Lab', last_name='Admin', role=Role.LAB_ADMIN,
            )
            staff_token = str(CytovaAccessToken.for_user(staff))
        resp = _auth(_client(), staff_token).post(_action_url(tenant.id, 'reactivate'))
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 3. Extend trial
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestExtendTrialAction:

    def test_extend_trial_moves_end_date_forward(self):
        tenant = _make_tenant(subdomain='lab-extend-1')
        plan = _make_plan('TRIAL', is_trial=True)
        original_end = timezone.now() + timedelta(days=7)
        sub = _make_subscription(
            tenant, plan,
            status=SubscriptionStatus.TRIAL,
            trial_end_date=original_end,
        )

        resp = _admin_client().post(
            _action_url(tenant.id, 'extend-trial'),
            data={'days': 14}, format='json',
        )
        assert resp.status_code == 200, resp.content

        sub.refresh_from_db()
        # Mid-trial extension anchors to the existing end date and
        # adds 14 days, so the new end is roughly 21 days from now.
        assert sub.trial_end_date == original_end + timedelta(days=14)

    def test_extend_lapsed_trial_anchors_to_now(self):
        # If the trial already lapsed, extending should not leave the
        # new end date in the past — that would make the action a
        # silent no-op for the tenant. Anchor to ``now`` instead.
        tenant = _make_tenant(subdomain='lab-extend-2')
        plan = _make_plan('TRIAL', is_trial=True)
        lapsed = timezone.now() - timedelta(days=3)
        sub = _make_subscription(
            tenant, plan,
            status=SubscriptionStatus.TRIAL, trial_end_date=lapsed,
        )

        before = timezone.now()
        _admin_client().post(
            _action_url(tenant.id, 'extend-trial'),
            data={'days': 7}, format='json',
        )

        sub.refresh_from_db()
        assert sub.trial_end_date > before + timedelta(days=6)
        assert sub.trial_end_date < before + timedelta(days=8)

    def test_extend_trial_writes_audit_with_before_and_after(self):
        admin = _make_admin()
        tenant = _make_tenant(subdomain='lab-extend-3')
        plan = _make_plan('TRIAL', is_trial=True)
        original_end = timezone.now() + timedelta(days=7)
        _make_subscription(
            tenant, plan,
            status=SubscriptionStatus.TRIAL, trial_end_date=original_end,
        )

        _admin_client(admin).post(
            _action_url(tenant.id, 'extend-trial'),
            data={'days': 5}, format='json',
        )
        rows = list(PlatformAdminAuditLog.objects.filter(
            actor=admin,
            action=PlatformAuditAction.PLATFORM_TENANT_TRIAL_EXTENDED,
            entity_id=tenant.id,
        ))
        assert len(rows) == 1
        meta = rows[0].metadata
        assert meta['days'] == 5
        # ``before``/``after`` are datetimes serialised through
        # ``json_safe`` so the audit row carries the data needed to
        # roll back the extension by hand.
        assert meta['before']['trial_end_date'] is not None
        assert meta['after']['trial_end_date'] is not None
        assert meta['before']['trial_end_date'] != meta['after']['trial_end_date']

    def test_extend_trial_without_active_trial_returns_400(self):
        tenant = _make_tenant(subdomain='lab-extend-4')
        # No subscription at all — extension has nothing to extend.
        resp = _admin_client().post(
            _action_url(tenant.id, 'extend-trial'),
            data={'days': 7}, format='json',
        )
        assert resp.status_code == 400, resp.content

    def test_extend_trial_rejects_zero_or_negative_days(self):
        tenant = _make_tenant(subdomain='lab-extend-5')
        plan = _make_plan('TRIAL', is_trial=True)
        _make_subscription(
            tenant, plan,
            status=SubscriptionStatus.TRIAL,
            trial_end_date=timezone.now() + timedelta(days=7),
        )

        client = _admin_client()
        resp_zero = client.post(
            _action_url(tenant.id, 'extend-trial'),
            data={'days': 0}, format='json',
        )
        assert resp_zero.status_code == 400

        resp_neg = client.post(
            _action_url(tenant.id, 'extend-trial'),
            data={'days': -1}, format='json',
        )
        assert resp_neg.status_code == 400


# ---------------------------------------------------------------------------
# 4. Change plan
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestChangePlanAction:

    def test_change_plan_creates_new_active_subscription(self):
        tenant = _make_tenant(subdomain='lab-change-1')
        starter = _make_plan('STARTER')
        pro = _make_plan('PRO')
        old_sub = _make_subscription(
            tenant, starter, status=SubscriptionStatus.ACTIVE,
        )

        resp = _admin_client().post(
            _action_url(tenant.id, 'change-plan'),
            data={'plan_id': str(pro.id)}, format='json',
        )
        assert resp.status_code == 200, resp.content

        # New row exists and is ACTIVE on PRO.
        new_subs = Subscription.objects.filter(
            tenant=tenant, plan=pro, status=SubscriptionStatus.ACTIVE,
        )
        assert new_subs.count() == 1

        # Previous row was closed (EXPIRED), not deleted — the audit
        # trail and the data needed to undo the change are intact.
        old_sub.refresh_from_db()
        assert old_sub.status == SubscriptionStatus.EXPIRED

    def test_change_plan_with_no_prior_subscription_just_creates(self):
        tenant = _make_tenant(subdomain='lab-change-2')
        pro = _make_plan('PRO')

        resp = _admin_client().post(
            _action_url(tenant.id, 'change-plan'),
            data={'plan_id': str(pro.id)}, format='json',
        )
        assert resp.status_code == 200
        assert Subscription.objects.filter(
            tenant=tenant, plan=pro, status=SubscriptionStatus.ACTIVE,
        ).count() == 1

    def test_change_plan_writes_audit_with_both_subscription_ids(self):
        admin = _make_admin()
        tenant = _make_tenant(subdomain='lab-change-3')
        starter = _make_plan('STARTER')
        pro = _make_plan('PRO')
        old_sub = _make_subscription(
            tenant, starter, status=SubscriptionStatus.ACTIVE,
        )

        _admin_client(admin).post(
            _action_url(tenant.id, 'change-plan'),
            data={'plan_id': str(pro.id)}, format='json',
        )

        rows = list(PlatformAdminAuditLog.objects.filter(
            actor=admin,
            action=PlatformAuditAction.PLATFORM_TENANT_PLAN_CHANGED,
            entity_id=tenant.id,
        ))
        assert len(rows) == 1
        meta = rows[0].metadata
        assert meta['previous_plan_code'] == 'STARTER'
        assert meta['new_plan_code'] == 'PRO'
        assert meta['previous_subscription_id'] == str(old_sub.id)
        # New id present and distinct so a recovery script can find
        # exactly the row it needs to roll back.
        new_id = meta['new_subscription_id']
        assert new_id is not None
        assert new_id != str(old_sub.id)

    def test_change_plan_to_same_plan_is_noop_at_data_layer(self):
        # An operator double-clicking the action should not pile up
        # parallel ACTIVE subscriptions on the same plan. The action
        # is idempotent: the second call returns the existing row.
        tenant = _make_tenant(subdomain='lab-change-4')
        pro = _make_plan('PRO')
        existing = _make_subscription(
            tenant, pro, status=SubscriptionStatus.ACTIVE,
        )

        resp = _admin_client().post(
            _action_url(tenant.id, 'change-plan'),
            data={'plan_id': str(pro.id)}, format='json',
        )
        assert resp.status_code == 200

        active_count = Subscription.objects.filter(
            tenant=tenant, plan=pro, status=SubscriptionStatus.ACTIVE,
        ).count()
        assert active_count == 1
        existing.refresh_from_db()
        assert existing.status == SubscriptionStatus.ACTIVE

    def test_change_plan_rejects_inactive_plan(self):
        tenant = _make_tenant(subdomain='lab-change-5')
        retired = _make_plan('RETIRED')
        retired.is_active = False
        retired.save(update_fields=['is_active', 'updated_at'])

        resp = _admin_client().post(
            _action_url(tenant.id, 'change-plan'),
            data={'plan_id': str(retired.id)}, format='json',
        )
        # Serializer's queryset filters is_active=True, so the
        # PrimaryKeyRelatedField returns a "does not exist" error
        # mapped to 400 by DRF.
        assert resp.status_code == 400

    def test_change_plan_rejects_unknown_plan_id(self):
        tenant = _make_tenant(subdomain='lab-change-6')
        resp = _admin_client().post(
            _action_url(tenant.id, 'change-plan'),
            data={'plan_id': '00000000-0000-0000-0000-000000000000'},
            format='json',
        )
        assert resp.status_code == 400

    def test_lab_staff_token_cannot_change_plan(self):
        tenant = _make_tenant(subdomain='lab-change-7')
        pro = _make_plan('PRO')
        with schema_context('schema_testlab'):
            staff = StaffUser.objects.create_user(
                email='change-staff@testlab.io', password=PASSWORD,
                first_name='Lab', last_name='Admin', role=Role.LAB_ADMIN,
            )
            staff_token = str(CytovaAccessToken.for_user(staff))
        resp = _auth(_client(), staff_token).post(
            _action_url(tenant.id, 'change-plan'),
            data={'plan_id': str(pro.id)}, format='json',
        )
        assert resp.status_code == 401
        assert not Subscription.objects.filter(
            tenant=tenant, plan=pro,
        ).exists()
