"""
Tests for strict exam definition domain rules:
- technique non-null at DB level
- SINGLE_VALUE: unit required, reference_range optional
- MULTI_PARAMETER: at least one parameter required
- result_structure immutable after creation
- no inconsistent exam definitions can be created
"""
import pytest
from django.db import IntegrityError
from django_tenants.utils import schema_context, get_public_schema_name
from rest_framework.test import APIClient

from apps.catalog.models import (
    ExamFamily, ExamTechnique, ExamDefinition, ExamParameter,
    SampleType, ResultStructure,
)
from apps.catalog.serializers import (
    ExamDefinitionCreateSerializer,
    ExamDefinitionUpdateSerializer,
)
from apps.catalog.services import ExamDefinitionService, ExamParameterService

API = '/api/v1/catalog'


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
def family():
    return ExamFamily.objects.create(name='Hematology', display_order=1)


@pytest.fixture()
def technique():
    return ExamTechnique.objects.create(name='Spectrophotometry')


# ---------------------------------------------------------------------------
# Technique non-null at DB level
# ---------------------------------------------------------------------------

class TestTechniqueNonNull:

    def test_db_rejects_null_technique(self, family):
        with pytest.raises(IntegrityError):
            ExamDefinition.objects.create(
                family=family,
                technique=None,
                code='NULLTECH',
                name='Null Technique',
                sample_type=SampleType.BLOOD,
            )

    def test_create_serializer_requires_technique(self, family):
        s = ExamDefinitionCreateSerializer(data={
            'family_id': str(family.id),
            'code': 'NOTECH',
            'name': 'No Technique',
            'sample_type': 'BLOOD',
            'unit': 'g/dL',
        })
        assert not s.is_valid()
        assert 'technique_id' in s.errors

    def test_create_with_technique_succeeds(
        self, family, technique, lab_admin, make_request,
    ):
        s = ExamDefinitionCreateSerializer(data={
            'family_id': str(family.id),
            'technique_id': str(technique.id),
            'code': 'TECH',
            'name': 'With Technique',
            'sample_type': 'BLOOD',
            'unit': 'g/dL',
        })
        assert s.is_valid(), s.errors
        exam = ExamDefinitionService.create(
            s.validated_data, lab_admin, make_request(lab_admin),
        )
        assert exam.technique_id == technique.id


# ---------------------------------------------------------------------------
# SINGLE_VALUE validation
# ---------------------------------------------------------------------------

class TestSingleValueValidation:

    def test_single_value_with_unit_succeeds(
        self, family, technique, lab_admin, make_request,
    ):
        s = ExamDefinitionCreateSerializer(data={
            'family_id': str(family.id),
            'technique_id': str(technique.id),
            'code': 'HGB',
            'name': 'Hemoglobin',
            'sample_type': 'BLOOD',
            'result_structure': 'SINGLE_VALUE',
            'unit': 'g/dL',
            'reference_range': '12.0–16.0',
        })
        assert s.is_valid(), s.errors
        exam = ExamDefinitionService.create(
            s.validated_data, lab_admin, make_request(lab_admin),
        )
        assert exam.result_structure == ResultStructure.SINGLE_VALUE
        assert exam.unit == 'g/dL'
        assert exam.reference_range == '12.0–16.0'

    def test_single_value_without_unit_fails(self, family, technique):
        s = ExamDefinitionCreateSerializer(data={
            'family_id': str(family.id),
            'technique_id': str(technique.id),
            'code': 'NOUNIT',
            'name': 'No Unit',
            'sample_type': 'BLOOD',
            'result_structure': 'SINGLE_VALUE',
        })
        assert not s.is_valid()
        assert 'unit' in s.errors

    def test_single_value_with_blank_unit_fails(self, family, technique):
        s = ExamDefinitionCreateSerializer(data={
            'family_id': str(family.id),
            'technique_id': str(technique.id),
            'code': 'BLANK',
            'name': 'Blank Unit',
            'sample_type': 'BLOOD',
            'result_structure': 'SINGLE_VALUE',
            'unit': '',
        })
        assert not s.is_valid()
        assert 'unit' in s.errors

    def test_single_value_reference_range_optional(
        self, family, technique, lab_admin, make_request,
    ):
        s = ExamDefinitionCreateSerializer(data={
            'family_id': str(family.id),
            'technique_id': str(technique.id),
            'code': 'NOREF',
            'name': 'No Ref Range',
            'sample_type': 'BLOOD',
            'result_structure': 'SINGLE_VALUE',
            'unit': 'mg/dL',
        })
        assert s.is_valid(), s.errors
        exam = ExamDefinitionService.create(
            s.validated_data, lab_admin, make_request(lab_admin),
        )
        assert exam.reference_range == ''

    def test_update_cannot_blank_unit_on_single_value(
        self, family, technique, lab_admin, make_request,
    ):
        exam = ExamDefinition.objects.create(
            family=family, technique=technique,
            code='UPD', name='Update Test',
            sample_type=SampleType.BLOOD,
            result_structure=ResultStructure.SINGLE_VALUE,
            unit='g/dL',
        )
        s = ExamDefinitionUpdateSerializer(
            data={'unit': ''},
            context={'instance': exam},
        )
        assert not s.is_valid()
        assert 'unit' in s.errors


# ---------------------------------------------------------------------------
# MULTI_PARAMETER validation
# ---------------------------------------------------------------------------

