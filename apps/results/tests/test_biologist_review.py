"""
Tests for biologist review of submitted result versions.

Scope
-----
- Biologist can validate a submitted result version.
- Biologist can reject a submitted result version with a reason.
- Reject without reason fails.
- Item status updates correctly (UNDER_REVIEW → VALIDATED on validate,
  UNDER_REVIEW → RESULT_ENTERED on reject for re-entry).
- Request status derivation:
    * All items validated             → VALIDATED
    * Some items rejected, none under → RETEST_REQUIRED
    * Items still under review        → AWAITING_REVIEW
- Invalid review transitions are rejected.
- Permissions: IsBiologistOrAbove — technician/viewer rejected.
- Audit entries are written for validate and reject actions.
"""
from datetime import date

import pytest
from django_tenants.utils import schema_context, get_public_schema_name
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.catalog.models import ExamCategory, ExamDefinition, ExamFamily, SampleType
from apps.patients.models import Patient
from apps.requests.models import (
    AnalysisRequest, AnalysisRequestItem, ItemStatus,
    RequestStatus, SourceType,
)
from apps.requests.services import (
    AnalysisRequestItemService, AnalysisRequestService,
)
from apps.results.models import ResultVersion, ResultStatus
from apps.results.services import ResultVersionService


API_RESULTS = '/api/v1/results'


# ---------------------------------------------------------------------------
# Fixtures
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
                    'name': 'Test Trial',
                    'is_trial': True,
                    'trial_duration_days': 30,
                    'is_public': False,
                },
            )
            tenant = Tenant.objects.get(schema_name=_test_tenant_schema)
            Subscription.objects.get_or_create(
                tenant=tenant,
                status=SubscriptionStatus.TRIAL,
                defaults={'plan': plan},
            )
    yield


@pytest.fixture()
def api_client():
    return APIClient(HTTP_HOST='testlab.localhost')


@pytest.fixture()
def bio_client(api_client, biologist):
    api_client.force_authenticate(user=biologist)
    return api_client


@pytest.fixture()
def tech_client(api_client, technician):
    api_client.force_authenticate(user=technician)
    return api_client


@pytest.fixture()
def viewer_client(api_client, viewer_auditor):
    api_client.force_authenticate(user=viewer_auditor)
    return api_client


@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-REV-001',
        first_name='Rita',
        last_name='Review',
        date_of_birth=date(1985, 3, 20),
        gender='FEMALE',
        created_by=lab_admin,
    )


@pytest.fixture()
def category():
    return ExamCategory.objects.create(name='Labs', display_order=1)


@pytest.fixture()
def family_a():
    return ExamFamily.objects.create(name='Hematology', display_order=1)


@pytest.fixture()
def family_b():
    return ExamFamily.objects.create(name='Biochemistry', display_order=2)


@pytest.fixture()
def exam_cbc(family_a, category):
    return ExamDefinition.objects.create(
        category=category, family=family_a,
        code='CBC', name='Complete Blood Count',
        sample_type=SampleType.BLOOD,
    )


@pytest.fixture()
def exam_glu(family_b, category):
    return ExamDefinition.objects.create(
        category=category, family=family_b,
        code='GLU', name='Fasting Glucose',
        sample_type=SampleType.BLOOD,
    )


def _submitted_request(patient, lab_admin, technician, make_request, exam_ids):
    """Create a confirmed request, collect all items, enter+submit results."""
    ar = AnalysisRequestService.create(
        validated_data={
            'patient_id': patient.id,
            'source_type': SourceType.DIRECT_PATIENT,
            'items': [{'exam_definition_id': eid} for eid in exam_ids],
        },
        created_by=lab_admin,
        request=make_request(lab_admin),
        confirm_after=True,
    )
    req = make_request(technician)
    for item in ar.items.all():
        AnalysisRequestItemService.mark_collected(
            item=item, collected_by=technician, request=req,
        )
    for item in ar.items.all():
        item.refresh_from_db()
        v = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req,
            result_value='ok',
        )
        ResultVersionService.submit(
            version=v, submitted_by=technician, request=req,
        )
    ar.refresh_from_db()
    return ar


