"""
Cytova — Suppliers & Procurement Serializers
"""
from decimal import Decimal

from rest_framework import serializers

from apps.stock.models import StockItem, StockLot
from .models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    Reception,
    ReceptionItem,
    Supplier,
)


# ---------------------------------------------------------------------------
# Supplier
# ---------------------------------------------------------------------------

class SupplierListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = [
            'id', 'name', 'contact_name', 'email', 'phone',
            'is_active', 'created_at',
        ]


class SupplierDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = [
            'id', 'name', 'contact_name', 'email', 'phone',
            'address', 'notes', 'is_active', 'created_at', 'updated_at',
        ]


class SupplierCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    contact_name = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default='',
    )
    email = serializers.EmailField(required=False, allow_blank=True, default='')
    phone = serializers.CharField(
        max_length=50, required=False, allow_blank=True, default='',
    )
    address = serializers.CharField(required=False, allow_blank=True, default='')
    notes = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_name(self, value):
        if Supplier.objects.filter(name=value).exists():
            raise serializers.ValidationError(
                'A supplier with this name already exists.'
            )
        return value


class SupplierUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255, required=False)
    contact_name = serializers.CharField(
        max_length=255, required=False, allow_blank=True,
    )
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(
        max_length=50, required=False, allow_blank=True,
    )
    address = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate_name(self, value):
        instance = self.context.get('instance')
        qs = Supplier.objects.filter(name=value)
        if instance:
            qs = qs.exclude(pk=instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                'A supplier with this name already exists.'
            )
        return value


# ---------------------------------------------------------------------------
# PurchaseOrderItem
# ---------------------------------------------------------------------------

class PurchaseOrderItemSerializer(serializers.ModelSerializer):
    stock_item_code = serializers.CharField(
        source='stock_item.code', read_only=True,
    )
    stock_item_name = serializers.CharField(
        source='stock_item.name', read_only=True,
    )
    stock_item_unit = serializers.CharField(
        source='stock_item.unit', read_only=True,
    )
    remaining_quantity = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseOrderItem
        fields = [
            'id', 'stock_item_id', 'stock_item_code', 'stock_item_name',
            'stock_item_unit', 'ordered_quantity', 'received_quantity',
            'remaining_quantity', 'unit_price', 'notes', 'created_at',
        ]

    def get_remaining_quantity(self, obj):
        remaining = obj.ordered_quantity - obj.received_quantity
        return max(Decimal('0'), remaining)


class PurchaseOrderItemCreateSerializer(serializers.Serializer):
    stock_item_id = serializers.UUIDField()
    ordered_quantity = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=Decimal('0.0001'),
    )
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=4, required=False, allow_null=True,
        min_value=Decimal('0'),
    )
    notes = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_stock_item_id(self, value):
        if not StockItem.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError(
                'Stock item not found or inactive.'
            )
        return value


# Reused for inline creation when creating an order
class PurchaseOrderItemInlineSerializer(PurchaseOrderItemCreateSerializer):
    pass


# ---------------------------------------------------------------------------
# PurchaseOrder
# ---------------------------------------------------------------------------

class PurchaseOrderListSerializer(serializers.ModelSerializer):
    supplier_name = serializers.CharField(
        source='supplier.name', read_only=True,
    )
    items_count = serializers.SerializerMethodField()
    receptions_count = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseOrder
        fields = [
            'id', 'order_number', 'supplier_id', 'supplier_name',
            'status', 'expected_delivery_date',
            'items_count', 'receptions_count', 'created_at',
        ]

    def get_items_count(self, obj):
        return obj.items.count()

    def get_receptions_count(self, obj):
        return obj.receptions.count()


class PurchaseOrderDetailSerializer(serializers.ModelSerializer):
    supplier = SupplierListSerializer(read_only=True)
    items = PurchaseOrderItemSerializer(many=True, read_only=True)
    confirmed_by_email = serializers.EmailField(
        source='confirmed_by.email', read_only=True, default=None,
    )
    cancelled_by_email = serializers.EmailField(
        source='cancelled_by.email', read_only=True, default=None,
    )
    closed_by_email = serializers.EmailField(
        source='closed_by.email', read_only=True, default=None,
    )
    created_by_email = serializers.EmailField(
        source='created_by.email', read_only=True, default=None,
    )
    receptions_count = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseOrder
        fields = [
            'id', 'order_number', 'supplier', 'status',
            'expected_delivery_date', 'notes',
            'confirmed_at', 'confirmed_by_email',
            'cancelled_at', 'cancelled_by_email',
            'closed_at', 'closed_by_email',
            'created_by_email', 'receptions_count',
            'items', 'created_at', 'updated_at',
        ]

    def get_receptions_count(self, obj):
        return obj.receptions.count()


