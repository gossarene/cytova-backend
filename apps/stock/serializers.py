"""
Cytova — Stock Serializers
"""
from decimal import Decimal

from rest_framework import serializers

from .models import MovementType, StockCategory, StockItem, StockLot, StockMovement

REASON_REQUIRED_TYPES = frozenset({
    MovementType.LOSS,
    MovementType.ADJUSTMENT_IN,
    MovementType.ADJUSTMENT_OUT,
})


# ---------------------------------------------------------------------------
# StockCategory
# ---------------------------------------------------------------------------

class StockCategoryListSerializer(serializers.ModelSerializer):
    class Meta:
        model = StockCategory
        fields = ['id', 'name', 'description', 'display_order', 'is_active', 'created_at']


class StockCategoryDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = StockCategory
        fields = [
            'id', 'name', 'description', 'display_order',
            'is_active', 'created_at', 'updated_at',
        ]


class StockCategoryCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    display_order = serializers.IntegerField(required=False, default=0)

    def validate_name(self, value):
        if StockCategory.objects.filter(name=value).exists():
            raise serializers.ValidationError(
                'A stock category with this name already exists.'
            )
        return value


class StockCategoryUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    display_order = serializers.IntegerField(required=False)

    def validate_name(self, value):
        instance = self.context.get('instance')
        qs = StockCategory.objects.filter(name=value)
        if instance:
            qs = qs.exclude(pk=instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                'A stock category with this name already exists.'
            )
        return value


# ---------------------------------------------------------------------------
# StockItem
# ---------------------------------------------------------------------------

class StockItemListSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)
    # Populated via queryset annotation in StockItemViewSet.get_queryset()
    current_quantity = serializers.DecimalField(
        max_digits=12, decimal_places=4, read_only=True, default=Decimal('0')
    )

    class Meta:
        model = StockItem
        fields = [
            'id', 'code', 'name', 'category_id', 'category_name',
            'unit', 'minimum_threshold', 'current_quantity',
            'main_supplier_name', 'is_active', 'created_at',
        ]


class StockItemDetailSerializer(serializers.ModelSerializer):
    category = StockCategoryListSerializer(read_only=True)
    # Populated via queryset annotation in StockItemViewSet.get_queryset()
    current_quantity = serializers.DecimalField(
        max_digits=12, decimal_places=4, read_only=True, default=Decimal('0')
    )

    class Meta:
        model = StockItem
        fields = [
            'id', 'code', 'name', 'category',
            'description', 'unit',
            'minimum_threshold', 'reorder_quantity', 'current_quantity',
            'main_supplier_name',
            'is_active', 'created_at', 'updated_at',
        ]


class StockItemCreateSerializer(serializers.Serializer):
    category_id = serializers.UUIDField()
    code = serializers.CharField(max_length=50)
    name = serializers.CharField(max_length=255)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    unit = serializers.CharField(max_length=50)
    minimum_threshold = serializers.DecimalField(
        required=False, default=Decimal('0'), max_digits=12, decimal_places=4,
        min_value=Decimal('0'),
    )
    reorder_quantity = serializers.DecimalField(
        required=False, allow_null=True, max_digits=12, decimal_places=4,
        min_value=Decimal('0.0001'),
    )
    main_supplier_name = serializers.CharField(
        required=False, allow_blank=True, default='',
    )

    def validate_category_id(self, value):
        if not StockCategory.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError(
                'Stock category not found or inactive.'
            )
        return value

    def validate_code(self, value):
        code = value.upper()
        if StockItem.objects.filter(code=code).exists():
            raise serializers.ValidationError(
                'A stock item with this code already exists.'
            )
        return code


class StockItemUpdateSerializer(serializers.Serializer):
    category_id = serializers.UUIDField(required=False)
    name = serializers.CharField(max_length=255, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    unit = serializers.CharField(max_length=50, required=False)
    minimum_threshold = serializers.DecimalField(
        required=False, max_digits=12, decimal_places=4, min_value=Decimal('0'),
    )
    reorder_quantity = serializers.DecimalField(
        required=False, allow_null=True, max_digits=12, decimal_places=4,
        min_value=Decimal('0.0001'),
    )
    main_supplier_name = serializers.CharField(required=False, allow_blank=True)

    def validate_category_id(self, value):
        if not StockCategory.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError(
                'Stock category not found or inactive.'
            )
        return value


# ---------------------------------------------------------------------------
# StockLot
# ---------------------------------------------------------------------------

class StockLotSerializer(serializers.ModelSerializer):
    item_code = serializers.CharField(source='item.code', read_only=True)
    item_name = serializers.CharField(source='item.name', read_only=True)

    class Meta:
        model = StockLot
        fields = [
            'id', 'item_id', 'item_code', 'item_name',
            'lot_number', 'expiry_date', 'supplier_name',
            'initial_quantity', 'current_quantity', 'unit_cost',
            'received_at', 'notes', 'is_exhausted',
            'created_at', 'updated_at',
        ]


class StockLotCreateSerializer(serializers.Serializer):
    lot_number = serializers.CharField(max_length=100)
    expiry_date = serializers.DateField(required=False, allow_null=True)
    supplier_name = serializers.CharField(
        required=False, allow_blank=True, default='',
    )
    initial_quantity = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=Decimal('0.0001'),
    )
    unit_cost = serializers.DecimalField(
        required=False, allow_null=True, max_digits=12, decimal_places=4,
        min_value=Decimal('0'),
    )
    received_at = serializers.DateField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_lot_number(self, value):
        item_id = self.context.get('item_id')
        if item_id and StockLot.objects.filter(item_id=item_id, lot_number=value).exists():
            raise serializers.ValidationError(
                'This lot number already exists for this item.'
            )
        return value


# ---------------------------------------------------------------------------
# StockMovement
# ---------------------------------------------------------------------------

class StockMovementSerializer(serializers.ModelSerializer):
    performed_by_email = serializers.EmailField(
        source='performed_by.email', read_only=True, default=None,
    )

    class Meta:
        model = StockMovement
        fields = [
            'id', 'lot_id', 'movement_type',
            'quantity', 'quantity_before', 'quantity_after',
            'reason', 'reference', 'reference_type',
            'performed_by_id', 'performed_by_email', 'performed_at',
        ]


class StockMovementCreateSerializer(serializers.Serializer):
    movement_type = serializers.ChoiceField(choices=MovementType.choices)
    quantity = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=Decimal('0.0001'),
    )
    reason = serializers.CharField(required=False, allow_blank=True, default='')
    reference = serializers.CharField(
        required=False, allow_blank=True, default='', max_length=100,
    )
    reference_type = serializers.CharField(
        required=False, allow_blank=True, default='', max_length=50,
    )

    def validate(self, attrs):
        movement_type = attrs.get('movement_type')
        reason = attrs.get('reason', '').strip()
        if movement_type in REASON_REQUIRED_TYPES and not reason:
            raise serializers.ValidationError({
                'reason': f'A reason is required for {movement_type} movements.',
            })
        return attrs
