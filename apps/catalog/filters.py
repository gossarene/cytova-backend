import django_filters
from django.db.models import Q
from .models import ExamCategory, ExamDefinition, SampleType


class ExamCategoryFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = ExamCategory
        fields = ['is_active']


class IsEnabledFilter(django_filters.BooleanFilter):
    """
    Filter exam definitions by their lab-specific enabled state.

    is_enabled=true  → exams where lab_settings.is_enabled=True OR no lab_settings
                       exists (no settings = not yet customised → enabled by default)
    is_enabled=false → exams where lab_settings.is_enabled=False explicitly
    """

    def filter(self, qs, value):
        if value is True:
            return qs.filter(
                Q(lab_settings__isnull=True) | Q(lab_settings__is_enabled=True)
            )
        if value is False:
            return qs.filter(lab_settings__is_enabled=False)
        return qs


class ExamDefinitionFilter(django_filters.FilterSet):
    category_id = django_filters.UUIDFilter(field_name='category_id')
    sample_type = django_filters.ChoiceFilter(choices=SampleType.choices)
    is_active = django_filters.BooleanFilter()
    is_enabled = IsEnabledFilter()

    class Meta:
        model = ExamDefinition
        fields = ['category_id', 'sample_type', 'is_active']
