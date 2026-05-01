"""
HTTP-level tests for the Cytova share lifecycle on the lab tenant
side: notify-cytova email status, revoke endpoint, and the share-status
lookup. Builds on the same fixtures the original notify-cytova suite
uses (a finalized request with a generated PDF).
"""
from __future__ import annotations

from datetime import date

import pytest
from django.core.cache import cache
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.test import APIClient

from apps.audit.models import AuditAction, AuditLog
from apps.catalog.models import (
    ExamCategory, ExamDefinition, ExamFamily, ExamTechnique,
    ResultStructure, SampleType,
)
from apps.patient_portal.models import (
    PatientPortalAuditAction, PatientPortalAuditLog,
    PatientSharedResult, SharedResultStatus,
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
SHARE_STATUS_URL = '/api/v1/requests/{ar_id}/cytova-share/'
REVOKE_URL = '/api/v1/requests/{ar_id}/revoke-cytova-share/'


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
# Lab-side fixtures: a finalized request with a generated PDF
# ---------------------------------------------------------------------------

@pytest.fixture()
def lab_patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-LIFE-001',
        first_name='Lab', last_name='Side',
        date_of_birth=date(1970, 1, 1), gender='FEMALE',
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
# Patient portal fixture (created in tenant context — falls through to
# public schema since the patient_portal tables only exist there)
# ---------------------------------------------------------------------------

@pytest.fixture()
def portal_account():
    return register_patient_account(
        email='lifecycle@portal.test',
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


def _payload(portal_account):
    profile = portal_account.profile
    return {
        'cytova_patient_id': profile.cytova_patient_id,
        'first_name': profile.first_name,
        'last_name': profile.last_name,
        'date_of_birth': profile.date_of_birth.isoformat(),
    }


# ---------------------------------------------------------------------------
# 1. Notify response + email status + patient audit
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestNotifyResponseAndEmail:

    def test_response_includes_email_notification_status(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        resp = admin_client.post(
            NOTIFY_URL.format(ar_id=ar.id), data=_payload(portal_account),
            format='json',
        )
        assert resp.status_code == 200, resp.content
        data = resp.json()['data']
        # Spec contract: response carries the email notification outcome
        # so the lab UI can surface a "we couldn't email the patient"
        # warning when delivery fails.
        assert 'email_notification' in data
        assert data['email_notification'] in ('SENT', 'FAILED')
        # Snapshot row records the same status.
        shared = PatientSharedResult.objects.get(pk=data['shared_result_id'])
        assert shared.email_notification_status == data['email_notification']

    def test_share_succeeds_even_when_email_fails(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request, monkeypatch,
    ):
        # Force the email service to return a failed result. Sharing
        # must still succeed — the snapshot row is the canonical
        # artefact and the email is best-effort.
        from common.email.providers.base import EmailResult
        from apps.requests import notify_cytova_service as svc

        def _fail(*args, **kwargs):
            return EmailResult(ok=False, error='simulated SMTP outage')

        class _FakeService:
            def send_patient_shared_result_email(self, **_):
                return _fail()

        monkeypatch.setattr(svc, 'get_email_service', lambda: _FakeService())

        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        resp = admin_client.post(
            NOTIFY_URL.format(ar_id=ar.id), data=_payload(portal_account),
            format='json',
        )
        assert resp.status_code == 200, resp.content
        data = resp.json()['data']
        assert data['email_notification'] == 'FAILED'
        # Snapshot row exists despite the email failure.
        shared = PatientSharedResult.objects.get(pk=data['shared_result_id'])
        assert shared.status == SharedResultStatus.ACTIVE
        assert shared.email_notification_status == 'FAILED'
        assert shared.email_notification_sent_at is None

    def test_notify_writes_patient_side_audit(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        admin_client.post(
            NOTIFY_URL.format(ar_id=ar.id), data=_payload(portal_account),
            format='json',
        )
        rows = PatientPortalAuditLog.objects.filter(
            patient_account=portal_account,
            action=PatientPortalAuditAction.PATIENT_RESULT_SHARED.value,
        )
        assert rows.count() == 1
        meta = rows.first().metadata
        # Allow-listed metadata only; no PII / tokens / storage paths.
        assert set(meta.keys()) <= {
            'shared_result_id', 'source_request_reference',
            'source_name', 'email_notification_status',
        }


# ---------------------------------------------------------------------------
# 2. Revoke endpoint
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestRevokeShare:

    def _share_first(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        resp = admin_client.post(
            NOTIFY_URL.format(ar_id=ar.id), data=_payload(portal_account),
            format='json',
        )
        assert resp.status_code == 200, resp.content
        return ar

    def test_revoke_marks_share_revoked_and_writes_audits(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = self._share_first(
            admin_client, portal_account, lab_patient, exam_def,
            lab_admin, technician, biologist, make_request,
        )
        resp = admin_client.post(
            REVOKE_URL.format(ar_id=ar.id), format='json',
        )
        assert resp.status_code == 200, resp.content
        body = resp.json()['data']
        assert body['revoked_count'] == 1

        share = PatientSharedResult.objects.get(
            patient_account=portal_account,
        )
        assert share.status == SharedResultStatus.REVOKED
        assert share.revoked_at is not None
        assert share.revoked_by_lab  # snapshot of staff email / lab id

        # Tenant audit gets a REVOKED outcome row. Action is now the
        # dedicated ``CYTOVA_SHARE_REVOKED`` lifecycle event.
        tenant_rows = AuditLog.objects.filter(
            entity_type='AnalysisRequest', entity_id=ar.id,
            action=AuditAction.CYTOVA_SHARE_REVOKED,
        )
        revoke_rows = [
            r for r in tenant_rows
            if (r.diff or {}).get('after', {}).get('notify_cytova_outcome') == 'REVOKED'
        ]
        assert len(revoke_rows) == 1

        # Patient-side audit gets a REVOKED_BY_LAB row.
        portal_rows = PatientPortalAuditLog.objects.filter(
            patient_account=portal_account,
            action=PatientPortalAuditAction.PATIENT_RESULT_REVOKED_BY_LAB.value,
        )
        assert portal_rows.count() == 1

    def test_revoke_is_idempotent(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = self._share_first(
            admin_client, portal_account, lab_patient, exam_def,
            lab_admin, technician, biologist, make_request,
        )
        first = admin_client.post(REVOKE_URL.format(ar_id=ar.id), format='json')
        second = admin_client.post(REVOKE_URL.format(ar_id=ar.id), format='json')
        assert first.json()['data']['revoked_count'] == 1
        assert second.json()['data']['revoked_count'] == 0

    def test_revoked_share_excluded_from_patient_list_and_download(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        from apps.patient_portal.services import issue_patient_tokens

        ar = self._share_first(
            admin_client, portal_account, lab_patient, exam_def,
            lab_admin, technician, biologist, make_request,
        )
        # Capture file token BEFORE revoke (the patient still has the
        # download URL the lab gave them).
        share = PatientSharedResult.objects.get(patient_account=portal_account)
        token = share.files.first().file_token

        # Revoke.
        admin_client.post(REVOKE_URL.format(ar_id=ar.id), format='json')

        # Patient-side: list excludes the row, download returns 404.
        portal_account.email_verified_at = timezone.now()
        portal_account.save(update_fields=['email_verified_at'])
        tokens = issue_patient_tokens(portal_account)
        patient_client = APIClient(HTTP_HOST='testlab.localhost')
        patient_client.credentials(HTTP_AUTHORIZATION=f'Bearer {tokens["access_token"]}')

        list_resp = patient_client.get('/api/v1/patient-portal/results/')
        assert list_resp.json()['data']['results'] == []

        dl_resp = patient_client.get(
            f'/api/v1/patient-portal/results/files/{token}/download/',
        )
        assert dl_resp.status_code == 404, dl_resp.content

    def test_unauthorized_role_cannot_revoke(
        self, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        # Build TWO independent APIClient instances so the
        # ``force_authenticate`` calls don't clobber each other (they
        # would on a shared fixture). Share via admin (technician CAN
        # share — IsTechnicianOrAbove); verify technician CANNOT
        # revoke (IsReceptionistOrLabAdmin).
        admin_client_local = APIClient(HTTP_HOST='testlab.localhost')
        admin_client_local.force_authenticate(user=lab_admin)
        technician_client_local = APIClient(HTTP_HOST='testlab.localhost')
        technician_client_local.force_authenticate(user=technician)

        ar = self._share_first(
            admin_client_local, portal_account, lab_patient, exam_def,
            lab_admin, technician, biologist, make_request,
        )
        resp = technician_client_local.post(
            REVOKE_URL.format(ar_id=ar.id), format='json',
        )
        assert resp.status_code == 403, resp.content
        share = PatientSharedResult.objects.get(patient_account=portal_account)
        assert share.status == SharedResultStatus.ACTIVE


# ---------------------------------------------------------------------------
# 3. Lab-side share status lookup
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestShareStatusLookup:

    def test_returns_null_when_no_share(
        self, admin_client, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        resp = admin_client.get(SHARE_STATUS_URL.format(ar_id=ar.id))
        assert resp.status_code == 200, resp.content
        body = resp.json()['data']
        assert body['status'] is None
        assert body['shared_result_id'] is None

    def test_returns_active_after_share(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        admin_client.post(
            NOTIFY_URL.format(ar_id=ar.id), data=_payload(portal_account),
            format='json',
        )
        resp = admin_client.get(SHARE_STATUS_URL.format(ar_id=ar.id))
        body = resp.json()['data']
        assert body['status'] == SharedResultStatus.ACTIVE
        assert body['shared_result_id']

    def test_returns_revoked_after_revoke(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        admin_client.post(
            NOTIFY_URL.format(ar_id=ar.id), data=_payload(portal_account),
            format='json',
        )
        admin_client.post(REVOKE_URL.format(ar_id=ar.id), format='json')
        resp = admin_client.get(SHARE_STATUS_URL.format(ar_id=ar.id))
        body = resp.json()['data']
        assert body['status'] == SharedResultStatus.REVOKED
        assert body['revoked_at']