class PurchaseOrderCreateSerializer(serializers.Serializer):
    supplier_id = serializers.UUIDField()
    expected_delivery_date = serializers.DateField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, default='')
    items = PurchaseOrderItemInlineSerializer(many=True, required=False, default=list)

    def validate_supplier_id(self, value):
        if not Supplier.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError('Supplier not found or inactive.')
        return value

    def validate_items(self, value):
        if not value:
            return value
        seen_ids = set()
        for item in value:
            sid = str(item.get('stock_item_id'))
            if sid in seen_ids:
                raise serializers.ValidationError(
                    f'Duplicate stock item in order lines: {sid}.'
                )
            seen_ids.add(sid)
        return value


class PurchaseOrderUpdateSerializer(serializers.Serializer):
    """Only notes and expected_delivery_date may be updated on a DRAFT order."""
    expected_delivery_date = serializers.DateField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True)


# ---------------------------------------------------------------------------
# ReceptionItem
# ---------------------------------------------------------------------------

class ReceptionItemSerializer(serializers.ModelSerializer):
    stock_item_code = serializers.CharField(
        source='order_item.stock_item.code', read_only=True,
    )
    stock_item_name = serializers.CharField(
        source='order_item.stock_item.name', read_only=True,
    )
    stock_lot_id = serializers.UUIDField(
        source='stock_lot.id', read_only=True, default=None,
    )

    class Meta:
        model = ReceptionItem
        fields = [
            'id', 'order_item_id', 'stock_item_code', 'stock_item_name',
            'received_quantity', 'lot_number', 'expiry_date', 'unit_cost',
            'discrepancy_quantity', 'notes', 'stock_lot_id', 'created_at',
        ]


class ReceptionItemCreateSerializer(serializers.Serializer):
    order_item_id = serializers.UUIDField()
    received_quantity = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=Decimal('0.0001'),
    )
    lot_number = serializers.CharField(max_length=100)
    expiry_date = serializers.DateField(required=False, allow_null=True)
    unit_cost = serializers.DecimalField(
        max_digits=12, decimal_places=4, required=False, allow_null=True,
        min_value=Decimal('0'),
    )
    notes = serializers.CharField(required=False, allow_blank=True, default='')


# ---------------------------------------------------------------------------
# Reception
# ---------------------------------------------------------------------------

class ReceptionListSerializer(serializers.ModelSerializer):
    received_by_email = serializers.EmailField(
        source='received_by.email', read_only=True, default=None,
    )
    items_count = serializers.SerializerMethodField()

    class Meta:
        model = Reception
        fields = [
            'id', 'order_id', 'received_at', 'received_by_email',
            'has_discrepancy', 'items_count', 'created_at',
        ]

    def get_items_count(self, obj):
        return obj.items.count()


class ReceptionDetailSerializer(serializers.ModelSerializer):
    received_by_email = serializers.EmailField(
        source='received_by.email', read_only=True, default=None,
    )
    items = ReceptionItemSerializer(many=True, read_only=True)

    class Meta:
        model = Reception
        fields = [
            'id', 'order_id', 'received_at', 'received_by_email',
            'notes', 'has_discrepancy', 'items', 'created_at',
        ]


class ReceptionCreateSerializer(serializers.Serializer):
    received_at = serializers.DateField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True, default='')
    items = ReceptionItemCreateSerializer(many=True, min_length=1)

    def validate_items(self, value):
        seen_ids = set()
        for item in value:
            oid = str(item.get('order_item_id'))
            if oid in seen_ids:
                raise serializers.ValidationError(
                    f'Duplicate order item in reception lines: {oid}.'
                )
            seen_ids.add(oid)
        return value
