"""
Tests for request-level status derivation during biologist review.

Verifies that the centralized derivation correctly handles mixed
item states across multi-item requests — the main bug this fixes
is: rejecting one item incorrectly pushed the request to IN_ANALYSIS
when other items were still UNDER_REVIEW.
"""
from datetime import date

import pytest
from django_tenants.utils import schema_context, get_public_schema_name

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
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-DER-001',
        first_name='Diana',
        last_name='Derivation',
        date_of_birth=date(1992, 7, 10),
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
def exam_a(family, category, default_technique):
    return ExamDefinition.objects.create(
        category=category, family=family, technique=default_technique,
        code='EXA', name='Exam A', sample_type=SampleType.BLOOD,
    )


@pytest.fixture()
def exam_b(family, category, default_technique):
    return ExamDefinition.objects.create(
        category=category, family=family, technique=default_technique,
        code='EXB', name='Exam B', sample_type=SampleType.BLOOD,
    )


@pytest.fixture()
def exam_c(family, category, default_technique):
    return ExamDefinition.objects.create(
        category=category, family=family, technique=default_technique,
        code='EXC', name='Exam C', sample_type=SampleType.BLOOD,
    )


def _submitted_request(patient, lab_admin, technician, make_request, exam_ids):
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
            item=item, entered_by=technician, request=req, result_value='ok',
        )
        ResultVersionService.submit(version=v, submitted_by=technician, request=req)
    ar.refresh_from_db()
    return ar


# ---------------------------------------------------------------------------
# Multi-item review derivation
# ---------------------------------------------------------------------------

