"""
Cytova — Suppliers & Procurement Models

Supplier
    Company that delivers stock items to the lab. Name unique within tenant.
    Hard delete blocked — use deactivation.

PurchaseOrder
    A formal procurement request issued to a supplier.
    `order_number` is auto-generated after first save: PO-{YYYY}-{8-char UUID}.

    Lifecycle:
        DRAFT             — editable; items can be added/removed
        CONFIRMED         — locked; no further item changes; receptions may begin
        PARTIALLY_RECEIVED — at least one reception recorded, not yet complete
        RECEIVED          — all ordered quantities accounted for (or force-closed)
        CANCELLED         — terminal; no receptions possible

    Hard delete blocked.

PurchaseOrderItem
    One stock item line on a purchase order. `ordered_quantity` is fixed at
    confirmation. `received_quantity` is a cached running total updated by the
    service layer on every reception.

    Hard delete is blocked. For DRAFT orders the service uses queryset.delete()
    to remove a line before confirmation. Once CONFIRMED, no removal is possible.

Reception
    An immutable record of a physical delivery event against a PurchaseOrder.
    A single order may have multiple receptions (partial deliveries).
    Triggers StockLot creation for every line item via the service layer.
    Immutable after creation — save() raises PermissionError on update.

ReceptionItem
    What was received for one PurchaseOrderItem in one reception event.
    `discrepancy_quantity` = received_quantity − remaining_quantity at the
    moment of reception:
        > 0  over-delivered (received more than was still owed)
        = 0  exact
        < 0  accepted short delivery for this batch
    `stock_lot` is the StockLot created atomically with this record.
    Immutable after creation.
"""
import uuid
from decimal import Decimal

from django.db import models
from django.utils import timezone

from common.models import BaseModel


# ---------------------------------------------------------------------------
# Supplier
# ---------------------------------------------------------------------------

class Supplier(BaseModel):
    name = models.CharField(
        max_length=255,
        unique=True,
        help_text='Company name. Must be unique within the tenant.',
    )
    contact_name = models.CharField(max_length=255, blank=True, default='')
    email = models.EmailField(blank=True, default='')
    phone = models.CharField(max_length=50, blank=True, default='')
    address = models.TextField(blank=True, default='')
    notes = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        verbose_name = 'Supplier'
        verbose_name_plural = 'Suppliers'
        ordering = ['name']

    def __str__(self):
        return self.name

    def delete(self, *args, **kwargs):
        raise PermissionError(
            'Suppliers cannot be deleted. Use deactivation instead.'
        )


# ---------------------------------------------------------------------------
# PurchaseOrder
# ---------------------------------------------------------------------------

class PurchaseOrderStatus(models.TextChoices):
    DRAFT             = 'DRAFT',             'Draft'
    CONFIRMED         = 'CONFIRMED',         'Confirmed'
    PARTIALLY_RECEIVED = 'PARTIALLY_RECEIVED', 'Partially Received'
    RECEIVED          = 'RECEIVED',          'Received'
    CANCELLED         = 'CANCELLED',         'Cancelled'


# Statuses from which no further state change is possible
TERMINAL_ORDER_STATUSES = frozenset({
    PurchaseOrderStatus.RECEIVED,
    PurchaseOrderStatus.CANCELLED,
})

# Statuses that allow receptions to be recorded
RECEIVABLE_STATUSES = frozenset({
    PurchaseOrderStatus.CONFIRMED,
    PurchaseOrderStatus.PARTIALLY_RECEIVED,
})


class PurchaseOrder(BaseModel):
    """
    Formal procurement request to a supplier.

    `order_number` is blank at first save; the service generates and writes it
    immediately after. Never set order_number manually.
    """
    order_number = models.CharField(
        max_length=30,
        unique=True,
        blank=True,
        db_index=True,
        help_text='Auto-generated: PO-{YYYY}-{8-char UUID}.',
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name='purchase_orders',
    )
    status = models.CharField(
        max_length=25,
        choices=PurchaseOrderStatus.choices,
        default=PurchaseOrderStatus.DRAFT,
        db_index=True,
    )
    expected_delivery_date = models.DateField(
        null=True,
        blank=True,
        db_index=True,
        help_text='Requested or agreed delivery date.',
    )
    notes = models.TextField(blank=True, default='')

    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='confirmed_purchase_orders',
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cancelled_purchase_orders',
    )
    closed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Set when order is force-closed with outstanding quantities.',
    )
    closed_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='closed_purchase_orders',
    )
    created_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_purchase_orders',
    )

    class Meta:
        verbose_name = 'Purchase Order'
        verbose_name_plural = 'Purchase Orders'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['supplier', 'status']),
            models.Index(fields=['status', 'expected_delivery_date']),
        ]

    def __str__(self):
        return f'{self.order_number} — {self.supplier.name}'

    def delete(self, *args, **kwargs):
        raise PermissionError(
            'Purchase orders cannot be deleted. Cancel instead.'
        )


