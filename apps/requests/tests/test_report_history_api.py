"""
Phase 3 — Lab-side ``GET /requests/{id}/report-history/`` endpoint.

The endpoint is the staff traceability surface that joins the
lab-internal ``AnalysisRequestReport`` version line with the
public-schema ``PatientSharedResult`` rows that referenced each
version. It is the lab-facing counterpart of the patient-portal
versions endpoint added in Phase 2 — and crucially, it shows EVERY
share event, including revoked / hidden ones, so the lab can see the
full lifecycle the patient view deliberately hides.

Invariants under test
---------------------
- lab versions appear regardless of share status (lab can see internal
  versions),
- patient-share cross-reference is keyed on
  ``report_version_number`` so each share lands under the correct
  version,
- supersession bookkeeping (``is_current_for_patient``) survives the
  serialisation,
- revoked / hidden shares still show up on the lab side (they only
  disappear from the patient view),
- ``channels_used`` aggregates distinct channels from patient-share
  rows,
- internal storage keys are never serialised,
- tenant isolation: a share row recorded against a different
  ``source_tenant_schema`` does not leak into this tenant's history,
- non-staff / unauthenticated callers are rejected.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from django.core.cache import cache
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.test import APIClient

from apps.catalog.models import (
    ExamCategory, ExamDefinition, ExamFamily, ExamTechnique,
    ResultStructure, SampleType,
)
from apps.patient_portal.models import (
    PatientSharedChannel, PatientSharedResult, PatientSharedResultFile,
    SharedResultStatus,
)
from apps.patient_portal.services import register_patient_account
from apps.patients.models import Patient
from apps.requests.models import RequestStatus, SourceType
from apps.requests.report_service import RequestReportService
from apps.requests.services import (
    AnalysisRequestItemService, AnalysisRequestService,
)
from apps.results.services import ResultVersionService


HISTORY_URL = '/api/v1/requests/{ar_id}/report-history/'
NOTIFY_URL = '/api/v1/requests/{ar_id}/notify-cytova/'
REOPEN_URL = '/api/v1/requests/{ar_id}/reopen-result/'
REVOKE_URL = '/api/v1/requests/{ar_id}/revoke-cytova-share/'


# ---------------------------------------------------------------------------
# Subscription + cache fixtures (mirror the share-lifecycle suite)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _usable_subscription(_test_tenant_schema, django_db_blocker):
    from apps.tenants.models import (
        Subscription, SubscriptionPlan, SubscriptionStatus, Tenant,
    )
    with django_db_blocker.unblock():
        with schema_context(get_public_schema_name()):
            plan, _ = SubscriptionPlan.objects.get_or_create(
                code='TEST_TRIAL',
                defaults={
                    'name': 'Test Trial', 'is_trial': True,
                    'trial_duration_days': 30, 'is_public': False,
                },
            )
            tenant = Tenant.objects.get(schema_name=_test_tenant_schema)
            Subscription.objects.get_or_create(
                tenant=tenant,
                status=SubscriptionStatus.TRIAL,
                defaults={'plan': plan},
            )
    yield


@pytest.fixture(autouse=True)
def _reset_throttle_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _stub_email_service(monkeypatch):
    """Notify-Cytova emails the patient on share. Stub the provider so
    these tests don't depend on SMTP."""
    from common.email.providers.base import EmailResult
    from apps.requests import notify_cytova_service as svc

    class _FakeService:
        def send_patient_shared_result_email(self, **_):
            return EmailResult(ok=True)

    monkeypatch.setattr(svc, 'get_email_service', lambda: _FakeService())


# ---------------------------------------------------------------------------
# Lab fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def lab_patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-HIST-001',
        first_name='History', last_name='Patient',
        date_of_birth=date(1985, 3, 14), gender='FEMALE',
        created_by=lab_admin,
    )


@pytest.fixture()
def exam_def():
    cat = ExamCategory.objects.create(name='Labs', display_order=1)
    fam = ExamFamily.objects.create(name='Hematology', display_order=1)
    tech = ExamTechnique.objects.create(name='Spectrophotometry')
    return ExamDefinition.objects.create(
        category=cat, family=fam, technique=tech,
        code='GLU', name='Glucose', sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.SINGLE_VALUE,
        unit='mg/dL', reference_range='70-100',
    )


