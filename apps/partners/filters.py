import django_filters

from .models import OrganizationType, PartnerOrganization


class PartnerOrganizationFilter(django_filters.FilterSet):
    organization_type = django_filters.ChoiceFilter(choices=OrganizationType.choices)
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = PartnerOrganization
        fields = ['organization_type', 'is_active']
