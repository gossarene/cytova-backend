"""
Cytova — Suppliers & Procurement Services

All business logic and audit logging for the suppliers module.

SupplierService:       create, update, deactivate
PurchaseOrderService:  create, add_item, remove_item, update, confirm, cancel, close
ReceptionService:      create  (immutable; triggers StockLotService per item)
"""
import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.audit.models import AuditAction, AuditLog, ActorType
from apps.stock.services import StockLotService
from apps.users.models import StaffUser
from .models import (
    RECEIVABLE_STATUSES,
    TERMINAL_ORDER_STATUSES,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    Reception,
    ReceptionItem,
    Supplier,
)

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
# SupplierService
# ---------------------------------------------------------------------------

class SupplierService:

    @staticmethod
    def create(
        validated_data: dict,
        created_by: StaffUser,
        request,
    ) -> Supplier:
        supplier = Supplier(**validated_data)
        supplier.save()

        _audit(
            actor=created_by,
            action=AuditAction.CREATE,
            entity_type='Supplier',
            entity_id=supplier.id,
            diff={'after': {'name': supplier.name}},
            request=request,
        )

        return supplier

    @staticmethod
    def update(
        supplier: Supplier,
        validated_data: dict,
        updated_by: StaffUser,
        request,
    ) -> Supplier:
        before = {k: getattr(supplier, k) for k in validated_data}

        for field, value in validated_data.items():
            setattr(supplier, field, value)
        supplier.save(update_fields=list(validated_data.keys()) + ['updated_at'])

        after = {k: getattr(supplier, k) for k in validated_data}

        _audit(
            actor=updated_by,
            action=AuditAction.UPDATE,
            entity_type='Supplier',
            entity_id=supplier.id,
            diff={'before': before, 'after': after},
            request=request,
        )

        return supplier

    @staticmethod
    def deactivate(
        supplier: Supplier,
        deactivated_by: StaffUser,
        request,
    ) -> Supplier:
        if not supplier.is_active:
            return supplier

        supplier.is_active = False
        supplier.save(update_fields=['is_active', 'updated_at'])

        _audit(
            actor=deactivated_by,
            action=AuditAction.DEACTIVATE,
            entity_type='Supplier',
            entity_id=supplier.id,
            diff={'after': {'is_active': False}},
            request=request,
        )

        return supplier


# ---------------------------------------------------------------------------
# PurchaseOrderService
# ---------------------------------------------------------------------------

