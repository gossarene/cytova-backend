"""
Cytova — Stock Service

All stock write operations with business logic live here.
Views validate input and delegate; audit logging covers every mutation.

StockCategoryService: create, update, deactivate
StockItemService:     create, update, deactivate
StockLotService:      create, record_movement
"""
import logging
from decimal import Decimal

from django.db import transaction

from apps.audit.models import AuditAction, AuditLog, ActorType
from apps.users.models import StaffUser
from .models import DECREASING_TYPES, StockCategory, StockItem, StockLot, StockMovement

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


# ---------------------------------------------------------------------------
# StockCategoryService
# ---------------------------------------------------------------------------

class StockCategoryService:

    @staticmethod
    def create(
        validated_data: dict,
        created_by: StaffUser,
        request,
    ) -> StockCategory:
        category = StockCategory(**validated_data)
        category.save()

        _audit(
            actor=created_by,
            action=AuditAction.CREATE,
            entity_type='StockCategory',
            entity_id=category.id,
            diff={'after': {'name': category.name}},
            request=request,
        )

        return category

    @staticmethod
    def update(
        category: StockCategory,
        validated_data: dict,
        updated_by: StaffUser,
        request,
    ) -> StockCategory:
        before = {k: getattr(category, k) for k in validated_data}

        for field, value in validated_data.items():
            setattr(category, field, value)
        category.save(update_fields=list(validated_data.keys()) + ['updated_at'])

        after = {k: getattr(category, k) for k in validated_data}

        _audit(
            actor=updated_by,
            action=AuditAction.UPDATE,
            entity_type='StockCategory',
            entity_id=category.id,
            diff={'before': before, 'after': after},
            request=request,
        )

        return category

    @staticmethod
    def deactivate(
        category: StockCategory,
        deactivated_by: StaffUser,
        request,
    ) -> StockCategory:
        if not category.is_active:
            return category

        category.is_active = False
        category.save(update_fields=['is_active', 'updated_at'])

        _audit(
            actor=deactivated_by,
            action=AuditAction.DEACTIVATE,
            entity_type='StockCategory',
            entity_id=category.id,
            diff={'after': {'is_active': False}},
            request=request,
        )

        return category


# ---------------------------------------------------------------------------
# StockItemService
# ---------------------------------------------------------------------------

class StockItemService:

    @staticmethod
    def create(
        validated_data: dict,
        created_by: StaffUser,
        request,
    ) -> StockItem:
        item = StockItem(**validated_data)
        item.save()

        _audit(
            actor=created_by,
            action=AuditAction.CREATE,
            entity_type='StockItem',
            entity_id=item.id,
            diff={'after': {
                'code': item.code,
                'name': item.name,
                'unit': item.unit,
            }},
            request=request,
        )

        return item

    @staticmethod
    def update(
        item: StockItem,
        validated_data: dict,
        updated_by: StaffUser,
        request,
    ) -> StockItem:
        before = {k: getattr(item, k) for k in validated_data}

        for field, value in validated_data.items():
            setattr(item, field, value)
        item.save(update_fields=list(validated_data.keys()) + ['updated_at'])

        after = {k: getattr(item, k) for k in validated_data}

        _audit(
            actor=updated_by,
            action=AuditAction.UPDATE,
            entity_type='StockItem',
            entity_id=item.id,
            diff={'before': before, 'after': after},
            request=request,
        )

        return item

    @staticmethod
    def deactivate(
        item: StockItem,
        deactivated_by: StaffUser,
        request,
    ) -> StockItem:
        if not item.is_active:
            return item

        item.is_active = False
        item.save(update_fields=['is_active', 'updated_at'])

        _audit(
            actor=deactivated_by,
            action=AuditAction.DEACTIVATE,
            entity_type='StockItem',
            entity_id=item.id,
            diff={'after': {'is_active': False}},
            request=request,
        )

        return item


# ---------------------------------------------------------------------------
# StockLotService
# ---------------------------------------------------------------------------

class StockLotService:

    @staticmethod
    @transaction.atomic
    def create(
        item: StockItem,
        validated_data: dict,
        created_by: StaffUser,
        request,
    ) -> StockLot:
        """
        Creates a new lot and records the initial IN movement in the same
        transaction. `current_quantity` is set equal to `initial_quantity`.
        """
        initial_quantity = validated_data['initial_quantity']

        lot = StockLot(
            item=item,
            lot_number=validated_data['lot_number'],
            expiry_date=validated_data.get('expiry_date'),
            supplier_name=validated_data.get('supplier_name', ''),
            initial_quantity=initial_quantity,
            current_quantity=initial_quantity,
            unit_cost=validated_data.get('unit_cost'),
            notes=validated_data.get('notes', ''),
        )
        if 'received_at' in validated_data:
            lot.received_at = validated_data['received_at']
        lot.save()

        # Record the initial reception movement
        StockMovement.objects.create(
            lot=lot,
            movement_type='IN',
            quantity=initial_quantity,
            quantity_before=Decimal('0'),
            quantity_after=initial_quantity,
            reason='Initial stock reception',
            performed_by=created_by,
        )

        _audit(
            actor=created_by,
            action=AuditAction.CREATE,
            entity_type='StockLot',
            entity_id=lot.id,
            diff={'after': {
                'item_code': item.code,
                'lot_number': lot.lot_number,
                'initial_quantity': str(initial_quantity),
            }},
            request=request,
        )

        return lot

    @staticmethod
    @transaction.atomic
    def record_movement(
        lot: StockLot,
        movement_type: str,
        quantity: Decimal,
        reason: str,
        reference: str,
        reference_type: str,
        performed_by: StaffUser,
        request,
    ) -> StockMovement:
        """
        Records a stock movement against the given lot.

        Uses SELECT FOR UPDATE to prevent concurrent movements from producing
        inconsistent quantity snapshots. Raises ValueError if the movement
        would result in a negative stock quantity.
        """
        lot = StockLot.objects.select_for_update().get(pk=lot.pk)

        quantity_before = lot.current_quantity

        if movement_type in DECREASING_TYPES:
            new_quantity = quantity_before - quantity
            if new_quantity < Decimal('0'):
                raise ValueError(
                    f'Movement of {quantity} would result in negative stock '
                    f'(current: {quantity_before}).'
                )
        else:
            new_quantity = quantity_before + quantity

        movement = StockMovement.objects.create(
            lot=lot,
            movement_type=movement_type,
            quantity=quantity,
            quantity_before=quantity_before,
            quantity_after=new_quantity,
            reason=reason,
            reference=reference,
            reference_type=reference_type,
            performed_by=performed_by,
        )

        lot.current_quantity = new_quantity
        lot.is_exhausted = new_quantity <= Decimal('0')
        lot.save(update_fields=['current_quantity', 'is_exhausted', 'updated_at'])

        _audit(
            actor=performed_by,
            action=AuditAction.UPDATE,
            entity_type='StockMovement',
            entity_id=movement.id,
            diff={
                'lot_id': str(lot.id),
                'movement_type': movement_type,
                'quantity': str(quantity),
                'quantity_before': str(quantity_before),
                'quantity_after': str(new_quantity),
            },
            request=request,
        )

        return movement
