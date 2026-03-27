"""
Tests for PartnerOrganizationService — CRUD + deactivation + audit.
"""
import pytest

from apps.audit.models import AuditLog, AuditAction
from apps.partners.models import OrganizationType, PartnerOrganization
from apps.partners.services import PartnerOrganizationService


class TestPartnerOrganizationCreate:

    def test_create_partner(self, lab_admin, make_request):
        partner = PartnerOrganizationService.create(
            validated_data={
                'code': 'CLN-001',
                'name': 'City Clinic',
                'organization_type': OrganizationType.CLINIC,
                'contact_person': 'Dr. Smith',
                'phone': '+1-555-0100',
                'email': 'contact@cityclinic.com',
                'address': '123 Main St',
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
        )

        assert partner.pk is not None
        assert partner.code == 'CLN-001'
        assert partner.name == 'City Clinic'
        assert partner.organization_type == OrganizationType.CLINIC
        assert partner.is_active is True

        log = AuditLog.objects.filter(
            entity_type='PartnerOrganization',
            entity_id=partner.id,
            action=AuditAction.CREATE,
        ).first()
        assert log is not None
        assert log.actor_email == lab_admin.email

    def test_create_partner_all_types(self, lab_admin, make_request):
        for i, org_type in enumerate(OrganizationType):
            partner = PartnerOrganizationService.create(
                validated_data={
                    'code': f'ORG-{i:03d}',
                    'name': f'Partner {org_type.label}',
                    'organization_type': org_type.value,
                },
                created_by=lab_admin,
                request=make_request(lab_admin),
            )
            assert partner.organization_type == org_type.value


class TestPartnerOrganizationUpdate:

    def test_update_partner(self, lab_admin, make_request):
        partner = PartnerOrganizationService.create(
            validated_data={
                'code': 'UPD-001',
                'name': 'Original Name',
                'organization_type': OrganizationType.HOSPITAL,
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
        )

        updated = PartnerOrganizationService.update(
            partner=partner,
            validated_data={
                'name': 'Updated Hospital',
                'contact_person': 'Dr. Jones',
                'payment_terms_days': 30,
            },
            updated_by=lab_admin,
            request=make_request(lab_admin),
        )

        assert updated.name == 'Updated Hospital'
        assert updated.contact_person == 'Dr. Jones'
        assert updated.payment_terms_days == 30
        assert updated.code == 'UPD-001'  # unchanged


class TestPartnerOrganizationDeactivate:

    def test_deactivate_partner(self, lab_admin, make_request):
        partner = PartnerOrganizationService.create(
            validated_data={
                'code': 'DEA-001',
                'name': 'Deactivatable Clinic',
                'organization_type': OrganizationType.CLINIC,
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
        )
        assert partner.is_active is True

        result = PartnerOrganizationService.deactivate(
            partner=partner,
            deactivated_by=lab_admin,
            request=make_request(lab_admin),
        )

        assert result.is_active is False
        log = AuditLog.objects.filter(
            entity_type='PartnerOrganization',
            entity_id=partner.id,
            action=AuditAction.DEACTIVATE,
        ).first()
        assert log is not None

    def test_deactivate_idempotent(self, lab_admin, make_request):
        partner = PartnerOrganizationService.create(
            validated_data={
                'code': 'DEA-002',
                'name': 'Already Inactive',
                'organization_type': OrganizationType.OTHER,
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
        )
        PartnerOrganizationService.deactivate(
            partner=partner,
            deactivated_by=lab_admin,
            request=make_request(lab_admin),
        )
        count_before = AuditLog.objects.filter(
            entity_type='PartnerOrganization',
            entity_id=partner.id,
            action=AuditAction.DEACTIVATE,
        ).count()

        # Second deactivation — no new audit log
        PartnerOrganizationService.deactivate(
            partner=partner,
            deactivated_by=lab_admin,
            request=make_request(lab_admin),
        )
        count_after = AuditLog.objects.filter(
            entity_type='PartnerOrganization',
            entity_id=partner.id,
            action=AuditAction.DEACTIVATE,
        ).count()
        assert count_after == count_before


class TestPartnerHardDeleteBlocked:

    def test_model_delete_raises(self, lab_admin, make_request):
        partner = PartnerOrganizationService.create(
            validated_data={
                'code': 'DEL-001',
                'name': 'Undeletable',
                'organization_type': OrganizationType.CLINIC,
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
        )
        with pytest.raises(PermissionError):
            partner.delete()
