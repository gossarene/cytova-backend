"""
Tests for the ResultValue layer — structured result values with snapshotted
metadata for both SINGLE_VALUE and MULTI_PARAMETER exam definitions.
"""
from datetime import date

import pytest
from django_tenants.utils import schema_context, get_public_schema_name

from apps.catalog.models import (
    ExamCategory, ExamDefinition, ExamFamily, ExamParameter,
    ExamTechnique, ResultStructure, SampleType,
)
from apps.patients.models import Patient
from apps.requests.models import (
    AnalysisRequestItem, ItemStatus, RequestStatus, SourceType,
)
from apps.requests.services import (
    AnalysisRequestItemService, AnalysisRequestService,
)
from apps.results.models import ResultValue, ResultVersion, ResultStatus
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
def technique():
    return ExamTechnique.objects.create(name='Test Technique')


@pytest.fixture()
def family():
    return ExamFamily.objects.create(name='Hematology', display_order=1)


@pytest.fixture()
def category():
    return ExamCategory.objects.create(name='Labs', display_order=1)


@pytest.fixture()
def single_exam(family, category, technique):
    return ExamDefinition.objects.create(
        family=family, category=category, technique=technique,
        code='HGB', name='Hemoglobin',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.SINGLE_VALUE,
        unit='g/dL',
        reference_range='12.0–16.0',
    )


@pytest.fixture()
def multi_exam(family, category, technique):
    exam = ExamDefinition.objects.create(
        family=family, category=category, technique=technique,
        code='CBC', name='Complete Blood Count',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.MULTI_PARAMETER,
    )
    ExamParameter.objects.create(
        exam_definition=exam, code='WBC', name='White Blood Cells',
        unit='10^3/uL', reference_range='4.5–11.0', display_order=1,
    )
    ExamParameter.objects.create(
        exam_definition=exam, code='RBC', name='Red Blood Cells',
        unit='10^6/uL', reference_range='4.5–5.5', display_order=2,
    )
    ExamParameter.objects.create(
        exam_definition=exam, code='HGB', name='Hemoglobin',
        unit='g/dL', reference_range='12.0–16.0', display_order=3,
    )
    return exam


@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-RV-001',
        first_name='Rose',
        last_name='Values',
        date_of_birth=date(1991, 6, 20),
        gender='FEMALE',
        created_by=lab_admin,
    )


def _collected_item(patient, lab_admin, technician, make_request, exam):
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
    AnalysisRequestItemService.mark_collected(
        item=item, collected_by=technician, request=make_request(technician),
    )
    item.refresh_from_db()
    return item


# ---------------------------------------------------------------------------
# SINGLE_VALUE
# ---------------------------------------------------------------------------

class TestSingleValue:

    def test_creates_one_value_row(
        self, patient, single_exam, lab_admin, technician, make_request,
    ):
        item = _collected_item(patient, lab_admin, technician, make_request, single_exam)
        version = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=make_request(technician),
            result_value='14.5',
            values=[{'value': '14.5', 'is_abnormal': False}],
        )
        vals = list(version.values.all())
        assert len(vals) == 1
        assert vals[0].value == '14.5'
        assert vals[0].parameter is None
        assert vals[0].unit_snapshot == 'g/dL'
        assert vals[0].reference_range_snapshot == '12.0–16.0'
        assert vals[0].name_snapshot == ''

    def test_snapshots_from_exam_definition(
        self, patient, single_exam, lab_admin, technician, make_request,
    ):
        item = _collected_item(patient, lab_admin, technician, make_request, single_exam)
        version = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=make_request(technician),
            result_value='10.0',
            values=[{'value': '10.0'}],
        )
        val = version.values.first()
        assert val.unit_snapshot == single_exam.unit
        assert val.reference_range_snapshot == single_exam.reference_range

    def test_rejects_multiple_values_for_single(
        self, patient, single_exam, lab_admin, technician, make_request,
    ):
        item = _collected_item(patient, lab_admin, technician, make_request, single_exam)
        from rest_framework.exceptions import ValidationError
        with pytest.raises(ValidationError, match='at most one'):
            ResultVersionService.create_draft(
                item=item, entered_by=technician, request=make_request(technician),
                result_value='14.5',
                values=[
                    {'value': '14.5'},
                    {'value': '15.0'},
                ],
            )

    def test_snapshot_stable_after_catalog_change(
        self, patient, single_exam, lab_admin, technician, make_request,
    ):
        item = _collected_item(patient, lab_admin, technician, make_request, single_exam)
        version = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=make_request(technician),
            result_value='14.5',
            values=[{'value': '14.5'}],
        )

        single_exam.unit = 'mg/dL'
        single_exam.reference_range = '140–160'
        single_exam.save()

        val = ResultValue.objects.get(result_version=version)
        assert val.unit_snapshot == 'g/dL'
        assert val.reference_range_snapshot == '12.0–16.0'


