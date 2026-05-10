"""
Platform-admin dashboard — Phase 5 tests.

Pins the contract of ``GET /api/v1/platform-admin/dashboard/``:

  - Returns the documented top-level keys with integer counts only.
  - Counts move correctly when the underlying tables grow.
  - Cross-token rejection (lab-staff, patient, anonymous, inactive
    admin).
  - PII / row-identifier blocklist — no email, no subdomain, no
    UUID list.
  - One ``PLATFORM_DASHBOARD_VIEWED`` audit row per success.

The "30 days" window is treated as an opaque sliding window: tests
either place activity at ``now`` (must be counted) or at ``now -
35 days`` (must not). They never assert exact day-edge behaviour
because that's the job of a unit test on the cutoff helper, not a
view-level integration test.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context
from rest_framework.test import APIClient

from apps.patient_portal.models import (
    PatientAccount, PatientSharedResult, SharedResultStatus,
)
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


URL = '/api/v1/platform-admin/dashboard/'

PASSWORD = 'Strong-Pass-1234!'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client() -> APIClient:
    return APIClient(HTTP_HOST='core.localhost')


def _auth(client: APIClient, token: str) -> APIClient:
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return client


def _make_admin() -> PlatformAdminUser:
    return PlatformAdminUser.objects.create_user(
        email='dashboard-admin@cytova.io', password=PASSWORD,
        role=PlatformAdminRole.SUPER_ADMIN,
    )


def _admin_client(admin: PlatformAdminUser | None = None) -> APIClient:
    admin = admin or _make_admin()
    return _auth(_client(), str(PlatformAdminAccessToken.for_user(admin)))


def _make_tenant(
    *, subdomain: str, is_active: bool = True, with_domain: bool = True,
) -> Tenant:
    tenant = Tenant(
        name=f'Lab {subdomain}', subdomain=subdomain,
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


def _make_trial_plan(code: str = 'TRIAL_PLAN') -> SubscriptionPlan:
    return SubscriptionPlan.objects.create(
        code=code, name=code, is_trial=True, trial_duration_days=14,
    )


def _attach_subscription(
    tenant: Tenant, plan: SubscriptionPlan,
    *, status: str = SubscriptionStatus.TRIAL,
) -> Subscription:
    return Subscription.objects.create(
        tenant=tenant, plan=plan, status=status,
        trial_end_date=(
            timezone.now() + timedelta(days=14)
            if status == SubscriptionStatus.TRIAL else None
        ),
    )


def _make_account(
    *, email: str, is_active: bool = True, created_at=None,
) -> PatientAccount:
    account = PatientAccount.objects.create_user(
        email=email, password=PASSWORD,
    )
    fields = []
    if not is_active:
        account.is_active = False
        fields.append('is_active')
    if created_at is not None:
        # ``created_at`` defaults to ``timezone.now``; updating via
        # the manager bypasses the default for backdating tests.
        PatientAccount.objects.filter(pk=account.pk).update(
            created_at=created_at,
        )
    if fields:
        fields.append('updated_at')
        account.save(update_fields=fields)
    return account


def _make_share(
    *, account: PatientAccount,
    created_at=None,
    last_downloaded_at=None,
    email_status: str = '',
    email_sent_at=None,
    status: str = SharedResultStatus.ACTIVE,
) -> PatientSharedResult:
    share = PatientSharedResult.objects.create(
        patient_account=account,
        source_type='DIRECT', source_name='Test Lab',
        status=status,
        last_downloaded_at=last_downloaded_at,
        email_notification_status=email_status,
        email_notification_sent_at=email_sent_at,
    )
    if created_at is not None:
        PatientSharedResult.objects.filter(pk=share.pk).update(
            created_at=created_at,
        )
    return share


def _data(resp):
    body = resp.json()
    return body.get('data', body)


# ---------------------------------------------------------------------------
# 1. Access control
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestAccessControl:

    def test_anonymous_request_rejected(self):
        resp = _client().get(URL)
        assert resp.status_code == 401, resp.content

    def test_lab_staff_token_rejected(self):
        with schema_context('schema_testlab'):
            staff = StaffUser.objects.create_user(
                email='dashboard-staff@testlab.io', password=PASSWORD,
                first_name='Lab', last_name='Admin', role=Role.LAB_ADMIN,
            )
            staff_token = str(CytovaAccessToken.for_user(staff))
        resp = _auth(_client(), staff_token).get(URL)
        assert resp.status_code == 401, resp.content

    def test_patient_token_rejected(self):
        # Spin up a real patient account so the token claim shape
        # matches production rather than a hand-rolled stub.
        account = _make_account(email='dashboard-patient@portal.test')
        # ``for_patient`` requires a profile in production — mark
        # the account verified via the model directly so the test
        # stays focused on the auth boundary.
        from apps.patient_portal.models import PatientProfile
        PatientProfile.objects.create(
            account=account, cytova_patient_id='CV-DASH-1234',
            first_name='Pat', last_name='Test',
            date_of_birth='1990-01-01',
        )
        token = str(
            PatientAccessToken.for_patient(account, profile=account.profile),
        )
        resp = _auth(_client(), token).get(URL)
        assert resp.status_code == 401, resp.content

    def test_inactive_admin_rejected(self):
        admin = _make_admin()
        admin.is_active = False
        admin.save(update_fields=['is_active', 'updated_at'])
        token = str(PlatformAdminAccessToken.for_user(admin))
        resp = _auth(_client(), token).get(URL)
        assert resp.status_code == 401, resp.content


# ---------------------------------------------------------------------------
# 2. Response shape
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestResponseShape:

    def test_top_level_keys(self):
        resp = _admin_client().get(URL)
        assert resp.status_code == 200, resp.content
        data = _data(resp)
        assert set(data.keys()) == {
            'generated_at', 'window_days', 'tenants', 'patients', 'activity',
        }
        assert data['window_days'] == 30
        assert set(data['tenants'].keys()) == {
            'total', 'active', 'suspended', 'trial',
        }
        assert set(data['patients'].keys()) == {
            'total', 'active', 'new_last_30_days',
        }
        assert set(data['activity'].keys()) == {
            'results_shared_last_30_days',
            'results_downloaded_last_30_days',
            'emails_sent_last_30_days',
        }

    def test_no_pii_or_row_identifiers_in_response(self):
        # Hard-coded blocklist for the privacy contract: nothing
        # below should appear anywhere in the JSON tree, regardless
        # of nesting. The dashboard is meant to be screenshot-safe.
        _make_tenant(subdomain='shape-lab-1')
        _make_account(email='shape-patient@portal.test')

        import json
        body = json.dumps(_data(_admin_client().get(URL)))
        for forbidden in (
            'shape-lab-1',
            'shape-lab-1.cytova.io',
            'shape-patient@portal.test',
            'CV-',  # no Cytova IDs in the dashboard
        ):
            assert forbidden not in body, (
                f'Dashboard leaked {forbidden!r} into response body: {body}'
            )

    def test_counts_are_integers(self):
        # Defensive: serializer is declared with IntegerFields so a
        # regression that returns an Avg() (Decimal) would surface
        # at type level rather than as a confusing JSON number.
        data = _data(_admin_client().get(URL))
        for key in ('total', 'active', 'suspended', 'trial'):
            assert isinstance(data['tenants'][key], int)
        for key in ('total', 'active', 'new_last_30_days'):
            assert isinstance(data['patients'][key], int)
        for key in (
            'results_shared_last_30_days',
            'results_downloaded_last_30_days',
            'emails_sent_last_30_days',
        ):
            assert isinstance(data['activity'][key], int)


# ---------------------------------------------------------------------------
# 3. Tenant counters
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestTenantCounters:

    def test_total_active_suspended_split(self):
        # Baseline: the session-scoped ``testlab`` tenant always
        # exists and is active → captured as a delta against the
        # measurement before our fixtures.
        client = _admin_client()
        baseline = _data(client.get(URL))['tenants']

        _make_tenant(subdomain='count-active-1')
        _make_tenant(subdomain='count-suspended-1', is_active=False)
        _make_tenant(subdomain='count-suspended-2', is_active=False)

        after = _data(client.get(URL))['tenants']
        assert after['total'] == baseline['total'] + 3
        assert after['active'] == baseline['active'] + 1
        assert after['suspended'] == baseline['suspended'] + 2

    def test_trial_count_uses_subscription_state(self):
        client = _admin_client()
        baseline = _data(client.get(URL))['tenants']

        plan = _make_trial_plan('TR_DASH')
        # Two trial tenants
        for sub in ('trial-tenant-1', 'trial-tenant-2'):
            tenant = _make_tenant(subdomain=sub)
            _attach_subscription(tenant, plan, status=SubscriptionStatus.TRIAL)
        # One active tenant — must NOT be counted as trial.
        active_tenant = _make_tenant(subdomain='active-non-trial')
        _attach_subscription(
            active_tenant, plan, status=SubscriptionStatus.ACTIVE,
        )

        after = _data(client.get(URL))['tenants']
        assert after['trial'] == baseline['trial'] + 2


# ---------------------------------------------------------------------------
# 4. Patient counters
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestPatientCounters:

    def test_total_active_split(self):
        client = _admin_client()
        baseline = _data(client.get(URL))['patients']

        _make_account(email='dash-patient-1@portal.test')
        _make_account(email='dash-patient-2@portal.test', is_active=False)

        after = _data(client.get(URL))['patients']
        assert after['total'] == baseline['total'] + 2
        assert after['active'] == baseline['active'] + 1

    def test_new_last_30_days_excludes_old_accounts(self):
        client = _admin_client()
        baseline = _data(client.get(URL))['patients']

        # New (today)
        _make_account(email='dash-new-1@portal.test')
        # Old (35 days ago) — outside the window.
        _make_account(
            email='dash-old-1@portal.test',
            created_at=timezone.now() - timedelta(days=35),
        )

        after = _data(client.get(URL))['patients']
        assert after['new_last_30_days'] == baseline['new_last_30_days'] + 1


# ---------------------------------------------------------------------------
# 5. Activity counters
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestActivityCounters:

    def test_shared_30d_excludes_older_shares(self):
        client = _admin_client()
        baseline = _data(client.get(URL))['activity']

        account = _make_account(email='activity-1@portal.test')
        # In window
        _make_share(account=account, created_at=timezone.now())
        # Out of window
        _make_share(
            account=account,
            created_at=timezone.now() - timedelta(days=40),
        )
        after = _data(client.get(URL))['activity']
        assert (
            after['results_shared_last_30_days']
            == baseline['results_shared_last_30_days'] + 1
        )

    def test_downloads_count_uses_last_downloaded_at(self):
        client = _admin_client()
        baseline = _data(client.get(URL))['activity']

        account = _make_account(email='activity-2@portal.test')
        # Recent download
        _make_share(
            account=account,
            last_downloaded_at=timezone.now() - timedelta(days=2),
        )
        # Old download
        _make_share(
            account=account,
            last_downloaded_at=timezone.now() - timedelta(days=60),
        )
        # Never downloaded
        _make_share(account=account)

        after = _data(client.get(URL))['activity']
        assert (
            after['results_downloaded_last_30_days']
            == baseline['results_downloaded_last_30_days'] + 1
        )

    def test_emails_sent_30d_only_counts_sent_status(self):
        client = _admin_client()
        baseline = _data(client.get(URL))['activity']

        account = _make_account(email='activity-3@portal.test')
        # SENT in window
        _make_share(
            account=account,
            email_status='SENT',
            email_sent_at=timezone.now() - timedelta(days=1),
        )
        # FAILED — must not be counted as a successful email send.
        _make_share(
            account=account,
            email_status='FAILED',
            email_sent_at=timezone.now() - timedelta(days=1),
        )
        # SENT but old — outside the window.
        _make_share(
            account=account,
            email_status='SENT',
            email_sent_at=timezone.now() - timedelta(days=40),
        )

        after = _data(client.get(URL))['activity']
        assert (
            after['emails_sent_last_30_days']
            == baseline['emails_sent_last_30_days'] + 1
        )


# ---------------------------------------------------------------------------
# 6. Audit
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestAudit:

    def test_dashboard_view_writes_audit_row(self):
        admin = _make_admin()
        before = PlatformAdminAuditLog.objects.filter(
            actor=admin,
            action=PlatformAuditAction.PLATFORM_DASHBOARD_VIEWED,
        ).count()

        _admin_client(admin).get(URL)

        rows = list(PlatformAdminAuditLog.objects.filter(
            actor=admin,
            action=PlatformAuditAction.PLATFORM_DASHBOARD_VIEWED,
        ))
        assert len(rows) == before + 1
        assert rows[0].entity_type == 'Platform'
        assert rows[0].metadata == {'window_days': 30}

    def test_unauthorised_call_does_not_write_audit_row(self):
        before = PlatformAdminAuditLog.objects.filter(
            action=PlatformAuditAction.PLATFORM_DASHBOARD_VIEWED,
        ).count()
        _client().get(URL)  # 401
        after = PlatformAdminAuditLog.objects.filter(
            action=PlatformAuditAction.PLATFORM_DASHBOARD_VIEWED,
        ).count()
        assert before == after