def _data(resp):
    body = resp.json()
    return body.get('data', body)


# ---------------------------------------------------------------------------
# Service-level: validate
# ---------------------------------------------------------------------------

class TestValidate:

    def test_validates_submitted_result(
        self, patient, exam_cbc, lab_admin, technician, biologist, make_request,
    ):
        ar = _submitted_request(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        version = item.result_versions.get(is_current=True)
        assert version.status == ResultStatus.SUBMITTED

        version = ResultVersionService.validate(
            version=version,
            validation_notes='Looks good',
            validated_by=biologist,
            request=make_request(biologist),
        )

        assert version.status == ResultStatus.VALIDATED
        assert version.validated_by == biologist
        assert version.validated_at is not None
        assert version.validation_notes == 'Looks good'

        item.refresh_from_db()
        assert item.status == ItemStatus.VALIDATED

    def test_validates_all_items_moves_request_to_ready_for_release(
        self, patient, exam_cbc, exam_glu, lab_admin, technician, biologist,
        make_request,
    ):
        ar = _submitted_request(
            patient, lab_admin, technician, make_request,
            [exam_cbc.id, exam_glu.id],
        )
        req = make_request(biologist)

        for item in ar.items.all():
            v = item.result_versions.get(is_current=True)
            ResultVersionService.validate(
                version=v, validation_notes='', validated_by=biologist,
                request=req,
            )

        ar.refresh_from_db()
        assert ar.status == RequestStatus.READY_FOR_RELEASE

    def test_cannot_validate_draft_version(
        self, patient, exam_cbc, lab_admin, technician, biologist, make_request,
    ):
        ar = AnalysisRequestService.create(
            validated_data={
                'patient_id': patient.id,
                'source_type': SourceType.DIRECT_PATIENT,
                'items': [{'exam_definition_id': exam_cbc.id}],
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
            confirm_after=True,
        )
        item = ar.items.first()
        req = make_request(technician)
        AnalysisRequestItemService.mark_collected(
            item=item, collected_by=technician, request=req,
        )
        item.refresh_from_db()
        v = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req,
            result_value='10.0',
        )

        from rest_framework.exceptions import ValidationError
        with pytest.raises(ValidationError):
            ResultVersionService.validate(
                version=v, validation_notes='',
                validated_by=biologist, request=make_request(biologist),
            )

    def test_cannot_validate_already_validated(
        self, patient, exam_cbc, lab_admin, technician, biologist, make_request,
    ):
        ar = _submitted_request(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        v = item.result_versions.get(is_current=True)
        ResultVersionService.validate(
            version=v, validation_notes='', validated_by=biologist,
            request=make_request(biologist),
        )

        from rest_framework.exceptions import ValidationError
        with pytest.raises(ValidationError):
            ResultVersionService.validate(
                version=v, validation_notes='',
                validated_by=biologist, request=make_request(biologist),
            )


# ---------------------------------------------------------------------------
# Service-level: reject
# ---------------------------------------------------------------------------

class TestReject:

    def test_rejects_submitted_result_with_reason(
        self, patient, exam_cbc, lab_admin, technician, biologist, make_request,
    ):
        ar = _submitted_request(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        version = item.result_versions.get(is_current=True)

        version = ResultVersionService.reject(
            version=version,
            rejection_notes='Hemolyzed sample',
            rejected_by=biologist,
            request=make_request(biologist),
        )

        assert version.status == ResultStatus.REJECTED
        assert version.rejected_by == biologist
        assert version.rejected_at is not None
        assert version.rejection_notes == 'Hemolyzed sample'

        item.refresh_from_db()
        assert item.status == ItemStatus.RESULT_ENTERED

    def test_reject_requires_reason(
        self, patient, exam_cbc, lab_admin, technician, biologist, make_request,
    ):
        ar = _submitted_request(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        v = item.result_versions.get(is_current=True)

        bio_client_instance = APIClient(HTTP_HOST='testlab.localhost')
        bio_client_instance.force_authenticate(user=biologist)
        resp = bio_client_instance.post(
            f'{API_RESULTS}/{v.id}/reject/',
            {},
            format='json',
        )
        assert resp.status_code == 400

    def test_cannot_reject_draft_version(
        self, patient, exam_cbc, lab_admin, technician, biologist, make_request,
    ):
        ar = AnalysisRequestService.create(
            validated_data={
                'patient_id': patient.id,
                'source_type': SourceType.DIRECT_PATIENT,
                'items': [{'exam_definition_id': exam_cbc.id}],
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
            confirm_after=True,
        )
        item = ar.items.first()
        req = make_request(technician)
        AnalysisRequestItemService.mark_collected(
            item=item, collected_by=technician, request=req,
        )
        item.refresh_from_db()
        v = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req,
            result_value='10.0',
        )

        from rest_framework.exceptions import ValidationError
        with pytest.raises(ValidationError):
            ResultVersionService.reject(
                version=v, rejection_notes='Bad',
                rejected_by=biologist, request=make_request(biologist),
            )


# ---------------------------------------------------------------------------
# Request-level status derivation
# ---------------------------------------------------------------------------

class TestRequestStatusDerivation:

    def test_all_validated_becomes_ready_for_release(
        self, patient, exam_cbc, exam_glu, lab_admin, technician, biologist,
        make_request,
    ):
        ar = _submitted_request(
            patient, lab_admin, technician, make_request,
            [exam_cbc.id, exam_glu.id],
        )
        req = make_request(biologist)
        for item in ar.items.all():
            v = item.result_versions.get(is_current=True)
            ResultVersionService.validate(
                version=v, validation_notes='', validated_by=biologist,
                request=req,
            )

        ar.refresh_from_db()
        assert ar.status == RequestStatus.READY_FOR_RELEASE

    def test_one_rejected_becomes_retest_required(
        self, patient, exam_cbc, exam_glu, lab_admin, technician, biologist,
        make_request,
    ):
        ar = _submitted_request(
            patient, lab_admin, technician, make_request,
            [exam_cbc.id, exam_glu.id],
        )
        items = list(ar.items.order_by('created_at'))
        req = make_request(biologist)

        ResultVersionService.validate(
            version=items[0].result_versions.get(is_current=True),
            validation_notes='', validated_by=biologist, request=req,
        )
        ResultVersionService.reject(
            version=items[1].result_versions.get(is_current=True),
            rejection_notes='Recheck', rejected_by=biologist, request=req,
        )

        ar.refresh_from_db()
        assert ar.status == RequestStatus.RETEST_REQUIRED

    def test_partial_review_stays_awaiting(
        self, patient, exam_cbc, exam_glu, lab_admin, technician, biologist,
        make_request,
    ):
        ar = _submitted_request(
            patient, lab_admin, technician, make_request,
            [exam_cbc.id, exam_glu.id],
        )
        items = list(ar.items.order_by('created_at'))
        req = make_request(biologist)

        ResultVersionService.validate(
            version=items[0].result_versions.get(is_current=True),
            validation_notes='', validated_by=biologist, request=req,
        )

        ar.refresh_from_db()
        assert ar.status == RequestStatus.AWAITING_REVIEW

    def test_resubmit_after_rejection_returns_to_awaiting(
        self, patient, exam_cbc, lab_admin, technician, biologist, make_request,
    ):
        ar = _submitted_request(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        v = item.result_versions.get(is_current=True)
        req_bio = make_request(biologist)
        req_tech = make_request(technician)

        ResultVersionService.reject(
            version=v, rejection_notes='Bad', rejected_by=biologist,
            request=req_bio,
        )
        ar.refresh_from_db()
        assert ar.status == RequestStatus.RETEST_REQUIRED

        item.refresh_from_db()
        v2 = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req_tech,
            result_value='12.0',
        )
        ResultVersionService.submit(
            version=v2, submitted_by=technician, request=req_tech,
        )

        ar.refresh_from_db()
        assert ar.status == RequestStatus.AWAITING_REVIEW


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

class TestAudit:

    def test_validate_writes_audit(
        self, patient, exam_cbc, lab_admin, technician, biologist, make_request,
    ):
        ar = _submitted_request(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        v = item.result_versions.get(is_current=True)
        AuditLog.objects.all().delete()

        ResultVersionService.validate(
            version=v, validation_notes='Good',
            validated_by=biologist, request=make_request(biologist),
        )

        entry = AuditLog.objects.filter(
            entity_type='ResultVersion', action='VALIDATE',
        ).first()
        assert entry is not None
        assert entry.actor_email == biologist.email
        assert entry.diff['after']['status'] == 'VALIDATED'

    def test_reject_writes_audit(
        self, patient, exam_cbc, lab_admin, technician, biologist, make_request,
    ):
        ar = _submitted_request(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        v = item.result_versions.get(is_current=True)
        AuditLog.objects.all().delete()

        ResultVersionService.reject(
            version=v, rejection_notes='Hemolyzed',
            rejected_by=biologist, request=make_request(biologist),
        )

        entry = AuditLog.objects.filter(
            entity_type='ResultVersion', action='UPDATE',
        ).first()
        assert entry is not None
        assert entry.actor_email == biologist.email
        assert entry.diff['after']['rejection_notes'] == 'Hemolyzed'


# ---------------------------------------------------------------------------
# Endpoint permissions
# ---------------------------------------------------------------------------

class TestEndpointPermissions:

    def test_biologist_can_validate(
        self, bio_client, patient, exam_cbc, lab_admin, technician, biologist,
        make_request,
    ):
        ar = _submitted_request(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        v = item.result_versions.get(is_current=True)

        resp = bio_client.post(
            f'{API_RESULTS}/{v.id}/validate/',
            {'validation_notes': ''},
            format='json',
        )
        assert resp.status_code == 200

    def test_biologist_can_reject(
        self, bio_client, patient, exam_cbc, lab_admin, technician, biologist,
        make_request,
    ):
        ar = _submitted_request(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        v = item.result_versions.get(is_current=True)

        resp = bio_client.post(
            f'{API_RESULTS}/{v.id}/reject/',
            {'rejection_notes': 'Contaminated'},
            format='json',
        )
        assert resp.status_code == 200

    def test_technician_cannot_validate(
        self, tech_client, patient, exam_cbc, lab_admin, technician, make_request,
    ):
        ar = _submitted_request(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        v = item.result_versions.get(is_current=True)

        resp = tech_client.post(
            f'{API_RESULTS}/{v.id}/validate/',
            {'validation_notes': ''},
            format='json',
        )
        assert resp.status_code == 403

    def test_technician_cannot_reject(
        self, tech_client, patient, exam_cbc, lab_admin, technician, make_request,
    ):
        ar = _submitted_request(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        v = item.result_versions.get(is_current=True)

        resp = tech_client.post(
            f'{API_RESULTS}/{v.id}/reject/',
            {'rejection_notes': 'Bad'},
            format='json',
        )
        assert resp.status_code == 403

    def test_viewer_cannot_validate(
        self, viewer_client, patient, exam_cbc, lab_admin, technician, make_request,
    ):
        ar = _submitted_request(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        v = item.result_versions.get(is_current=True)

        resp = viewer_client.post(
            f'{API_RESULTS}/{v.id}/validate/',
            {'validation_notes': ''},
            format='json',
        )
        assert resp.status_code == 403
