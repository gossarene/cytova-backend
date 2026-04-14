"""
Tests for enriched exam catalog metadata:
ExamFamily, ExamSubFamily, TubeType, ExamTechnique, fasting_required.
"""
import pytest
from decimal import Decimal

from apps.catalog.models import (
    ExamFamily, ExamSubFamily, TubeType, ExamTechnique,
    ExamDefinition, SampleType,
)
from apps.catalog.serializers import (
    ExamDefinitionCreateSerializer,
    ExamDefinitionUpdateSerializer,
    ExamDefinitionListSerializer,
    ExamSubFamilyCreateSerializer,
    TubeTypeCreateSerializer,
    ExamTechniqueCreateSerializer,
)
from apps.catalog.filters import ExamDefinitionFilter
from apps.catalog.services import ExamDefinitionService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def family():
    return ExamFamily.objects.create(name='Hematology', display_order=1)


@pytest.fixture()
def sub_family(family):
    return ExamSubFamily.objects.create(family=family, name='Coagulation')


@pytest.fixture()
def tube_type():
    return TubeType.objects.create(name='EDTA')


@pytest.fixture()
def technique():
    return ExamTechnique.objects.create(name='Spectrophotometry')


@pytest.fixture()
def exam(family, technique):
    return ExamDefinition.objects.create(
        family=family,
        technique=technique,
        code='CBC',
        name='Complete Blood Count',
        sample_type=SampleType.BLOOD,
        unit_price=Decimal('50.0000'),
    )


# ---------------------------------------------------------------------------
# Reference model creation
# ---------------------------------------------------------------------------

class TestReferenceModels:

    def test_family_creation(self):
        f = ExamFamily.objects.create(name='Biochemistry', display_order=2)
        assert f.name == 'Biochemistry'
        assert f.is_active is True

    def test_family_unique_name(self, family):
        from django.db import IntegrityError
        with pytest.raises(IntegrityError):
            ExamFamily.objects.create(name='Hematology')

    def test_sub_family_creation(self, family):
        sf = ExamSubFamily.objects.create(family=family, name='Hemostasis')
        assert sf.family_id == family.id
        assert str(sf) == 'Hematology > Hemostasis'

    def test_sub_family_unique_per_family(self, sub_family):
        from django.db import IntegrityError
        with pytest.raises(IntegrityError):
            ExamSubFamily.objects.create(family=sub_family.family, name='Coagulation')

    def test_sub_family_same_name_different_family(self, sub_family):
        other = ExamFamily.objects.create(name='Immunology', display_order=3)
        sf = ExamSubFamily.objects.create(family=other, name='Coagulation')
        assert sf.id != sub_family.id

    def test_tube_type_creation(self):
        t = TubeType.objects.create(name='Citrate', description='For coag tests')
        assert t.name == 'Citrate'

    def test_technique_creation(self):
        t = ExamTechnique.objects.create(name='PCR', description='Polymerase chain reaction')
        assert t.name == 'PCR'


# ---------------------------------------------------------------------------
# Exam creation with new metadata
# ---------------------------------------------------------------------------