class PurchaseOrderService:

    @staticmethod
    @transaction.atomic
    def create(
        validated_data: dict,
        created_by: StaffUser,
        request,
    ) -> PurchaseOrder:
        """
        Creates a DRAFT order, auto-generates order_number, then adds any
        inline items provided in `validated_data['items']`.
        """
        items_data = validated_data.pop('items', [])
        supplier_id = validated_data.pop('supplier_id')

        order = PurchaseOrder(
            supplier_id=supplier_id,
            created_by=created_by,
            **validated_data,
        )
        order.save()

        # Auto-generate order number after PK is assigned
        year = order.created_at.year
        uid_fragment = str(order.id).upper().replace('-', '')[:8]
        order.order_number = f'PO-{year}-{uid_fragment}'
        order.save(update_fields=['order_number'])

        for item_data in items_data:
            PurchaseOrderService._create_item(order, item_data)

        _audit(
            actor=created_by,
            action=AuditAction.CREATE,
            entity_type='PurchaseOrder',
            entity_id=order.id,
            diff={'after': {
                'order_number': order.order_number,
                'supplier_id': str(supplier_id),
                'item_count': len(items_data),
            }},
            request=request,
        )

        return order

    @staticmethod
    def _create_item(order: PurchaseOrder, item_data: dict) -> PurchaseOrderItem:
        stock_item_id = item_data['stock_item_id']
        if PurchaseOrderItem.objects.filter(
            order=order, stock_item_id=stock_item_id
        ).exists():
            raise ValidationError(
                {'stock_item_id': f'Stock item {stock_item_id} is already on this order.'}
            )
        item = PurchaseOrderItem(
            order=order,
            stock_item_id=stock_item_id,
            ordered_quantity=item_data['ordered_quantity'],
            unit_price=item_data.get('unit_price'),
            notes=item_data.get('notes', ''),
        )
        item.save()
        return item

    @staticmethod
    def add_item(
        order: PurchaseOrder,
        validated_data: dict,
        added_by: StaffUser,
        request,
    ) -> PurchaseOrderItem:
        if order.status != PurchaseOrderStatus.DRAFT:
            raise ValidationError(
                'Items can only be added to DRAFT orders.'
            )

        item = PurchaseOrderService._create_item(order, validated_data)

        _audit(
            actor=added_by,
            action=AuditAction.UPDATE,
            entity_type='PurchaseOrder',
            entity_id=order.id,
            diff={'after': {
                'action': 'item_added',
                'stock_item_id': str(validated_data['stock_item_id']),
                'ordered_quantity': str(validated_data['ordered_quantity']),
            }},
            request=request,
        )

        return item

    @staticmethod
    def remove_item(
        order: PurchaseOrder,
        order_item: PurchaseOrderItem,
        removed_by: StaffUser,
        request,
    ) -> None:
        if order.status != PurchaseOrderStatus.DRAFT:
            raise ValidationError(
                'Items can only be removed from DRAFT orders.'
            )

        stock_item_code = order_item.stock_item.code
        # Use queryset.delete() to bypass the model-level guard
        PurchaseOrderItem.objects.filter(pk=order_item.pk).delete()

        _audit(
            actor=removed_by,
            action=AuditAction.UPDATE,
            entity_type='PurchaseOrder',
            entity_id=order.id,
            diff={'after': {
                'action': 'item_removed',
                'stock_item_code': stock_item_code,
            }},
            request=request,
        )

    @staticmethod
    def update(
        order: PurchaseOrder,
        validated_data: dict,
        updated_by: StaffUser,
        request,
    ) -> PurchaseOrder:
        if order.status != PurchaseOrderStatus.DRAFT:
            raise ValidationError('Only DRAFT orders can be updated.')

        before = {k: getattr(order, k) for k in validated_data}

        for field, value in validated_data.items():
            setattr(order, field, value)
        order.save(update_fields=list(validated_data.keys()) + ['updated_at'])

        after = {k: getattr(order, k) for k in validated_data}

        _audit(
            actor=updated_by,
            action=AuditAction.UPDATE,
            entity_type='PurchaseOrder',
            entity_id=order.id,
            diff={'before': before, 'after': after},
            request=request,
        )

        return order

    @staticmethod
    @transaction.atomic
    def confirm(
        order: PurchaseOrder,
        confirmed_by: StaffUser,
        request,
    ) -> PurchaseOrder:
        if order.status != PurchaseOrderStatus.DRAFT:
            raise ValidationError('Only DRAFT orders can be confirmed.')

        if not order.items.exists():
            raise ValidationError(
                'Cannot confirm an order with no items.'
            )

        order.status = PurchaseOrderStatus.CONFIRMED
        order.confirmed_at = timezone.now()
        order.confirmed_by = confirmed_by
        order.save(update_fields=['status', 'confirmed_at', 'confirmed_by', 'updated_at'])

        _audit(
            actor=confirmed_by,
            action=AuditAction.CONFIRM,
            entity_type='PurchaseOrder',
            entity_id=order.id,
            diff={'after': {'status': PurchaseOrderStatus.CONFIRMED}},
            request=request,
        )

        return order

    @staticmethod
    def cancel(
        order: PurchaseOrder,
        cancelled_by: StaffUser,
        request,
    ) -> PurchaseOrder:
        if order.status in TERMINAL_ORDER_STATUSES:
            raise ValidationError(
                f'Cannot cancel an order in {order.status} status.'
            )
        if order.status in (
            PurchaseOrderStatus.PARTIALLY_RECEIVED,
            PurchaseOrderStatus.RECEIVED,
        ):
            raise ValidationError(
                'Cannot cancel an order that has already received goods.'
            )

        order.status = PurchaseOrderStatus.CANCELLED
        order.cancelled_at = timezone.now()
        order.cancelled_by = cancelled_by
        order.save(
            update_fields=['status', 'cancelled_at', 'cancelled_by', 'updated_at']
        )

        _audit(
            actor=cancelled_by,
            action=AuditAction.CANCEL,
            entity_type='PurchaseOrder',
            entity_id=order.id,
            diff={'after': {'status': PurchaseOrderStatus.CANCELLED}},
            request=request,
        )

        return order

    @staticmethod
    def close(
        order: PurchaseOrder,
        closed_by: StaffUser,
        request,
    ) -> PurchaseOrder:
        """
        Force-complete an order even if not all quantities have been received.
        Used to accept a confirmed short delivery from the supplier.
        """
        if order.status not in RECEIVABLE_STATUSES:
            raise ValidationError(
                f'Only CONFIRMED or PARTIALLY_RECEIVED orders can be force-closed '
                f'(current status: {order.status}).'
            )

        order.status = PurchaseOrderStatus.RECEIVED
        order.closed_at = timezone.now()
        order.closed_by = closed_by
        order.save(
            update_fields=['status', 'closed_at', 'closed_by', 'updated_at']
        )

        _audit(
            actor=closed_by,
            action=AuditAction.UPDATE,
            entity_type='PurchaseOrder',
            entity_id=order.id,
            diff={'after': {
                'status': PurchaseOrderStatus.RECEIVED,
                'force_closed': True,
            }},
            request=request,
        )

        return order


