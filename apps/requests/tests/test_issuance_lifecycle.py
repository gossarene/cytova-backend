"""
HTTP-level tests for the result-issuance lifecycle:

- First patient-facing notification flips status VALIDATED → RESULT_ISSUED
- Locked: result-version edits + report regeneration refuse after issuance
- Re-notification requires ``force_resend=true`` (email + share-link)
- Notify Cytova is one-shot unless ``force_share=true`` AND privileged role
- ``reopen-result`` walks back to VALIDATED + supersedes the current report
- Patient PDF copy lands at ``patient_storage_key`` and survives lab-key removal
- Audit rows use the dedicated lifecycle actions (RESULT_ISSUED / REISSUED /
  REOPENED / RESULT_SHARED_CYTOVA / CYTOVA_SHARE_REVOKED)
"""
from __future__ import annotations

from datetime import date

import pytest
from django.core.cache import cache
from django.core.files.storage import default_storage
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.test import APIClient

from apps.audit.models import AuditAction, AuditLog
from apps.catalog.models import (
    ExamCategory, ExamDefinition, ExamFamily, ExamTechnique,
    ResultStructure, SampleType,
)
from apps.patient_portal.models import (
    PatientSharedResult, PatientSharedResultFile, SharedResultStatus,
)
from apps.patient_portal.services import register_patient_account
from apps.patients.models import Patient
from apps.requests.models import (
    AnalysisRequestReport, RequestStatus, SourceType,
)
from apps.requests.report_service import RequestReportService
from apps.requests.services import (
    AnalysisRequestItemService, AnalysisRequestService,
)
from apps.results.services import ResultVersionService


NOTIFY_PATIENT_URL = '/api/v1/requests/{ar_id}/notify-patient/'
NOTIFY_CYTOVA_URL = '/api/v1/requests/{ar_id}/notify-cytova/'
ACCESS_TOKEN_URL = '/api/v1/requests/{ar_id}/access-token/'
REGEN_TOKEN_URL = '/api/v1/requests/{ar_id}/access-token/regenerate/'
REOPEN_URL = '/api/v1/requests/{ar_id}/reopen-result/'


# ---------------------------------------------------------------------------
# Subscription + cache fixtures
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


# ---------------------------------------------------------------------------
# Lab-side fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def lab_patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-LCY-001',
        first_name='Lab', last_name='Side',
        date_of_birth=date(1970, 1, 1), gender='FEMALE',
        email='lab-side@patient.test',
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


def _finalized_request_with_report(
    *, lab_patient, exam_def, lab_admin, technician, biologist, make_request,
):
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
# Patient portal fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def portal_account():
    return register_patient_account(
        email='lifecycle-2@portal.test',
        password='Strong-Pass-1234!',
        first_name='Ada', last_name='Lovelace',
        date_of_birth=date(1990, 5, 17), accept_terms=True,
    )