# ---------------------------------------------------------------------------
# PurchaseOrderItem
# ---------------------------------------------------------------------------

class PurchaseOrderItem(BaseModel):
    """
    One stock-item line on a purchase order.

    `received_quantity` is updated atomically by ReceptionService on every
    reception — never write it directly.
    """
    order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name='items',
    )
    stock_item = models.ForeignKey(
        'stock.StockItem',
        on_delete=models.PROTECT,
        related_name='purchase_order_items',
    )
    ordered_quantity = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        help_text='Quantity requested from the supplier.',
    )
    received_quantity = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal('0'),
        help_text='Running total received. Maintained by ReceptionService.',
    )
    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        null=True,
        blank=True,
        help_text='Negotiated price per unit for this order.',
    )
    notes = models.TextField(blank=True, default='')

    class Meta:
        verbose_name = 'Purchase Order Item'
        verbose_name_plural = 'Purchase Order Items'
        ordering = ['created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['order', 'stock_item'],
                name='unique_stock_item_per_order',
            )
        ]

    def __str__(self):
        return f'{self.order.order_number} / {self.stock_item.code}'

    def delete(self, *args, **kwargs):
        raise PermissionError(
            'Purchase order items cannot be hard-deleted. '
            'Remove from DRAFT order via the service layer.'
        )


# ---------------------------------------------------------------------------
# Reception
# ---------------------------------------------------------------------------

class Reception(BaseModel):
    """
    Immutable record of a physical delivery event.

    save() raises PermissionError on any update — use queryset.update() for
    the single allowed mutation (has_discrepancy, set at creation time).
    """
    order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.PROTECT,
        related_name='receptions',
    )
    received_at = models.DateField(
        default=timezone.now,
        db_index=True,
        help_text='Date the goods physically arrived.',
    )
    received_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='receptions',
    )
    notes = models.TextField(blank=True, default='')
    has_discrepancy = models.BooleanField(
        default=False,
        db_index=True,
        help_text='True if any item was over- or short-delivered.',
    )

    class Meta:
        verbose_name = 'Reception'
        verbose_name_plural = 'Receptions'
        ordering = ['-received_at', '-created_at']
        indexes = [
            models.Index(fields=['order', 'received_at']),
        ]

    def __str__(self):
        return f'Reception {self.id} for {self.order.order_number}'

    def save(self, *args, **kwargs):
        """Immutable after creation."""
        if not self._state.adding:
            raise PermissionError(
                'Receptions are immutable audit records and cannot be updated.'
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError(
            'Receptions cannot be deleted. They are part of the traceability chain.'
        )


# ---------------------------------------------------------------------------
# ReceptionItem
# ---------------------------------------------------------------------------

class ReceptionItem(BaseModel):
    """
    One stock-item line within a reception event.

    `discrepancy_quantity` = received_quantity − (ordered_quantity − previously_received):
        > 0  over-delivered
        = 0  exact or accepted partial
        < 0  accepted short delivery for this batch

    `stock_lot` is the StockLot created atomically when this record is saved.
    Both this record and its lot are immutable after creation.
    """
    reception = models.ForeignKey(
        Reception,
        on_delete=models.CASCADE,
        related_name='items',
    )
    order_item = models.ForeignKey(
        PurchaseOrderItem,
        on_delete=models.PROTECT,
        related_name='reception_items',
    )
    received_quantity = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        help_text='Quantity physically received in this event.',
    )
    lot_number = models.CharField(
        max_length=100,
        help_text='Manufacturer or internal lot/batch number for this delivery.',
    )
    expiry_date = models.DateField(
        null=True,
        blank=True,
        help_text='Leave blank for non-expiring items.',
    )
    unit_cost = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        null=True,
        blank=True,
        help_text='Actual cost per unit for this delivery. Defaults to order unit_price.',
    )
    discrepancy_quantity = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal('0'),
        help_text=(
            'Signed discrepancy: received − remaining at time of reception. '
            'Positive = over-delivered, negative = accepted short delivery.'
        ),
    )
    notes = models.TextField(blank=True, default='')
    stock_lot = models.OneToOneField(
        'stock.StockLot',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reception_item',
        help_text='The StockLot created by this reception item.',
    )

    class Meta:
        verbose_name = 'Reception Item'
        verbose_name_plural = 'Reception Items'
        ordering = ['created_at']

    def __str__(self):
        return f'{self.reception_id} / {self.order_item.stock_item.code}'

    def save(self, *args, **kwargs):
        """Immutable after creation."""
        if not self._state.adding:
            raise PermissionError(
                'Reception items are immutable audit records and cannot be updated.'
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError(
            'Reception items cannot be deleted. They are part of the traceability chain.'
        )
