"""
Cytova — Partner Organization Service

All business logic and audit logging for partner organizations.

PartnerOrganizationService: create, update, deactivate
"""
import logging

from apps.audit.models import AuditAction, AuditLog, ActorType
from apps.users.models import StaffUser
from .models import PartnerOrganization

logger = logging.getLogger(__name__)


def _audit(*, actor: StaffUser, action: str, entity_type: str, entity_id,
           diff: dict, request) -> None:
    AuditLog.objects.create(
        actor_type=ActorType.STAFF_USER,
        actor_id=actor.id,
        actor_email=actor.email,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        diff=diff,
        ip_address=getattr(request, 'audit_ip', None),
        user_agent=getattr(request, 'audit_user_agent', ''),
    )


class PartnerOrganizationService:

    @staticmethod
    def create(
        validated_data: dict,
        created_by: StaffUser,
        request,
    ) -> PartnerOrganization:
        partner = PartnerOrganization(**validated_data)
        partner.save()

        _audit(
            actor=created_by,
            action=AuditAction.CREATE,
            entity_type='PartnerOrganization',
            entity_id=partner.id,
            diff={'after': {
                'code': partner.code,
                'name': partner.name,
                'organization_type': partner.organization_type,
            }},
            request=request,
        )

        return partner

    @staticmethod
    def update(
        partner: PartnerOrganization,
        validated_data: dict,
        updated_by: StaffUser,
        request,
    ) -> PartnerOrganization:
        before = {k: getattr(partner, k) for k in validated_data}

        for field, value in validated_data.items():
            setattr(partner, field, value)
        partner.save(update_fields=list(validated_data.keys()) + ['updated_at'])

        after = {k: getattr(partner, k) for k in validated_data}

        _audit(
            actor=updated_by,
            action=AuditAction.UPDATE,
            entity_type='PartnerOrganization',
            entity_id=partner.id,
            diff={'before': before, 'after': after},
            request=request,
        )

        return partner

    @staticmethod
    def deactivate(
        partner: PartnerOrganization,
        deactivated_by: StaffUser,
        request,
    ) -> PartnerOrganization:
        if not partner.is_active:
            return partner

        partner.is_active = False
        partner.save(update_fields=['is_active', 'updated_at'])

        _audit(
            actor=deactivated_by,
            action=AuditAction.DEACTIVATE,
            entity_type='PartnerOrganization',
            entity_id=partner.id,
            diff={'after': {'is_active': False}},
            request=request,
        )

        return partner
