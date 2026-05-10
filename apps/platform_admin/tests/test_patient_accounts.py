"""
Platform-admin patient accounts — Phase 4 tests.

Pins the contract of the patient-account surface mounted at
``/api/v1/platform-admin/patients/``.

Tests cover:
  - Authn / authz: only an active platform admin token reaches the
    handler. Anonymous, lab-staff, and patient-portal callers are
    rejected at the auth layer (401) before any state changes.
  - Field shape: every documented field is present, NO sensitive
    data (password hash, profile PII, share content, tokens) is
    exposed.
  - Search / filter: ``?search=`` matches email,
    ``?is_active=``, ``?is_email_verified=`` produce the expected
    subset.
  - Detail endpoint: id-based retrieval works.
  - Lifecycle: deactivate / reactivate toggle ``is_active`` and
    write the right audit action.
  - Audit: every successful list / detail / lifecycle call appends
    one row; rejected calls write nothing.
"""
from __future__ import annotations

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context
from rest_framework.test import APIClient

from apps.patient_portal.models import (
    PatientAccount, PatientProfile, PatientSharedResult,
    SharedResultStatus,
)
from apps.patient_portal.tokens import PatientAccessToken
from apps.platform_admin.models import (
    PlatformAdminAuditLog, PlatformAdminRole, PlatformAdminUser,
    PlatformAuditAction,
)
from apps.platform_admin.tokens import PlatformAdminAccessToken
from apps.users.models import Role, StaffUser
from apps.authentication.tokens import CytovaAccessToken


PASSWORD = 'Strong-Pass-1234!'

BASE = '/api/v1/platform-admin/patients/'


def _action_url(account_id, slug: str) -> str:
    return f'{BASE}{account_id}/{slug}/'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client() -> APIClient:
    return APIClient(HTTP_HOST='core.localhost')


def _auth(client: APIClient, token: str) -> APIClient:
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return client


def _make_admin(role: str = PlatformAdminRole.SUPER_ADMIN) -> PlatformAdminUser:
    return PlatformAdminUser.objects.create_user(
        email=f'{role.lower()}-patients@cytova.io',
        password=PASSWORD, role=role,
    )


def _admin_client(admin: PlatformAdminUser | None = None) -> APIClient:
    admin = admin or _make_admin()
    return _auth(_client(), str(PlatformAdminAccessToken.for_user(admin)))


def _make_account(
    *, email: str,
    is_active: bool = True,
    email_verified: bool = False,
    with_profile: bool = False,
    cytova_patient_id: str | None = None,
) -> PatientAccount:
    account = PatientAccount.objects.create_user(
        email=email, password=PASSWORD,
    )
    fields = []
    if not is_active:
        account.is_active = False
        fields.append('is_active')
    if email_verified:
        account.email_verified_at = timezone.now()
        fields.append('email_verified_at')
    if fields:
        fields.append('updated_at')
        account.save(update_fields=fields)
    if with_profile:
        PatientProfile.objects.create(
            account=account,
            cytova_patient_id=cytova_patient_id or f'CV-TEST-{str(account.id)[:4]}',
            first_name='Patty', last_name='Patient',
            date_of_birth='1990-01-01',
        )
    return account


def _data(resp):
    body = resp.json()
    return body.get('data', body)


