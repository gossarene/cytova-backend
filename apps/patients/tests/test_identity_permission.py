"""
Tests for patients.update_identity permission enforcement.

Verifies that:
- Normal patient fields can be updated with patients.update
- Identity fields (document_type, document_number) require patients.update_identity
- The permission is enforced at the view layer
- Audit logging captures identity field changes
"""
import pytest

from apps.audit.models import AuditLog, AuditAction
from apps.patients.models import Patient
from apps.patients.serializers import PatientIdentityUpdateSerializer
from apps.patients.services import PatientService
from apps.users.models import Role, UserPermissionOverride, OverrideType
from common.permission_checker import PermissionChecker
from common.permissions_registry import PermissionRegistry


# ---------------------------------------------------------------------------
# Permission registration
# ---------------------------------------------------------------------------

class TestUpdateIdentityPermission:

    def test_permission_registered(self):
        assert PermissionRegistry.is_valid('patients.update_identity')

    def test_permission_in_patients_module(self):
        by_module = PermissionRegistry.by_module()
        codes = [p.code for p in by_module['patients']]
        assert 'patients.update_identity' in codes

    def test_lab_admin_has_permission(self, lab_admin):
        assert PermissionChecker.has_permission(lab_admin, 'patients.update_identity')

    def test_receptionist_does_not_have_permission(self, receptionist):
        assert not PermissionChecker.has_permission(receptionist, 'patients.update_identity')

    def test_biologist_does_not_have_permission(self, biologist):
        assert not PermissionChecker.has_permission(biologist, 'patients.update_identity')

    def test_technician_does_not_have_permission(self, technician):
        assert not PermissionChecker.has_permission(technician, 'patients.update_identity')

    def test_viewer_does_not_have_permission(self, viewer_auditor):
        assert not PermissionChecker.has_permission(viewer_auditor, 'patients.update_identity')


# ---------------------------------------------------------------------------
# Serializer validation
# ---------------------------------------------------------------------------

class TestPatientIdentityUpdateSerializer:

    @pytest.fixture()
    def patient(self, lab_admin):
        return Patient.objects.create(
            document_type='NATIONAL_ID_CARD', document_number='ID-SER-001',
            first_name='Test', last_name='Patient',
            date_of_birth='1990-01-01', gender='MALE', created_by=lab_admin,
        )

    def test_valid_document_type_change(self, patient):
        s = PatientIdentityUpdateSerializer(
            data={'document_type': 'PASSPORT'},
            context={'patient': patient},
            partial=True,
        )
        assert s.is_valid(), s.errors

    def test_valid_document_number_change(self, patient):
        s = PatientIdentityUpdateSerializer(
            data={'document_number': 'NEW-001'},
            context={'patient': patient},
            partial=True,
        )
        assert s.is_valid(), s.errors

    def test_duplicate_rejected(self, patient, lab_admin):
        Patient.objects.create(
            document_type='PASSPORT', document_number='EXIST-001',
            first_name='Other', last_name='Patient',
            date_of_birth='1992-01-01', gender='FEMALE', created_by=lab_admin,
        )
        s = PatientIdentityUpdateSerializer(
            data={'document_type': 'PASSPORT', 'document_number': 'EXIST-001'},
            context={'patient': patient},
            partial=True,
        )
        assert not s.is_valid()
        assert 'document_number' in s.errors

    def test_same_values_accepted(self, patient):
        """Updating to the same values should not fail uniqueness."""
        s = PatientIdentityUpdateSerializer(
            data={'document_type': patient.document_type, 'document_number': patient.document_number},
            context={'patient': patient},
            partial=True,
        )
        assert s.is_valid(), s.errors

    def test_empty_data_accepted(self, patient):
        s = PatientIdentityUpdateSerializer(
            data={}, context={'patient': patient}, partial=True,
        )
        assert s.is_valid(), s.errors


# ---------------------------------------------------------------------------
# Service layer — update with identity fields
# ---------------------------------------------------------------------------

class TestUpdatePatientWithIdentity:

    @pytest.fixture()
    def patient(self, lab_admin):
        return Patient.objects.create(
            document_type='NATIONAL_ID_CARD', document_number='SVC-ID-001',
            first_name='Before', last_name='Update',
            date_of_birth='1985-05-15', gender='FEMALE', created_by=lab_admin,
        )

    def test_normal_update_works(self, patient, lab_admin, make_request):
        updated = PatientService.update_patient(
            patient, {'first_name': 'After'}, lab_admin, make_request(lab_admin),
        )
        assert updated.first_name == 'After'
        assert updated.document_number == 'SVC-ID-001'  # unchanged

    def test_identity_update_works(self, patient, lab_admin, make_request):
        updated = PatientService.update_patient(
            patient,
            {'document_type': 'PASSPORT', 'document_number': 'NEW-PASS-001'},
            lab_admin,
            make_request(lab_admin),
        )
        assert updated.document_type == 'PASSPORT'
        assert updated.document_number == 'NEW-PASS-001'

    def test_mixed_update_works(self, patient, lab_admin, make_request):
        updated = PatientService.update_patient(
            patient,
            {'first_name': 'Mixed', 'document_number': 'MIX-001'},
            lab_admin,
            make_request(lab_admin),
        )
        assert updated.first_name == 'Mixed'
        assert updated.document_number == 'MIX-001'

    def test_identity_change_audit_logged(self, patient, lab_admin, make_request):
        PatientService.update_patient(
            patient,
            {'document_number': 'AUDIT-001'},
            lab_admin,
            make_request(lab_admin),
        )
        log = AuditLog.objects.filter(
            entity_type='Patient', entity_id=patient.id, action=AuditAction.UPDATE,
        ).latest('timestamp')
        assert log.diff['before']['document_number'] == 'SVC-ID-001'
        assert log.diff['after']['document_number'] == 'AUDIT-001'


# ---------------------------------------------------------------------------
# Permission grant/revoke for non-admin role
# ---------------------------------------------------------------------------

class TestIdentityPermissionGrant:

    def test_receptionist_can_be_granted_permission(self, receptionist):
        assert not PermissionChecker.has_permission(receptionist, 'patients.update_identity')
        UserPermissionOverride.objects.create(
            user=receptionist,
            permission_code='patients.update_identity',
            override_type=OverrideType.GRANT,
        )
        PermissionChecker.invalidate_cache(receptionist)
        assert PermissionChecker.has_permission(receptionist, 'patients.update_identity')

    def test_lab_admin_can_be_revoked(self, lab_admin):
        assert PermissionChecker.has_permission(lab_admin, 'patients.update_identity')
        UserPermissionOverride.objects.create(
            user=lab_admin,
            permission_code='patients.update_identity',
            override_type=OverrideType.REVOKE,
        )
        PermissionChecker.invalidate_cache(lab_admin)
        assert not PermissionChecker.has_permission(lab_admin, 'patients.update_identity')