class TestExamCreationWithMetadata:

    def test_create_with_family_only(self, family, technique):
        s = ExamDefinitionCreateSerializer(data={
            'family_id': str(family.id),
            'technique_id': str(technique.id),
            'code': 'GLU',
            'name': 'Glucose',
            'sample_type': 'BLOOD',
            'unit': 'mg/dL',
            'unit_price': '30.0000',
        })
        assert s.is_valid(), s.errors
        assert s.validated_data['family_id'] == family.id

    def test_create_with_all_metadata(self, family, sub_family, tube_type, technique):
        s = ExamDefinitionCreateSerializer(data={
            'family_id': str(family.id),
            'sub_family_id': str(sub_family.id),
            'tube_type_id': str(tube_type.id),
            'technique_id': str(technique.id),
            'fasting_required': True,
            'code': 'PT',
            'name': 'Prothrombin Time',
            'sample_type': 'BLOOD',
            'unit': 'seconds',
            'unit_price': '40.0000',
        })
        assert s.is_valid(), s.errors
        assert s.validated_data['fasting_required'] is True

    def test_create_rejects_invalid_family(self):
        import uuid
        s = ExamDefinitionCreateSerializer(data={
            'family_id': str(uuid.uuid4()),
            'code': 'X',
            'name': 'X',
            'sample_type': 'BLOOD',
        })
        assert not s.is_valid()
        assert 'family_id' in s.errors

    def test_fasting_defaults_to_false(self, family, technique):
        s = ExamDefinitionCreateSerializer(data={
            'family_id': str(family.id),
            'technique_id': str(technique.id),
            'code': 'CRP',
            'name': 'C-Reactive Protein',
            'sample_type': 'BLOOD',
            'unit': 'mg/L',
        })
        assert s.is_valid(), s.errors
        assert s.validated_data['fasting_required'] is False

    def test_create_without_sub_family_succeeds(self, family, technique):
        """sub_family is optional: omitting it must not fail."""
        s = ExamDefinitionCreateSerializer(data={
            'family_id': str(family.id),
            'technique_id': str(technique.id),
            'code': 'NA-ONLY',
            'name': 'Sodium',
            'sample_type': 'BLOOD',
            'unit': 'mmol/L',
        })
        assert s.is_valid(), s.errors
        assert s.validated_data.get('sub_family_id') is None

    def test_create_with_explicit_null_sub_family_succeeds(self, family, technique):
        s = ExamDefinitionCreateSerializer(data={
            'family_id': str(family.id),
            'technique_id': str(technique.id),
            'sub_family_id': None,
            'code': 'K-ONLY',
            'name': 'Potassium',
            'sample_type': 'BLOOD',
            'unit': 'mmol/L',
        })
        assert s.is_valid(), s.errors
        assert s.validated_data['sub_family_id'] is None

    def test_create_with_matching_sub_family_succeeds(self, family, sub_family, technique):
        s = ExamDefinitionCreateSerializer(data={
            'family_id': str(family.id),
            'technique_id': str(technique.id),
            'sub_family_id': str(sub_family.id),
            'code': 'PT-OK',
            'name': 'PT',
            'sample_type': 'BLOOD',
            'unit': 'seconds',
        })
        assert s.is_valid(), s.errors

    def test_create_rejects_sub_family_from_other_family(self, family, sub_family, technique):
        """Coherence rule: sub_family must belong to the selected family."""
        other_family = ExamFamily.objects.create(name='Biochemistry', display_order=2)
        s = ExamDefinitionCreateSerializer(data={
            'family_id': str(other_family.id),
            'technique_id': str(technique.id),
            'sub_family_id': str(sub_family.id),
            'code': 'MISMATCH',
            'name': 'Mismatch Exam',
            'sample_type': 'BLOOD',
        })
        assert not s.is_valid()
        assert 'sub_family_id' in s.errors


# ---------------------------------------------------------------------------
# Exam update with new metadata
# ---------------------------------------------------------------------------

