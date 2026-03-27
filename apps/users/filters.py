import django_filters
from .models import StaffUser, Role


class StaffUserFilter(django_filters.FilterSet):
    role = django_filters.ChoiceFilter(choices=Role.choices)
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = StaffUser
        fields = ['role', 'is_active']
