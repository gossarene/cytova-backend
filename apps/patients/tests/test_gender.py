"""
Tests for patient gender validation, filtering, and enum enforcement.
"""
import pytest

from apps.patients.models import Gender, Patient
from apps.patients.serializers import PatientCreateSerializer, PatientUpdateSerializer
from apps.patients.filters import PatientFilter


# ---------------------------------------------------------------------------
# Gender enum
# ---------------------------------------------------------------------------

class TestGenderEnum:

    def test_only_male_and_female_defined(self):
        values = {c.value for c in Gender}
        assert values == {'MALE', 'FEMALE'}

    def test_other_not_in_choices(self):
        values = [v for v, _ in Gender.choices]
        assert 'OTHER' not in values


# ---------------------------------------------------------------------------
# Serializer validation — create
# ---------------------------------------------------------------------------

VALID_BASE = {
    'document_type': 'NATIONAL_ID_CARD',
    'document_number': 'NID-GENDER-001',
    'first_name': 'Alice',
    'last_name': 'Martin',
    'date_of_birth': '1990-05-20',
}


class TestPatientCreateGender:

    def test_male_accepted(self):
        s = PatientCreateSerializer(data={**VALID_BASE, 'gender': 'MALE'})
        assert s.is_valid(), s.errors

    def test_female_accepted(self):
        s = PatientCreateSerializer(data={
            **VALID_BASE,
            'document_number': 'NID-GENDER-002',
            'gender': 'FEMALE',
        })
        assert s.is_valid(), s.errors

    def test_other_rejected(self):
        s = PatientCreateSerializer(data={**VALID_BASE, 'gender': 'OTHER'})
        assert not s.is_valid()
        assert 'gender' in s.errors

    def test_invalid_value_rejected(self):
        s = PatientCreateSerializer(data={**VALID_BASE, 'gender': 'UNKNOWN'})
        assert not s.is_valid()
        assert 'gender' in s.errors

    def test_empty_gender_rejected(self):
        s = PatientCreateSerializer(data={**VALID_BASE, 'gender': ''})
        assert not s.is_valid()
        assert 'gender' in s.errors

    def test_missing_gender_rejected(self):
        s = PatientCreateSerializer(data=VALID_BASE)
        assert not s.is_valid()
        assert 'gender' in s.errors


# ---------------------------------------------------------------------------
# Serializer validation — update
# ---------------------------------------------------------------------------

class TestPatientUpdateGender:

    def test_male_accepted(self):
        s = PatientUpdateSerializer(data={'gender': 'MALE'}, partial=True)
        assert s.is_valid(), s.errors

    def test_female_accepted(self):
        s = PatientUpdateSerializer(data={'gender': 'FEMALE'}, partial=True)
        assert s.is_valid(), s.errors

    def test_other_rejected(self):
        s = PatientUpdateSerializer(data={'gender': 'OTHER'}, partial=True)
        assert not s.is_valid()
        assert 'gender' in s.errors

    def test_invalid_value_rejected(self):
        s = PatientUpdateSerializer(data={'gender': 'NONBINARY'}, partial=True)
        assert not s.is_valid()
        assert 'gender' in s.errors


# ---------------------------------------------------------------------------
# Gender filtering
# ---------------------------------------------------------------------------

class TestGenderFilter:

    @pytest.fixture()
    def patients(self, lab_admin):
        m = Patient.objects.create(
            document_type='NATIONAL_ID_CARD', document_number='F-M-001',
            first_name='John', last_name='Doe',
            date_of_birth='1985-03-10', gender='MALE', created_by=lab_admin,
        )
        f = Patient.objects.create(
            document_type='NATIONAL_ID_CARD', document_number='F-F-001',
            first_name='Jane', last_name='Doe',
            date_of_birth='1990-07-22', gender='FEMALE', created_by=lab_admin,
        )
        return m, f

    def test_filter_male(self, patients):
        qs = Patient.objects.all()
        f = PatientFilter(data={'gender': 'MALE'}, queryset=qs)
        results = f.qs
        assert results.count() == 1
        assert results.first().gender == 'MALE'

    def test_filter_female(self, patients):
        qs = Patient.objects.all()
        f = PatientFilter(data={'gender': 'FEMALE'}, queryset=qs)
        results = f.qs
        assert results.count() == 1
        assert results.first().gender == 'FEMALE'

    def test_filter_all_returns_both(self, patients):
        qs = Patient.objects.all()
        f = PatientFilter(data={}, queryset=qs)
        assert f.qs.count() == 2

    def test_filter_other_returns_empty(self, patients):
        qs = Patient.objects.all()
        f = PatientFilter(data={'gender': 'OTHER'}, queryset=qs)
        assert f.qs.count() == 0

    def test_filter_combined_with_status(self, patients):
        male, _ = patients
        male.is_active = False
        male.save()

        qs = Patient.objects.all()
        f = PatientFilter(data={'gender': 'MALE', 'is_active': 'true'}, queryset=qs)
        assert f.qs.count() == 0

        f2 = PatientFilter(data={'gender': 'FEMALE', 'is_active': 'true'}, queryset=qs)
        assert f2.qs.count() == 1