class TestExamUpdateWithMetadata:

    def test_update_family(self, exam):
        new_family = ExamFamily.objects.create(name='Biochemistry', display_order=2)
        s = ExamDefinitionUpdateSerializer(data={'family_id': str(new_family.id)})
        assert s.is_valid(), s.errors

    def test_update_fasting_required(self):
        s = ExamDefinitionUpdateSerializer(data={'fasting_required': True})
        assert s.is_valid(), s.errors
        assert s.validated_data['fasting_required'] is True

    def test_update_tube_type(self, tube_type):
        s = ExamDefinitionUpdateSerializer(data={'tube_type_id': str(tube_type.id)})
        assert s.is_valid(), s.errors

    def test_update_technique(self, technique):
        s = ExamDefinitionUpdateSerializer(data={'technique_id': str(technique.id)})
        assert s.is_valid(), s.errors

    def test_update_clears_optional_fk(self):
        s = ExamDefinitionUpdateSerializer(data={'sub_family_id': None})
        assert s.is_valid(), s.errors
        assert s.validated_data['sub_family_id'] is None

    def test_update_set_matching_sub_family(self, exam, sub_family):
        """Assigning a sub_family that belongs to the exam's current family is OK."""
        s = ExamDefinitionUpdateSerializer(
            data={'sub_family_id': str(sub_family.id)},
            context={'instance': exam},
        )
        assert s.is_valid(), s.errors

    def test_update_rejects_sub_family_from_other_family(self, exam):
        """Setting a sub_family that belongs to a different family must fail."""
        other_family = ExamFamily.objects.create(name='Biochemistry', display_order=2)
        foreign_sub = ExamSubFamily.objects.create(family=other_family, name='Enzymes')
        s = ExamDefinitionUpdateSerializer(
            data={'sub_family_id': str(foreign_sub.id)},
            context={'instance': exam},
        )
        assert not s.is_valid()
        assert 'sub_family_id' in s.errors

    def test_update_family_and_sub_family_together_ok(self, exam):
        """Client can atomically change both sides of the pair."""
        new_family = ExamFamily.objects.create(name='Biochemistry', display_order=2)
        new_sub = ExamSubFamily.objects.create(family=new_family, name='Enzymes')
        s = ExamDefinitionUpdateSerializer(
            data={
                'family_id': str(new_family.id),
                'sub_family_id': str(new_sub.id),
            },
            context={'instance': exam},
        )
        assert s.is_valid(), s.errors

    def test_update_family_rejects_stale_sub_family(self, family, sub_family, technique):
        """
        Changing family while leaving an existing sub_family dangling must fail —
        the client must either clear sub_family or provide a compatible one.
        """
        exam = ExamDefinition.objects.create(
            family=family,
            technique=technique,
            sub_family=sub_family,
            code='STALE',
            name='Stale Exam',
            sample_type=SampleType.BLOOD,
            unit_price=Decimal('10.0000'),
        )
        other_family = ExamFamily.objects.create(name='Biochemistry', display_order=2)
        s = ExamDefinitionUpdateSerializer(
            data={'family_id': str(other_family.id)},  # sub_family not touched
            context={'instance': exam},
        )
        assert not s.is_valid()
        assert 'sub_family_id' in s.errors

    def test_update_family_with_sub_family_cleared_ok(self, family, sub_family, technique):
        """Clearing sub_family in the same request while changing family is valid."""
        exam = ExamDefinition.objects.create(
            family=family,
            technique=technique,
            sub_family=sub_family,
            code='CLRD',
            name='Cleared Exam',
            sample_type=SampleType.BLOOD,
            unit_price=Decimal('10.0000'),
        )
        other_family = ExamFamily.objects.create(name='Biochemistry', display_order=2)
        s = ExamDefinitionUpdateSerializer(
            data={
                'family_id': str(other_family.id),
                'sub_family_id': None,
            },
            context={'instance': exam},
        )
        assert s.is_valid(), s.errors

    def test_update_unrelated_field_does_not_trigger_coherence(self, family, sub_family, technique):
        """Touching only e.g. name must not break because of existing FK state."""
        exam = ExamDefinition.objects.create(
            family=family,
            technique=technique,
            sub_family=sub_family,
            code='NOOP',
            name='Noop Exam',
            sample_type=SampleType.BLOOD,
            unit_price=Decimal('10.0000'),
        )
        s = ExamDefinitionUpdateSerializer(
            data={'name': 'Renamed'},
            context={'instance': exam},
        )
        assert s.is_valid(), s.errors


# ---------------------------------------------------------------------------
# Serializer output
# ---------------------------------------------------------------------------

class TestExamSerializerOutput:

    def test_list_includes_new_fields(self, family, sub_family, tube_type, technique, lab_admin):
        exam = ExamDefinition.objects.create(
            family=family, sub_family=sub_family, tube_type=tube_type,
            technique=technique, fasting_required=True,
            code='FER', name='Ferritin', sample_type=SampleType.BLOOD,
            unit_price=Decimal('60.0000'),
        )
        # Ensure select_related is used
        exam = ExamDefinition.objects.select_related(
            'family', 'sub_family', 'tube_type', 'technique',
        ).get(pk=exam.pk)
        data = ExamDefinitionListSerializer(exam).data
        assert data['family_name'] == 'Hematology'
        assert data['sub_family_name'] == 'Coagulation'
        assert data['tube_type_name'] == 'EDTA'
        assert data['technique_name'] == 'Spectrophotometry'
        assert data['fasting_required'] is True


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