def _build_finalized_request(
    *, lab_patient, exam_def, lab_admin, technician, biologist, make_request,
):
    """Walk a request all the way to VALIDATED + report v1."""
    ar = AnalysisRequestService.create(
        validated_data={
            'patient_id': lab_patient.id,
            'source_type': SourceType.DIRECT_PATIENT,
            'items': [{'exam_definition_id': exam_def.id}],
        },
        created_by=lab_admin, request=make_request(lab_admin),
        confirm_after=True,
    )
    req_tech = make_request(technician)
    req_bio = make_request(biologist)
    for item in ar.items.all():
        AnalysisRequestItemService.mark_collected(
            item=item, collected_by=technician, request=req_tech,
        )
    for item in ar.items.select_related('exam_definition').all():
        v = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req_tech,
            result_value='85',
            values=[{'value': '85', 'is_abnormal': False}],
            comments='Fasting confirmed.',
        )
        ResultVersionService.submit(version=v, submitted_by=technician, request=req_tech)
        v = item.result_versions.get(is_current=True)
        ResultVersionService.validate(
            version=v, validation_notes='OK',
            validated_by=biologist, request=req_bio,
        )
    ar.refresh_from_db()
    AnalysisRequestService.finalize_validation(
        analysis_request=ar, finalized_by=biologist, request=req_bio,
    )
    ar.refresh_from_db()
    RequestReportService.generate_or_get(
        analysis_request=ar, generated_by=biologist,
        request=make_request(biologist),
    )
    ar.refresh_from_db()
    return ar


@pytest.fixture()
def portal_account():
    return register_patient_account(
        email='hist-patient@portal.test',
        password='Strong-Pass-1234!',
        first_name='Ada', last_name='Lovelace',
        date_of_birth=date(1990, 5, 17), accept_terms=True,
    )


# ---------------------------------------------------------------------------
# HTTP fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def api_client():
    return APIClient(HTTP_HOST='testlab.localhost')


@pytest.fixture()
def admin_client(api_client, lab_admin):
    api_client.force_authenticate(user=lab_admin)
    return api_client


@pytest.fixture()
def technician_client(api_client, technician):
    api_client.force_authenticate(user=technician)
    return api_client


def _share_payload(account, *, force_share=False):
    profile = account.profile
    body = {
        'cytova_patient_id': profile.cytova_patient_id,
        'first_name': profile.first_name,
        'last_name': profile.last_name,
        'date_of_birth': profile.date_of_birth.isoformat(),
    }
    if force_share:
        body['force_share'] = True
    return body


def _share(admin_client, ar, account, *, force_share=False):
    resp = admin_client.post(
        NOTIFY_URL.format(ar_id=ar.id),
        data=_share_payload(account, force_share=force_share),
        format='json',
    )
    assert resp.status_code == 200, resp.content
    return resp.json()['data']['shared_result_id']


def _reopen(admin_client, ar):
    resp = admin_client.post(
        REOPEN_URL.format(ar_id=ar.id),
        data={'reason': 'Recalibrated analyzer; reissuing.'},
        format='json',
    )
    assert resp.status_code == 200, resp.content


def _regen_to_v2(ar, biologist, make_request):
    ar.refresh_from_db()
    return RequestReportService.regenerate(
        analysis_request=ar, generated_by=biologist,
        request=make_request(biologist),
    )


