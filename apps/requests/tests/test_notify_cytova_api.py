"""
HTTP-level tests for ``POST /api/v1/requests/{id}/notify-cytova/``.

The endpoint runs in tenant context (lab subdomain) but writes
snapshot rows into the ``public``-schema patient portal tables. The
django-tenants ``search_path`` falls through to ``public`` for any
unqualified table that doesn't exist in the lab schema, so the
service can read/write portal models from inside a tenant request
with no special schema gymnastics.
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
    PatientSharedResult, PatientSharedResultFile,
)
from apps.patient_portal.services import register_patient_account
from apps.patients.models import Patient
from apps.requests.models import RequestStatus, SourceType
from apps.requests.report_service import RequestReportService
from apps.requests.services import (
    AnalysisRequestItemService, AnalysisRequestService,
)
from apps.results.services import ResultVersionService


URL = '/api/v1/requests/{ar_id}/notify-cytova/'


# ---------------------------------------------------------------------------
# Subscription fixture (same pattern as test_report.py)
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
# Lab-side fixtures: a finalized request with a generated PDF.
# ---------------------------------------------------------------------------

@pytest.fixture()
def lab_patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-NTF-001',
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
# Patient portal fixture: an account in the public schema.
# ---------------------------------------------------------------------------

@pytest.fixture()
def portal_account():
    """Created via the patient_portal service. With search_path set to
    ``schema_testlab,public``, the writes fall through to ``public``
    because the patient_portal tables only exist there."""
    return register_patient_account(
        email='ada@portal.test',
        password='Strong-Pass-1234!',
        first_name='Ada', last_name='Lovelace',
        date_of_birth=date(1990, 5, 17), accept_terms=True,
    )


# ---------------------------------------------------------------------------
# HTTP fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def api_client():
    return APIClient(HTTP_HOST='testlab.localhost')


@pytest.fixture()
def admin_client(api_client, lab_admin):
    api_client.force_authenticate(user=lab_admin)
    return api_client


@pytest.fixture()
def viewer_client(api_client, viewer_auditor):
    api_client.force_authenticate(user=viewer_auditor)
    return api_client


def _payload(portal_account, **overrides):
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
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestNotifyCytova:

    def test_success_creates_snapshot_in_public_schema(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        resp = admin_client.post(
            URL.format(ar_id=ar.id), data=_payload(portal_account),
            format='json',
        )
        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert body['errors'] == []
        assert body['data']['message'] == "Result successfully shared with patient."

        # Snapshot row in the public-schema patient portal tables.
        shared = PatientSharedResult.objects.get(
            patient_account=portal_account,
        )
        assert shared.source_type == 'DIRECT'
        assert shared.request_reference == (
            ar.public_reference or ar.request_number
        )
        assert shared.status == 'ACTIVE'

        sfile = PatientSharedResultFile.objects.get(shared_result=shared)
        # File token is opaque + URL-safe; storage_key snapshots the
        # tenant-side PDF location for the future download endpoint.
        assert len(sfile.file_token) >= 32
        assert sfile.storage_key  # non-empty
        assert sfile.filename.endswith('.pdf')

    def test_identity_mismatch_returns_400_with_generic_code(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        resp = admin_client.post(
            URL.format(ar_id=ar.id),
            data=_payload(portal_account, last_name='WrongName'),
            format='json',
        )
        assert resp.status_code == 400, resp.content
        codes = {e['code'] for e in resp.json()['errors']}
        # Single non-distinguishing code regardless of which field failed.
        assert codes == {'IDENTITY_VERIFICATION_FAILED'}
        # No snapshot row created on failure.
        assert not PatientSharedResult.objects.filter(
            patient_account=portal_account,
        ).exists()

    def test_unknown_cytova_id_returns_same_generic_error(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        resp = admin_client.post(
            URL.format(ar_id=ar.id),
            data=_payload(portal_account, cytova_patient_id='CV-XXXX-YYYY'),
            format='json',
        )
        assert resp.status_code == 400, resp.content
        codes = {e['code'] for e in resp.json()['errors']}
        # Same code as wrong-name — the lab user cannot tell whether
        # the ID exists or whether identity didn't match.
        assert codes == {'IDENTITY_VERIFICATION_FAILED'}

    def test_only_validated_requests_can_be_shared(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, make_request,
    ):
        # Draft request — never validated, no report.
        ar = AnalysisRequestService.create(
            validated_data={
                'patient_id': lab_patient.id,
                'source_type': SourceType.DIRECT_PATIENT,
                'items': [{'exam_definition_id': exam_def.id}],
            },
            created_by=lab_admin, request=make_request(lab_admin),
        )
        resp = admin_client.post(
            URL.format(ar_id=ar.id), data=_payload(portal_account),
            format='json',
        )
        assert resp.status_code == 400, resp.content
        codes = {e['code'] for e in resp.json()['errors']}
        # Either pre-validation gate fires, or the no-report gate
        # depending on workflow order. Both are valid refusals.
        assert codes & {'REQUEST_NOT_VALIDATED', 'REPORT_NOT_AVAILABLE'}

    def test_audit_log_records_share_outcome(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        resp = admin_client.post(
            URL.format(ar_id=ar.id), data=_payload(portal_account),
            format='json',
        )
        assert resp.status_code == 200, resp.content
        # Look for the SHARED outcome row written by the service.
        # Action is now ``RESULT_SHARED_CYTOVA`` (dedicated lifecycle
        # event) rather than the legacy ``UPDATE`` — the diff payload
        # still carries ``notify_cytova_outcome=SHARED`` for back-compat.
        rows = AuditLog.objects.filter(
            entity_type='AnalysisRequest', entity_id=ar.id,
            action=AuditAction.RESULT_SHARED_CYTOVA,
        )
        shared_rows = [
            r for r in rows
            if (r.diff or {}).get('after', {}).get('notify_cytova_outcome') == 'SHARED'
        ]
        assert len(shared_rows) == 1
        diff = shared_rows[0].diff['after']
        # Audit captures account ID + share id, NEVER patient PII.
        assert diff['patient_account_id'] == str(portal_account.id)
        assert 'shared_result_id' in diff
        assert 'first_name' not in diff and 'last_name' not in diff
        assert 'date_of_birth' not in diff and 'email' not in diff

    def test_audit_log_records_failed_attempt_without_pii(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        admin_client.post(
            URL.format(ar_id=ar.id),
            data=_payload(portal_account, last_name='WrongName'),
            format='json',
        )
        rows = AuditLog.objects.filter(
            entity_type='AnalysisRequest', entity_id=ar.id,
            action=AuditAction.UPDATE,
        )
        mismatch_rows = [
            r for r in rows
            if (r.diff or {}).get('after', {}).get('notify_cytova_outcome') == 'IDENTITY_MISMATCH'
        ]
        assert len(mismatch_rows) == 1
        diff = mismatch_rows[0].diff['after']
        # Cytova ID (already public) is captured for forensics; the
        # name/DOB the user typed is NEVER stored.
        assert 'cytova_patient_id_attempted' in diff
        assert 'first_name' not in diff and 'last_name' not in diff
        assert 'date_of_birth' not in diff

    def test_viewer_role_is_rejected_with_403(
        self, viewer_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalized_request_with_report(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician, biologist=biologist,
            make_request=make_request,
        )
        resp = viewer_client.post(
            URL.format(ar_id=ar.id), data=_payload(portal_account),
            format='json',
        )
        assert resp.status_code == 403, resp.content
        assert not PatientSharedResult.objects.filter(
            patient_account=portal_account,
        ).exists()
