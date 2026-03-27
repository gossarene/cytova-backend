from decimal import Decimal

import django_filters
from django.db.models import DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce

from .models import MovementType, StockCategory, StockItem, StockLot, StockMovement


class StockCategoryFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = StockCategory
        fields = ['is_active']


class StockItemFilter(django_filters.FilterSet):
    category_id = django_filters.UUIDFilter(field_name='category_id')
    is_active = django_filters.BooleanFilter()
    below_threshold = django_filters.BooleanFilter(method='filter_below_threshold')

    class Meta:
        model = StockItem
        fields = ['category_id', 'is_active']

    def filter_below_threshold(self, qs, name, value):
        """
        value=True  → items where active lot quantity sum < minimum_threshold
        value=False → items where active lot quantity sum >= minimum_threshold
        """
        annotated = qs.annotate(
            active_qty=Coalesce(
                Sum(
                    'lots__current_quantity',
                    filter=Q(lots__is_exhausted=False),
                ),
                Value(Decimal('0')),
                output_field=DecimalField(),
            )
        )
        if value:
            return annotated.filter(active_qty__lt=F('minimum_threshold'))
        return annotated.filter(active_qty__gte=F('minimum_threshold'))


class StockLotFilter(django_filters.FilterSet):
    item_id = django_filters.UUIDFilter(field_name='item_id')
    is_exhausted = django_filters.BooleanFilter()
    expiry_before = django_filters.DateFilter(field_name='expiry_date', lookup_expr='lte')
    expiry_after = django_filters.DateFilter(field_name='expiry_date', lookup_expr='gte')

    class Meta:
        model = StockLot
        fields = ['item_id', 'is_exhausted']


class StockMovementFilter(django_filters.FilterSet):
    lot_id = django_filters.UUIDFilter(field_name='lot_id')
    movement_type = django_filters.ChoiceFilter(choices=MovementType.choices)
    performed_at_from = django_filters.DateTimeFilter(
        field_name='performed_at', lookup_expr='gte'
    )
    performed_at_to = django_filters.DateTimeFilter(
        field_name='performed_at', lookup_expr='lte'
    )

    class Meta:
        model = StockMovement
        fields = ['lot_id', 'movement_type']
