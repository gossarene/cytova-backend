"""
Tests for biologist editing of the patient-facing Comments field
during review, and locking after final request validation.
"""
from datetime import date

import pytest
from django_tenants.utils import schema_context, get_public_schema_name
from rest_framework.test import APIClient

from apps.audit.models import AuditLog
from apps.catalog.models import ExamCategory, ExamDefinition, ExamFamily, ExamTechnique, SampleType
from apps.patients.models import Patient
from apps.requests.models import RequestStatus, SourceType
from apps.requests.services import AnalysisRequestItemService, AnalysisRequestService
from apps.results.models import ResultStatus
from apps.results.services import ResultVersionService


API = '/api/v1/results'


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
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-CMT-001',
        first_name='Carla',
        last_name='Comments',
        date_of_birth=date(1993, 4, 10),
        gender='FEMALE',
        created_by=lab_admin,
    )


@pytest.fixture()
def category():
    return ExamCategory.objects.create(name='Labs', display_order=1)


@pytest.fixture()
def family():
    return ExamFamily.objects.create(name='Hematology', display_order=1)


@pytest.fixture()
def exam(family, category, default_technique):
    return ExamDefinition.objects.create(
        family=family, category=category, technique=default_technique,
        code='HGB', name='Hemoglobin',
        sample_type=SampleType.BLOOD, unit='g/dL',
    )


def _submitted_version(patient, lab_admin, technician, make_request, exam):
    ar = AnalysisRequestService.create(
        validated_data={
            'patient_id': patient.id,
            'source_type': SourceType.DIRECT_PATIENT,
            'items': [{'exam_definition_id': exam.id}],
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
        result_value='14.5',
        values=[{'value': '14.5'}],
    )
    ResultVersionService.submit(version=v, submitted_by=technician, request=req)
    v.refresh_from_db()
    return v, ar


# ---------------------------------------------------------------------------
# Service-level: comment editing during review
# ---------------------------------------------------------------------------

class TestReviewCommentEditing:

    def test_biologist_can_edit_comments_on_submitted(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        v, ar = _submitted_version(patient, lab_admin, technician, make_request, exam)
        assert v.status == ResultStatus.SUBMITTED

        v = ResultVersionService.update_review_comments(
            version=v,
            validated_data={'comments': 'Patient note updated'},
            updated_by=biologist,
            request=make_request(biologist),
        )
        assert v.comments == 'Patient note updated'

    def test_biologist_can_edit_comments_on_validated(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        v, ar = _submitted_version(patient, lab_admin, technician, make_request, exam)
        ResultVersionService.validate(
            version=v, validation_notes='', validated_by=biologist,
            request=make_request(biologist),
        )
        v.refresh_from_db()
        assert v.status == ResultStatus.VALIDATED

        v = ResultVersionService.update_review_comments(
            version=v,
            validated_data={'comments': 'Final patient note'},
            updated_by=biologist,
            request=make_request(biologist),
        )
        assert v.comments == 'Final patient note'

    def test_internal_notes_remains_distinct(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        v, ar = _submitted_version(patient, lab_admin, technician, make_request, exam)
        v = ResultVersionService.update_review_comments(
            version=v,
            validated_data={
                'comments': 'Patient-facing',
                'validation_notes': 'Biologist internal',
            },
            updated_by=biologist,
            request=make_request(biologist),
        )
        assert v.comments == 'Patient-facing'
        assert v.validation_notes == 'Biologist internal'
        assert v.internal_notes != v.comments


# ---------------------------------------------------------------------------
# Locking after final validation
# ---------------------------------------------------------------------------

class TestPostFinalizationLocking:

    def test_comments_blocked_after_request_validated(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        v, ar = _submitted_version(patient, lab_admin, technician, make_request, exam)
        req_bio = make_request(biologist)

        ResultVersionService.validate(
            version=v, validation_notes='', validated_by=biologist,
            request=req_bio,
        )
        ar.refresh_from_db()
        AnalysisRequestService.finalize_validation(
            analysis_request=ar, finalized_by=biologist, request=req_bio,
        )
        ar.refresh_from_db()
        assert ar.status == RequestStatus.VALIDATED

        v.refresh_from_db()
        from rest_framework.exceptions import ValidationError
        with pytest.raises(ValidationError, match='finalized'):
            ResultVersionService.update_review_comments(
                version=v,
                validated_data={'comments': 'Too late'},
                updated_by=biologist,
                request=req_bio,
            )

    def test_draft_comments_not_editable_via_review(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        ar = AnalysisRequestService.create(
            validated_data={
                'patient_id': patient.id,
                'source_type': SourceType.DIRECT_PATIENT,
                'items': [{'exam_definition_id': exam.id}],
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
            result_value='14.5',
        )

        from rest_framework.exceptions import ValidationError
        with pytest.raises(ValidationError, match='SUBMITTED or VALIDATED'):
            ResultVersionService.update_review_comments(
                version=v,
                validated_data={'comments': 'Not allowed'},
                updated_by=biologist,
                request=make_request(biologist),
            )


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

class TestPermissions:

    def test_technician_cannot_edit_review_comments(
        self, tech_client, patient, exam, lab_admin, technician, make_request,
    ):
        v, ar = _submitted_version(patient, lab_admin, technician, make_request, exam)
        resp = tech_client.patch(
            f'{API}/{v.id}/review-comments/',
            {'comments': 'Unauthorized'},
            format='json',
        )
        assert resp.status_code == 403

    def test_biologist_can_edit_via_endpoint(
        self, bio_client, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        v, ar = _submitted_version(patient, lab_admin, technician, make_request, exam)
        resp = bio_client.patch(
            f'{API}/{v.id}/review-comments/',
            {'comments': 'Edited via API'},
            format='json',
        )
        assert resp.status_code == 200
        body = resp.json().get('data', resp.json())
        assert body['comments'] == 'Edited via API'


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

class TestAudit:

    def test_comment_update_writes_audit(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        v, ar = _submitted_version(patient, lab_admin, technician, make_request, exam)
        AuditLog.objects.all().delete()

        ResultVersionService.update_review_comments(
            version=v,
            validated_data={'comments': 'Audited note'},
            updated_by=biologist,
            request=make_request(biologist),
        )

        entry = AuditLog.objects.filter(
            entity_type='ResultVersion', action='UPDATE',
        ).first()
        assert entry is not None
        assert entry.actor_email == biologist.email
        assert entry.diff['after']['comments'] == 'Audited note'