# ---------------------------------------------------------------------------
# 1. Authn / authz
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestAccessControl:

    def test_anonymous_request_rejected(self):
        resp = _client().get(BASE)
        assert resp.status_code == 401, resp.content

    def test_lab_staff_token_rejected(self):
        with schema_context('schema_testlab'):
            staff = StaffUser.objects.create_user(
                email='patients-staff@testlab.io', password=PASSWORD,
                first_name='Lab', last_name='Admin', role=Role.LAB_ADMIN,
            )
            staff_token = str(CytovaAccessToken.for_user(staff))
        resp = _auth(_client(), staff_token).get(BASE)
        assert resp.status_code == 401, resp.content

    def test_patient_portal_token_rejected(self):
        # Patients themselves must not have a back door into the
        # admin surface that lists every patient.
        account = _make_account(
            email='self-patient@portal.test',
            email_verified=True, with_profile=True,
        )
        token = str(
            PatientAccessToken.for_patient(account, profile=account.profile),
        )
        resp = _auth(_client(), token).get(BASE)
        assert resp.status_code == 401, resp.content

    def test_inactive_admin_rejected(self):
        admin = _make_admin()
        admin.is_active = False
        admin.save(update_fields=['is_active', 'updated_at'])
        token = str(PlatformAdminAccessToken.for_user(admin))
        resp = _auth(_client(), token).get(BASE)
        assert resp.status_code == 401, resp.content


# ---------------------------------------------------------------------------
# 2. List response shape (no sensitive data leak)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestListShape:

    def test_platform_admin_can_list_patients(self):
        _make_account(email='listed@portal.test')
        resp = _admin_client().get(BASE)
        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert 'data' in body and 'meta' in body and 'errors' in body
        assert isinstance(body['data'], list)
        assert any(r['email'] == 'listed@portal.test' for r in body['data'])

    def test_response_includes_documented_fields(self):
        _make_account(
            email='fields-check@portal.test',
            email_verified=True, with_profile=True,
            cytova_patient_id='CV-AAAA-BBBB',
        )
        resp = _admin_client().get(f'{BASE}?search=fields-check')
        rows = _data(resp)
        assert len(rows) == 1
        row = rows[0]

        # Pin the documented allow-list. No drift permitted.
        assert set(row.keys()) == {
            'id', 'email', 'is_active',
            'created_at', 'last_login',
            'is_email_verified', 'cytova_patient_id', 'results_count',
        }
        assert row['email'] == 'fields-check@portal.test'
        assert row['is_email_verified'] is True
        assert row['cytova_patient_id'] == 'CV-AAAA-BBBB'

    def test_response_does_not_leak_sensitive_fields(self):
        # Hard-coded blocklist of fields that MUST NEVER appear in
        # the platform-admin patient surface. If a future refactor
        # widens the serializer, this test fails loudly so the
        # privacy contract isn't quietly broken.
        _make_account(
            email='sensitive-check@portal.test',
            email_verified=True, with_profile=True,
        )
        resp = _admin_client().get(f'{BASE}?search=sensitive-check')
        row = _data(resp)[0]

        # Auth credentials
        assert 'password' not in row
        # PII from PatientProfile
        for key in ('first_name', 'last_name', 'date_of_birth', 'phone'):
            assert key not in row
        # Verification timestamp itself is not exposed — only the
        # boolean derived from it. Operators don't need to know the
        # exact verification moment for support.
        assert 'email_verified_at' not in row
        # Tokens / shares
        for key in ('outstanding_tokens', 'shared_results', 'consents'):
            assert key not in row

    def test_results_count_excludes_revoked_and_hidden(self):
        # ``results_count`` is the only aggregate we expose. It must
        # only count ACTIVE shared results so the metric doesn't
        # leak revocation behaviour (a count that drops after a
        # revoke would tell an operator something happened).
        account = _make_account(
            email='counts@portal.test', with_profile=True,
        )
        for status in (
            SharedResultStatus.ACTIVE,
            SharedResultStatus.ACTIVE,
            SharedResultStatus.REVOKED,
            SharedResultStatus.HIDDEN_BY_PATIENT,
        ):
            PatientSharedResult.objects.create(
                patient_account=account,
                source_type='DIRECT',
                source_name='Test Lab',
                status=status,
            )
        resp = _admin_client().get(f'{BASE}?search=counts')
        row = _data(resp)[0]
        assert row['results_count'] == 2

    def test_account_without_profile_returns_null_cytova_id(self):
        _make_account(email='no-profile@portal.test', with_profile=False)
        resp = _admin_client().get(f'{BASE}?search=no-profile')
        row = _data(resp)[0]
        assert row['cytova_patient_id'] is None


