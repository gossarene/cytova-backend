import django_filters
from .models import (
    AnalysisRequest, AnalysisRequestItem, ClosureStatus,
    RequestStatus, ItemStatus, ExecutionMode, SourceType, BillingMode,
)


# Lifecycle is the user-facing closure-status filter. The default ('active')
# excludes delivered and archived rows; explicit values surface a single
# bucket; 'all' disables the closure filter entirely.
LIFECYCLE_ACTIVE    = 'active'
LIFECYCLE_DELIVERED = 'delivered'
LIFECYCLE_ARCHIVED  = 'archived'
LIFECYCLE_ALL       = 'all'

LIFECYCLE_CHOICES = (
    (LIFECYCLE_ACTIVE,    'Active'),
    (LIFECYCLE_DELIVERED, 'Delivered'),
    (LIFECYCLE_ARCHIVED,  'Archived'),
    (LIFECYCLE_ALL,       'All'),
)


class AnalysisRequestFilter(django_filters.FilterSet):
    """List filter for analysis requests.

    Two axes — strictly orthogonal:

      ``status``     — workflow status (DRAFT...CANCELLED). DELIVERED and
                       ARCHIVED used to live here in an earlier design but
                       were extracted to closure_status; the workflow filter
                       no longer offers them as choices.

      ``lifecycle``  — closure-status preset, defaulting to "active":
                         active    → closure_status = OPEN  (default)
                         delivered → closure_status = DELIVERED
                         archived  → closure_status = ARCHIVED
                         all       → no closure filter (every row)

    Backward-compat aliases (kept so any in-flight client URL still works):
      ``include_delivered=true`` — promoted to ``lifecycle=all`` if neither
                                    ``lifecycle`` nor ``include_archived``
                                    is set; otherwise honoured by widening
                                    the closure set.
      ``include_archived=true``  — same.
    """
    # Status choices intentionally omit DELIVERED/ARCHIVED — those are not
    # workflow statuses anymore. A request that is ARCHIVED will still have
    # a workflow status (e.g. VALIDATED) that the dropdown can target.
    status = django_filters.ChoiceFilter(choices=RequestStatus.choices)

    patient_id = django_filters.UUIDFilter(field_name='patient_id')
    created_by_id = django_filters.UUIDFilter(field_name='created_by_id')
    created_from = django_filters.DateFilter(field_name='created_at', lookup_expr='date__gte')
    created_to   = django_filters.DateFilter(field_name='created_at', lookup_expr='date__lte')
    source_type = django_filters.ChoiceFilter(choices=SourceType.choices)
    partner_organization_id = django_filters.UUIDFilter(field_name='partner_organization_id')
    billing_mode = django_filters.ChoiceFilter(choices=BillingMode.choices)

    # closure_status is exposed for direct queries too — useful for ad-hoc
    # tooling (e.g. ``?closure_status=DELIVERED``) but the lifecycle preset
    # is the recommended UX.
    closure_status = django_filters.ChoiceFilter(choices=ClosureStatus.choices)
    lifecycle = django_filters.ChoiceFilter(method='_noop_lifecycle', choices=LIFECYCLE_CHOICES)

    # Backward-compat with the previous (now-removed) checkbox flags.
    include_delivered = django_filters.BooleanFilter(method='_noop_legacy_flag')
    include_archived  = django_filters.BooleanFilter(method='_noop_legacy_flag')

    class Meta:
        model = AnalysisRequest
        fields = [
            'status', 'patient_id', 'created_by_id',
            'source_type', 'partner_organization_id', 'billing_mode',
            'closure_status',
        ]

    # The lifecycle / include_* params drive queryset narrowing in `qs`;
    # individual method filters are no-ops.
    def _noop_lifecycle(self, queryset, name, value):  # noqa: ARG002
        return queryset

    def _noop_legacy_flag(self, queryset, name, value):  # noqa: ARG002
        return queryset

    @property
    def qs(self):
        base = super().qs
        cleaned = self.form.cleaned_data

        # An explicit closure_status= filter wins — honour it verbatim.
        if cleaned.get('closure_status'):
            return base

        lifecycle = cleaned.get('lifecycle')
        include_delivered = bool(cleaned.get('include_delivered'))
        include_archived  = bool(cleaned.get('include_archived'))

        # Translate legacy include_* flags into the equivalent closure set.
        if not lifecycle and (include_delivered or include_archived):
            allowed = {ClosureStatus.OPEN}
            if include_delivered:
                allowed.add(ClosureStatus.DELIVERED)
            if include_archived:
                allowed.add(ClosureStatus.ARCHIVED)
            return base.filter(closure_status__in=allowed)

        # New lifecycle param.
        if lifecycle == LIFECYCLE_ALL:
            return base
        if lifecycle == LIFECYCLE_DELIVERED:
            return base.filter(closure_status=ClosureStatus.DELIVERED)
        if lifecycle == LIFECYCLE_ARCHIVED:
            return base.filter(closure_status=ClosureStatus.ARCHIVED)

        # Default: active only.
        return base.filter(closure_status=ClosureStatus.OPEN)


class AnalysisRequestItemFilter(django_filters.FilterSet):
    status = django_filters.ChoiceFilter(choices=ItemStatus.choices)
    execution_mode = django_filters.ChoiceFilter(choices=ExecutionMode.choices)
    exam_definition_id = django_filters.UUIDFilter(field_name='exam_definition_id')

    class Meta:
        model = AnalysisRequestItem
        fields = ['status', 'execution_mode', 'exam_definition_id']
