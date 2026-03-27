"""
Tests for request source tracking — SourceType, BillingMode, partner FK.

Covers:
- Direct patient request creation
- Partner-originated request creation
- Validation: partner required when source_type=PARTNER_ORGANIZATION
- Validation: partner must be null when source_type=DIRECT_PATIENT
- Validation: billing_mode consistency
- Validation: inactive partner rejected
- Source fields in update (DRAFT only)
- Audit logging captures source info
"""
import pytest

from apps.audit.models import AuditLog, AuditAction
from apps.requests.models import BillingMode, RequestStatus, SourceType
from apps.requests.serializers import (
    AnalysisRequestCreateSerializer,
    AnalysisRequestUpdateSerializer,
)
from apps.requests.services import AnalysisRequestService


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------

class TestDirectPatientRequest:

    def test_create_direct_patient_request(self, patient, lab_admin, make_request):
        ar = AnalysisRequestService.create(
            validated_data={
                'patient_id': patient.id,
                'notes': 'Walk-in patient',
                'source_type': SourceType.DIRECT_PATIENT,
                'billing_mode': BillingMode.DIRECT_PAYMENT,
                'items': [],
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
        )

        assert ar.source_type == SourceType.DIRECT_PATIENT
        assert ar.partner_organization is None
        assert ar.billing_mode == BillingMode.DIRECT_PAYMENT
        assert ar.external_reference == ''
        assert ar.status == RequestStatus.DRAFT

    def test_defaults_to_direct_patient(self, patient, lab_admin, make_request):
        ar = AnalysisRequestService.create(
            validated_data={
                'patient_id': patient.id,
                'items': [],
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
        )

        assert ar.source_type == SourceType.DIRECT_PATIENT
        assert ar.billing_mode == BillingMode.DIRECT_PAYMENT


class TestPartnerOriginatedRequest:

    def test_create_partner_request(self, patient, partner_org, lab_admin, make_request):
        ar = AnalysisRequestService.create(
            validated_data={
                'patient_id': patient.id,
                'source_type': SourceType.PARTNER_ORGANIZATION,
                'partner_organization_id': partner_org.id,
                'external_reference': 'EXT-REF-2026-001',
                'billing_mode': BillingMode.PARTNER_BILLING,
                'source_notes': 'Referred by Dr. Test',
                'items': [],
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
        )

        assert ar.source_type == SourceType.PARTNER_ORGANIZATION
        assert ar.partner_organization_id == partner_org.id
        assert ar.external_reference == 'EXT-REF-2026-001'
        assert ar.billing_mode == BillingMode.PARTNER_BILLING
        assert ar.source_notes == 'Referred by Dr. Test'

    def test_partner_request_with_direct_billing(
        self, patient, partner_org, lab_admin, make_request,
    ):
        """A partner can refer a patient who pays directly."""
        ar = AnalysisRequestService.create(
            validated_data={
                'patient_id': patient.id,
                'source_type': SourceType.PARTNER_ORGANIZATION,
                'partner_organization_id': partner_org.id,
                'billing_mode': BillingMode.DIRECT_PAYMENT,
                'items': [],
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
        )
        assert ar.billing_mode == BillingMode.DIRECT_PAYMENT

    def test_audit_log_captures_source(
        self, patient, partner_org, lab_admin, make_request,
    ):
        ar = AnalysisRequestService.create(
            validated_data={
                'patient_id': patient.id,
                'source_type': SourceType.PARTNER_ORGANIZATION,
                'partner_organization_id': partner_org.id,
                'billing_mode': BillingMode.PARTNER_BILLING,
                'items': [],
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
        )

        log = AuditLog.objects.filter(
            entity_type='AnalysisRequest',
            entity_id=ar.id,
            action=AuditAction.CREATE,
        ).first()
        assert log is not None
        assert log.diff['after']['source_type'] == SourceType.PARTNER_ORGANIZATION
        assert log.diff['after']['partner_organization_id'] == str(partner_org.id)
        assert log.diff['after']['billing_mode'] == BillingMode.PARTNER_BILLING


class TestSourceTrackingUpdate:

    def test_update_source_on_draft(
        self, patient, partner_org, lab_admin, make_request,
    ):
        ar = AnalysisRequestService.create(
            validated_data={
                'patient_id': patient.id,
                'items': [],
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
        )
        assert ar.source_type == SourceType.DIRECT_PATIENT

        ar = AnalysisRequestService.update(
            analysis_request=ar,
            validated_data={
                'source_type': SourceType.PARTNER_ORGANIZATION,
                'partner_organization_id': partner_org.id,
                'billing_mode': BillingMode.PARTNER_BILLING,
            },
            updated_by=lab_admin,
            request=make_request(lab_admin),
        )
        assert ar.source_type == SourceType.PARTNER_ORGANIZATION
        assert ar.partner_organization_id == partner_org.id


# ---------------------------------------------------------------------------
# Serializer validation tests
# ---------------------------------------------------------------------------

class TestSourceValidation:

    def test_partner_org_required_for_partner_source(self, patient):
        s = AnalysisRequestCreateSerializer(data={
            'patient_id': str(patient.id),
            'source_type': SourceType.PARTNER_ORGANIZATION,
            'billing_mode': BillingMode.PARTNER_BILLING,
        })
        assert not s.is_valid()
        assert 'partner_organization_id' in s.errors

    def test_partner_org_must_be_null_for_direct(self, patient, partner_org):
        s = AnalysisRequestCreateSerializer(data={
            'patient_id': str(patient.id),
            'source_type': SourceType.DIRECT_PATIENT,
            'partner_organization_id': str(partner_org.id),
            'billing_mode': BillingMode.DIRECT_PAYMENT,
        })
        assert not s.is_valid()
        assert 'partner_organization_id' in s.errors

    def test_partner_billing_invalid_for_direct(self, patient):
        s = AnalysisRequestCreateSerializer(data={
            'patient_id': str(patient.id),
            'source_type': SourceType.DIRECT_PATIENT,
            'billing_mode': BillingMode.PARTNER_BILLING,
        })
        assert not s.is_valid()
        assert 'billing_mode' in s.errors

    def test_inactive_partner_rejected(self, patient, inactive_partner):
        s = AnalysisRequestCreateSerializer(data={
            'patient_id': str(patient.id),
            'source_type': SourceType.PARTNER_ORGANIZATION,
            'partner_organization_id': str(inactive_partner.id),
            'billing_mode': BillingMode.PARTNER_BILLING,
        })
        assert not s.is_valid()
        assert 'partner_organization_id' in s.errors

    def test_valid_direct_patient_request(self, patient):
        s = AnalysisRequestCreateSerializer(data={
            'patient_id': str(patient.id),
            'source_type': SourceType.DIRECT_PATIENT,
            'billing_mode': BillingMode.DIRECT_PAYMENT,
        })
        assert s.is_valid(), s.errors

    def test_valid_partner_request(self, patient, partner_org):
        s = AnalysisRequestCreateSerializer(data={
            'patient_id': str(patient.id),
            'source_type': SourceType.PARTNER_ORGANIZATION,
            'partner_organization_id': str(partner_org.id),
            'billing_mode': BillingMode.PARTNER_BILLING,
        })
        assert s.is_valid(), s.errors

    def test_update_cross_validation_with_instance(
        self, patient, partner_org, lab_admin, make_request,
    ):
        """
        Switching to DIRECT_PATIENT while partner_organization_id is still
        set on the instance (not in payload) must fail.
        """
        ar = AnalysisRequestService.create(
            validated_data={
                'patient_id': patient.id,
                'source_type': SourceType.PARTNER_ORGANIZATION,
                'partner_organization_id': partner_org.id,
                'billing_mode': BillingMode.PARTNER_BILLING,
                'items': [],
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
        )

        s = AnalysisRequestUpdateSerializer(
            data={'source_type': SourceType.DIRECT_PATIENT},
            context={'instance': ar},
        )
        assert not s.is_valid()
        assert 'partner_organization_id' in s.errors

    def test_update_clear_partner_when_switching_to_direct(
        self, patient, partner_org, lab_admin, make_request,
    ):
        """Explicitly nulling partner while switching to DIRECT_PATIENT must pass."""
        ar = AnalysisRequestService.create(
            validated_data={
                'patient_id': patient.id,
                'source_type': SourceType.PARTNER_ORGANIZATION,
                'partner_organization_id': partner_org.id,
                'billing_mode': BillingMode.PARTNER_BILLING,
                'items': [],
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
        )

        s = AnalysisRequestUpdateSerializer(
            data={
                'source_type': SourceType.DIRECT_PATIENT,
                'partner_organization_id': None,
                'billing_mode': BillingMode.DIRECT_PAYMENT,
            },
            context={'instance': ar},
        )
        assert s.is_valid(), s.errors
