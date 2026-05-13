from django.db.models import Q
import django_filters
from .models import ResultVersion, ResultStatus


class ResultVersionFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=ResultStatus.choices)
    is_abnormal = django_filters.BooleanFilter()
    is_current = django_filters.BooleanFilter()
    item_id = django_filters.UUIDFilter(field_name='item_id')
    exam_definition_id = django_filters.UUIDFilter(
        field_name='item__exam_definition_id',
    )
    patient_id = django_filters.UUIDFilter(
        field_name='item__analysis_request__patient_id',
    )
    request_id = django_filters.UUIDFilter(
        field_name='item__analysis_request_id',
    )
    published_from = django_filters.DateFilter(
        field_name='published_at', lookup_expr='date__gte',
    )
    published_to = django_filters.DateFilter(
        field_name='published_at', lookup_expr='date__lte',
    )
    # Worklist date filters. The chosen reference timestamp is
    # ``submitted_at`` (when the technician handed the row to the
    # biologist for review) with a fallback to ``created_at`` for
    # rows that never reached the SUBMITTED state — keeps DRAFT
    # rows visible inside the current-month default window.
    #
    # The frontend's default range is "first day of current month
    # → today", driven from the UI rather than baked into the
    # filterset so the operator can clear/widen it explicitly.
    date_from = django_filters.DateFilter(method='filter_date_from')
    date_to = django_filters.DateFilter(method='filter_date_to')

    class Meta:
        model = ResultVersion
        fields = ['status', 'is_abnormal', 'is_current', 'item_id']

    def filter_date_from(self, queryset, name, value):
        if value is None:
            return queryset
        return queryset.filter(
            Q(submitted_at__date__gte=value)
            | (Q(submitted_at__isnull=True) & Q(created_at__date__gte=value))
        )

    def filter_date_to(self, queryset, name, value):
        if value is None:
            return queryset
        return queryset.filter(
            Q(submitted_at__date__lte=value)
            | (Q(submitted_at__isnull=True) & Q(created_at__date__lte=value))
        )