class TestMultiItemReviewDerivation:

    def test_all_submitted_is_awaiting_review(
        self, patient, exam_a, exam_b, lab_admin, technician, make_request,
    ):
        ar = _submitted_request(
            patient, lab_admin, technician, make_request,
            [exam_a.id, exam_b.id],
        )
        assert ar.status == RequestStatus.AWAITING_REVIEW

    def test_first_reject_keeps_awaiting_review_when_others_pending(
        self, patient, exam_a, exam_b, lab_admin, technician, biologist, make_request,
    ):
        """Rejecting one item while another is still UNDER_REVIEW keeps
        the request in AWAITING_REVIEW — not IN_ANALYSIS."""
        ar = _submitted_request(
            patient, lab_admin, technician, make_request,
            [exam_a.id, exam_b.id],
        )
        items = list(ar.items.order_by('created_at'))
        req = make_request(biologist)

        ResultVersionService.reject(
            version=items[0].result_versions.get(is_current=True),
            rejection_notes='Hemolyzed', rejected_by=biologist, request=req,
        )

        ar.refresh_from_db()
        assert ar.status == RequestStatus.AWAITING_REVIEW

    def test_second_reject_on_remaining_item_succeeds(
        self, patient, exam_a, exam_b, lab_admin, technician, biologist, make_request,
    ):
        """Both items can be rejected independently."""
        ar = _submitted_request(
            patient, lab_admin, technician, make_request,
            [exam_a.id, exam_b.id],
        )
        items = list(ar.items.order_by('created_at'))
        req = make_request(biologist)

        ResultVersionService.reject(
            version=items[0].result_versions.get(is_current=True),
            rejection_notes='Hemolyzed', rejected_by=biologist, request=req,
        )
        ResultVersionService.reject(
            version=items[1].result_versions.get(is_current=True),
            rejection_notes='Clotted', rejected_by=biologist, request=req,
        )

        ar.refresh_from_db()
        assert ar.status == RequestStatus.RETEST_REQUIRED

    def test_all_rejected_becomes_retest_required(
        self, patient, exam_a, exam_b, lab_admin, technician, biologist, make_request,
    ):
        ar = _submitted_request(
            patient, lab_admin, technician, make_request,
            [exam_a.id, exam_b.id],
        )
        req = make_request(biologist)
        for item in ar.items.all():
            ResultVersionService.reject(
                version=item.result_versions.get(is_current=True),
                rejection_notes='Redo', rejected_by=biologist, request=req,
            )

        ar.refresh_from_db()
        assert ar.status == RequestStatus.RETEST_REQUIRED

    def test_mixed_validated_and_rejected_becomes_retest_required(
        self, patient, exam_a, exam_b, lab_admin, technician, biologist, make_request,
    ):
        ar = _submitted_request(
            patient, lab_admin, technician, make_request,
            [exam_a.id, exam_b.id],
        )
        items = list(ar.items.order_by('created_at'))
        req = make_request(biologist)

        ResultVersionService.validate(
            version=items[0].result_versions.get(is_current=True),
            validation_notes='', validated_by=biologist, request=req,
        )
        ResultVersionService.reject(
            version=items[1].result_versions.get(is_current=True),
            rejection_notes='Redo', rejected_by=biologist, request=req,
        )

        ar.refresh_from_db()
        assert ar.status == RequestStatus.RETEST_REQUIRED

    def test_all_validated_becomes_ready_for_release(
        self, patient, exam_a, exam_b, lab_admin, technician, biologist, make_request,
    ):
        ar = _submitted_request(
            patient, lab_admin, technician, make_request,
            [exam_a.id, exam_b.id],
        )
        req = make_request(biologist)
        for item in ar.items.all():
            ResultVersionService.validate(
                version=item.result_versions.get(is_current=True),
                validation_notes='', validated_by=biologist, request=req,
            )

        ar.refresh_from_db()
        assert ar.status == RequestStatus.READY_FOR_RELEASE

    def test_resubmit_after_reject_returns_to_awaiting_review(
        self, patient, exam_a, exam_b, lab_admin, technician, biologist, make_request,
    ):
        ar = _submitted_request(
            patient, lab_admin, technician, make_request,
            [exam_a.id, exam_b.id],
        )
        items = list(ar.items.order_by('created_at'))
        req_bio = make_request(biologist)
        req_tech = make_request(technician)

        # Validate A, reject B
        ResultVersionService.validate(
            version=items[0].result_versions.get(is_current=True),
            validation_notes='', validated_by=biologist, request=req_bio,
        )
        ResultVersionService.reject(
            version=items[1].result_versions.get(is_current=True),
            rejection_notes='Redo', rejected_by=biologist, request=req_bio,
        )
        ar.refresh_from_db()
        assert ar.status == RequestStatus.RETEST_REQUIRED

        # Technician re-enters and re-submits B
        items[1].refresh_from_db()
        v2 = ResultVersionService.create_draft(
            item=items[1], entered_by=technician, request=req_tech,
            result_value='12.0',
        )
        ResultVersionService.submit(
            version=v2, submitted_by=technician, request=req_tech,
        )

        ar.refresh_from_db()
        assert ar.status == RequestStatus.AWAITING_REVIEW

    def test_three_items_reject_first_validate_second_third_still_under_review(
        self, patient, exam_a, exam_b, exam_c,
        lab_admin, technician, biologist, make_request,
    ):
        """With 3 items: reject A, validate B, C still under review →
        request stays AWAITING_REVIEW (C hasn't been reviewed yet)."""
        ar = _submitted_request(
            patient, lab_admin, technician, make_request,
            [exam_a.id, exam_b.id, exam_c.id],
        )
        items = list(ar.items.order_by('created_at'))
        req = make_request(biologist)

        ResultVersionService.reject(
            version=items[0].result_versions.get(is_current=True),
            rejection_notes='Bad', rejected_by=biologist, request=req,
        )
        ResultVersionService.validate(
            version=items[1].result_versions.get(is_current=True),
            validation_notes='', validated_by=biologist, request=req,
        )

        ar.refresh_from_db()
        assert ar.status == RequestStatus.AWAITING_REVIEW

        items[2].refresh_from_db()
        assert items[2].status == ItemStatus.UNDER_REVIEW


# ---------------------------------------------------------------------------
# No regression in collection/submission flow
# ---------------------------------------------------------------------------

class TestNoCollectionRegression:

    def test_collection_still_drives_in_analysis(
        self, patient, exam_a, exam_b, lab_admin, technician, make_request,
    ):
        ar = AnalysisRequestService.create(
            validated_data={
                'patient_id': patient.id,
                'source_type': SourceType.DIRECT_PATIENT,
                'items': [{'exam_definition_id': exam_a.id},
                          {'exam_definition_id': exam_b.id}],
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

        ar.refresh_from_db()
        assert ar.status == RequestStatus.IN_ANALYSIS

    def test_partial_submission_reaches_awaiting_review(
        self, patient, exam_a, exam_b, lab_admin, technician, make_request,
    ):
        """Even one submitted item means biologist work is needed."""
        ar = AnalysisRequestService.create(
            validated_data={
                'patient_id': patient.id,
                'source_type': SourceType.DIRECT_PATIENT,
                'items': [{'exam_definition_id': exam_a.id},
                          {'exam_definition_id': exam_b.id}],
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
        item_a = ar.items.order_by('created_at').first()
        item_a.refresh_from_db()
        v = ResultVersionService.create_draft(
            item=item_a, entered_by=technician, request=req, result_value='ok',
        )
        ResultVersionService.submit(version=v, submitted_by=technician, request=req)

        ar.refresh_from_db()
        assert ar.status == RequestStatus.AWAITING_REVIEW
