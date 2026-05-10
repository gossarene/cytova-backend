"""
Platform-admin tenant listing — Phase 2 tests.

Pins the contract of the new read-only ``/api/v1/platform-admin/tenants/``
surface:

  - Authn / authz: only an active platform admin token reaches the
    handler. Anonymous, lab-staff, and patient-portal callers are
    rejected at the auth layer (401) before the queryset runs.
  - Field shape: every documented field is present on every row. The
    serializer never queries tenant-schema tables — only public-schema
    metadata (Tenant + Domain + Subscription).
  - Filtering / ordering: ``?search=``, ``?is_active=``,
    ``?subscription_status=``, and ``?ordering=`` produce the expected
    result subset / order.
  - Detail endpoint: id-based retrieval returns the same shape for a
    single row and 404s for unknown ids.
  - Audit: every successful list/detail call appends one
    ``PLATFORM_TENANT_LIST_VIEWED`` /
    ``PLATFORM_TENANT_DETAIL_VIEWED`` row, with the actor and entity
    captured. Audit failures on rejected calls would be a regression.

Fixture strategy
----------------
The session-scoped ``_test_tenant_schema`` already creates the
``testlab`` tenant with a real schema. Additional tenants in this
file set ``auto_create_schema = False`` on the instance so they
don't trigger a per-test CREATE SCHEMA + migrate (which is not
transactional and would slow the suite). The platform-admin surface
never reads tenant-schema tables — only public-schema metadata —
so missing schemas do not break any code path under test.
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
    PlatformAdminAuditLog,
    PlatformAdminRole,
    PlatformAdminUser,
    PlatformAuditAction,
)
from apps.platform_admin.tokens import PlatformAdminAccessToken
from apps.tenants.models import (
    Domain,
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
    Tenant,
)
from apps.users.models import Role, StaffUser
from apps.authentication.tokens import CytovaAccessToken


LIST_URL = '/api/v1/platform-admin/tenants/'

PASSWORD = 'Strong-Pass-1234!'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client() -> APIClient:
    """APIClient targeting the public host so the public-schema URL conf
    serves these routes (mirrors the auth-foundation tests)."""
    return APIClient(HTTP_HOST='core.localhost')


def _auth(client: APIClient, token: str) -> APIClient:
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return client


def _make_admin(role: str = PlatformAdminRole.SUPER_ADMIN) -> PlatformAdminUser:
    return PlatformAdminUser.objects.create_user(
        email=f'{role.lower()}@cytova.io',
        password=PASSWORD,
        role=role,
    )


def _admin_client(admin: PlatformAdminUser | None = None) -> APIClient:
    admin = admin or _make_admin()
    token = str(PlatformAdminAccessToken.for_user(admin))
    return _auth(_client(), token)


def _make_tenant(
    *, name: str, subdomain: str, schema_name: str | None = None,
    is_active: bool = True, domain: str | None = None,
) -> Tenant:
    """Create a Tenant row WITHOUT creating its schema.

    The platform-admin tenant list does not query tenant-schema
    tables, so the schema is unnecessary for these tests. Skipping
    CREATE SCHEMA keeps the suite fast and lets every test run
    inside a single transaction.
    """
    tenant = Tenant(
        name=name, subdomain=subdomain,
        schema_name=schema_name or f'schema_{subdomain.replace("-", "_")}',
        is_active=is_active,
    )
    tenant.auto_create_schema = False
    tenant.save()
    if domain:
        Domain.objects.create(domain=domain, tenant=tenant, is_primary=True)
    return tenant


def _make_trial_plan() -> SubscriptionPlan:
    return SubscriptionPlan.objects.create(
        code='TEST_TRIAL', name='Test Trial',
        is_trial=True, trial_duration_days=14,
    )


def _attach_subscription(
    tenant: Tenant, plan: SubscriptionPlan,
    *, status: str = SubscriptionStatus.TRIAL,
    trial_end_date=None,
) -> Subscription:
    return Subscription.objects.create(
        tenant=tenant, plan=plan, status=status,
        trial_end_date=trial_end_date,
    )


def _data(resp):
    """Unwrap the standard Cytova envelope. Paginated responses come
    pre-wrapped by the cursor paginator; single-resource responses are
    wrapped by ``CytovaJSONRenderer``."""
    body = resp.json()
    return body.get('data', body)


# ---------------------------------------------------------------------------
# 1. Authn / authz
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestAccessControl:

    def test_anonymous_request_rejected(self):
        # No token → 401. Body is the Cytova error envelope.
        resp = _client().get(LIST_URL)
        assert resp.status_code == 401, resp.content

    def test_lab_staff_token_rejected(self):
        # Real per-tenant StaffUser token. Even though the JWT is
        # signed with the same key, ``user_type`` is missing —
        # PlatformAdminJWTAuthentication refuses on that check
        # before any DB lookup.
        with schema_context('schema_testlab'):
            staff = StaffUser.objects.create_user(
                email='staff@testlab.io', password=PASSWORD,
                first_name='Lab', last_name='Admin',
                role=Role.LAB_ADMIN,
            )
            staff_token = str(CytovaAccessToken.for_user(staff))
        resp = _auth(_client(), staff_token).get(LIST_URL)
        assert resp.status_code == 401, resp.content

    def test_patient_portal_token_rejected(self):
        # Patient tokens carry ``user_type='PATIENT'`` — also rejected
        # by the platform-admin auth class.
        account = register_patient_account(
            email='patient@portal.test', password=PASSWORD,
            first_name='Pat', last_name='Test',
            date_of_birth='1990-05-17', accept_terms=True,
        )
        account.email_verified_at = timezone.now()
        account.save(update_fields=['email_verified_at'])
        patient_token = str(
            PatientAccessToken.for_patient(account, profile=account.profile)
        )
        resp = _auth(_client(), patient_token).get(LIST_URL)
        assert resp.status_code == 401, resp.content

    def test_inactive_admin_rejected(self):
        admin = _make_admin()
        admin.is_active = False
        admin.save(update_fields=['is_active', 'updated_at'])
        token = str(PlatformAdminAccessToken.for_user(admin))
        resp = _auth(_client(), token).get(LIST_URL)
        # The auth class refuses before the permission class runs.
        assert resp.status_code == 401, resp.content


# ---------------------------------------------------------------------------
# 2. List response shape
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestListShape:

    def test_platform_admin_can_list_tenants(self):
        resp = _admin_client().get(LIST_URL)
        assert resp.status_code == 200, resp.content
        body = resp.json()
        # Cursor paginator wraps in {data, meta, errors}.
        assert 'data' in body and 'meta' in body and 'errors' in body
        assert isinstance(body['data'], list)

    def test_response_includes_documented_fields(self):
        plan = _make_trial_plan()
        trial_end = timezone.now() + timedelta(days=14)
        tenant = _make_tenant(
            name='Lab Alpha', subdomain='lab-alpha',
            domain='lab-alpha.cytova.io',
        )
        _attach_subscription(
            tenant, plan, status=SubscriptionStatus.TRIAL,
            trial_end_date=trial_end,
        )

        resp = _admin_client().get(f'{LIST_URL}?search=lab-alpha')
        assert resp.status_code == 200
        rows = _data(resp)
        assert len(rows) == 1
        row = rows[0]

        # Pin the documented field set so a downstream renamer can't
        # silently drop one. ``slug`` aliases ``subdomain`` per spec.
        assert set(row.keys()) == {
            'id', 'name', 'slug', 'domain_url',
            'is_active', 'created_at',
            'trial_end_date', 'subscription_status',
        }
        assert row['name'] == 'Lab Alpha'
        assert row['slug'] == 'lab-alpha'
        assert row['domain_url'] == 'https://lab-alpha.cytova.io'
        assert row['is_active'] is True
        assert row['subscription_status'] == SubscriptionStatus.TRIAL
        assert row['trial_end_date'] is not None

    def test_tenant_without_subscription_returns_null_status(self):
        _make_tenant(
            name='No-Sub Lab', subdomain='no-sub-lab',
            domain='no-sub-lab.cytova.io',
        )
        resp = _admin_client().get(f'{LIST_URL}?search=no-sub-lab')
        rows = _data(resp)
        assert len(rows) == 1
        assert rows[0]['subscription_status'] is None
        assert rows[0]['trial_end_date'] is None

    def test_tenant_without_domain_returns_null_url(self):
        _make_tenant(name='Domainless Lab', subdomain='domainless-lab')
        resp = _admin_client().get(f'{LIST_URL}?search=domainless-lab')
        rows = _data(resp)
        assert len(rows) == 1
        assert rows[0]['domain_url'] is None


# ---------------------------------------------------------------------------
# 3. Filtering
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestFiltering:

    def test_search_matches_name_and_subdomain(self):
        _make_tenant(name='Laboratoire Lyon', subdomain='lab-lyon')
        _make_tenant(name='Marseille Lab', subdomain='msl')

        client = _admin_client()

        # ``search`` matches ``name``
        resp = client.get(f'{LIST_URL}?search=Lyon')
        rows = _data(resp)
        assert {r['slug'] for r in rows} == {'lab-lyon'}

        # ``search`` matches ``subdomain`` too
        resp = client.get(f'{LIST_URL}?search=msl')
        rows = _data(resp)
        assert {r['slug'] for r in rows} == {'msl'}

    def test_filter_is_active_false_returns_only_inactive(self):
        _make_tenant(name='Active Lab', subdomain='active-lab')
        _make_tenant(
            name='Suspended Lab', subdomain='suspended-lab', is_active=False,
        )
        resp = _admin_client().get(f'{LIST_URL}?is_active=false')
        slugs = {r['slug'] for r in _data(resp)}
        # The session-scoped testlab tenant is active → not in this set.
        assert 'suspended-lab' in slugs
        assert 'active-lab' not in slugs

    def test_filter_subscription_status_uses_latest_subscription(self):
        plan = _make_trial_plan()
        # A tenant whose latest subscription is ACTIVE should NOT
        # match a TRIAL filter even though it once had a trial.
        graduated = _make_tenant(name='Graduated Lab', subdomain='graduated-lab')
        old_trial = _attach_subscription(
            graduated, plan, status=SubscriptionStatus.TRIAL,
            trial_end_date=timezone.now() - timedelta(days=1),
        )
        # Backdate the trial via a queryset .update so the ACTIVE
        # subscription wins the "latest by created_at" tiebreak.
        # ``auto_now_add=True`` blocks ``.save()`` for this field.
        Subscription.objects.filter(pk=old_trial.pk).update(
            created_at=timezone.now() - timedelta(days=30),
        )
        _attach_subscription(
            graduated, plan, status=SubscriptionStatus.ACTIVE,
        )

        # A separate tenant whose latest subscription is TRIAL.
        trialing = _make_tenant(name='Trial Lab', subdomain='trial-lab')
        _attach_subscription(
            trialing, plan, status=SubscriptionStatus.TRIAL,
            trial_end_date=timezone.now() + timedelta(days=14),
        )

        resp = _admin_client().get(f'{LIST_URL}?subscription_status=TRIAL')
        slugs = {r['slug'] for r in _data(resp)}
        assert 'trial-lab' in slugs
        assert 'graduated-lab' not in slugs


# ---------------------------------------------------------------------------
# 4. Ordering
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestOrdering:

    def test_default_ordering_is_created_at_desc(self):
        # The newest tenant should appear before older ones in the
        # default response. Force the timestamps with ``update`` so
        # we don't rely on sub-second create-call ordering.
        old = _make_tenant(name='Old Lab', subdomain='old-lab')
        new = _make_tenant(name='New Lab', subdomain='new-lab')
        Tenant.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=10),
        )
        Tenant.objects.filter(pk=new.pk).update(
            created_at=timezone.now() - timedelta(seconds=1),
        )

        resp = _admin_client().get(f'{LIST_URL}?search=-lab')
        rows = _data(resp)
        # Filter to just our two so the testlab fixture doesn't
        # interfere with the index assertions.
        slugs = [r['slug'] for r in rows if r['slug'] in {'old-lab', 'new-lab'}]
        assert slugs == ['new-lab', 'old-lab']

    def test_ordering_by_name_ascending(self):
        _make_tenant(name='Zeta Lab', subdomain='zeta-lab')
        _make_tenant(name='Alpha Lab', subdomain='alpha-lab')
        resp = _admin_client().get(f'{LIST_URL}?ordering=name&search=Lab')
        rows = _data(resp)
        names = [r['name'] for r in rows if r['name'] in {'Alpha Lab', 'Zeta Lab'}]
        assert names == ['Alpha Lab', 'Zeta Lab']


# ---------------------------------------------------------------------------
# 5. Detail endpoint
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestDetail:

    def test_detail_returns_same_shape_as_list_row(self):
        plan = _make_trial_plan()
        tenant = _make_tenant(
            name='Detail Lab', subdomain='detail-lab',
            domain='detail-lab.cytova.io',
        )
        _attach_subscription(
            tenant, plan, status=SubscriptionStatus.TRIAL,
            trial_end_date=timezone.now() + timedelta(days=7),
        )

        resp = _admin_client().get(f'{LIST_URL}{tenant.id}/')
        assert resp.status_code == 200, resp.content
        body = _data(resp)
        # CytovaJSONRenderer wraps in ``data``; the unwrapped payload
        # is the serialized tenant dict.
        assert set(body.keys()) == {
            'id', 'name', 'slug', 'domain_url',
            'is_active', 'created_at',
            'trial_end_date', 'subscription_status',
        }
        assert body['id'] == str(tenant.id)
        assert body['slug'] == 'detail-lab'
        assert body['subscription_status'] == SubscriptionStatus.TRIAL

    def test_detail_unknown_id_returns_404(self):
        # An id that doesn't match any tenant 404s, with no audit
        # row for the rejected call (asserted in the audit suite).
        resp = _admin_client().get(
            f'{LIST_URL}00000000-0000-0000-0000-000000000000/',
        )
        assert resp.status_code == 404, resp.content


# ---------------------------------------------------------------------------
# 6. Audit
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestAuditTrail:

    def test_list_writes_audit_row_with_query_params(self):
        admin = _make_admin()
        client = _admin_client(admin)
        client.get(f'{LIST_URL}?is_active=true')

        rows = list(PlatformAdminAuditLog.objects.filter(
            actor=admin,
            action=PlatformAuditAction.PLATFORM_TENANT_LIST_VIEWED,
        ))
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_email == admin.email
        assert row.entity_type == 'Tenant'
        # Query-param snapshot lets a SIEM see what slice the admin
        # asked for. Stored as ``{key: [values]}``.
        assert row.metadata['query_params']['is_active'] == ['true']

    def test_detail_writes_audit_row_with_entity_id(self):
        admin = _make_admin()
        tenant = _make_tenant(name='Audited Lab', subdomain='audited-lab')

        _admin_client(admin).get(f'{LIST_URL}{tenant.id}/')

        rows = list(PlatformAdminAuditLog.objects.filter(
            actor=admin,
            action=PlatformAuditAction.PLATFORM_TENANT_DETAIL_VIEWED,
        ))
        assert len(rows) == 1
        assert rows[0].entity_id == tenant.id
        assert rows[0].entity_type == 'Tenant'

    def test_rejected_request_does_not_write_audit_row(self):
        # Anonymous → 401 BEFORE the view runs → no audit row.
        # Pin this so a future refactor that moves the audit call
        # into a middleware (or before super().list) doesn't
        # accidentally start logging unauthorised attempts as if
        # they had read tenant data.
        before = PlatformAdminAuditLog.objects.filter(
            action=PlatformAuditAction.PLATFORM_TENANT_LIST_VIEWED,
        ).count()
        resp = _client().get(LIST_URL)
        assert resp.status_code == 401
        after = PlatformAdminAuditLog.objects.filter(
            action=PlatformAuditAction.PLATFORM_TENANT_LIST_VIEWED,
        ).count()
        assert before == after

    def test_unknown_id_does_not_write_detail_audit_row(self):
        # 404 → ``self.get_object()`` raises before the audit call,
        # so the audit log doesn't claim someone viewed something
        # that doesn't exist.
        before = PlatformAdminAuditLog.objects.filter(
            action=PlatformAuditAction.PLATFORM_TENANT_DETAIL_VIEWED,
        ).count()
        _admin_client().get(
            f'{LIST_URL}00000000-0000-0000-0000-000000000000/',
        )
        after = PlatformAdminAuditLog.objects.filter(
            action=PlatformAuditAction.PLATFORM_TENANT_DETAIL_VIEWED,
        ).count()
        assert before == after
