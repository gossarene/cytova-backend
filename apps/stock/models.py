"""
Cytova — Stock Models

StockCategory
    Thematic grouping for stock items. Name unique within the tenant.
    Hard delete blocked — use deactivation.

StockItem
    A supply type. `code` is the stable cross-module identifier (unique,
    uppercase). Hard delete blocked — use deactivation.

StockLot
    A physical batch of a StockItem, identified by its lot number. Carries
    the received quantity, unit cost, expiry date, and supplier for this
    specific delivery. `current_quantity` is a cached running total kept
    in sync by every StockMovement recorded against the lot — queries never
    need to aggregate movements to know the available quantity.
    Hard delete blocked — lots are part of the traceability chain.

StockMovement
    Immutable, append-only record of a quantity change on a lot.
    Five movement types cover the full operational vocabulary:
        IN              — stock received from supplier
        OUT             — stock consumed in lab operations
        ADJUSTMENT_IN   — positive inventory correction (recount)
        ADJUSTMENT_OUT  — negative inventory correction (recount)
        LOSS            — physical loss (damage, spillage, expiry)

    `quantity` is always stored as a positive number; direction is
    implied by the movement type:
        positive (IN, ADJUSTMENT_IN)            → +quantity
        negative (OUT, ADJUSTMENT_OUT, LOSS)    → −quantity

    `quantity_before` and `quantity_after` are snapshots at the exact
    moment the movement was recorded — enabling full point-in-time
    reconstruction of lot stock levels without replaying movements.

    save() and delete() are permanently blocked — movements are medical-
    grade audit records.
"""
import uuid
from decimal import Decimal

from django.db import models
from django.utils import timezone

from common.models import BaseModel


# ---------------------------------------------------------------------------
# StockCategory
# ---------------------------------------------------------------------------

class StockCategory(BaseModel):
    name = models.CharField(max_length=150, unique=True)
    description = models.TextField(blank=True, default='')
    display_order = models.IntegerField(default=0, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        verbose_name = 'Stock Category'
        verbose_name_plural = 'Stock Categories'
        ordering = ['display_order', 'name']

    def __str__(self):
        return self.name

    def delete(self, *args, **kwargs):
        raise PermissionError(
            'Stock categories cannot be deleted. Use deactivation instead.'
        )


# ---------------------------------------------------------------------------
# StockItem
# ---------------------------------------------------------------------------

class StockItem(BaseModel):
    """
    `code` is the stable cross-module identifier used as the reference key
    for lot-level traceability. Stored uppercase by the service layer.

    `main_supplier_name` is a plain text field until apps.suppliers is
    implemented, at which point it becomes a FK.
    """
    category = models.ForeignKey(
        StockCategory,
        on_delete=models.PROTECT,
        related_name='items',
    )
    code = models.CharField(max_length=50, unique=True, db_index=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default='')
    unit = models.CharField(
        max_length=50,
        help_text='Unit of measure (e.g. mL, box, vial, tube, piece).',
    )
    minimum_threshold = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal('0'),
        help_text=(
            'Low-stock alert threshold. An alert fires when the sum of '
            'non-exhausted lot quantities falls below this value.'
        ),
    )
    reorder_quantity = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        null=True,
        blank=True,
        help_text='Suggested order quantity (future procurement module).',
    )
    main_supplier_name = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text='Will become a FK to Supplier once that module is implemented.',
    )
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        verbose_name = 'Stock Item'
        verbose_name_plural = 'Stock Items'
        ordering = ['category__display_order', 'name']
        indexes = [
            models.Index(fields=['category', 'is_active']),
        ]

    def __str__(self):
        return f'[{self.code}] {self.name}'

    def delete(self, *args, **kwargs):
        raise PermissionError(
            'Stock items cannot be deleted. Use deactivation instead.'
        )


# ---------------------------------------------------------------------------
# StockLot
# ---------------------------------------------------------------------------

