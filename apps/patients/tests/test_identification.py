"""
Tests for patient identification model: document_type, document_number,
nationality, city_of_residence.
"""
import pytest

from apps.patients.models import DocumentType, Patient
from apps.patients.serializers import (
    PatientCreateSerializer,
    PatientUpdateSerializer,
    PatientDetailSerializer,
    PatientListSerializer,
)
from apps.patients.filters import PatientFilter
from apps.patients.services import PatientService


# ---------------------------------------------------------------------------
# DocumentType enum
# ---------------------------------------------------------------------------

class TestDocumentTypeEnum:

    def test_expected_values(self):
        values = {c.value for c in DocumentType}
        assert values == {
            'NATIONAL_ID_CARD', 'PASSPORT', 'CIP',
            'RESIDENCE_PERMIT', 'OTHER', 'UNKNOWN',
        }


# ---------------------------------------------------------------------------
# Create serializer
# ---------------------------------------------------------------------------

VALID_PATIENT = {
    'document_type': 'NATIONAL_ID_CARD',
    'document_number': 'DOC-001',
    'first_name': 'Alice',
    'last_name': 'Martin',
    'date_of_birth': '1990-05-20',
    'gender': 'FEMALE',
}


class TestPatientCreateIdentification:

    def test_valid_with_national_id_card(self):
        s = PatientCreateSerializer(data=VALID_PATIENT)
        assert s.is_valid(), s.errors

    def test_valid_with_passport(self):
        s = PatientCreateSerializer(data={
            **VALID_PATIENT,
            'document_type': 'PASSPORT',
            'document_number': 'PASS-001',
        })
        assert s.is_valid(), s.errors

    def test_valid_with_cip(self):
        s = PatientCreateSerializer(data={
            **VALID_PATIENT,
            'document_type': 'CIP',
            'document_number': 'CIP-001',
        })
        assert s.is_valid(), s.errors

    def test_valid_with_residence_permit(self):
        s = PatientCreateSerializer(data={
            **VALID_PATIENT,
            'document_type': 'RESIDENCE_PERMIT',
            'document_number': 'RP-001',
        })
        assert s.is_valid(), s.errors

    def test_valid_with_other(self):
        s = PatientCreateSerializer(data={
            **VALID_PATIENT,
            'document_type': 'OTHER',
            'document_number': 'OTHER-001',
        })
        assert s.is_valid(), s.errors

    def test_invalid_document_type_rejected(self):
        s = PatientCreateSerializer(data={
            **VALID_PATIENT,
            'document_type': 'DRIVERS_LICENSE',
        })
        assert not s.is_valid()
        assert 'document_type' in s.errors

    def test_missing_document_type_rejected(self):
        data = {**VALID_PATIENT}
        del data['document_type']
        s = PatientCreateSerializer(data=data)
        assert not s.is_valid()
        assert 'document_type' in s.errors

    def test_missing_document_number_rejected(self):
        data = {**VALID_PATIENT}
        del data['document_number']
        s = PatientCreateSerializer(data=data)
        assert not s.is_valid()
        assert 'document_number' in s.errors

    def test_duplicate_document_rejected(self):
        """Same document_type + document_number within tenant must be rejected."""
        s1 = PatientCreateSerializer(data=VALID_PATIENT)
        assert s1.is_valid(), s1.errors
        Patient.objects.create(**s1.validated_data)

        s2 = PatientCreateSerializer(data={
            **VALID_PATIENT,
            'first_name': 'Bob',
        })
        assert not s2.is_valid()
        assert 'document_number' in s2.errors

    def test_same_number_different_type_allowed(self):
        """Same number but different type is a different document — must be allowed."""
        Patient.objects.create(
            document_type='NATIONAL_ID_CARD', document_number='SHARED-001',
            first_name='A', last_name='B', date_of_birth='1990-01-01', gender='MALE',
        )
        s = PatientCreateSerializer(data={
            **VALID_PATIENT,
            'document_type': 'PASSPORT',
            'document_number': 'SHARED-001',
        })
        assert s.is_valid(), s.errors

    def test_nationality_optional(self):
        s = PatientCreateSerializer(data=VALID_PATIENT)
        assert s.is_valid(), s.errors
        assert s.validated_data.get('nationality', '') == ''

    def test_nationality_accepted(self):
        s = PatientCreateSerializer(data={**VALID_PATIENT, 'nationality': 'Ivorian'})
        assert s.is_valid(), s.errors
        assert s.validated_data['nationality'] == 'Ivorian'

    def test_city_of_residence_optional(self):
        s = PatientCreateSerializer(data=VALID_PATIENT)
        assert s.is_valid(), s.errors
        assert s.validated_data.get('city_of_residence', '') == ''

    def test_city_of_residence_accepted(self):
        s = PatientCreateSerializer(data={
            **VALID_PATIENT,
            'document_number': 'CITY-001',
            'city_of_residence': 'Abidjan',
        })
        assert s.is_valid(), s.errors
        assert s.validated_data['city_of_residence'] == 'Abidjan'


# ---------------------------------------------------------------------------
# Update serializer
# ---------------------------------------------------------------------------

