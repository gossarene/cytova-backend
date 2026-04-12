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

    class Meta:
        model = ResultVersion
        fields = ['status', 'is_abnormal', 'is_current', 'item_id']