# ---------------------------------------------------------------------------
# ReceptionService
# ---------------------------------------------------------------------------

class ReceptionService:

    @staticmethod
    @transaction.atomic
    def create(
        order: PurchaseOrder,
        validated_data: dict,
        received_by: StaffUser,
        request,
    ) -> Reception:
        """
        Records a physical delivery event.

        For each reception item:
        1. Validates the order_item belongs to this order.
        2. Computes discrepancy_quantity vs remaining_quantity.
        3. Calls StockLotService.create() to produce a StockLot atomically.
        4. Creates the ReceptionItem record linked to the lot.
        5. Updates PurchaseOrderItem.received_quantity via queryset.update().

        After all items are processed, auto-advances the order status.
        """
        if order.status not in RECEIVABLE_STATUSES:
            raise ValidationError(
                f'Receptions can only be recorded against CONFIRMED or '
                f'PARTIALLY_RECEIVED orders (current status: {order.status}).'
            )

        items_data = validated_data.pop('items')

        # Validate all order_item_ids upfront (fail fast, one error message)
        order_item_ids = [item['order_item_id'] for item in items_data]
        db_items = {
            str(oi.id): oi
            for oi in PurchaseOrderItem.objects.filter(
                order=order, id__in=order_item_ids
            ).select_related('stock_item')
        }
        missing = [
            str(oid) for oid in order_item_ids if str(oid) not in db_items
        ]
        if missing:
            raise ValidationError(
                {'items': f'Order items not found on this order: {missing}.'}
            )

        # Validate lot numbers are unique per stock item before touching DB
        ReceptionService._validate_lot_numbers(items_data, db_items)

        # Create the reception header
        reception_kwargs = {k: v for k, v in validated_data.items()}
        reception = Reception(
            order=order,
            received_by=received_by,
            **reception_kwargs,
        )
        reception.save()

        any_discrepancy = False
        reception_items = []

        for item_data in items_data:
            order_item = db_items[str(item_data['order_item_id'])]
            received_qty = item_data['received_quantity']
            remaining = order_item.ordered_quantity - order_item.received_quantity
            discrepancy = received_qty - remaining  # signed

            if discrepancy != Decimal('0'):
                any_discrepancy = True

            # Create the StockLot (and its initial IN movement) via service
            lot = StockLotService.create(
                item=order_item.stock_item,
                validated_data={
                    'lot_number': item_data['lot_number'],
                    'expiry_date': item_data.get('expiry_date'),
                    'supplier_name': order.supplier.name,
                    'initial_quantity': received_qty,
                    'unit_cost': item_data.get('unit_cost') or order_item.unit_price,
                    'notes': item_data.get('notes', ''),
                },
                created_by=received_by,
                request=request,
            )

            reception_item = ReceptionItem(
                reception=reception,
                order_item=order_item,
                received_quantity=received_qty,
                lot_number=item_data['lot_number'],
                expiry_date=item_data.get('expiry_date'),
                unit_cost=item_data.get('unit_cost') or order_item.unit_price,
                discrepancy_quantity=discrepancy,
                notes=item_data.get('notes', ''),
                stock_lot=lot,
            )
            reception_item.save()
            reception_items.append((order_item, received_qty))

        # Stamp has_discrepancy on the reception header now that we know
        if any_discrepancy:
            Reception.objects.filter(pk=reception.pk).update(has_discrepancy=True)
            reception.has_discrepancy = True

        # Update PurchaseOrderItem.received_quantity for each line
        for order_item, received_qty in reception_items:
            PurchaseOrderItem.objects.filter(pk=order_item.pk).update(
                received_quantity=order_item.received_quantity + received_qty
            )

        # Auto-advance order status
        ReceptionService._advance_order_status(order)

        _audit(
            actor=received_by,
            action=AuditAction.CREATE,
            entity_type='Reception',
            entity_id=reception.id,
            diff={
                'after': {
                    'order_number': order.order_number,
                    'item_count': len(items_data),
                    'has_discrepancy': any_discrepancy,
                }
            },
            request=request,
        )

        return reception

    @staticmethod
    def _validate_lot_numbers(items_data: list, db_items: dict) -> None:
        """
        Checks each lot_number is unique per stock_item across both:
        - Existing lots in the DB (UniqueConstraint catch)
        - The current reception batch (duplicate lines in the same request)
        Raises ValidationError with a list of all conflicts found.
        """
        from apps.stock.models import StockLot

        errors = []
        seen_in_batch: dict[str, set] = {}  # stock_item_id → set of lot numbers

        for item_data in items_data:
            order_item = db_items[str(item_data['order_item_id'])]
            stock_item = order_item.stock_item
            lot_number = item_data['lot_number']
            sid = str(stock_item.id)

            # Within this reception batch
            seen_in_batch.setdefault(sid, set())
            if lot_number in seen_in_batch[sid]:
                errors.append(
                    f'Duplicate lot number "{lot_number}" for item {stock_item.code} '
                    f'in the same reception.'
                )
            seen_in_batch[sid].add(lot_number)

            # Against the database
            if StockLot.objects.filter(
                item=stock_item, lot_number=lot_number
            ).exists():
                errors.append(
                    f'Lot number "{lot_number}" already exists for '
                    f'stock item {stock_item.code}.'
                )

        if errors:
            raise ValidationError({'items': errors})

    @staticmethod
    def _advance_order_status(order: PurchaseOrder) -> None:
        """
        Reloads all order items and advances the order to RECEIVED when every
        item's received_quantity >= ordered_quantity; otherwise PARTIALLY_RECEIVED.
        Uses queryset.update() to avoid the Reception immutability guard.
        """
        items = list(
            PurchaseOrderItem.objects.filter(order=order).values(
                'ordered_quantity', 'received_quantity'
            )
        )
        if not items:
            return

        all_received = all(
            item['received_quantity'] >= item['ordered_quantity']
            for item in items
        )
        new_status = (
            PurchaseOrderStatus.RECEIVED
            if all_received
            else PurchaseOrderStatus.PARTIALLY_RECEIVED
        )
        PurchaseOrder.objects.filter(pk=order.pk).update(
            status=new_status,
            updated_at=timezone.now(),
        )
        order.status = new_status