class TestPatientUpdateIdentification:

    def test_document_type_not_updatable(self):
        """document_type is excluded from update — it should be ignored."""
        s = PatientUpdateSerializer(data={'document_type': 'PASSPORT'}, partial=True)
        assert s.is_valid(), s.errors
        assert 'document_type' not in s.validated_data

    def test_document_number_not_updatable(self):
        """document_number is excluded from update — it should be ignored."""
        s = PatientUpdateSerializer(data={'document_number': 'NEW-001'}, partial=True)
        assert s.is_valid(), s.errors
        assert 'document_number' not in s.validated_data

    def test_nationality_updatable(self):
        s = PatientUpdateSerializer(data={'nationality': 'Senegalese'}, partial=True)
        assert s.is_valid(), s.errors
        assert s.validated_data['nationality'] == 'Senegalese'

    def test_city_of_residence_updatable(self):
        s = PatientUpdateSerializer(data={'city_of_residence': 'Dakar'}, partial=True)
        assert s.is_valid(), s.errors
        assert s.validated_data['city_of_residence'] == 'Dakar'


# ---------------------------------------------------------------------------
# Serializer output
# ---------------------------------------------------------------------------

class TestPatientSerializerOutput:

    @pytest.fixture()
    def patient(self, lab_admin):
        return Patient.objects.create(
            document_type='PASSPORT',
            document_number='SER-OUT-001',
            first_name='Claire',
            last_name='Dupont',
            date_of_birth='1988-11-03',
            gender='FEMALE',
            nationality='French',
            city_of_residence='Lyon',
            created_by=lab_admin,
        )

    def test_list_serializer_fields(self, patient):
        data = PatientListSerializer(patient).data
        assert data['document_type'] == 'PASSPORT'
        assert data['document_number'] == 'SER-OUT-001'
        assert data['nationality'] == 'French'
        assert 'national_id' not in data

    def test_detail_serializer_fields(self, patient):
        data = PatientDetailSerializer(patient).data
        assert data['document_type'] == 'PASSPORT'
        assert data['document_number'] == 'SER-OUT-001'
        assert data['nationality'] == 'French'
        assert data['city_of_residence'] == 'Lyon'
        assert 'national_id' not in data


# ---------------------------------------------------------------------------
# Filter by document_type
# ---------------------------------------------------------------------------

class TestDocumentTypeFilter:

    @pytest.fixture()
    def patients(self, lab_admin):
        nid = Patient.objects.create(
            document_type='NATIONAL_ID_CARD', document_number='FLT-NID-001',
            first_name='A', last_name='X',
            date_of_birth='1990-01-01', gender='MALE', created_by=lab_admin,
        )
        passport = Patient.objects.create(
            document_type='PASSPORT', document_number='FLT-PASS-001',
            first_name='B', last_name='Y',
            date_of_birth='1992-02-02', gender='FEMALE', created_by=lab_admin,
        )
        return nid, passport

    def test_filter_national_id_card(self, patients):
        qs = Patient.objects.all()
        f = PatientFilter(data={'document_type': 'NATIONAL_ID_CARD'}, queryset=qs)
        assert f.qs.count() == 1
        assert f.qs.first().document_type == 'NATIONAL_ID_CARD'

    def test_filter_passport(self, patients):
        qs = Patient.objects.all()
        f = PatientFilter(data={'document_type': 'PASSPORT'}, queryset=qs)
        assert f.qs.count() == 1
        assert f.qs.first().document_type == 'PASSPORT'

    def test_filter_all_returns_both(self, patients):
        qs = Patient.objects.all()
        f = PatientFilter(data={}, queryset=qs)
        assert f.qs.count() == 2


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------

class TestPatientServiceIdentification:

    def test_create_with_new_fields(self, lab_admin, make_request):
        patient = PatientService.create_patient(
            validated_data={
                'document_type': 'PASSPORT',
                'document_number': 'SVC-001',
                'first_name': 'Test',
                'last_name': 'Service',
                'date_of_birth': '1995-06-15',
                'gender': 'MALE',
                'nationality': 'Ivorian',
                'city_of_residence': 'Bouaké',
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
        )
        assert patient.document_type == 'PASSPORT'
        assert patient.document_number == 'SVC-001'
        assert patient.nationality == 'Ivorian'
        assert patient.city_of_residence == 'Bouaké'

    def test_update_nationality_and_city(self, lab_admin, make_request):
        patient = Patient.objects.create(
            document_type='NATIONAL_ID_CARD', document_number='SVC-UPD-001',
            first_name='Old', last_name='Patient',
            date_of_birth='1980-01-01', gender='FEMALE', created_by=lab_admin,
        )
        updated = PatientService.update_patient(
            patient,
            {'nationality': 'Malian', 'city_of_residence': 'Bamako'},
            lab_admin,
            make_request(lab_admin),
        )
        assert updated.nationality == 'Malian'
        assert updated.city_of_residence == 'Bamako'


# ---------------------------------------------------------------------------
# Model constraints
# ---------------------------------------------------------------------------

class TestPatientModelConstraints:

    def test_str_uses_document_number(self, lab_admin):
        p = Patient.objects.create(
            document_type='NATIONAL_ID_CARD', document_number='STR-001',
            first_name='John', last_name='Doe',
            date_of_birth='1990-01-01', gender='MALE', created_by=lab_admin,
        )
        assert 'STR-001' in str(p)
        assert 'DOE' in str(p)

    def test_unique_constraint_enforced(self, lab_admin):
        Patient.objects.create(
            document_type='NATIONAL_ID_CARD', document_number='UNQ-001',
            first_name='A', last_name='B',
            date_of_birth='1990-01-01', gender='MALE', created_by=lab_admin,
        )
        from django.db import IntegrityError
        with pytest.raises(IntegrityError):
            Patient.objects.create(
                document_type='NATIONAL_ID_CARD', document_number='UNQ-001',
                first_name='C', last_name='D',
                date_of_birth='1991-01-01', gender='FEMALE', created_by=lab_admin,
            )
