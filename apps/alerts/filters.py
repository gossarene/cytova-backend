import django_filters

from .models import AlertSeverity, AlertStatus, AlertType, InventoryAlert


class InventoryAlertFilter(django_filters.FilterSet):
    alert_type = django_filters.ChoiceFilter(choices=AlertType.choices)
    severity = django_filters.ChoiceFilter(choices=AlertSeverity.choices)
    status = django_filters.ChoiceFilter(choices=AlertStatus.choices)
    stock_item_id = django_filters.UUIDFilter(field_name='stock_item_id')
    stock_lot_id = django_filters.UUIDFilter(field_name='stock_lot_id')
    created_after = django_filters.DateTimeFilter(
        field_name='created_at', lookup_expr='gte',
    )
    created_before = django_filters.DateTimeFilter(
        field_name='created_at', lookup_expr='lte',
    )

    class Meta:
        model = InventoryAlert
        fields = ['alert_type', 'severity', 'status', 'stock_item_id', 'stock_lot_id']