class StockLot(BaseModel):
    """
    A physical delivery batch of a StockItem.

    `lot_number` uniquely identifies the batch within an item (manufacturer
    lot number or internal reception number).

    `current_quantity` is maintained atomically by StockLotService using
    SELECT FOR UPDATE — it must never be written outside the service layer.

    `is_exhausted` is set True when current_quantity reaches zero. Exhausted
    lots are excluded from available-stock calculations but are retained for
    the traceability record.
    """
    item = models.ForeignKey(
        StockItem,
        on_delete=models.PROTECT,
        related_name='lots',
    )
    lot_number = models.CharField(
        max_length=100,
        help_text='Manufacturer or internal lot / batch number.',
    )
    expiry_date = models.DateField(
        null=True,
        blank=True,
        db_index=True,
        help_text='Leave blank for non-expiring items (equipment, etc.).',
    )
    supplier_name = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text='Supplier for this specific delivery.',
    )
    initial_quantity = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        help_text='Quantity received at lot creation. Immutable after first save.',
    )
    current_quantity = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        help_text='Running total. Maintained by StockLotService — do not write directly.',
    )
    unit_cost = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        null=True,
        blank=True,
        help_text='Cost per unit for this delivery (optional).',
    )
    received_at = models.DateField(
        default=timezone.now,
        db_index=True,
    )
    notes = models.TextField(blank=True, default='')
    is_exhausted = models.BooleanField(
        default=False,
        db_index=True,
        help_text='True when current_quantity <= 0.',
    )

    class Meta:
        verbose_name = 'Stock Lot'
        verbose_name_plural = 'Stock Lots'
        ordering = ['-received_at', 'lot_number']
        constraints = [
            models.UniqueConstraint(
                fields=['item', 'lot_number'],
                name='unique_lot_number_per_item',
            )
        ]
        indexes = [
            models.Index(fields=['item', 'is_exhausted']),
            models.Index(fields=['expiry_date']),
        ]

    def __str__(self):
        return f'{self.item.code} / {self.lot_number}'

    def delete(self, *args, **kwargs):
        raise PermissionError(
            'Stock lots cannot be deleted. They are part of the traceability chain.'
        )


# ---------------------------------------------------------------------------
# StockMovement
# ---------------------------------------------------------------------------

class MovementType(models.TextChoices):
    IN             = 'IN',             'Stock In (received from supplier)'
    OUT            = 'OUT',            'Stock Out (consumed in operations)'
    ADJUSTMENT_IN  = 'ADJUSTMENT_IN',  'Adjustment In (inventory recount +)'
    ADJUSTMENT_OUT = 'ADJUSTMENT_OUT', 'Adjustment Out (inventory recount −)'
    LOSS           = 'LOSS',           'Loss (damage / spillage / expiry)'


# Movement types that reduce lot quantity
DECREASING_TYPES = frozenset({
    MovementType.OUT,
    MovementType.ADJUSTMENT_OUT,
    MovementType.LOSS,
})


class StockMovement(models.Model):
    """
    Immutable record of a quantity change on a StockLot.

    Snapshots (quantity_before, quantity_after) provide full point-in-time
    traceability without replaying the movement chain.

    save()   raises PermissionError if the record already exists.
    delete() is permanently blocked.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    lot = models.ForeignKey(
        StockLot,
        on_delete=models.PROTECT,
        related_name='movements',
    )
    movement_type = models.CharField(
        max_length=20,
        choices=MovementType.choices,
        db_index=True,
    )

    # Always positive — direction implied by movement_type
    quantity = models.DecimalField(max_digits=12, decimal_places=4)

    # Point-in-time snapshots
    quantity_before = models.DecimalField(max_digits=12, decimal_places=4)
    quantity_after = models.DecimalField(max_digits=12, decimal_places=4)

    reason = models.TextField(
        blank=True,
        default='',
        help_text='Required for LOSS, ADJUSTMENT_IN, ADJUSTMENT_OUT.',
    )
    reference = models.CharField(
        max_length=100,
        blank=True,
        default='',
        help_text='Soft link to another entity (e.g. REQ-2024-ABCD1234).',
    )
    reference_type = models.CharField(
        max_length=50,
        blank=True,
        default='',
        help_text='Type of referenced entity (e.g. AnalysisRequest, PurchaseOrder).',
    )

    performed_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        related_name='stock_movements',
    )
    performed_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        verbose_name = 'Stock Movement'
        verbose_name_plural = 'Stock Movements'
        ordering = ['-performed_at']
        indexes = [
            models.Index(fields=['lot', 'performed_at']),
            models.Index(fields=['movement_type', 'performed_at']),
        ]

    def __str__(self):
        return f'{self.movement_type} {self.quantity} × {self.lot}'

    def save(self, *args, **kwargs):
        """Immutable after creation."""
        if not self._state.adding:
            raise PermissionError(
                'Stock movements are immutable audit records and cannot be updated.'
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError(
            'Stock movements cannot be deleted. They are part of the traceability chain.'
        )