class TestMultiParameterValidation:

    def test_multi_param_with_params_succeeds(
        self, family, technique, lab_admin, make_request,
    ):
        s = ExamDefinitionCreateSerializer(data={
            'family_id': str(family.id),
            'technique_id': str(technique.id),
            'code': 'CBC',
            'name': 'Complete Blood Count',
            'sample_type': 'BLOOD',
            'result_structure': 'MULTI_PARAMETER',
            'parameters': [
                {'code': 'WBC', 'name': 'White Blood Cells', 'unit': '10^3/uL'},
                {'code': 'RBC', 'name': 'Red Blood Cells', 'unit': '10^6/uL'},
            ],
        })
        assert s.is_valid(), s.errors
        exam = ExamDefinitionService.create(
            s.validated_data, lab_admin, make_request(lab_admin),
        )
        assert exam.result_structure == ResultStructure.MULTI_PARAMETER
        assert exam.parameters.count() == 2

    def test_multi_param_without_params_fails(self, family, technique):
        s = ExamDefinitionCreateSerializer(data={
            'family_id': str(family.id),
            'technique_id': str(technique.id),
            'code': 'EMPTY',
            'name': 'Empty Panel',
            'sample_type': 'BLOOD',
            'result_structure': 'MULTI_PARAMETER',
            'parameters': [],
        })
        assert not s.is_valid()
        assert 'parameters' in s.errors

    def test_multi_param_duplicate_codes_rejected(self, family, technique):
        s = ExamDefinitionCreateSerializer(data={
            'family_id': str(family.id),
            'technique_id': str(technique.id),
            'code': 'DUP',
            'name': 'Duplicate',
            'sample_type': 'BLOOD',
            'result_structure': 'MULTI_PARAMETER',
            'parameters': [
                {'code': 'P1', 'name': 'A'},
                {'code': 'P1', 'name': 'B'},
            ],
        })
        assert not s.is_valid()
        assert 'parameters' in s.errors

    def test_cannot_add_param_to_single_value_exam(
        self, family, technique, lab_admin, make_request,
    ):
        exam = ExamDefinition.objects.create(
            family=family, technique=technique,
            code='SV', name='Single',
            sample_type=SampleType.BLOOD,
            result_structure=ResultStructure.SINGLE_VALUE,
            unit='g/dL',
        )
        from rest_framework.exceptions import ValidationError
        with pytest.raises(ValidationError, match='MULTI_PARAMETER'):
            ExamParameterService.create(
                exam=exam,
                validated_data={'code': 'X', 'name': 'Bad'},
                created_by=lab_admin,
                request=make_request(lab_admin),
            )


# ---------------------------------------------------------------------------
# result_structure immutable
# ---------------------------------------------------------------------------

class TestResultStructureImmutable:

    def test_update_rejects_result_structure_change(self, family, technique):
        exam = ExamDefinition.objects.create(
            family=family, technique=technique,
            code='IMMUT', name='Immutable',
            sample_type=SampleType.BLOOD,
            result_structure=ResultStructure.SINGLE_VALUE,
            unit='g/dL',
        )
        s = ExamDefinitionUpdateSerializer(
            data={'result_structure': 'MULTI_PARAMETER'},
            context={'instance': exam},
        )
        assert not s.is_valid()
        assert 'result_structure' in s.errors


# ---------------------------------------------------------------------------
# Endpoint integration
# ---------------------------------------------------------------------------

class TestEndpoints:

    def test_create_single_value_via_api(self, admin_client, family, technique):
        resp = admin_client.post(
            f'{API}/exams/',
            {
                'family_id': str(family.id),
                'technique_id': str(technique.id),
                'code': 'API1',
                'name': 'API Exam',
                'sample_type': 'BLOOD',
                'result_structure': 'SINGLE_VALUE',
                'unit': 'g/dL',
            },
            format='json',
        )
        assert resp.status_code == 201
        d = resp.json().get('data', resp.json())
        assert d['result_structure'] == 'SINGLE_VALUE'
        assert d['unit'] == 'g/dL'

    def test_create_single_value_without_unit_fails_via_api(
        self, admin_client, family, technique,
    ):
        resp = admin_client.post(
            f'{API}/exams/',
            {
                'family_id': str(family.id),
                'technique_id': str(technique.id),
                'code': 'API2',
                'name': 'No Unit',
                'sample_type': 'BLOOD',
                'result_structure': 'SINGLE_VALUE',
            },
            format='json',
        )
        assert resp.status_code == 400

    def test_create_multi_param_via_api(self, admin_client, family, technique):
        resp = admin_client.post(
            f'{API}/exams/',
            {
                'family_id': str(family.id),
                'technique_id': str(technique.id),
                'code': 'API3',
                'name': 'API Panel',
                'sample_type': 'BLOOD',
                'result_structure': 'MULTI_PARAMETER',
                'parameters': [
                    {'code': 'A', 'name': 'Alpha', 'unit': 'U/L'},
                ],
            },
            format='json',
        )
        assert resp.status_code == 201

    def test_create_without_technique_fails_via_api(
        self, admin_client, family,
    ):
        resp = admin_client.post(
            f'{API}/exams/',
            {
                'family_id': str(family.id),
                'code': 'API4',
                'name': 'No Tech',
                'sample_type': 'BLOOD',
                'unit': 'g/dL',
            },
            format='json',
        )
        assert resp.status_code == 400
