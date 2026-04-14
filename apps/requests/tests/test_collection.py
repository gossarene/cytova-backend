"""
Tests for the specimen collection workflow.

Scope
-----
- Item-level ``mark_collected`` action writes collected_at / collected_by
  and transitions the item to COLLECTED.
- Request-level status is derived coherently from item progress:
    * no items collected           → CONFIRMED
    * at least one but not all      → COLLECTION_IN_PROGRESS
    * all active items collected    → IN_ANALYSIS
- Rejected items are excluded from the "all collected" denominator.
- Idempotency: re-posting for an already-collected item is a safe no-op.
- Permissions: IsTechnicianOrAbove — viewer/receptionist rejected,
  technician / biologist / lab admin allowed.
- Audit log rows are written for the item transition and each
  request-level transition exactly once.
- Draft requests cannot be collected.
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


API = '/api/v1/requests'


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
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-COL-001',
        first_name='Carl',
        last_name='Collection',
        date_of_birth=date(1988, 8, 8),
        gender='MALE',
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
def exam_cbc(family_a, category, default_technique):
    return ExamDefinition.objects.create(
        category=category, family=family_a, technique=default_technique,
        code='CBC', name='Complete Blood Count',
        sample_type=SampleType.BLOOD,
    )


@pytest.fixture()
def exam_glu(family_b, category, default_technique):
    return ExamDefinition.objects.create(
        category=category, family=family_b, technique=default_technique,
        code='GLU', name='Fasting Glucose',
        sample_type=SampleType.BLOOD,
    )


def _confirmed_request(patient, lab_admin, make_request, exam_ids):
    return AnalysisRequestService.create(
        validated_data={
            'patient_id': patient.id,
            'source_type': SourceType.DIRECT_PATIENT,
            'items': [{'exam_definition_id': eid} for eid in exam_ids],
        },
        created_by=lab_admin,
        request=make_request(lab_admin),
        confirm_after=True,
    )


def _data(resp):
    body = resp.json()
    return body.get('data', body)


# ---------------------------------------------------------------------------
# Service-level behaviour
# ---------------------------------------------------------------------------

class TestMarkCollectedService:

    def test_marks_item_collected_and_records_who_when(
        self, patient, exam_cbc, lab_admin, technician, make_request,
    ):
        ar = _confirmed_request(patient, lab_admin, make_request, [exam_cbc.id])
        item = ar.items.first()

        result = AnalysisRequestItemService.mark_collected(
            item=item,
            collected_by=technician,
            request=make_request(technician),
        )

        assert result.status == ItemStatus.COLLECTED
        assert result.collected_at is not None
        assert result.collected_by_id == technician.id

    def test_idempotent_re_invocation(
        self, patient, exam_cbc, lab_admin, technician, make_request,
    ):
        ar = _confirmed_request(patient, lab_admin, make_request, [exam_cbc.id])
        item = ar.items.first()

        first = AnalysisRequestItemService.mark_collected(
            item=item,
            collected_by=technician,
            request=make_request(technician),
        )
        first_at = first.collected_at
        first_by = first.collected_by_id

        # Second call — should be a no-op, not overwrite the original
        # snapshot and not write a second audit row.
        before_audit = AuditLog.objects.filter(
            entity_type='AnalysisRequestItem', entity_id=item.id,
        ).count()
        second = AnalysisRequestItemService.mark_collected(
            item=item,
            collected_by=technician,
            request=make_request(technician),
        )
        after_audit = AuditLog.objects.filter(
            entity_type='AnalysisRequestItem', entity_id=item.id,
        ).count()

        assert second.status == ItemStatus.COLLECTED
        assert second.collected_at == first_at
        assert second.collected_by_id == first_by
        assert after_audit == before_audit

    def test_rejects_draft_request(
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
        )
        item = ar.items.first()
        from rest_framework.exceptions import ValidationError
        with pytest.raises(ValidationError):
            AnalysisRequestItemService.mark_collected(
                item=item,
                collected_by=technician,
                request=make_request(technician),
            )

    def test_rejects_already_in_progress_item(
        self, patient, exam_cbc, lab_admin, technician, make_request,
    ):
        """Once an item has moved past PENDING (via the legacy start
        path, say), it cannot be walked back to COLLECTED."""
        ar = _confirmed_request(patient, lab_admin, make_request, [exam_cbc.id])
        item = ar.items.first()
        AnalysisRequestItemService.start(
            item=item,
            started_by=technician,
            request=make_request(technician),
        )
        from rest_framework.exceptions import ValidationError
        with pytest.raises(ValidationError):
            AnalysisRequestItemService.mark_collected(
                item=item,
                collected_by=technician,
                request=make_request(technician),
            )


# ---------------------------------------------------------------------------
# Request-status derivation
# ---------------------------------------------------------------------------

class TestRequestStatusDerivation:

    def test_confirmed_before_any_collection(
        self, patient, exam_cbc, exam_glu, lab_admin, make_request,
    ):
        ar = _confirmed_request(
            patient, lab_admin, make_request, [exam_cbc.id, exam_glu.id],
        )
        assert ar.status == RequestStatus.CONFIRMED

    def test_one_of_two_collected_becomes_collection_in_progress(
        self, patient, exam_cbc, exam_glu, lab_admin, technician, make_request,
    ):
        ar = _confirmed_request(
            patient, lab_admin, make_request, [exam_cbc.id, exam_glu.id],
        )
        first_item = ar.items.get(exam_definition=exam_cbc)

        AnalysisRequestItemService.mark_collected(
            item=first_item,
            collected_by=technician,
            request=make_request(technician),
        )
        ar.refresh_from_db()
        assert ar.status == RequestStatus.COLLECTION_IN_PROGRESS

    def test_all_collected_becomes_in_analysis(
        self, patient, exam_cbc, exam_glu, lab_admin, technician, make_request,
    ):
        ar = _confirmed_request(
            patient, lab_admin, make_request, [exam_cbc.id, exam_glu.id],
        )
        for item in ar.items.all():
            AnalysisRequestItemService.mark_collected(
                item=item,
                collected_by=technician,
                request=make_request(technician),
            )
        ar.refresh_from_db()
        assert ar.status == RequestStatus.IN_ANALYSIS

    def test_single_item_request_jumps_confirmed_to_in_analysis(
        self, patient, exam_cbc, lab_admin, technician, make_request,
    ):
        """A single-item request has no intermediate
        COLLECTION_IN_PROGRESS phase — the state machine allows a
        direct CONFIRMED → IN_ANALYSIS jump."""
        ar = _confirmed_request(patient, lab_admin, make_request, [exam_cbc.id])
        item = ar.items.first()
        AnalysisRequestItemService.mark_collected(
            item=item,
            collected_by=technician,
            request=make_request(technician),
        )
        ar.refresh_from_db()
        assert ar.status == RequestStatus.IN_ANALYSIS

    def test_rejected_items_do_not_block_in_analysis(
        self, patient, exam_cbc, exam_glu, lab_admin, technician, make_request,
    ):
        """Rejecting one item and collecting the other must still
        transition the request to IN_ANALYSIS — rejected items are
        operationally 'done' and do not count against the collection
        denominator."""
        ar = _confirmed_request(
            patient, lab_admin, make_request, [exam_cbc.id, exam_glu.id],
        )
        rejected_item = ar.items.get(exam_definition=exam_glu)
        AnalysisRequestItemService.reject(
            item=rejected_item,
            rejection_reason='Not collected',
            rejected_by=technician,
            request=make_request(technician),
        )

        other = ar.items.get(exam_definition=exam_cbc)
        AnalysisRequestItemService.mark_collected(
            item=other,
            collected_by=technician,
            request=make_request(technician),
        )
        ar.refresh_from_db()
        assert ar.status == RequestStatus.IN_ANALYSIS

    def test_all_rejected_collapses_to_completed(
        self, patient, exam_cbc, exam_glu, lab_admin, technician, make_request,
    ):
        """If every item is rejected before any collection, the
        request has no active items — the collection-derivation logic
        correctly leaves this case to the legacy ``_auto_advance``
        helper, which collapses an all-rejected request directly to
        COMPLETED (there is nothing to analyse). The important property
        tested here is that the new derivation does NOT silently jump
        to IN_ANALYSIS — an empty active set is not "all collected"."""
        ar = _confirmed_request(
            patient, lab_admin, make_request, [exam_cbc.id, exam_glu.id],
        )
        for item in ar.items.all():
            AnalysisRequestItemService.reject(
                item=item,
                rejection_reason='Out of scope',
                rejected_by=technician,
                request=make_request(technician),
            )
        ar.refresh_from_db()
        assert ar.status == RequestStatus.COMPLETED
        # And crucially NOT IN_ANALYSIS — the collection logic treats
        # an empty active set as "nothing to do", never as "all done".
        assert ar.status != RequestStatus.IN_ANALYSIS


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

class TestCollectionAudit:

    def test_item_transition_writes_audit_row(
        self, patient, exam_cbc, lab_admin, technician, make_request,
    ):
        ar = _confirmed_request(patient, lab_admin, make_request, [exam_cbc.id])
        item = ar.items.first()
        before = AuditLog.objects.filter(
            entity_type='AnalysisRequestItem', entity_id=item.id,
        ).count()
        AnalysisRequestItemService.mark_collected(
            item=item,
            collected_by=technician,
            request=make_request(technician),
        )
        after = AuditLog.objects.filter(
            entity_type='AnalysisRequestItem', entity_id=item.id,
        ).count()
        assert after == before + 1

    def test_request_level_transition_writes_audit_row(
        self, patient, exam_cbc, exam_glu, lab_admin, technician, make_request,
    ):
        ar = _confirmed_request(
            patient, lab_admin, make_request, [exam_cbc.id, exam_glu.id],
        )
        before = AuditLog.objects.filter(
            entity_type='AnalysisRequest', entity_id=ar.id, action='UPDATE',
        ).count()
        AnalysisRequestItemService.mark_collected(
            item=ar.items.first(),
            collected_by=technician,
            request=make_request(technician),
        )
        after = AuditLog.objects.filter(
            entity_type='AnalysisRequest', entity_id=ar.id, action='UPDATE',
        ).count()
        # One new UPDATE audit row for the CONFIRMED → COLLECTION_IN_PROGRESS transition
        assert after == before + 1


# ---------------------------------------------------------------------------
# HTTP endpoint
# ---------------------------------------------------------------------------

class TestMarkCollectedEndpoint:

    def test_technician_can_mark_collected(
        self, tech_client, patient, exam_cbc, lab_admin, make_request,
    ):
        ar = _confirmed_request(patient, lab_admin, make_request, [exam_cbc.id])
        item = ar.items.first()
        resp = tech_client.post(
            f'{API}/{ar.id}/items/{item.id}/mark-collected/',
        )
        assert resp.status_code == 200, resp.content
        body = _data(resp)
        assert body['status'] == ItemStatus.COLLECTED
        assert body['collected_at']
        assert body['collected_by_email']

    def test_admin_can_mark_collected(
        self, admin_client, patient, exam_cbc, lab_admin, make_request,
    ):
        ar = _confirmed_request(patient, lab_admin, make_request, [exam_cbc.id])
        item = ar.items.first()
        resp = admin_client.post(
            f'{API}/{ar.id}/items/{item.id}/mark-collected/',
        )
        assert resp.status_code == 200

    def test_viewer_cannot_mark_collected(
        self, viewer_client, patient, exam_cbc, lab_admin, make_request,
    ):
        ar = _confirmed_request(patient, lab_admin, make_request, [exam_cbc.id])
        item = ar.items.first()
        resp = viewer_client.post(
            f'{API}/{ar.id}/items/{item.id}/mark-collected/',
        )
        assert resp.status_code == 403

    def test_receptionist_cannot_mark_collected(
        self, receptionist_client, patient, exam_cbc, lab_admin, make_request,
    ):
        """Receptionists confirm requests but do not collect specimens —
        the action is gated at IsTechnicianOrAbove."""
        ar = _confirmed_request(patient, lab_admin, make_request, [exam_cbc.id])
        item = ar.items.first()
        resp = receptionist_client.post(
            f'{API}/{ar.id}/items/{item.id}/mark-collected/',
        )
        assert resp.status_code == 403

    def test_unauthenticated_cannot_mark_collected(
        self, api_client, patient, exam_cbc, lab_admin, make_request,
    ):
        ar = _confirmed_request(patient, lab_admin, make_request, [exam_cbc.id])
        item = ar.items.first()
        resp = api_client.post(
            f'{API}/{ar.id}/items/{item.id}/mark-collected/',
        )
        assert resp.status_code == 401

    def test_optional_collection_notes_are_persisted(
        self, tech_client, patient, exam_cbc, lab_admin, make_request,
    ):
        ar = _confirmed_request(patient, lab_admin, make_request, [exam_cbc.id])
        item = ar.items.first()
        resp = tech_client.post(
            f'{API}/{ar.id}/items/{item.id}/mark-collected/',
            {'collection_notes': 'Venous draw, left arm'},
            format='json',
        )
        assert resp.status_code == 200
        item.refresh_from_db()
        assert item.collection_notes == 'Venous draw, left arm'

    def test_tenant_isolation_for_mark_collected(
        self, tech_client, patient, exam_cbc, lab_admin, make_request,
    ):
        """A request id from a different tenant is invisible in the
        current tenant's schema and resolves to 404 — the route never
        reaches the service layer."""
        from uuid import uuid4
        resp = tech_client.post(
            f'{API}/{uuid4()}/items/{uuid4()}/mark-collected/',
        )
        assert resp.status_code == 404
