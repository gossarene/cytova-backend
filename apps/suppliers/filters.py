import django_filters

from .models import PurchaseOrder, PurchaseOrderStatus, Reception, Supplier


class SupplierFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = Supplier
        fields = ['is_active']


class PurchaseOrderFilter(django_filters.FilterSet):
    supplier_id = django_filters.UUIDFilter(field_name='supplier_id')
    status = django_filters.ChoiceFilter(choices=PurchaseOrderStatus.choices)
    expected_before = django_filters.DateFilter(
        field_name='expected_delivery_date', lookup_expr='lte',
    )
    expected_after = django_filters.DateFilter(
        field_name='expected_delivery_date', lookup_expr='gte',
    )
    created_after = django_filters.DateTimeFilter(
        field_name='created_at', lookup_expr='gte',
    )
    created_before = django_filters.DateTimeFilter(
        field_name='created_at', lookup_expr='lte',
    )

    class Meta:
        model = PurchaseOrder
        fields = ['supplier_id', 'status']


class ReceptionFilter(django_filters.FilterSet):
    order_id = django_filters.UUIDFilter(field_name='order_id')
    has_discrepancy = django_filters.BooleanFilter()
    received_after = django_filters.DateFilter(
        field_name='received_at', lookup_expr='gte',
    )
    received_before = django_filters.DateFilter(
        field_name='received_at', lookup_expr='lte',
    )

    class Meta:
        model = Reception
        fields = ['order_id', 'has_discrepancy']