# ---------------------------------------------------------------------------
# 1. Unshared lab versions still surface — lab can see internal history
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestUnsharedVersionsVisibleToLab:

    def test_two_versions_neither_shared(
        self, admin_client, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _build_finalized_request(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician,
            biologist=biologist, make_request=make_request,
        )
        # Lab regenerates v2 internally without ever sharing.
        # ``regenerate`` requires VALIDATED; the request is currently
        # VALIDATED (no share happened yet) so this succeeds directly.
        v2 = _regen_to_v2(ar, biologist, make_request)
        assert v2.version_number == 2

        resp = admin_client.get(
            HISTORY_URL.format(ar_id=ar.id), format='json',
        )
        assert resp.status_code == 200, resp.content
        data = resp.json()['data']

        # Both lab versions present; v2 first (newest); v2 is current.
        versions = data['lab_versions']
        assert [v['version_number'] for v in versions] == [2, 1]
        assert versions[0]['is_current'] is True
        assert versions[1]['is_current'] is False

        # Critically: neither version has a patient-share entry. Lab
        # regen alone does NOT materialise the patient view.
        assert versions[0]['shared_with_patient'] == []
        assert versions[1]['shared_with_patient'] == []
        # No channels used because nothing was shared.
        assert data['channels_used'] == []
        # Issuance snapshot reflects "not yet issued".
        assert data['request_status'] == RequestStatus.VALIDATED
        assert data['issued_at'] is None
        assert data['issued_by_email'] is None


# ---------------------------------------------------------------------------
# 2. Sharing v1 cross-references under v1 only
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestShareCrossReference:

    def test_v1_share_appears_under_v1_only(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _build_finalized_request(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician,
            biologist=biologist, make_request=make_request,
        )
        v1_share_id = _share(admin_client, ar, portal_account)

        resp = admin_client.get(HISTORY_URL.format(ar_id=ar.id))
        assert resp.status_code == 200
        data = resp.json()['data']

        # Only one lab version exists at this point.
        versions = data['lab_versions']
        assert len(versions) == 1
        v1 = versions[0]
        assert v1['version_number'] == 1
        assert len(v1['shared_with_patient']) == 1
        share = v1['shared_with_patient'][0]
        assert share['shared_result_id'] == v1_share_id
        assert share['shared_channel'] == PatientSharedChannel.CYTOVA.value
        assert share['share_status'] == SharedResultStatus.ACTIVE
        assert share['is_current_for_patient'] is True
        # patient_account_id is the global id — already known to both
        # sides; the exposed identity is the lab's audit reference for
        # the share, not the patient's email.
        assert share['patient_account_id'] == str(portal_account.id)

        # Channels aggregate.
        assert data['channels_used'] == [PatientSharedChannel.CYTOVA.value]

        # Request transitioned to RESULT_ISSUED on first share.
        assert data['request_status'] == RequestStatus.RESULT_ISSUED
        assert data['issued_at'] is not None

    def test_v2_share_after_reopen_lands_on_v2_and_keeps_v1_history(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _build_finalized_request(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician,
            biologist=biologist, make_request=make_request,
        )
        v1_share_id = _share(admin_client, ar, portal_account)
        _reopen(admin_client, ar)
        _regen_to_v2(ar, biologist, make_request)
        v2_share_id = _share(
            admin_client, ar, portal_account, force_share=True,
        )

        resp = admin_client.get(HISTORY_URL.format(ar_id=ar.id))
        data = resp.json()['data']

        # Two versions, v2 first, v2 is current on the lab side.
        versions = data['lab_versions']
        assert [v['version_number'] for v in versions] == [2, 1]
        assert versions[0]['is_current'] is True   # v2
        assert versions[1]['is_current'] is False  # v1

        # v2's share is current for patient; v1's is no longer current
        # — but the row is still ACTIVE (supersession only flipped the
        # flag) and the lab can still see it.
        v2_shares = versions[0]['shared_with_patient']
        v1_shares = versions[1]['shared_with_patient']
        assert len(v2_shares) == 1
        assert v2_shares[0]['shared_result_id'] == v2_share_id
        assert v2_shares[0]['is_current_for_patient'] is True

        assert len(v1_shares) == 1
        assert v1_shares[0]['shared_result_id'] == v1_share_id
        assert v1_shares[0]['is_current_for_patient'] is False
        assert v1_shares[0]['share_status'] == SharedResultStatus.ACTIVE

        # Reopen lifecycle visible.
        assert data['reopened_at'] is not None
        assert data['reopen_reason'].startswith('Recalibrated')


# ---------------------------------------------------------------------------
# 3. Revoked shares stay visible on the lab side
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestRevokedSharesVisibleToLab:

    def test_revoked_share_appears_with_revoked_status(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _build_finalized_request(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician,
            biologist=biologist, make_request=make_request,
        )
        _share(admin_client, ar, portal_account)
        # Revoke. The dedicated endpoint flips status to REVOKED.
        revoke_resp = admin_client.post(
            REVOKE_URL.format(ar_id=ar.id), format='json',
        )
        assert revoke_resp.status_code == 200, revoke_resp.content

        resp = admin_client.get(HISTORY_URL.format(ar_id=ar.id))
        data = resp.json()['data']

        # The revoked share still shows up under v1 — the lab needs
        # to see "we shared, then revoked", which the patient view
        # deliberately hides.
        v1_shares = data['lab_versions'][0]['shared_with_patient']
        assert len(v1_shares) == 1
        assert v1_shares[0]['share_status'] == SharedResultStatus.REVOKED


# ---------------------------------------------------------------------------
# 4. No internal storage paths in the response
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestNoInternalLeaks:

    def test_response_omits_storage_keys(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _build_finalized_request(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician,
            biologist=biologist, make_request=make_request,
        )
        _share(admin_client, ar, portal_account)

        resp = admin_client.get(HISTORY_URL.format(ar_id=ar.id))
        flat = resp.content.decode()
        # Internal lab storage path
        assert 'pdf_file_key' not in flat
        # Internal patient-portal storage paths
        assert 'storage_key' not in flat
        assert 'patient_storage_key' not in flat
        # Internal cross-schema linkage keys
        assert 'source_tenant_schema' not in flat
        assert 'source_request_id' not in flat
        # File access tokens
        assert 'file_token' not in flat


# ---------------------------------------------------------------------------
# 5. Tenant isolation — cross-tenant share row excluded
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestTenantIsolation:

    def test_share_row_from_a_different_tenant_does_not_leak(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        """Even if a hypothetical other tenant's share row carried the
        same source_request_id (UUID collisions are practically
        impossible but the filter must defend by schema anyway), it
        must not be included in this tenant's report history."""
        ar = _build_finalized_request(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician,
            biologist=biologist, make_request=make_request,
        )
        # No real share — just seed a public-schema row pretending
        # another tenant shared a result with the same request id.
        with schema_context(get_public_schema_name()):
            other_share = PatientSharedResult.objects.create(
                patient_account=portal_account,
                source_type='DIRECT',
                source_name='Other Lab',
                request_reference='OTHER-REQ',
                request_date=date(2026, 4, 1),
                result_available_date=date(2026, 4, 2),
                source_tenant_schema='schema_OTHER_LAB',  # not ours
                source_request_id=ar.id,                  # same UUID
                report_version_number=1,
                report_generated_at=timezone.now(),
                shared_at=timezone.now(),
                shared_channel=PatientSharedChannel.CYTOVA,
                is_current_for_patient=True,
                status=SharedResultStatus.ACTIVE,
            )
            PatientSharedResultFile.objects.create(
                shared_result=other_share,
                file_token=f'tok_other_{other_share.id.hex[:16]}',
                filename='other.pdf',
            )

        resp = admin_client.get(HISTORY_URL.format(ar_id=ar.id))
        data = resp.json()['data']

        # No shares should be reported for our tenant — the other
        # tenant's row was filtered out by source_tenant_schema.
        flat = resp.content.decode()
        assert 'OTHER-REQ' not in flat
        assert str(other_share.id) not in flat
        for v in data['lab_versions']:
            assert v['shared_with_patient'] == []
        assert data['channels_used'] == []


# ---------------------------------------------------------------------------
# 6. Auth gate — non-staff and unauthenticated rejected
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestAuthGate:

    def test_unauthenticated_request_rejected(
        self, api_client, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _build_finalized_request(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician,
            biologist=biologist, make_request=make_request,
        )
        resp = api_client.get(HISTORY_URL.format(ar_id=ar.id))
        assert resp.status_code in (401, 403), resp.content

    def test_unknown_request_returns_404(self, admin_client):
        resp = admin_client.get(
            HISTORY_URL.format(ar_id=uuid.uuid4()),
        )
        assert resp.status_code == 404, resp.content

    def test_technician_can_read_history(
        self, technician_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        """The endpoint is gated at IsAnyStaff — every authenticated
        staff role must be able to read traceability, so the lab can
        debug share lifecycle without escalating to a biologist."""
        ar = _build_finalized_request(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician,
            biologist=biologist, make_request=make_request,
        )
        # No share — just verify a tech can read the empty history.
        resp = technician_client.get(HISTORY_URL.format(ar_id=ar.id))
        assert resp.status_code == 200, resp.content
