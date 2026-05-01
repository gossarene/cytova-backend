"""
Phase 1 — Patient-portal version supersession on Notify Cytova.

The patient must only see report versions that the lab actually shared
with them. The lab can regenerate report versions internally any number
of times — the patient view only updates when the lab calls
``notify-cytova/`` (the only Phase-1 channel). These tests pin that
contract end-to-end against the public-schema ``PatientSharedResult``
table that the (out-of-scope) patient versions API will read in
Phase 2.

Scope
-----
- Lab-side ``AnalysisRequestReport`` versioning is exercised end-to-end
  but is NOT what's under test here — it pre-existed and has its own
  suite (``test_report_versioning.py``). This file only asserts the
  patient-facing supersession invariants.
- All tests run in tenant context via the ambient autouse fixture; the
  patient portal tables live in the public schema and are accessed
  directly through the ORM (django-tenants routes them by table).
"""
from __future__ import annotations

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
    PatientPortalAuditAction, PatientPortalAuditLog,
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


NOTIFY_URL = '/api/v1/requests/{ar_id}/notify-cytova/'
REOPEN_URL = '/api/v1/requests/{ar_id}/reopen-result/'


# ---------------------------------------------------------------------------
# Subscription + cache fixtures (mirrors the share-lifecycle test setup)
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
    """Notify-Cytova always tries to email the patient. Stub the
    provider so tests don't depend on SMTP and don't pollute logs.
    The version-supersession invariants under test are independent of
    delivery outcome — the stub returns ok=True for predictability."""
    from common.email.providers.base import EmailResult
    from apps.requests import notify_cytova_service as svc

    class _FakeService:
        def send_patient_shared_result_email(self, **_):
            return EmailResult(ok=True)

    monkeypatch.setattr(svc, 'get_email_service', lambda: _FakeService())


# ---------------------------------------------------------------------------
# Lab fixtures: a finalized request with a generated v1 report
# ---------------------------------------------------------------------------

@pytest.fixture()
def lab_patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-VER-001',
        first_name='Versioning', last_name='Patient',
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
    """Walk a request all the way through to RESULT_ISSUED-eligible state
    (VALIDATED + report v1). Mirrors the helper used by the share-
    lifecycle suite; duplicated here to keep this file self-contained
    rather than importing private helpers across modules."""
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


# ---------------------------------------------------------------------------
# Patient fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def portal_account():
    return register_patient_account(
        email='ver-patient@portal.test',
        password='Strong-Pass-1234!',
        first_name='Ada', last_name='Lovelace',
        date_of_birth=date(1990, 5, 17), accept_terms=True,
    )