# ---------------------------------------------------------------------------
# 3. Search + filtering
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestSearchAndFilter:

    def test_search_matches_email(self):
        _make_account(email='alpha@portal.test')
        _make_account(email='beta@portal.test')

        resp = _admin_client().get(f'{BASE}?search=alpha')
        emails = {r['email'] for r in _data(resp)}
        assert emails == {'alpha@portal.test'}

    def test_filter_is_active_false_returns_only_inactive(self):
        _make_account(email='active@portal.test', is_active=True)
        _make_account(email='inactive@portal.test', is_active=False)

        resp = _admin_client().get(f'{BASE}?is_active=false')
        emails = {r['email'] for r in _data(resp)}
        assert 'inactive@portal.test' in emails
        assert 'active@portal.test' not in emails

    def test_filter_is_email_verified(self):
        _make_account(email='verified@portal.test', email_verified=True)
        _make_account(email='unverified@portal.test', email_verified=False)

        client = _admin_client()
        resp_v = client.get(f'{BASE}?is_email_verified=true')
        verified_emails = {r['email'] for r in _data(resp_v)}
        assert 'verified@portal.test' in verified_emails
        assert 'unverified@portal.test' not in verified_emails

        resp_uv = client.get(f'{BASE}?is_email_verified=false')
        unverified_emails = {r['email'] for r in _data(resp_uv)}
        assert 'unverified@portal.test' in unverified_emails
        assert 'verified@portal.test' not in unverified_emails


# ---------------------------------------------------------------------------
# 4. Detail
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestDetail:

    def test_detail_returns_same_shape_as_list_row(self):
        account = _make_account(
            email='detail@portal.test',
            email_verified=True, with_profile=True,
        )
        resp = _admin_client().get(f'{BASE}{account.id}/')
        assert resp.status_code == 200, resp.content
        body = _data(resp)
        assert set(body.keys()) == {
            'id', 'email', 'is_active',
            'created_at', 'last_login',
            'is_email_verified', 'cytova_patient_id', 'results_count',
        }
        assert body['id'] == str(account.id)
        assert body['email'] == 'detail@portal.test'

    def test_detail_unknown_id_returns_404(self):
        resp = _admin_client().get(
            f'{BASE}00000000-0000-0000-0000-000000000000/',
        )
        assert resp.status_code == 404, resp.content


# ---------------------------------------------------------------------------
# 5. Deactivate / Reactivate
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestDeactivateAction:

    def test_deactivate_flips_is_active_false(self):
        account = _make_account(email='deact-1@portal.test')
        assert account.is_active is True
        resp = _admin_client().post(_action_url(account.id, 'deactivate'))
        assert resp.status_code == 200, resp.content

        account.refresh_from_db()
        assert account.is_active is False
        assert _data(resp)['is_active'] is False

    def test_deactivate_is_idempotent(self):
        account = _make_account(email='deact-2@portal.test', is_active=False)
        resp = _admin_client().post(_action_url(account.id, 'deactivate'))
        assert resp.status_code == 200
        account.refresh_from_db()
        assert account.is_active is False

    def test_deactivate_writes_audit_row(self):
        admin = _make_admin()
        account = _make_account(email='deact-3@portal.test')

        _admin_client(admin).post(_action_url(account.id, 'deactivate'))
        rows = list(PlatformAdminAuditLog.objects.filter(
            actor=admin,
            action=PlatformAuditAction.PLATFORM_PATIENT_DEACTIVATED,
            entity_id=account.id,
        ))
        assert len(rows) == 1
        meta = rows[0].metadata
        assert meta['before']['is_active'] is True
        assert meta['after']['is_active'] is False

    def test_lab_staff_cannot_deactivate(self):
        account = _make_account(email='deact-4@portal.test')
        with schema_context('schema_testlab'):
            staff = StaffUser.objects.create_user(
                email='deact-staff@testlab.io', password=PASSWORD,
                first_name='Lab', last_name='Admin', role=Role.LAB_ADMIN,
            )
            staff_token = str(CytovaAccessToken.for_user(staff))
        resp = _auth(_client(), staff_token).post(
            _action_url(account.id, 'deactivate'),
        )
        assert resp.status_code == 401
        account.refresh_from_db()
        assert account.is_active is True

    def test_anonymous_cannot_deactivate(self):
        account = _make_account(email='deact-5@portal.test')
        before = PlatformAdminAuditLog.objects.filter(
            action=PlatformAuditAction.PLATFORM_PATIENT_DEACTIVATED,
        ).count()
        resp = _client().post(_action_url(account.id, 'deactivate'))
        assert resp.status_code == 401
        account.refresh_from_db()
        assert account.is_active is True
        # Audit only fires on the success path. A 401 must not leave
        # a trail that suggests a deactivation happened.
        after = PlatformAdminAuditLog.objects.filter(
            action=PlatformAuditAction.PLATFORM_PATIENT_DEACTIVATED,
        ).count()
        assert before == after


