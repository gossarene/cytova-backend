"""
Tests for the historical comparison (previous results) in report generation.

Covers:
- SINGLE_VALUE exam: previous result found and surfaced
- MULTI_PARAMETER exam: per-parameter previous values resolved
- No previous result → previous_value is None
- Only VALIDATED results count as previous
- Most recent previous request wins (ordering)
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context

from apps.catalog.models import (
    ExamCategory, ExamDefinition, ExamFamily, ExamParameter,
    ExamTechnique, ResultStructure, SampleType,
)
from apps.patients.models import Patient
from apps.requests.models import SourceType
from apps.requests.report_service import _collect_sections
from apps.requests.services import (
    AnalysisRequestItemService, AnalysisRequestService,
)
from apps.results.services import ResultVersionService


# ---------------------------------------------------------------------------
# Subscription fixture
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


# ---------------------------------------------------------------------------
# Domain fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-PREV-001',
        first_name='Hana', last_name='Historical',
        date_of_birth=date(1985, 3, 15), gender='FEMALE',
        created_by=lab_admin,
    )


@pytest.fixture()
def other_patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-PREV-002',
        first_name='Other', last_name='Person',
        date_of_birth=date(1990, 1, 1), gender='MALE',
        created_by=lab_admin,
    )


@pytest.fixture()
def category():
    return ExamCategory.objects.create(name='Labs', display_order=1)


@pytest.fixture()
def family():
    return ExamFamily.objects.create(name='Biochemistry', display_order=1)


@pytest.fixture()
def technique():
    return ExamTechnique.objects.create(name='Spectrophotometry')


@pytest.fixture()
def single_exam(category, family, technique):
    return ExamDefinition.objects.create(
        category=category, family=family, technique=technique,
        code='GLU-H', name='Fasting Glucose',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.SINGLE_VALUE,
        unit='mg/dL', reference_range='70-100',
        unit_price=Decimal('50'),
    )


@pytest.fixture()
def multi_exam(category, family, technique):
    exam = ExamDefinition.objects.create(
        category=category, family=family, technique=technique,
        code='CBC-H', name='CBC',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.MULTI_PARAMETER,
        unit_price=Decimal('80'),
    )
    ExamParameter.objects.create(
        exam_definition=exam, code='WBC', name='White Blood Cells',
        unit='10^3/uL', reference_range='4.5-11.0', display_order=1,
    )
    ExamParameter.objects.create(
        exam_definition=exam, code='HGB', name='Hemoglobin',
        unit='g/dL', reference_range='12.0-16.0', display_order=2,
    )
    return exam


def _finalize(patient, lab_admin, technician, biologist, make_request, exam,
              *, single_value='85', multi_values=None, created_offset=None):
    """
    Create → confirm → collect → enter → validate → finalize.

    ``created_offset``: if given, monkey-patches created_at to be
    ``now() - offset`` so tests can control chronological ordering.
    """
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
    if created_offset is not None:
        from apps.requests.models import AnalysisRequest
        AnalysisRequest.objects.filter(pk=ar.pk).update(
            created_at=timezone.now() - created_offset,
        )
        ar.refresh_from_db()

    req_tech = make_request(technician)
    req_bio = make_request(biologist)

    for item in ar.items.all():
        AnalysisRequestItemService.mark_collected(
            item=item, collected_by=technician, request=req_tech,
        )

    for item in ar.items.select_related('exam_definition').all():
        item.refresh_from_db()
        if exam.result_structure == ResultStructure.MULTI_PARAMETER:
            params = list(exam.parameters.order_by('display_order'))
            vals = multi_values or [
                str(10 + i) for i, _ in enumerate(params)
            ]
            v = ResultVersionService.create_draft(
                item=item, entered_by=technician, request=req_tech,
                values=[
                    {'parameter_id': str(p.id), 'value': vals[i], 'is_abnormal': False}
                    for i, p in enumerate(params)
                ],
                comments='',
            )
        else:
            v = ResultVersionService.create_draft(
                item=item, entered_by=technician, request=req_tech,
                result_value=single_value,
                values=[{'value': single_value, 'is_abnormal': False}],
                comments='',
            )
        ResultVersionService.submit(version=v, submitted_by=technician, request=req_tech)
        v = item.result_versions.get(is_current=True)
        ResultVersionService.validate(
            version=v, validation_notes='OK', validated_by=biologist,
            request=req_bio,
        )

    ar.refresh_from_db()
    AnalysisRequestService.finalize_validation(
        analysis_request=ar, finalized_by=biologist, request=req_bio,
    )
    ar.refresh_from_db()
    return ar


# ---------------------------------------------------------------------------
# SINGLE_VALUE previous results
# ---------------------------------------------------------------------------

class TestSingleValuePrevious:

    def test_previous_result_found(
        self, patient, single_exam,
        lab_admin, technician, biologist, make_request,
    ):
        _finalize(
            patient, lab_admin, technician, biologist, make_request,
            single_exam, single_value='90',
            created_offset=timedelta(days=7),
        )
        ar2 = _finalize(
            patient, lab_admin, technician, biologist, make_request,
            single_exam, single_value='85',
        )
        sections = _collect_sections(ar2)
        exam = sections[0]['exams'][0]
        assert exam['previous_value'] == '90'
        assert exam['previous_date'] is not None

    def test_no_previous_returns_none(
        self, patient, single_exam,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize(
            patient, lab_admin, technician, biologist, make_request,
            single_exam, single_value='85',
        )
        sections = _collect_sections(ar)
        exam = sections[0]['exams'][0]
        assert exam['previous_value'] is None
        assert exam['previous_date'] is None

    def test_different_patient_not_mixed(
        self, patient, other_patient, single_exam,
        lab_admin, technician, biologist, make_request,
    ):
        _finalize(
            other_patient, lab_admin, technician, biologist, make_request,
            single_exam, single_value='200',
            created_offset=timedelta(days=7),
        )
        ar2 = _finalize(
            patient, lab_admin, technician, biologist, make_request,
            single_exam, single_value='85',
        )
        sections = _collect_sections(ar2)
        exam = sections[0]['exams'][0]
        assert exam['previous_value'] is None

    def test_most_recent_previous_wins(
        self, patient, single_exam,
        lab_admin, technician, biologist, make_request,
    ):
        _finalize(
            patient, lab_admin, technician, biologist, make_request,
            single_exam, single_value='100',
            created_offset=timedelta(days=14),
        )
        _finalize(
            patient, lab_admin, technician, biologist, make_request,
            single_exam, single_value='95',
            created_offset=timedelta(days=3),
        )
        ar3 = _finalize(
            patient, lab_admin, technician, biologist, make_request,
            single_exam, single_value='85',
        )
        sections = _collect_sections(ar3)
        assert sections[0]['exams'][0]['previous_value'] == '95'


# ---------------------------------------------------------------------------
# MULTI_PARAMETER previous results
# ---------------------------------------------------------------------------

class TestMultiParamPrevious:

    def test_per_parameter_previous(
        self, patient, multi_exam,
        lab_admin, technician, biologist, make_request,
    ):
        _finalize(
            patient, lab_admin, technician, biologist, make_request,
            multi_exam, multi_values=['6.5', '14.0'],
            created_offset=timedelta(days=5),
        )
        ar2 = _finalize(
            patient, lab_admin, technician, biologist, make_request,
            multi_exam, multi_values=['7.0', '13.5'],
        )
        sections = _collect_sections(ar2)
        values = sections[0]['exams'][0]['values']
        assert len(values) == 2

        wbc = next(v for v in values if v.name_snapshot == 'White Blood Cells')
        hgb = next(v for v in values if v.name_snapshot == 'Hemoglobin')
        assert wbc.previous_value == '6.5'
        assert hgb.previous_value == '14.0'
        assert wbc.previous_date is not None

    def test_no_previous_multi_param(
        self, patient, multi_exam,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize(
            patient, lab_admin, technician, biologist, make_request,
            multi_exam, multi_values=['7.0', '13.5'],
        )
        sections = _collect_sections(ar)
        for v in sections[0]['exams'][0]['values']:
            assert v.previous_value is None
            assert v.previous_date is None


# ---------------------------------------------------------------------------
# Validation rules
# ---------------------------------------------------------------------------

class TestValidationRules:

    def test_only_validated_results_count(
        self, patient, single_exam,
        lab_admin, technician, biologist, make_request,
    ):
        """A previous request whose result is DRAFT/SUBMITTED (not yet
        validated) must NOT surface as a previous result."""
        # First request — create but do NOT validate the result
        ar1 = AnalysisRequestService.create(
            validated_data={
                'patient_id': patient.id,
                'source_type': SourceType.DIRECT_PATIENT,
                'items': [{'exam_definition_id': single_exam.id}],
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
            confirm_after=True,
        )
        from apps.requests.models import AnalysisRequest
        AnalysisRequest.objects.filter(pk=ar1.pk).update(
            created_at=timezone.now() - timedelta(days=7),
        )
        ar1.refresh_from_db()
        req_tech = make_request(technician)
        for item in ar1.items.all():
            AnalysisRequestItemService.mark_collected(
                item=item, collected_by=technician, request=req_tech,
            )
        for item in ar1.items.select_related('exam_definition').all():
            item.refresh_from_db()
            ResultVersionService.create_draft(
                item=item, entered_by=technician, request=req_tech,
                result_value='200',
                values=[{'value': '200', 'is_abnormal': False}],
                comments='',
            )
            # Intentionally NOT submitted/validated

        # Second request — fully finalized
        ar2 = _finalize(
            patient, lab_admin, technician, biologist, make_request,
            single_exam, single_value='85',
        )
        sections = _collect_sections(ar2)
        assert sections[0]['exams'][0]['previous_value'] is None