# ---------------------------------------------------------------------------
# MULTI_PARAMETER
# ---------------------------------------------------------------------------

class TestMultiParameter:

    def test_creates_one_row_per_parameter(
        self, patient, multi_exam, lab_admin, technician, make_request,
    ):
        item = _collected_item(patient, lab_admin, technician, make_request, multi_exam)
        params = list(multi_exam.parameters.order_by('display_order'))
        version = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=make_request(technician),
            result_value='',
            values=[
                {'parameter_id': str(params[0].id), 'value': '7.5'},
                {'parameter_id': str(params[1].id), 'value': '4.8'},
                {'parameter_id': str(params[2].id), 'value': '14.2'},
            ],
        )
        vals = list(version.values.order_by('display_order'))
        assert len(vals) == 3
        assert vals[0].name_snapshot == 'White Blood Cells'
        assert vals[0].unit_snapshot == '10^3/uL'
        assert vals[0].value == '7.5'
        assert vals[1].name_snapshot == 'Red Blood Cells'
        assert vals[2].name_snapshot == 'Hemoglobin'

    def test_snapshots_parameter_metadata(
        self, patient, multi_exam, lab_admin, technician, make_request,
    ):
        item = _collected_item(patient, lab_admin, technician, make_request, multi_exam)
        params = list(multi_exam.parameters.order_by('display_order'))
        version = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=make_request(technician),
            values=[{'parameter_id': str(params[0].id), 'value': '8.0'}],
        )
        val = version.values.first()
        assert val.parameter_id == params[0].id
        assert val.unit_snapshot == params[0].unit
        assert val.reference_range_snapshot == params[0].reference_range
        assert val.display_order == params[0].display_order

    def test_rejects_duplicate_parameters(
        self, patient, multi_exam, lab_admin, technician, make_request,
    ):
        item = _collected_item(patient, lab_admin, technician, make_request, multi_exam)
        params = list(multi_exam.parameters.all())
        from rest_framework.exceptions import ValidationError
        with pytest.raises(ValidationError, match='Duplicate'):
            ResultVersionService.create_draft(
                item=item, entered_by=technician, request=make_request(technician),
                values=[
                    {'parameter_id': str(params[0].id), 'value': '7.5'},
                    {'parameter_id': str(params[0].id), 'value': '8.0'},
                ],
            )

    def test_rejects_invalid_parameter(
        self, patient, multi_exam, lab_admin, technician, make_request,
    ):
        item = _collected_item(patient, lab_admin, technician, make_request, multi_exam)
        import uuid
        from rest_framework.exceptions import ValidationError
        with pytest.raises(ValidationError, match='not a valid'):
            ResultVersionService.create_draft(
                item=item, entered_by=technician, request=make_request(technician),
                values=[{'parameter_id': str(uuid.uuid4()), 'value': '1.0'}],
            )

    def test_partial_entry_allowed(
        self, patient, multi_exam, lab_admin, technician, make_request,
    ):
        item = _collected_item(patient, lab_admin, technician, make_request, multi_exam)
        params = list(multi_exam.parameters.order_by('display_order'))
        version = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=make_request(technician),
            values=[{'parameter_id': str(params[0].id), 'value': '7.5'}],
        )
        assert version.values.count() == 1


# ---------------------------------------------------------------------------
# Draft update
# ---------------------------------------------------------------------------

