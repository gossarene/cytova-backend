import django_filters
from .models import ExamResult, ResultStatus


class ExamResultFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=ResultStatus.choices)
    is_abnormal = django_filters.BooleanFilter()
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
        model = ExamResult
        fields = ['status', 'is_abnormal', 'item_id']