@pytest.fixture()
def other_portal_account():
    return register_patient_account(
        email='ver-other@portal.test',
        password='Strong-Pass-1234!',
        first_name='Grace', last_name='Hopper',
        date_of_birth=date(1992, 12, 9), accept_terms=True,
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


def _payload(account, *, force_share=False):
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


def _share_v1(admin_client, ar, account):
    resp = admin_client.post(
        NOTIFY_URL.format(ar_id=ar.id), data=_payload(account),
        format='json',
    )
    assert resp.status_code == 200, resp.content
    return resp.json()['data']


def _reopen(admin_client, ar):
    resp = admin_client.post(
        REOPEN_URL.format(ar_id=ar.id),
        data={'reason': 'Recalibrated analyzer; reissuing.'},
        format='json',
    )
    assert resp.status_code == 200, resp.content


def _regenerate_to_v2(ar, biologist, make_request):
    ar.refresh_from_db()
    return RequestReportService.regenerate(
        analysis_request=ar, generated_by=biologist,
        request=make_request(biologist),
    )


def _share_again(admin_client, ar, account):
    """Privileged re-share — the view's one-shot guard requires
    ``force_share=True`` for LAB_ADMIN/BIOLOGIST."""
    resp = admin_client.post(
        NOTIFY_URL.format(ar_id=ar.id),
        data=_payload(account, force_share=True),
        format='json',
    )
    assert resp.status_code == 200, resp.content
    return resp.json()['data']


# ---------------------------------------------------------------------------
# 1. v1 share → patient row reflects v1 with full version metadata
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestShareV1CreatesCurrentPatientVersion:

    def test_v1_share_stamps_version_metadata(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _build_finalized_request(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician,
            biologist=biologist, make_request=make_request,
        )
        before = timezone.now()
        body = _share_v1(admin_client, ar, portal_account)

        shared = PatientSharedResult.objects.get(pk=body['shared_result_id'])
        assert shared.report_version_number == 1
        assert shared.report_generated_at is not None
        assert shared.shared_at is not None
        assert shared.shared_at >= before
        assert shared.shared_channel == PatientSharedChannel.CYTOVA
        assert shared.is_current_for_patient is True
        assert shared.status == SharedResultStatus.ACTIVE


# ---------------------------------------------------------------------------
# 2. Internal regenerate (no share) must NOT touch the patient view
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestInternalRegenerateLeavesPortalUntouched:

    def test_regenerate_v2_without_share_keeps_v1_current_for_patient(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _build_finalized_request(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician,
            biologist=biologist, make_request=make_request,
        )
        body = _share_v1(admin_client, ar, portal_account)
        v1_shared_id = body['shared_result_id']

        # Reopen → request goes back to VALIDATED → regenerate v2.
        _reopen(admin_client, ar)
        new_report = _regenerate_to_v2(ar, biologist, make_request)
        assert new_report.version_number == 2

        # Patient portal: still exactly the v1 row, still current.
        rows = list(
            PatientSharedResult.objects
            .filter(patient_account=portal_account)
            .order_by('created_at')
        )
        assert len(rows) == 1
        only = rows[0]
        assert str(only.id) == v1_shared_id
        assert only.report_version_number == 1
        assert only.is_current_for_patient is True


# ---------------------------------------------------------------------------
# 3. Sharing v2 supersedes v1 for this patient
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestShareV2Supersedes:

    def _share_v1_then_v2(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _build_finalized_request(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician,
            biologist=biologist, make_request=make_request,
        )
        v1_body = _share_v1(admin_client, ar, portal_account)
        _reopen(admin_client, ar)
        _regenerate_to_v2(ar, biologist, make_request)
        v2_body = _share_again(admin_client, ar, portal_account)
        return ar, v1_body, v2_body

    def test_v2_share_marks_v1_not_current_and_v2_current(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        _, v1_body, v2_body = self._share_v1_then_v2(
            admin_client, portal_account, lab_patient, exam_def,
            lab_admin, technician, biologist, make_request,
        )

        v1 = PatientSharedResult.objects.get(pk=v1_body['shared_result_id'])
        v2 = PatientSharedResult.objects.get(pk=v2_body['shared_result_id'])
        assert v1.id != v2.id

        assert v1.report_version_number == 1
        assert v1.is_current_for_patient is False
        assert v1.shared_channel == PatientSharedChannel.CYTOVA
        # v1 row itself is preserved — supersession only flips the flag,
        # never deletes the historical snapshot.
        assert v1.status == SharedResultStatus.ACTIVE

        assert v2.report_version_number == 2
        assert v2.is_current_for_patient is True
        assert v2.shared_channel == PatientSharedChannel.CYTOVA

        # Exactly one row marked current_for_patient for the (patient, request).
        currents = PatientSharedResult.objects.filter(
            patient_account=portal_account,
            source_request_id=v2.source_request_id,
            is_current_for_patient=True,
        )
        assert currents.count() == 1
        assert currents.first().id == v2.id

    def test_each_shared_version_owns_its_own_pdf_copy(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        _, v1_body, v2_body = self._share_v1_then_v2(
            admin_client, portal_account, lab_patient, exam_def,
            lab_admin, technician, biologist, make_request,
        )
        v1_files = list(PatientSharedResultFile.objects.filter(
            shared_result_id=v1_body['shared_result_id']
        ))
        v2_files = list(PatientSharedResultFile.objects.filter(
            shared_result_id=v2_body['shared_result_id']
        ))
        assert len(v1_files) == 1
        assert len(v2_files) == 1
        v1f, v2f = v1_files[0], v2_files[0]

        # Distinct opaque tokens — patient-side download identity is
        # never reused across versions.
        assert v1f.file_token != v2f.file_token

        # Each version's effective storage key is its own. The
        # ``effective_storage_key`` property prefers the patient-owned
        # copy when present and falls back to the lab-side snapshot;
        # whichever path the test environment exercises, the two
        # versions must not collide.
        assert v1f.effective_storage_key
        assert v2f.effective_storage_key
        assert v1f.effective_storage_key != v2f.effective_storage_key

        # v1's row was not mutated by the v2 share — the historical
        # filename/token/storage-key remain exactly as they were.
        v1f_refetched = PatientSharedResultFile.objects.get(pk=v1f.pk)
        assert v1f_refetched.file_token == v1f.file_token
        assert v1f_refetched.storage_key == v1f.storage_key
        assert v1f_refetched.patient_storage_key == v1f.patient_storage_key


# ---------------------------------------------------------------------------
# 4. API responses must not leak storage_key
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestNoStorageKeyExposure:

    def test_notify_response_omits_storage_key(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _build_finalized_request(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician,
            biologist=biologist, make_request=make_request,
        )
        resp = admin_client.post(
            NOTIFY_URL.format(ar_id=ar.id), data=_payload(portal_account),
            format='json',
        )
        assert resp.status_code == 200
        body = resp.content.decode()
        # Both the lab-side ``pdf_file_key`` and the snapshot
        # ``storage_key`` are internal — neither name should appear in
        # any response field, even nested. The check is intentionally
        # broad (substring match against the raw JSON) to catch future
        # additions that might inadvertently re-introduce the path.
        assert 'storage_key' not in body
        assert 'pdf_file_key' not in body
        assert 'patient_storage_key' not in body


# ---------------------------------------------------------------------------
# 5. Cross-patient isolation: supersession is patient-scoped
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestCrossPatientIsolation:

    def test_share_to_other_patient_does_not_demote_first_patient(
        self, admin_client, portal_account, other_portal_account,
        lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _build_finalized_request(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician,
            biologist=biologist, make_request=make_request,
        )
        first = _share_v1(admin_client, ar, portal_account)

        # Re-share the SAME request to a different patient account.
        # The view's one-shot guard requires force_share for the
        # privileged role.
        resp = admin_client.post(
            NOTIFY_URL.format(ar_id=ar.id),
            data=_payload(other_portal_account, force_share=True),
            format='json',
        )
        assert resp.status_code == 200, resp.content
        second_id = resp.json()['data']['shared_result_id']

        first_row = PatientSharedResult.objects.get(pk=first['shared_result_id'])
        second_row = PatientSharedResult.objects.get(pk=second_id)

        # Both rows are current for THEIR OWN patient — supersession
        # is scoped to (patient_account, source_request_id), so a
        # share-to-a-different-account does NOT touch the first
        # patient's view.
        assert first_row.patient_account_id == portal_account.id
        assert second_row.patient_account_id == other_portal_account.id
        assert first_row.is_current_for_patient is True
        assert second_row.is_current_for_patient is True


# ---------------------------------------------------------------------------
# 6. Audit emits the version-aware events
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestVersionAuditEvents:

    def test_v1_share_emits_version_shared(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _build_finalized_request(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician,
            biologist=biologist, make_request=make_request,
        )
        body = _share_v1(admin_client, ar, portal_account)

        rows = list(PatientPortalAuditLog.objects.filter(
            patient_account=portal_account,
            action=PatientPortalAuditAction.PATIENT_VERSION_SHARED.value,
        ))
        assert len(rows) == 1
        meta = rows[0].metadata
        assert meta['shared_result_id'] == body['shared_result_id']
        assert meta['report_version_number'] == 1
        assert meta['shared_channel'] == PatientSharedChannel.CYTOVA.value
        # Allow-list is enforced — no PII / tokens / storage paths.
        assert set(meta.keys()) <= {
            'shared_result_id', 'source_request_reference',
            'report_version_number', 'shared_channel',
        }

    def test_v2_share_emits_superseded_for_v1(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _build_finalized_request(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician,
            biologist=biologist, make_request=make_request,
        )
        v1_body = _share_v1(admin_client, ar, portal_account)
        _reopen(admin_client, ar)
        _regenerate_to_v2(ar, biologist, make_request)
        v2_body = _share_again(admin_client, ar, portal_account)

        superseded = list(PatientPortalAuditLog.objects.filter(
            patient_account=portal_account,
            action=PatientPortalAuditAction.PATIENT_VERSION_SUPERSEDED.value,
        ))
        assert len(superseded) == 1
        meta = superseded[0].metadata
        assert meta['shared_result_id'] == v1_body['shared_result_id']
        assert meta['superseded_by_shared_result_id'] == v2_body['shared_result_id']
        # Allow-list enforcement.
        assert set(meta.keys()) <= {
            'shared_result_id', 'source_request_reference',
            'report_version_number', 'superseded_by_shared_result_id',
        }