def _cytova_payload(portal_account, **overrides):
    profile = portal_account.profile
    base = {
        'cytova_patient_id': profile.cytova_patient_id,
        'first_name': profile.first_name,
        'last_name': profile.last_name,
        'date_of_birth': profile.date_of_birth.isoformat(),
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# HTTP fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def admin_client(lab_admin):
    c = APIClient(HTTP_HOST='testlab.localhost')
    c.force_authenticate(user=lab_admin)
    return c


@pytest.fixture()
def biologist_client(biologist):
    c = APIClient(HTTP_HOST='testlab.localhost')
    c.force_authenticate(user=biologist)
    return c


@pytest.fixture()
def technician_client(technician):
    c = APIClient(HTTP_HOST='testlab.localhost')
    c.force_authenticate(user=technician)
    return c


# ---------------------------------------------------------------------------
# 1. First-notification → ISSUED
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestFirstNotificationIssues:

    def test_first_access_token_creation_issues_request(
        self, admin_client, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        assert ar.status == RequestStatus.VALIDATED
        # First POST to access-token mints the link → issuance fires.
        resp = admin_client.post(ACCESS_TOKEN_URL.format(ar_id=ar.id))
        assert resp.status_code == 200, resp.content
        ar.refresh_from_db()
        assert ar.status == RequestStatus.RESULT_ISSUED
        assert ar.issued_at is not None

        # Audit row recorded with the dedicated lifecycle action.
        rows = AuditLog.objects.filter(
            entity_type='AnalysisRequest', entity_id=ar.id,
            action=AuditAction.RESULT_ISSUED,
        )
        assert rows.count() == 1
        diff = rows.first().diff['after']
        assert diff['channel'] == 'SHARE_LINK'

    def test_idempotent_post_does_not_re_audit(
        self, admin_client, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        admin_client.post(ACCESS_TOKEN_URL.format(ar_id=ar.id))
        # Second POST returns the existing token (idempotent) — must
        # NOT write a second RESULT_ISSUED row, and must not re-fire
        # the issuance hook (already issued).
        admin_client.post(ACCESS_TOKEN_URL.format(ar_id=ar.id))
        rows = AuditLog.objects.filter(
            entity_type='AnalysisRequest', entity_id=ar.id,
            action=AuditAction.RESULT_ISSUED,
        )
        assert rows.count() == 1


# ---------------------------------------------------------------------------
# 2. Locking — result mutations refuse on issued requests
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestIssuanceLocks:

    def test_regenerate_report_refused_after_issuance(
        self, admin_client, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        admin_client.post(ACCESS_TOKEN_URL.format(ar_id=ar.id))  # → issued
        from rest_framework.exceptions import ValidationError
        with pytest.raises(ValidationError) as exc:
            RequestReportService.regenerate(
                analysis_request=ar, generated_by=biologist,
                request=make_request(biologist),
            )
        # Spec-mandated copy.
        assert 'already been issued' in str(exc.value).lower()

    def test_result_validate_refused_after_issuance(
        self, admin_client, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        admin_client.post(ACCESS_TOKEN_URL.format(ar_id=ar.id))  # → issued
        item = ar.items.first()
        v = item.result_versions.get(is_current=True)
        from rest_framework.exceptions import ValidationError
        # ``validate`` was the original transition; calling it again on
        # an issued request must refuse via the new lock.
        with pytest.raises(ValidationError) as exc:
            ResultVersionService.validate(
                version=v, validation_notes='retry',
                validated_by=biologist, request=make_request(biologist),
            )
        assert 'already been issued' in str(exc.value).lower()


# ---------------------------------------------------------------------------
# 3. Re-notification gate
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestResendGate:

    def test_regenerate_token_refused_without_force_resend(
        self, admin_client, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        admin_client.post(ACCESS_TOKEN_URL.format(ar_id=ar.id))  # → issued

        resp = admin_client.post(REGEN_TOKEN_URL.format(ar_id=ar.id))
        # 409 with the ALREADY_ISSUED code so the frontend can show
        # the confirmation modal.
        assert resp.status_code == 409, resp.content
        codes = {e['code'] for e in resp.json()['errors']}
        assert 'ALREADY_ISSUED' in codes

    def test_regenerate_token_succeeds_with_force_resend(
        self, admin_client, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        admin_client.post(ACCESS_TOKEN_URL.format(ar_id=ar.id))  # → issued
        resp = admin_client.post(
            REGEN_TOKEN_URL.format(ar_id=ar.id),
            data={'force_resend': True}, format='json',
        )
        assert resp.status_code == 200, resp.content
        # RESULT_REISSUED audit row recorded.
        assert AuditLog.objects.filter(
            entity_type='AnalysisRequest', entity_id=ar.id,
            action=AuditAction.RESULT_REISSUED,
        ).count() == 1


# ---------------------------------------------------------------------------
# 4. Notify Cytova one-shot + force_share role gate
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestCytovaOneShot:

    def _share(self, client, ar, portal_account, **overrides):
        return client.post(
            NOTIFY_CYTOVA_URL.format(ar_id=ar.id),
            data=_cytova_payload(portal_account, **overrides),
            format='json',
        )

    def test_first_share_succeeds(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        resp = self._share(admin_client, ar, portal_account)
        assert resp.status_code == 200, resp.content
        ar.refresh_from_db()
        assert ar.status == RequestStatus.RESULT_ISSUED

    def test_second_share_blocked_by_default(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        self._share(admin_client, ar, portal_account)
        resp2 = self._share(admin_client, ar, portal_account)
        assert resp2.status_code == 409, resp2.content
        codes = {e['code'] for e in resp2.json()['errors']}
        assert 'CYTOVA_ALREADY_SHARED' in codes

    def test_force_share_requires_privileged_role(
        self, admin_client, technician_client, portal_account,
        lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        # Initial share by admin.
        self._share(admin_client, ar, portal_account)

        # Technician CAN share (IsTechnicianOrAbove) but CANNOT force
        # a re-share — the role gate catches it.
        resp_tech = self._share(
            technician_client, ar, portal_account, force_share=True,
        )
        assert resp_tech.status_code == 409, resp_tech.content

        # Admin CAN force-share (LAB_ADMIN is privileged).
        resp_admin = self._share(
            admin_client, ar, portal_account, force_share=True,
        )
        assert resp_admin.status_code == 200, resp_admin.content


# ---------------------------------------------------------------------------
# 5. Reopen flow
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestReopen:

    def test_reopen_walks_back_and_supersedes_report(
        self, biologist_client, admin_client, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        admin_client.post(ACCESS_TOKEN_URL.format(ar_id=ar.id))  # → issued
        ar.refresh_from_db()
        assert ar.status == RequestStatus.RESULT_ISSUED

        resp = biologist_client.post(
            REOPEN_URL.format(ar_id=ar.id),
            data={'reason': 'Wrong reference range used.'},
            format='json',
        )
        assert resp.status_code == 200, resp.content
        ar.refresh_from_db()
        assert ar.status == RequestStatus.VALIDATED
        assert ar.reopened_at is not None
        assert ar.reopen_reason == 'Wrong reference range used.'

        # Previous report version is no longer current.
        assert AnalysisRequestReport.objects.filter(
            analysis_request=ar, is_current=True,
        ).count() == 0
        assert AnalysisRequestReport.objects.filter(
            analysis_request=ar, is_current=False,
        ).count() >= 1

        # Dedicated audit row.
        assert AuditLog.objects.filter(
            entity_type='AnalysisRequest', entity_id=ar.id,
            action=AuditAction.RESULT_REOPENED,
        ).count() == 1

    def test_reopen_refused_when_not_issued(
        self, biologist_client, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        # Not issued yet — endpoint refuses.
        resp = biologist_client.post(
            REOPEN_URL.format(ar_id=ar.id),
            data={'reason': 'Too soon.'}, format='json',
        )
        assert resp.status_code == 409, resp.content
        codes = {e['code'] for e in resp.json()['errors']}
        assert 'NOT_ISSUED' in codes

    def test_reopen_requires_privileged_role(
        self, technician_client, admin_client, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        admin_client.post(ACCESS_TOKEN_URL.format(ar_id=ar.id))
        # Technician hits the permission gate (IsBiologistOrAbove).
        resp = technician_client.post(
            REOPEN_URL.format(ar_id=ar.id),
            data={'reason': 'I tried.'}, format='json',
        )
        assert resp.status_code == 403, resp.content


# ---------------------------------------------------------------------------
# 6. Patient PDF copy + download fallback
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestPatientFileCopy:

    def test_notify_cytova_copies_pdf_to_patient_storage(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        admin_client.post(
            NOTIFY_CYTOVA_URL.format(ar_id=ar.id),
            data=_cytova_payload(portal_account), format='json',
        )
        sfile = PatientSharedResultFile.objects.get(
            shared_result__patient_account=portal_account,
        )
        # Patient-owned copy created + storage_origin flipped.
        assert sfile.patient_storage_key
        assert sfile.patient_storage_key != sfile.storage_key
        assert sfile.storage_origin == 'PATIENT'
        assert default_storage.exists(sfile.patient_storage_key)

    def test_download_uses_patient_storage_key(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        from apps.patient_portal.services import issue_patient_tokens
        from django.utils import timezone

        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        admin_client.post(
            NOTIFY_CYTOVA_URL.format(ar_id=ar.id),
            data=_cytova_payload(portal_account), format='json',
        )
        sfile = PatientSharedResultFile.objects.get(
            shared_result__patient_account=portal_account,
        )
        # Wipe the LAB-side file to prove the patient endpoint reads
        # from ``patient_storage_key`` rather than falling back to it.
        default_storage.delete(sfile.storage_key)

        portal_account.email_verified_at = timezone.now()
        portal_account.save(update_fields=['email_verified_at'])
        tokens = issue_patient_tokens(portal_account)
        patient_client = APIClient(HTTP_HOST='testlab.localhost')
        patient_client.credentials(HTTP_AUTHORIZATION=f'Bearer {tokens["access_token"]}')

        resp = patient_client.get(
            f'/api/v1/patient-portal/results/files/{sfile.file_token}/download/',
        )
        # Still 200 — the patient-owned copy serves the bytes even
        # though the lab-side blob was deleted.
        assert resp.status_code == 200, resp.content

    def test_download_falls_back_to_lab_when_patient_copy_absent(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        """Legacy rows (or copy-time failures) leave
        ``patient_storage_key`` empty + ``storage_origin='LAB'``.
        Download must still succeed via the lab-side snapshot."""
        from apps.patient_portal.services import issue_patient_tokens
        from django.utils import timezone

        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        admin_client.post(
            NOTIFY_CYTOVA_URL.format(ar_id=ar.id),
            data=_cytova_payload(portal_account), format='json',
        )
        sfile = PatientSharedResultFile.objects.get(
            shared_result__patient_account=portal_account,
        )
        # Simulate copy-time failure: clear the patient copy and flip
        # back to LAB origin.
        if sfile.patient_storage_key:
            default_storage.delete(sfile.patient_storage_key)
        sfile.patient_storage_key = ''
        sfile.storage_origin = 'LAB'
        sfile.save(update_fields=['patient_storage_key', 'storage_origin'])

        portal_account.email_verified_at = timezone.now()
        portal_account.save(update_fields=['email_verified_at'])
        tokens = issue_patient_tokens(portal_account)
        patient_client = APIClient(HTTP_HOST='testlab.localhost')
        patient_client.credentials(HTTP_AUTHORIZATION=f'Bearer {tokens["access_token"]}')

        resp = patient_client.get(
            f'/api/v1/patient-portal/results/files/{sfile.file_token}/download/',
        )
        assert resp.status_code == 200, resp.content