class TestDraftUpdate:

    def test_update_replaces_value_rows(
        self, patient, single_exam, lab_admin, technician, make_request,
    ):
        item = _collected_item(patient, lab_admin, technician, make_request, single_exam)
        req = make_request(technician)
        version = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req,
            result_value='14.5',
            values=[{'value': '14.5'}],
        )
        assert version.values.first().value == '14.5'

        version = ResultVersionService.update_draft(
            version=version,
            validated_data={'values': [{'value': '15.0', 'is_abnormal': True}]},
            updated_by=technician,
            request=req,
        )
        vals = list(version.values.all())
        assert len(vals) == 1
        assert vals[0].value == '15.0'
        assert vals[0].is_abnormal is True
        assert vals[0].unit_snapshot == 'g/dL'

    def test_update_multi_param_values(
        self, patient, multi_exam, lab_admin, technician, make_request,
    ):
        item = _collected_item(patient, lab_admin, technician, make_request, multi_exam)
        params = list(multi_exam.parameters.order_by('display_order'))
        req = make_request(technician)
        version = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req,
            values=[{'parameter_id': str(params[0].id), 'value': '7.5'}],
        )
        assert version.values.count() == 1

        version = ResultVersionService.update_draft(
            version=version,
            validated_data={'values': [
                {'parameter_id': str(params[0].id), 'value': '8.0'},
                {'parameter_id': str(params[1].id), 'value': '4.9'},
            ]},
            updated_by=technician,
            request=req,
        )
        assert version.values.count() == 2


# ---------------------------------------------------------------------------
# No regression in existing flow
# ---------------------------------------------------------------------------

class TestNoRegression:

    def test_create_without_values_still_works(
        self, patient, single_exam, lab_admin, technician, make_request,
    ):
        item = _collected_item(patient, lab_admin, technician, make_request, single_exam)
        version = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=make_request(technician),
            result_value='ok',
        )
        assert version.status == ResultStatus.DRAFT
        assert version.result_value == 'ok'

    def test_submit_still_works(
        self, patient, single_exam, lab_admin, technician, make_request,
    ):
        item = _collected_item(patient, lab_admin, technician, make_request, single_exam)
        req = make_request(technician)
        version = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req,
            result_value='14.5',
            values=[{'value': '14.5'}],
        )
        version = ResultVersionService.submit(
            version=version, submitted_by=technician, request=req,
        )
        assert version.status == ResultStatus.SUBMITTED


# ---------------------------------------------------------------------------
# Submission readiness for multi-parameter
# ---------------------------------------------------------------------------

class TestMultiParameterSubmission:

    def test_partial_multi_param_cannot_submit(
        self, patient, multi_exam, lab_admin, technician, make_request,
    ):
        item = _collected_item(patient, lab_admin, technician, make_request, multi_exam)
        params = list(multi_exam.parameters.order_by('display_order'))
        req = make_request(technician)
        version = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req,
            values=[{'parameter_id': str(params[0].id), 'value': '7.5'}],
        )
        from rest_framework.exceptions import ValidationError
        with pytest.raises(ValidationError, match='parameter'):
            ResultVersionService.submit(
                version=version, submitted_by=technician, request=req,
            )

    def test_fully_filled_multi_param_can_submit(
        self, patient, multi_exam, lab_admin, technician, make_request,
    ):
        item = _collected_item(patient, lab_admin, technician, make_request, multi_exam)
        params = list(multi_exam.parameters.order_by('display_order'))
        req = make_request(technician)
        version = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req,
            values=[
                {'parameter_id': str(p.id), 'value': str(i + 1)}
                for i, p in enumerate(params)
            ],
        )
        version = ResultVersionService.submit(
            version=version, submitted_by=technician, request=req,
        )
        assert version.status == ResultStatus.SUBMITTED

    def test_value_zero_is_valid(
        self, patient, multi_exam, lab_admin, technician, make_request,
    ):
        item = _collected_item(patient, lab_admin, technician, make_request, multi_exam)
        params = list(multi_exam.parameters.order_by('display_order'))
        req = make_request(technician)
        version = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req,
            values=[
                {'parameter_id': str(p.id), 'value': '0'}
                for p in params
            ],
        )
        version = ResultVersionService.submit(
            version=version, submitted_by=technician, request=req,
        )
        assert version.status == ResultStatus.SUBMITTED

    def test_inactive_params_do_not_block_submission(
        self, patient, multi_exam, lab_admin, technician, make_request,
    ):
        params = list(multi_exam.parameters.order_by('display_order'))
        params[2].is_active = False
        params[2].save(update_fields=['is_active'])

        item = _collected_item(patient, lab_admin, technician, make_request, multi_exam)
        req = make_request(technician)
        version = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req,
            values=[
                {'parameter_id': str(params[0].id), 'value': '7.5'},
                {'parameter_id': str(params[1].id), 'value': '4.8'},
            ],
        )
        version = ResultVersionService.submit(
            version=version, submitted_by=technician, request=req,
        )
        assert version.status == ResultStatus.SUBMITTED
