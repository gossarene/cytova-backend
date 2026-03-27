import django_filters
from .models import AnalysisRequest, AnalysisRequestItem, RequestStatus, ItemStatus, ExecutionMode


class AnalysisRequestFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=RequestStatus.choices)
    patient_id = django_filters.UUIDFilter(field_name='patient_id')
    created_by_id = django_filters.UUIDFilter(field_name='created_by_id')
    created_from = django_filters.DateFilter(field_name='created_at', lookup_expr='date__gte')
    created_to   = django_filters.DateFilter(field_name='created_at', lookup_expr='date__lte')

    class Meta:
        model = AnalysisRequest
        fields = ['status', 'patient_id', 'created_by_id']


class AnalysisRequestItemFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=ItemStatus.choices)
    execution_mode = django_filters.ChoiceFilter(choices=ExecutionMode.choices)
    exam_definition_id = django_filters.UUIDFilter(field_name='exam_definition_id')

    class Meta:
        model = AnalysisRequestItem
        fields = ['status', 'execution_mode', 'exam_definition_id']
