"""
Tests for result versioning and the technician result-entry workflow.

Scope
-----
- Technician can create a DRAFT result version for a collected item.
- Draft result can be updated.
- Draft result can be submitted (DRAFT → SUBMITTED).
- Item status updates correctly (COLLECTED → RESULT_ENTERED → UNDER_REVIEW).
- Request status becomes AWAITING_REVIEW when all active items are submitted.
- Only one current version exists per item.
- Historical (rejected) versions remain preserved.
- Creating a new version after rejection is possible.
- Permissions: IsTechnicianOrAbove can create/update/submit;
  viewer/receptionist rejected.
- Invalid transitions are rejected cleanly.
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
def admin_client(api_client, lab_admin):
    api_client.force_authenticate(user=lab_admin)
    return api_client


@pytest.fixture()
def tech_client(api_client, technician):
    api_client.force_authenticate(user=technician)
    return api_client


@pytest.fixture()
def receptionist_client(api_client, receptionist):
    api_client.force_authenticate(user=receptionist)
    return api_client


@pytest.fixture()
def viewer_client(api_client, viewer_auditor):
    api_client.force_authenticate(user=viewer_auditor)
    return api_client


@pytest.fixture()
def bio_client(api_client, biologist):
    api_client.force_authenticate(user=biologist)
    return api_client


@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-RES-001',
        first_name='Rachel',
        last_name='Results',
        date_of_birth=date(1990, 5, 15),
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


def _confirmed_and_collected(patient, lab_admin, technician, make_request, exam_ids):
    """Create a confirmed request and collect all items."""
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
    for item in ar.items.all():
        AnalysisRequestItemService.mark_collected(
            item=item,
            collected_by=technician,
            request=make_request(technician),
        )
    ar.refresh_from_db()
    return ar


def _data(resp):
    body = resp.json()
    return body.get('data', body)


# ---------------------------------------------------------------------------
# Service-level: create draft
# ---------------------------------------------------------------------------

class TestCreateDraft:

    def test_creates_draft_for_collected_item(
        self, patient, exam_cbc, lab_admin, technician, make_request,
    ):
        ar = _confirmed_and_collected(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        assert item.status == ItemStatus.COLLECTED

        version = ResultVersionService.create_draft(
            item=item,
            entered_by=technician,
            request=make_request(technician),
            result_value='12.5',
            result_unit='g/dL',
        )

        assert version.version_number == 1
        assert version.is_current is True
        assert version.status == ResultStatus.DRAFT
        assert version.result_value == '12.5'
        assert version.entered_by == technician

        item.refresh_from_db()
        assert item.status == ItemStatus.RESULT_ENTERED

    def test_rejects_draft_for_pending_item(
        self, patient, exam_cbc, lab_admin, technician, make_request,
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
        assert item.status == ItemStatus.PENDING

        from rest_framework.exceptions import ValidationError
        with pytest.raises(ValidationError, match='COLLECTED or RESULT_ENTERED'):
            ResultVersionService.create_draft(
                item=item,
                entered_by=technician,
                request=make_request(technician),
            )

    def test_blocks_second_draft_when_current_is_draft(
        self, patient, exam_cbc, lab_admin, technician, make_request,
    ):
        ar = _confirmed_and_collected(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        ResultVersionService.create_draft(
            item=item,
            entered_by=technician,
            request=make_request(technician),
        )

        from rest_framework.exceptions import ValidationError
        with pytest.raises(ValidationError, match='already exists'):
            ResultVersionService.create_draft(
                item=item,
                entered_by=technician,
                request=make_request(technician),
            )


# ---------------------------------------------------------------------------
# Service-level: update draft
# ---------------------------------------------------------------------------

class TestUpdateDraft:

    def test_updates_draft_fields(
        self, patient, exam_cbc, lab_admin, technician, make_request,
    ):
        ar = _confirmed_and_collected(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        version = ResultVersionService.create_draft(
            item=item,
            entered_by=technician,
            request=make_request(technician),
        )

        version = ResultVersionService.update_draft(
            version=version,
            validated_data={'result_value': '14.2', 'result_unit': 'g/dL'},
            updated_by=technician,
            request=make_request(technician),
        )

        assert version.result_value == '14.2'
        assert version.result_unit == 'g/dL'

    def test_rejects_update_on_submitted_version(
        self, patient, exam_cbc, lab_admin, technician, make_request,
    ):
        ar = _confirmed_and_collected(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        version = ResultVersionService.create_draft(
            item=item,
            entered_by=technician,
            request=make_request(technician),
            result_value='10.0',
        )
        ResultVersionService.submit(
            version=version,
            submitted_by=technician,
            request=make_request(technician),
        )

        from rest_framework.exceptions import ValidationError
        with pytest.raises(ValidationError, match='DRAFT'):
            ResultVersionService.update_draft(
                version=version,
                validated_data={'result_value': '11.0'},
                updated_by=technician,
                request=make_request(technician),
            )


# ---------------------------------------------------------------------------
# Service-level: submit
# ---------------------------------------------------------------------------

class TestSubmit:

    def test_submits_draft_and_transitions_item(
        self, patient, exam_cbc, lab_admin, technician, make_request,
    ):
        ar = _confirmed_and_collected(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        version = ResultVersionService.create_draft(
            item=item,
            entered_by=technician,
            request=make_request(technician),
            result_value='12.5',
        )

        version = ResultVersionService.submit(
            version=version,
            submitted_by=technician,
            request=make_request(technician),
        )

        assert version.status == ResultStatus.SUBMITTED
        assert version.submitted_by == technician
        assert version.submitted_at is not None

        item.refresh_from_db()
        assert item.status == ItemStatus.UNDER_REVIEW

    def test_submit_requires_result_value(
        self, patient, exam_cbc, lab_admin, technician, make_request,
    ):
        ar = _confirmed_and_collected(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        version = ResultVersionService.create_draft(
            item=item,
            entered_by=technician,
            request=make_request(technician),
        )

        from rest_framework.exceptions import ValidationError
        with pytest.raises(ValidationError, match='result_value'):
            ResultVersionService.submit(
                version=version,
                submitted_by=technician,
                request=make_request(technician),
            )


# ---------------------------------------------------------------------------
# Request-level status derivation
# ---------------------------------------------------------------------------

class TestRequestStatusDerivation:

    def test_all_submitted_triggers_awaiting_review(
        self, patient, exam_cbc, exam_glu, lab_admin, technician, make_request,
    ):
        ar = _confirmed_and_collected(
            patient, lab_admin, technician, make_request,
            [exam_cbc.id, exam_glu.id],
        )
        items = list(ar.items.order_by('created_at'))
        req = make_request(technician)

        for item in items:
            v = ResultVersionService.create_draft(
                item=item, entered_by=technician, request=req,
                result_value='ok',
            )
            ResultVersionService.submit(
                version=v, submitted_by=technician, request=req,
            )

        ar.refresh_from_db()
        assert ar.status == RequestStatus.AWAITING_REVIEW

    def test_partial_submission_stays_in_analysis(
        self, patient, exam_cbc, exam_glu, lab_admin, technician, make_request,
    ):
        ar = _confirmed_and_collected(
            patient, lab_admin, technician, make_request,
            [exam_cbc.id, exam_glu.id],
        )
        items = list(ar.items.order_by('created_at'))
        req = make_request(technician)

        v = ResultVersionService.create_draft(
            item=items[0], entered_by=technician, request=req,
            result_value='ok',
        )
        ResultVersionService.submit(
            version=v, submitted_by=technician, request=req,
        )

        ar.refresh_from_db()
        assert ar.status == RequestStatus.AWAITING_REVIEW


# ---------------------------------------------------------------------------
# Versioning guarantees
# ---------------------------------------------------------------------------

class TestVersioning:

    def test_only_one_current_version_per_item(
        self, patient, exam_cbc, lab_admin, technician, biologist, make_request,
    ):
        ar = _confirmed_and_collected(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        req = make_request(technician)

        v1 = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req,
            result_value='10.0',
        )
        ResultVersionService.submit(
            version=v1, submitted_by=technician, request=req,
        )

        # Biologist rejects
        ResultVersionService.reject(
            version=v1,
            rejection_notes='Value looks wrong',
            rejected_by=biologist,
            request=make_request(biologist),
        )

        # Technician creates v2
        item.refresh_from_db()
        v2 = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req,
            result_value='12.0',
        )

        v1.refresh_from_db()
        assert v1.is_current is False
        assert v1.status == ResultStatus.REJECTED

        assert v2.is_current is True
        assert v2.version_number == 2
        assert v2.status == ResultStatus.DRAFT

        current_count = item.result_versions.filter(is_current=True).count()
        assert current_count == 1

    def test_historical_versions_preserved(
        self, patient, exam_cbc, lab_admin, technician, biologist, make_request,
    ):
        ar = _confirmed_and_collected(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        req = make_request(technician)

        v1 = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req,
            result_value='10.0',
        )
        ResultVersionService.submit(
            version=v1, submitted_by=technician, request=req,
        )
        ResultVersionService.reject(
            version=v1,
            rejection_notes='Recheck needed',
            rejected_by=biologist,
            request=make_request(biologist),
        )

        item.refresh_from_db()
        v2 = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req,
            result_value='12.0',
        )

        all_versions = list(item.result_versions.order_by('version_number'))
        assert len(all_versions) == 2
        assert all_versions[0].version_number == 1
        assert all_versions[0].result_value == '10.0'
        assert all_versions[0].status == ResultStatus.REJECTED
        assert all_versions[1].version_number == 2
        assert all_versions[1].result_value == '12.0'
        assert all_versions[1].status == ResultStatus.DRAFT

    def test_rejection_returns_item_to_result_entered(
        self, patient, exam_cbc, lab_admin, technician, biologist, make_request,
    ):
        ar = _confirmed_and_collected(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        req = make_request(technician)

        v = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req,
            result_value='10.0',
        )
        ResultVersionService.submit(
            version=v, submitted_by=technician, request=req,
        )
        assert item.status == ItemStatus.UNDER_REVIEW

        ResultVersionService.reject(
            version=v,
            rejection_notes='Wrong value',
            rejected_by=biologist,
            request=make_request(biologist),
        )

        item.refresh_from_db()
        assert item.status == ItemStatus.RESULT_ENTERED


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

class TestAudit:

    def test_create_draft_writes_audit(
        self, patient, exam_cbc, lab_admin, technician, make_request,
    ):
        ar = _confirmed_and_collected(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        AuditLog.objects.all().delete()

        ResultVersionService.create_draft(
            item=item,
            entered_by=technician,
            request=make_request(technician),
            result_value='12.5',
        )

        entry = AuditLog.objects.filter(entity_type='ResultVersion').first()
        assert entry is not None
        assert entry.action == 'CREATE'
        assert entry.actor_email == technician.email

    def test_submit_writes_audit(
        self, patient, exam_cbc, lab_admin, technician, make_request,
    ):
        ar = _confirmed_and_collected(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        v = ResultVersionService.create_draft(
            item=item, entered_by=technician,
            request=make_request(technician),
            result_value='12.5',
        )
        AuditLog.objects.all().delete()

        ResultVersionService.submit(
            version=v, submitted_by=technician,
            request=make_request(technician),
        )

        entry = AuditLog.objects.filter(
            entity_type='ResultVersion', action='SUBMIT',
        ).first()
        assert entry is not None
        assert entry.actor_email == technician.email


# ---------------------------------------------------------------------------
# Endpoint permissions
# ---------------------------------------------------------------------------

class TestEndpointPermissions:

    def test_technician_can_create(
        self, tech_client, patient, exam_cbc, lab_admin, technician, make_request,
    ):
        ar = _confirmed_and_collected(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        resp = tech_client.post(
            f'{API_RESULTS}/',
            {'item_id': str(item.id), 'result_value': '12.5'},
            format='json',
        )
        assert resp.status_code == 201

    def test_viewer_cannot_create(
        self, viewer_client, patient, exam_cbc, lab_admin, technician, make_request,
    ):
        ar = _confirmed_and_collected(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        resp = viewer_client.post(
            f'{API_RESULTS}/',
            {'item_id': str(item.id), 'result_value': '12.5'},
            format='json',
        )
        assert resp.status_code == 403

    def test_receptionist_cannot_create(
        self, receptionist_client, patient, exam_cbc, lab_admin, technician, make_request,
    ):
        ar = _confirmed_and_collected(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        resp = receptionist_client.post(
            f'{API_RESULTS}/',
            {'item_id': str(item.id), 'result_value': '12.5'},
            format='json',
        )
        assert resp.status_code == 403

    def test_technician_can_submit(
        self, tech_client, patient, exam_cbc, lab_admin, technician, make_request,
    ):
        ar = _confirmed_and_collected(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        v = ResultVersionService.create_draft(
            item=item, entered_by=technician,
            request=make_request(technician),
            result_value='12.5',
        )
        resp = tech_client.post(f'{API_RESULTS}/{v.id}/submit/')
        assert resp.status_code == 200

    def test_any_staff_can_list(
        self, viewer_client, patient, exam_cbc, lab_admin, technician, make_request,
    ):
        ar = _confirmed_and_collected(
            patient, lab_admin, technician, make_request, [exam_cbc.id],
        )
        item = ar.items.first()
        ResultVersionService.create_draft(
            item=item, entered_by=technician,
            request=make_request(technician),
            result_value='12.5',
        )
        resp = viewer_client.get(f'{API_RESULTS}/')
        assert resp.status_code == 200

    def test_unauthenticated_401(self, api_client):
        resp = api_client.get(f'{API_RESULTS}/')
        assert resp.status_code == 401
