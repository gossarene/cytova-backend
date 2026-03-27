"""
Cytova — Inventory Alert Services

InventoryAlertService
    User-facing actions: acknowledge, resolve, bulk_acknowledge.

InventoryAlertScanService
    Batch scanner invoked by Celery tasks (or on-demand).
    1. Auto-resolves alerts whose conditions no longer apply.
    2. Creates new alerts for newly detected conditions.
    Idempotent — safe to run repeatedly; duplicate prevention via
    conditional UniqueConstraints and application-level checks.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError
from django.db.models import DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.audit.models import AuditAction, AuditLog, ActorType
from apps.stock.models import StockItem, StockLot
from apps.users.models import StaffUser
from .models import (
    ALERT_SEVERITY_MAP,
    OPEN_STATUSES,
    AlertStatus,
    AlertType,
    InventoryAlert,
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
# InventoryAlertService  (user-facing actions)
# ---------------------------------------------------------------------------

class InventoryAlertService:

    @staticmethod
    def acknowledge(
        alert: InventoryAlert,
        acknowledged_by: StaffUser,
        request,
    ) -> InventoryAlert:
        if alert.status != AlertStatus.ACTIVE:
            from rest_framework.exceptions import ValidationError
            raise ValidationError(
                f'Only ACTIVE alerts can be acknowledged (current: {alert.status}).'
            )

        alert.status = AlertStatus.ACKNOWLEDGED
        alert.acknowledged_at = timezone.now()
        alert.acknowledged_by = acknowledged_by
        alert.save(update_fields=[
            'status', 'acknowledged_at', 'acknowledged_by', 'updated_at',
        ])

        _audit(
            actor=acknowledged_by,
            action=AuditAction.UPDATE,
            entity_type='InventoryAlert',
            entity_id=alert.id,
            diff={'after': {'status': AlertStatus.ACKNOWLEDGED}},
            request=request,
        )

        return alert

    @staticmethod
    def resolve(
        alert: InventoryAlert,
        resolved_by: StaffUser,
        request,
    ) -> InventoryAlert:
        if alert.status == AlertStatus.RESOLVED:
            from rest_framework.exceptions import ValidationError
            raise ValidationError('Alert is already resolved.')

        alert.status = AlertStatus.RESOLVED
        alert.resolved_at = timezone.now()
        alert.resolved_by = resolved_by
        alert.save(update_fields=[
            'status', 'resolved_at', 'resolved_by', 'updated_at',
        ])

        _audit(
            actor=resolved_by,
            action=AuditAction.UPDATE,
            entity_type='InventoryAlert',
            entity_id=alert.id,
            diff={'after': {'status': AlertStatus.RESOLVED}},
            request=request,
        )

        return alert

    @staticmethod
    def bulk_acknowledge(
        alert_ids: list,
        acknowledged_by: StaffUser,
        request,
    ) -> int:
        """
        Acknowledges multiple ACTIVE alerts. Returns the number updated.
        """
        now = timezone.now()
        count = InventoryAlert.objects.filter(
            id__in=alert_ids,
            status=AlertStatus.ACTIVE,
        ).update(
            status=AlertStatus.ACKNOWLEDGED,
            acknowledged_at=now,
            acknowledged_by=acknowledged_by,
            updated_at=now,
        )

        if count:
            _audit(
                actor=acknowledged_by,
                action=AuditAction.UPDATE,
                entity_type='InventoryAlert',
                entity_id=None,
                diff={'after': {
                    'action': 'bulk_acknowledge',
                    'count': count,
                }},
                request=request,
            )

        return count


# ---------------------------------------------------------------------------
# InventoryAlertScanService  (automated scanning)
# ---------------------------------------------------------------------------

class InventoryAlertScanService:
    """
    Scans inventory state and creates/resolves alerts accordingly.
    All methods are idempotent and safe to call concurrently (the DB
    UniqueConstraint prevents duplicate open alerts).
    """

    @classmethod
    def run_full_scan(cls) -> dict:
        """
        Runs all scan phases in order. Returns a summary dict of actions taken.
        """
        summary = {
            'auto_resolved': cls._auto_resolve(),
            'low_stock_created': cls._scan_low_stock(),
            'out_of_stock_created': cls._scan_out_of_stock(),
            'expiring_soon_created': cls._scan_expiring_soon(),
            'expired_created': cls._scan_expired(),
        }
        logger.info('Alert scan complete: %s', summary)
        return summary

    # ----- auto-resolve -----

    @classmethod
    def _auto_resolve(cls) -> int:
        """
        Resolves open alerts whose condition no longer applies.
        """
        count = 0
        count += cls._resolve_low_stock()
        count += cls._resolve_out_of_stock()
        count += cls._resolve_lot_alerts()
        return count

    @classmethod
    def _resolve_low_stock(cls) -> int:
        """Resolve LOW_STOCK alerts where available_qty >= threshold."""
        alerts = InventoryAlert.objects.filter(
            alert_type=AlertType.LOW_STOCK,
            status__in=OPEN_STATUSES,
        ).select_related('stock_item')

        to_resolve = []
        for alert in alerts:
            item = alert.stock_item
            if not item.is_active:
                to_resolve.append(alert.pk)
                continue
            available = cls._available_qty(item)
            if available >= item.minimum_threshold:
                to_resolve.append(alert.pk)

        return cls._bulk_resolve(to_resolve)

    @classmethod
    def _resolve_out_of_stock(cls) -> int:
        """Resolve OUT_OF_STOCK alerts where available_qty > 0."""
        alerts = InventoryAlert.objects.filter(
            alert_type=AlertType.OUT_OF_STOCK,
            status__in=OPEN_STATUSES,
        ).select_related('stock_item')

        to_resolve = []
        for alert in alerts:
            item = alert.stock_item
            if not item.is_active:
                to_resolve.append(alert.pk)
                continue
            if cls._available_qty(item) > Decimal('0'):
                to_resolve.append(alert.pk)

        return cls._bulk_resolve(to_resolve)

    @classmethod
    def _resolve_lot_alerts(cls) -> int:
        """Resolve EXPIRING_SOON / EXPIRED alerts where the lot is exhausted."""
        alerts = InventoryAlert.objects.filter(
            alert_type__in=[AlertType.EXPIRING_SOON, AlertType.EXPIRED],
            status__in=OPEN_STATUSES,
            stock_lot__isnull=False,
        ).select_related('stock_lot')

        to_resolve = [
            alert.pk for alert in alerts
            if alert.stock_lot.is_exhausted
        ]

        return cls._bulk_resolve(to_resolve)

    @classmethod
    def _bulk_resolve(cls, alert_pks: list) -> int:
        if not alert_pks:
            return 0
        now = timezone.now()
        return InventoryAlert.objects.filter(pk__in=alert_pks).update(
            status=AlertStatus.RESOLVED,
            resolved_at=now,
            updated_at=now,
        )

    # ----- low stock -----

    @classmethod
    def _scan_low_stock(cls) -> int:
        """Create LOW_STOCK alerts for items below threshold."""
        items = (
            StockItem.objects
            .filter(is_active=True, minimum_threshold__gt=Decimal('0'))
            .annotate(available_qty=cls._available_qty_annotation())
            .filter(available_qty__lt=F('minimum_threshold'), available_qty__gt=0)
        )

        created = 0
        for item in items:
            created += cls._create_alert(
                alert_type=AlertType.LOW_STOCK,
                stock_item=item,
                stock_lot=None,
                threshold=item.minimum_threshold,
                current=item.available_qty,
                title=f'Low Stock: [{item.code}] {item.name}',
                message=(
                    f'Available quantity ({item.available_qty}) is below '
                    f'the minimum threshold ({item.minimum_threshold}).'
                ),
            )
        return created

    # ----- out of stock -----

    @classmethod
    def _scan_out_of_stock(cls) -> int:
        """Create OUT_OF_STOCK alerts for items with zero available qty."""
        items = (
            StockItem.objects
            .filter(is_active=True)
            .annotate(available_qty=cls._available_qty_annotation())
            .filter(available_qty__lte=0)
        )

        created = 0
        for item in items:
            created += cls._create_alert(
                alert_type=AlertType.OUT_OF_STOCK,
                stock_item=item,
                stock_lot=None,
                threshold=None,
                current=Decimal('0'),
                title=f'Out of Stock: [{item.code}] {item.name}',
                message='All lots are exhausted. No stock available.',
            )
        return created

    # ----- expiring soon -----

    @classmethod
    def _scan_expiring_soon(cls) -> int:
        """Create EXPIRING_SOON alerts for lots within the warning window."""
        warning_days = getattr(settings, 'ALERT_EXPIRY_WARNING_DAYS', 30)
        today = timezone.now().date()
        cutoff = today + timedelta(days=warning_days)

        lots = StockLot.objects.filter(
            is_exhausted=False,
            expiry_date__isnull=False,
            expiry_date__gt=today,
            expiry_date__lte=cutoff,
            item__is_active=True,
        ).select_related('item')

        created = 0
        for lot in lots:
            days_left = (lot.expiry_date - today).days
            created += cls._create_alert(
                alert_type=AlertType.EXPIRING_SOON,
                stock_item=lot.item,
                stock_lot=lot,
                threshold=Decimal(str(warning_days)),
                current=Decimal(str(days_left)),
                title=(
                    f'Expiring Soon: Lot {lot.lot_number} '
                    f'[{lot.item.code}]'
                ),
                message=(
                    f'Lot {lot.lot_number} for {lot.item.name} expires on '
                    f'{lot.expiry_date} ({days_left} day{"s" if days_left != 1 else ""} remaining).'
                ),
            )
        return created

    # ----- expired -----

    @classmethod
    def _scan_expired(cls) -> int:
        """Create EXPIRED alerts for lots past their expiry date."""
        today = timezone.now().date()

        lots = StockLot.objects.filter(
            is_exhausted=False,
            expiry_date__isnull=False,
            expiry_date__lte=today,
            item__is_active=True,
        ).select_related('item')

        created = 0
        for lot in lots:
            days_past = (today - lot.expiry_date).days
            created += cls._create_alert(
                alert_type=AlertType.EXPIRED,
                stock_item=lot.item,
                stock_lot=lot,
                threshold=None,
                current=Decimal(str(-days_past)),
                title=f'Expired: Lot {lot.lot_number} [{lot.item.code}]',
                message=(
                    f'Lot {lot.lot_number} for {lot.item.name} expired on '
                    f'{lot.expiry_date} ({days_past} day{"s" if days_past != 1 else ""} ago). '
                    f'Current quantity: {lot.current_quantity}.'
                ),
            )
        return created

    # ----- helpers -----

    @staticmethod
    def _create_alert(
        *,
        alert_type: str,
        stock_item: StockItem,
        stock_lot,
        threshold,
        current,
        title: str,
        message: str,
    ) -> int:
        """
        Creates an alert if no open alert already exists for the same condition.
        Returns 1 if created, 0 if duplicate. Gracefully handles race conditions
        via IntegrityError from the UniqueConstraint.
        """
        # Application-level duplicate check (fast path)
        qs = InventoryAlert.objects.filter(
            alert_type=alert_type,
            stock_item=stock_item,
            status__in=OPEN_STATUSES,
        )
        if stock_lot is not None:
            qs = qs.filter(stock_lot=stock_lot)
        else:
            qs = qs.filter(stock_lot__isnull=True)

        if qs.exists():
            return 0

        try:
            InventoryAlert.objects.create(
                alert_type=alert_type,
                severity=ALERT_SEVERITY_MAP[alert_type],
                stock_item=stock_item,
                stock_lot=stock_lot,
                title=title,
                message=message,
                threshold_value=threshold,
                current_value=current,
            )
            return 1
        except IntegrityError:
            # Race condition — another process created the alert first
            return 0

    @staticmethod
    def _available_qty_annotation():
        """Reusable annotation for sum of non-exhausted lot quantities."""
        return Coalesce(
            Sum(
                'lots__current_quantity',
                filter=Q(lots__is_exhausted=False),
            ),
            Value(Decimal('0')),
            output_field=DecimalField(),
        )

    @staticmethod
    def _available_qty(item: StockItem) -> Decimal:
        """Compute available quantity for a single item."""
        result = StockLot.objects.filter(
            item=item, is_exhausted=False,
        ).aggregate(
            total=Coalesce(
                Sum('current_quantity'),
                Value(Decimal('0')),
                output_field=DecimalField(),
            ),
        )
        return result['total']
