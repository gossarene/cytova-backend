"""
Cytova — Inventory Alert Serializers
"""
from rest_framework import serializers

from .models import InventoryAlert


class InventoryAlertListSerializer(serializers.ModelSerializer):
    stock_item_code = serializers.CharField(
        source='stock_item.code', read_only=True,
    )
    stock_item_name = serializers.CharField(
        source='stock_item.name', read_only=True,
    )
    lot_number = serializers.CharField(
        source='stock_lot.lot_number', read_only=True, default=None,
    )

    class Meta:
        model = InventoryAlert
        fields = [
            'id', 'alert_type', 'severity', 'status',
            'stock_item_id', 'stock_item_code', 'stock_item_name',
            'stock_lot_id', 'lot_number',
            'title', 'threshold_value', 'current_value',
            'created_at',
        ]


class InventoryAlertDetailSerializer(serializers.ModelSerializer):
    stock_item_code = serializers.CharField(
        source='stock_item.code', read_only=True,
    )
    stock_item_name = serializers.CharField(
        source='stock_item.name', read_only=True,
    )
    lot_number = serializers.CharField(
        source='stock_lot.lot_number', read_only=True, default=None,
    )
    acknowledged_by_email = serializers.EmailField(
        source='acknowledged_by.email', read_only=True, default=None,
    )
    resolved_by_email = serializers.EmailField(
        source='resolved_by.email', read_only=True, default=None,
    )

    class Meta:
        model = InventoryAlert
        fields = [
            'id', 'alert_type', 'severity', 'status',
            'stock_item_id', 'stock_item_code', 'stock_item_name',
            'stock_lot_id', 'lot_number',
            'title', 'message', 'threshold_value', 'current_value',
            'acknowledged_at', 'acknowledged_by_email',
            'resolved_at', 'resolved_by_email',
            'created_at', 'updated_at',
        ]


class BulkAcknowledgeSerializer(serializers.Serializer):
    alert_ids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=1,
    )

    def validate_alert_ids(self, value):
        from django.conf import settings
        max_ids = getattr(settings, 'ALERT_BULK_ACKNOWLEDGE_MAX', 200)
        if len(value) > max_ids:
            raise serializers.ValidationError(
                f'Cannot acknowledge more than {max_ids} alerts at once.'
            )
        return value


class AlertSummarySerializer(serializers.Serializer):
    """Read-only output for the /summary/ endpoint."""
    alert_type = serializers.CharField()
    severity = serializers.CharField()
    count = serializers.IntegerField()
