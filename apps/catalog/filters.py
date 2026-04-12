import django_filters
from django.db.models import Q
from .models import (
    ExamCategory, ExamFamily, ExamSubFamily, TubeType, ExamTechnique,
    ExamDefinition, SampleType,
)


class ExamCategoryFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = ExamCategory
        fields = ['is_active']


class ExamFamilyFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = ExamFamily
        fields = ['is_active']


class ExamSubFamilyFilter(django_filters.FilterSet):
    """
    First-class ``family_id`` filter is mandatory for the frontend
    cascading dropdown (sub-families shown after a family is picked).
    """
    family_id = django_filters.UUIDFilter(field_name='family_id')
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = ExamSubFamily
        fields = ['family_id', 'is_active']


class TubeTypeFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = TubeType
        fields = ['is_active']


class ExamTechniqueFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = ExamTechnique
        fields = ['is_active']


class IsEnabledFilter(django_filters.BooleanFilter):
    """
    Filter exam definitions by their lab-specific enabled state.

    is_enabled=true  -> exams where lab_settings.is_enabled=True OR no lab_settings
    is_enabled=false -> exams where lab_settings.is_enabled=False explicitly
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
    # New structured filters
    family_id = django_filters.UUIDFilter(field_name='family_id')
    sub_family_id = django_filters.UUIDFilter(field_name='sub_family_id')
    tube_type_id = django_filters.UUIDFilter(field_name='tube_type_id')
    technique_id = django_filters.UUIDFilter(field_name='technique_id')
    fasting_required = django_filters.BooleanFilter()
    sample_type = django_filters.ChoiceFilter(choices=SampleType.choices)
    is_active = django_filters.BooleanFilter()
    is_enabled = IsEnabledFilter()
    # Legacy
    category_id = django_filters.UUIDFilter(field_name='category_id')

    class Meta:
        model = ExamDefinition
        fields = [
            'family_id', 'sub_family_id', 'tube_type_id', 'technique_id',
            'fasting_required', 'category_id', 'sample_type', 'is_active',
        ]
