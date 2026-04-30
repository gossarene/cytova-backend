"""
Cytova — Partner Organization Service

All business logic and audit logging for partner organizations and their
agreed pricing configurations.

PartnerOrganizationService: create, update, deactivate
PartnerExamPriceService: create, update, deactivate, reactivate
"""
import logging

from rest_framework.exceptions import ValidationError

from apps.audit.models import AuditAction, AuditLog, ActorType
from apps.users.models import StaffUser
from .models import PartnerExamPrice, PartnerOrganization

logger = logging.getLogger(__name__)


def _branding_audit_value(value):
    """Coerce a branding field value into something JSON-safe + concise
    for the audit log. ``ImageFieldFile`` instances are surfaced as
    their storage path (``str(field)``) rather than the file bytes."""
    if value is None:
        return None
    # ``ImageFieldFile`` carries a ``name`` attribute that is the storage
    # key (or empty string when no file is attached). It evaluates truthy
    # only when a file is set, so falsy → empty payload.
    name_attr = getattr(value, 'name', None)
    if name_attr is not None and not isinstance(value, str):
        return name_attr or ''
    return value


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

    # Branding fields persisted by ``update_branding``. ``clear_logo`` is
    # excluded — it's a write-only flag that maps to deleting the logo
    # file rather than to a stored field.
    BRANDING_FIELDS = (
        'custom_report_branding_enabled',
        'report_header_name', 'report_header_subtitle',
        'report_header_address', 'report_header_phone',
        'report_header_email', 'report_header_logo',
        'report_footer_text',
    )

    @staticmethod
    def update_branding(
        partner: PartnerOrganization,
        validated_data: dict,
        updated_by: StaffUser,
        request,
    ) -> PartnerOrganization:
        """
        Apply the optional report-branding override.

        - ``clear_logo`` removes any existing uploaded file (delete on
          storage) before saving — it's intentionally distinct from
          uploading a replacement, which the serializer rejects in the
          same payload.
        - The ``ImageField`` itself handles writing the new file to
          storage when ``report_header_logo`` is supplied as an
          UploadedFile.
        - The audit diff records which field names changed but not the
          full file contents (only the storage key, via ``str(field)``)
          to keep the audit log readable.
        """
        clear_logo = validated_data.pop('clear_logo', False)

        before = {
            k: _branding_audit_value(getattr(partner, k))
            for k in PartnerOrganizationService.BRANDING_FIELDS
            if k in validated_data or (k == 'report_header_logo' and clear_logo)
        }

        if clear_logo and partner.report_header_logo:
            # ``ImageField.delete`` removes the underlying file via the
            # storage backend. Falling back to setting the field to None
            # alone would leave the file orphaned on disk.
            partner.report_header_logo.delete(save=False)

        update_fields = []
        for field, value in validated_data.items():
            setattr(partner, field, value)
            update_fields.append(field)

        if clear_logo and 'report_header_logo' not in update_fields:
            update_fields.append('report_header_logo')

        if update_fields:
            partner.save(update_fields=update_fields + ['updated_at'])

        after = {
            k: _branding_audit_value(getattr(partner, k))
            for k in before.keys()
        }

        if before:
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


# ---------------------------------------------------------------------------
# PartnerExamPrice
#
# Follows the same shape as the catalog reference services: create / update
# / deactivate / reactivate, every write producing one AuditLog row. All
# uniqueness and reactivation-conflict guards live here so the viewset
# stays a thin HTTP wrapper and the data model's partial unique constraint
# is the last line of defense (race-safe).
# ---------------------------------------------------------------------------

class PartnerExamPriceService:
    ENTITY = 'PartnerExamPrice'

    @staticmethod
    def create(
        partner: PartnerOrganization,
        validated_data: dict,
        created_by: StaffUser,
        request,
    ) -> PartnerExamPrice:
        price = PartnerExamPrice(
            partner=partner,
            exam_definition_id=validated_data['exam_definition_id'],
            agreed_price=validated_data['agreed_price'],
            notes=validated_data.get('notes', ''),
        )
        price.save()

        _audit(
            actor=created_by,
            action=AuditAction.CREATE,
            entity_type=PartnerExamPriceService.ENTITY,
            entity_id=price.id,
            diff={'after': {
                'partner_id': str(partner.id),
                'exam_definition_id': str(price.exam_definition_id),
                'agreed_price': str(price.agreed_price),
            }},
            request=request,
        )
        return price

    @staticmethod
    def update(
        price: PartnerExamPrice,
        validated_data: dict,
        updated_by: StaffUser,
        request,
    ) -> PartnerExamPrice:
        """
        Partial update of the negotiated price or notes. Changing
        ``agreed_price`` here never touches existing ``AnalysisRequestItem``
        rows — those snapshot ``billed_price`` at creation time, so the
        historical-integrity guarantee is enforced by the request data
        model, not by this service.
        """
        if not validated_data:
            return price

        before = {k: getattr(price, k) for k in validated_data}

        for field, value in validated_data.items():
            setattr(price, field, value)
        price.save(update_fields=list(validated_data.keys()) + ['updated_at'])

        after = {k: getattr(price, k) for k in validated_data}

        _audit(
            actor=updated_by,
            action=AuditAction.UPDATE,
            entity_type=PartnerExamPriceService.ENTITY,
            entity_id=price.id,
            diff={
                'before': {k: str(v) if v is not None else None for k, v in before.items()},
                'after': {k: str(v) if v is not None else None for k, v in after.items()},
            },
            request=request,
        )
        return price

    @staticmethod
    def deactivate(
        price: PartnerExamPrice,
        deactivated_by: StaffUser,
        request,
    ) -> PartnerExamPrice:
        if not price.is_active:
            return price

        price.is_active = False
        price.save(update_fields=['is_active', 'updated_at'])

        _audit(
            actor=deactivated_by,
            action=AuditAction.DEACTIVATE,
            entity_type=PartnerExamPriceService.ENTITY,
            entity_id=price.id,
            diff={'after': {'is_active': False}},
            request=request,
        )
        return price

    @staticmethod
    def reactivate(
        price: PartnerExamPrice,
        reactivated_by: StaffUser,
        request,
    ) -> PartnerExamPrice:
        """
        Flip an inactive row back to active. Rejects the call if another
        active row already exists for the same (partner, exam) pair —
        otherwise the partial unique constraint at the DB level would
        raise an IntegrityError. This gives the caller a clean 400 with
        an actionable message instead of a 500.
        """
        if price.is_active:
            return price

        conflict = PartnerExamPrice.objects.filter(
            partner_id=price.partner_id,
            exam_definition_id=price.exam_definition_id,
            is_active=True,
        ).exclude(pk=price.pk).exists()
        if conflict:
            raise ValidationError({
                'is_active': (
                    'Another active agreed price already exists for this '
                    'partner and exam. Deactivate it first before '
                    'reactivating this row.'
                ),
            })

        price.is_active = True
        price.save(update_fields=['is_active', 'updated_at'])

        _audit(
            actor=reactivated_by,
            action=AuditAction.REACTIVATE,
            entity_type=PartnerExamPriceService.ENTITY,
            entity_id=price.id,
            diff={'before': {'is_active': False}, 'after': {'is_active': True}},
            request=request,
        )
        return price