@pytest.mark.django_db
class TestReactivateAction:

    def test_reactivate_restores_access(self):
        account = _make_account(email='react-1@portal.test', is_active=False)
        resp = _admin_client().post(_action_url(account.id, 'reactivate'))
        assert resp.status_code == 200, resp.content
        account.refresh_from_db()
        assert account.is_active is True
        assert _data(resp)['is_active'] is True

    def test_reactivate_is_idempotent(self):
        account = _make_account(email='react-2@portal.test')
        resp = _admin_client().post(_action_url(account.id, 'reactivate'))
        assert resp.status_code == 200

    def test_reactivate_writes_audit_row(self):
        admin = _make_admin()
        account = _make_account(email='react-3@portal.test', is_active=False)
        _admin_client(admin).post(_action_url(account.id, 'reactivate'))
        rows = list(PlatformAdminAuditLog.objects.filter(
            actor=admin,
            action=PlatformAuditAction.PLATFORM_PATIENT_REACTIVATED,
            entity_id=account.id,
        ))
        assert len(rows) == 1
        meta = rows[0].metadata
        assert meta['before']['is_active'] is False
        assert meta['after']['is_active'] is True


# ---------------------------------------------------------------------------
# 6. Audit on list + detail
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestAuditTrail:

    def test_list_writes_audit_row_with_query_params(self):
        admin = _make_admin()
        client = _admin_client(admin)
        client.get(f'{BASE}?is_active=true')

        rows = list(PlatformAdminAuditLog.objects.filter(
            actor=admin,
            action=PlatformAuditAction.PLATFORM_PATIENT_LIST_VIEWED,
        ))
        assert len(rows) == 1
        assert rows[0].entity_type == 'PatientAccount'
        assert rows[0].metadata['query_params']['is_active'] == ['true']

    def test_detail_writes_audit_row_with_entity_id(self):
        admin = _make_admin()
        account = _make_account(email='audit-detail@portal.test')

        _admin_client(admin).get(f'{BASE}{account.id}/')
        rows = list(PlatformAdminAuditLog.objects.filter(
            actor=admin,
            action=PlatformAuditAction.PLATFORM_PATIENT_DETAIL_VIEWED,
            entity_id=account.id,
        ))
        assert len(rows) == 1
        assert rows[0].entity_type == 'PatientAccount'

    def test_unknown_id_does_not_write_detail_audit_row(self):
        before = PlatformAdminAuditLog.objects.filter(
            action=PlatformAuditAction.PLATFORM_PATIENT_DETAIL_VIEWED,
        ).count()
        _admin_client().get(
            f'{BASE}00000000-0000-0000-0000-000000000000/',
        )
        after = PlatformAdminAuditLog.objects.filter(
            action=PlatformAuditAction.PLATFORM_PATIENT_DETAIL_VIEWED,
        ).count()
        assert before == after
