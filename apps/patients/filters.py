import django_filters
from .models import Patient


class HasPortalAccountFilter(django_filters.BooleanFilter):
    """
    Filter patients by whether they have a portal account linked.
    ?has_portal_account=true  → patients with a portal account
    ?has_portal_account=false → patients without a portal account
    """

    def filter(self, qs, value):
        if value is True:
            return qs.filter(portal_account__isnull=False)
        if value is False:
            return qs.filter(portal_account__isnull=True)
        return qs


class PatientFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter()
    has_portal_account = HasPortalAccountFilter()

    class Meta:
        model = Patient
        fields = ['is_active']