class TestExamFiltering:

    @pytest.fixture()
    def exams(self, family, sub_family, tube_type, technique):
        e1 = ExamDefinition.objects.create(
            family=family, sub_family=sub_family, tube_type=tube_type,
            technique=technique, fasting_required=True,
            code='FLT-A', name='Exam A', sample_type=SampleType.BLOOD,
            unit_price=Decimal('10.0000'),
        )
        e2 = ExamDefinition.objects.create(
            family=family, technique=technique, fasting_required=False,
            code='FLT-B', name='Exam B', sample_type=SampleType.URINE,
            unit_price=Decimal('20.0000'),
        )
        return e1, e2

    def test_filter_by_family(self, exams, family):
        f = ExamDefinitionFilter(data={'family_id': str(family.id)}, queryset=ExamDefinition.objects.all())
        assert f.qs.count() == 2

    def test_filter_by_sub_family(self, exams, sub_family):
        f = ExamDefinitionFilter(data={'sub_family_id': str(sub_family.id)}, queryset=ExamDefinition.objects.all())
        assert f.qs.count() == 1

    def test_filter_by_tube_type(self, exams, tube_type):
        f = ExamDefinitionFilter(data={'tube_type_id': str(tube_type.id)}, queryset=ExamDefinition.objects.all())
        assert f.qs.count() == 1

    def test_filter_by_technique(self, exams, technique):
        f = ExamDefinitionFilter(data={'technique_id': str(technique.id)}, queryset=ExamDefinition.objects.all())
        assert f.qs.count() == 2

    def test_filter_by_fasting_required(self, exams):
        f = ExamDefinitionFilter(data={'fasting_required': 'true'}, queryset=ExamDefinition.objects.all())
        assert f.qs.count() == 1
        assert f.qs.first().fasting_required is True


# ---------------------------------------------------------------------------
# Service layer with new fields
# ---------------------------------------------------------------------------

class TestExamServiceWithMetadata:

    def test_create_via_service(self, family, tube_type, technique, lab_admin, make_request):
        exam = ExamDefinitionService.create(
            validated_data={
                'family_id': family.id,
                'tube_type_id': tube_type.id,
                'technique_id': technique.id,
                'fasting_required': True,
                'code': 'SVC-META',
                'name': 'Service Meta Test',
                'sample_type': SampleType.BLOOD,
                'unit_price': Decimal('45.0000'),
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
        )
        assert exam.family_id == family.id
        assert exam.tube_type_id == tube_type.id
        assert exam.fasting_required is True

    def test_update_via_service(self, exam, technique, lab_admin, make_request):
        updated = ExamDefinitionService.update(
            exam=exam,
            validated_data={'technique_id': technique.id, 'fasting_required': True},
            updated_by=lab_admin,
            request=make_request(lab_admin),
        )
        assert updated.technique_id == technique.id
        assert updated.fasting_required is True


# ---------------------------------------------------------------------------
# Sub-family serializer validation
# ---------------------------------------------------------------------------

class TestSubFamilySerializer:

    def test_valid_creation(self, family):
        s = ExamSubFamilyCreateSerializer(data={
            'family_id': str(family.id),
            'name': 'Hemostasis',
        })
        assert s.is_valid(), s.errors

    def test_duplicate_rejected(self, sub_family):
        s = ExamSubFamilyCreateSerializer(data={
            'family_id': str(sub_family.family_id),
            'name': 'Coagulation',
        })
        assert not s.is_valid()
        assert 'name' in s.errors


class TestTubeTypeSerializer:

    def test_valid_creation(self):
        s = TubeTypeCreateSerializer(data={'name': 'Heparin'})
        assert s.is_valid(), s.errors

    def test_duplicate_rejected(self, tube_type):
        s = TubeTypeCreateSerializer(data={'name': 'EDTA'})
        assert not s.is_valid()
        assert 'name' in s.errors


class TestExamTechniqueSerializer:

    def test_valid_creation(self):
        s = ExamTechniqueCreateSerializer(data={'name': 'ELISA'})
        assert s.is_valid(), s.errors

    def test_duplicate_rejected(self, technique):
        s = ExamTechniqueCreateSerializer(data={'name': 'Spectrophotometry'})
        assert not s.is_valid()
        assert 'name' in s.errors
