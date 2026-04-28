"""
Cytova — Role-aware dashboard cockpit composer.

A single ``build_cockpit(user)`` call returns the full payload the frontend
needs to render the role-specific dashboard:

    {
      "role": "<TENANT_ROLE>",
      "greeting_name": "Alice",
      "kpis":    [ { key, label, value, icon, tone, href? }, ... ],
      "actions": [ { key, title, count, description, cta, href, tone }, ... ],
      "charts":  {
        "requests_over_time": [...],
        "requests_by_status": [...],
        "requests_by_source": [...],
        "results_pipeline":   [...],
      }
    }

Design notes
------------
* Metrics are computed once (``_compute_metrics``) and read by every role
  builder. Each metric is a small aggregate query — no N+1, no per-row
  service calls.
* Role builders pick a slice of those metrics and frame them as KPIs +
  actions for that role. UI vocabulary (icon, tone, CTA copy) lives here
  so the frontend stays generic.
* Revenue / billing metrics are added only for roles that own that
  surface (LAB_ADMIN, BILLING_OFFICER) — never leaked to other roles.
* Tenant isolation is implicit: every query runs in the active tenant
  schema set by ``CytovaTenantMiddleware``.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone


# ---------------------------------------------------------------------------
# Aggregate metrics (one query per metric; cheap on the indexed columns)
# ---------------------------------------------------------------------------

def _compute_metrics() -> Dict[str, Any]:
    from apps.alerts.models import InventoryAlert, OPEN_STATUSES, AlertSeverity
    from apps.requests.models import (
        AnalysisRequest, AnalysisRequestItem,
        ClosureStatus, ItemStatus, RequestStatus, SourceType,
    )
    from apps.results.models import ResultStatus, ResultVersion

    now = timezone.now()
    today = now.date()
    start_of_month = today.replace(day=1)
    chart_window_start = today - timedelta(days=13)  # last 14 days incl. today

    AR = AnalysisRequest.objects
    AI = AnalysisRequestItem.objects
    RV = ResultVersion.objects

    # ---- Requests (workflow + closure) ------------------------------------

    # Active requests = anything that is not a terminal lifecycle state. We
    # treat closure ARCHIVED as removed from the active worklist; CANCELLED
    # is a terminal workflow state.
    active_qs = AR.exclude(
        status=RequestStatus.CANCELLED,
    ).exclude(
        closure_status=ClosureStatus.ARCHIVED,
    )

    requests_active = active_qs.count()
    requests_created_today = AR.filter(created_at__date=today).count()
    requests_pending_confirmation = AR.filter(status=RequestStatus.DRAFT).count()
    requests_awaiting_review = AR.filter(status=RequestStatus.AWAITING_REVIEW).count()
    requests_ready_for_release = AR.filter(status=RequestStatus.READY_FOR_RELEASE).count()
    requests_validated = AR.filter(status=RequestStatus.VALIDATED).count()
    requests_retest_required = AR.filter(status=RequestStatus.RETEST_REQUIRED).count()

    # "Ready to notify" — workflow finished + closure still OPEN. The notify
    # endpoint additionally requires a generated report; this count is the
    # upper bound. It's the right signal for a worklist KPI ("here is what
    # could be sent today") even if a few rows lack a report.
    requests_ready_to_notify = AR.filter(
        status=RequestStatus.VALIDATED,
        closure_status=ClosureStatus.OPEN,
    ).count()

    requests_delivered_today = AR.filter(
        closure_status=ClosureStatus.DELIVERED,
        delivered_at__date=today,
    ).count()

    requests_validated_this_month = AR.filter(
        status=RequestStatus.VALIDATED,
        updated_at__date__gte=start_of_month,
    ).count()

    # ---- Items / pipeline -------------------------------------------------

    items_pending_collection = AI.filter(status=ItemStatus.PENDING).count()
    items_in_analysis = AI.filter(
        status__in=[ItemStatus.COLLECTED, ItemStatus.IN_PROGRESS],
    ).count()
    items_awaiting_review = AI.filter(status=ItemStatus.UNDER_REVIEW).count()

    # ---- Results ----------------------------------------------------------

    results_pending_validation = RV.filter(
        status=ResultStatus.SUBMITTED, is_current=True,
    ).count()
    results_validated_today = RV.filter(
        status=ResultStatus.VALIDATED,
        validated_at__date=today,
    ).count()
    results_published_this_month = RV.filter(
        status=ResultStatus.PUBLISHED,
        published_at__date__gte=start_of_month,
    ).count()
    results_abnormal_published = RV.filter(
        status=ResultStatus.PUBLISHED,
        is_abnormal=True,
        published_at__date__gte=start_of_month,
    ).count()

    # ---- Alerts -----------------------------------------------------------

    open_alerts_qs = InventoryAlert.objects.filter(status__in=OPEN_STATUSES)
    alerts_open = open_alerts_qs.count()
    alerts_critical = open_alerts_qs.filter(severity=AlertSeverity.CRITICAL).count()

    # ---- Revenue (gated — only added to LAB_ADMIN / BILLING_OFFICER) -----

    revenue_month = AI.filter(
        analysis_request__status__in=[
            RequestStatus.VALIDATED, RequestStatus.COMPLETED,
        ],
        analysis_request__confirmed_at__date__gte=start_of_month,
    ).aggregate(
        total=Coalesce(
            Sum('billed_price'),
            Value(Decimal('0')),
            output_field=DecimalField(),
        ),
    )['total']

    # ---- Charts -----------------------------------------------------------

    # Requests over the last 14 days, with zero-fill for empty days.
    raw_over_time = dict(
        AR.filter(created_at__date__gte=chart_window_start)
          .annotate(date=TruncDate('created_at'))
          .values('date')
          .annotate(count=Count('id'))
          .values_list('date', 'count')
    )
    requests_over_time = [
        {
            'date': (chart_window_start + timedelta(days=offset)).isoformat(),
            'count': raw_over_time.get(chart_window_start + timedelta(days=offset), 0),
        }
        for offset in range(14)
    ]

    requests_by_status = [
        {'status': row['status'], 'count': row['count']}
        for row in AR.values('status').annotate(count=Count('id')).order_by('status')
    ]

    requests_by_source_raw = dict(
        AR.values_list('source_type').annotate(count=Count('id')).values_list('source_type', 'count')
    )
    requests_by_source = [
        {'source': source, 'count': requests_by_source_raw.get(source, 0)}
        for source in (SourceType.DIRECT_PATIENT, SourceType.PARTNER_ORGANIZATION)
    ]

    results_pipeline = [
        {'status': row['status'], 'count': row['count']}
        for row in RV.values('status').annotate(count=Count('id')).order_by('status')
    ]

    return {
        'now': now,
        'today': today,
        # KPI building blocks
        'requests_active':              requests_active,
        'requests_created_today':       requests_created_today,
        'requests_pending_confirmation': requests_pending_confirmation,
        'requests_awaiting_review':     requests_awaiting_review,
        'requests_ready_for_release':   requests_ready_for_release,
        'requests_validated':           requests_validated,
        'requests_retest_required':     requests_retest_required,
        'requests_ready_to_notify':     requests_ready_to_notify,
        'requests_delivered_today':     requests_delivered_today,
        'requests_validated_this_month': requests_validated_this_month,
        'items_pending_collection':     items_pending_collection,
        'items_in_analysis':            items_in_analysis,
        'items_awaiting_review':        items_awaiting_review,
        'results_pending_validation':   results_pending_validation,
        'results_validated_today':      results_validated_today,
        'results_published_this_month': results_published_this_month,
        'results_abnormal_published':   results_abnormal_published,
        'alerts_open':                  alerts_open,
        'alerts_critical':              alerts_critical,
        'revenue_month':                revenue_month,
        # Charts
        'requests_over_time': requests_over_time,
        'requests_by_status': requests_by_status,
        'requests_by_source': requests_by_source,
        'results_pipeline':   results_pipeline,
    }


# ---------------------------------------------------------------------------
# KPI / action helpers
# ---------------------------------------------------------------------------

def _kpi(*, key: str, label: str, value: int, icon: str, tone: str,
         href: Optional[str] = None) -> Dict[str, Any]:
    """Frontend-agnostic KPI shape. ``icon`` is a semantic name the
    frontend resolves to a Lucide component; ``tone`` maps to a colour."""
    return {
        'key': key, 'label': label, 'value': value,
        'icon': icon, 'tone': tone, 'href': href,
    }


def _action(*, key: str, title: str, count: int, description: str,
            cta: str, href: str, tone: str = 'primary') -> Dict[str, Any]:
    return {
        'key': key, 'title': title, 'count': count,
        'description': description, 'cta': cta, 'href': href, 'tone': tone,
    }


# ---------------------------------------------------------------------------
# Role builders
# ---------------------------------------------------------------------------

def _receptionist(m: Dict[str, Any]) -> Dict[str, List]:
    return {
        'kpis': [
            _kpi(key='created_today', label='Requests created today',
                 value=m['requests_created_today'], icon='clipboard-list',
                 tone='primary', href='/requests'),
            _kpi(key='pending_confirmation', label='Pending confirmation',
                 value=m['requests_pending_confirmation'], icon='hourglass',
                 tone='warning', href='/requests?status=DRAFT'),
            _kpi(key='ready_to_notify', label='Results ready to notify',
                 value=m['requests_ready_to_notify'], icon='mail',
                 tone='success', href='/requests?status=VALIDATED'),
            _kpi(key='delivered_today', label='Delivered today',
                 value=m['requests_delivered_today'], icon='package-check',
                 tone='neutral', href='/requests?lifecycle=delivered'),
        ],
        'actions': [
            _action(key='create_request', title='Create new request',
                    count=0, description='Register a new analysis request for a patient.',
                    cta='New request', href='/requests/new', tone='primary'),
            _action(key='confirm_drafts', title='Confirm drafts',
                    count=m['requests_pending_confirmation'],
                    description='Drafts waiting to be confirmed and sent to processing.',
                    cta='Open drafts', href='/requests?status=DRAFT', tone='warning'),
            _action(key='notify_patients', title='Notify patients',
                    count=m['requests_ready_to_notify'],
                    description='Validated results not yet delivered to the patient.',
                    cta='Open queue', href='/requests?status=VALIDATED', tone='success'),
        ],
    }


def _technician(m: Dict[str, Any]) -> Dict[str, List]:
    return {
        'kpis': [
            _kpi(key='pending_collection', label='Samples pending collection',
                 value=m['items_pending_collection'], icon='pipette',
                 tone='primary', href='/requests?status=CONFIRMED'),
            _kpi(key='in_analysis', label='In analysis',
                 value=m['items_in_analysis'], icon='flask-conical',
                 tone='primary', href='/requests?status=IN_ANALYSIS'),
            _kpi(key='awaiting_review', label='Awaiting review',
                 value=m['items_awaiting_review'], icon='eye',
                 tone='warning', href='/requests?status=AWAITING_REVIEW'),
            _kpi(key='retest_required', label='Retest required',
                 value=m['requests_retest_required'], icon='refresh-cw',
                 tone='danger', href='/requests?status=RETEST_REQUIRED'),
        ],
        'actions': [
            _action(key='collection_queue', title='Collection queue',
                    count=m['items_pending_collection'],
                    description='Confirmed requests waiting for sample collection.',
                    cta='Open queue', href='/requests?status=CONFIRMED', tone='primary'),
            _action(key='analysis_queue', title='Analysis queue',
                    count=m['items_in_analysis'],
                    description='Items currently being processed in the lab.',
                    cta='Open queue', href='/requests?status=IN_ANALYSIS', tone='primary'),
            _action(key='retests', title='Retests required',
                    count=m['requests_retest_required'],
                    description='Items rejected during review that need re-analysis.',
                    cta='View retests', href='/requests?status=RETEST_REQUIRED', tone='danger'),
        ],
    }


def _biologist(m: Dict[str, Any]) -> Dict[str, List]:
    return {
        'kpis': [
            _kpi(key='pending_validation', label='Pending validation',
                 value=m['results_pending_validation'], icon='clipboard-check',
                 tone='warning', href='/results?status=SUBMITTED'),
            _kpi(key='retest_required', label='Retest required',
                 value=m['requests_retest_required'], icon='refresh-cw',
                 tone='danger', href='/requests?status=RETEST_REQUIRED'),
            _kpi(key='validated_today', label='Validated today',
                 value=m['results_validated_today'], icon='check-circle-2',
                 tone='success', href='/results?status=VALIDATED'),
            _kpi(key='abnormal_month', label='Abnormal results this month',
                 value=m['results_abnormal_published'], icon='alert-triangle',
                 tone='danger', href='/results?status=PUBLISHED'),
        ],
        'actions': [
            _action(key='review_pending', title='Review pending validations',
                    count=m['results_pending_validation'],
                    description='Submitted results awaiting your sign-off.',
                    cta='Open queue', href='/results?status=SUBMITTED', tone='warning'),
            _action(key='ready_for_release', title='Ready for release',
                    count=m['requests_ready_for_release'],
                    description='Requests with all items validated, awaiting finalization.',
                    cta='Open requests', href='/requests?status=READY_FOR_RELEASE',
                    tone='success'),
            _action(key='retests', title='Retest required',
                    count=m['requests_retest_required'],
                    description='Items you rejected that need re-analysis.',
                    cta='View retests', href='/requests?status=RETEST_REQUIRED',
                    tone='danger'),
        ],
    }


def _lab_admin(m: Dict[str, Any], *, include_revenue: bool) -> Dict[str, List]:
    kpis = [
        _kpi(key='active_requests', label='Active requests',
             value=m['requests_active'], icon='clipboard-list',
             tone='primary', href='/requests'),
        _kpi(key='validated_month', label='Validated this month',
             value=m['requests_validated_this_month'], icon='check-circle-2',
             tone='success', href='/requests?status=VALIDATED'),
        _kpi(key='delivered_today', label='Delivered today',
             value=m['requests_delivered_today'], icon='package-check',
             tone='neutral', href='/requests?lifecycle=delivered'),
        _kpi(key='alerts', label='Open alerts',
             value=m['alerts_open'], icon='bell',
             tone='danger' if m['alerts_critical'] else 'warning', href='/alerts'),
    ]
    if include_revenue:
        kpis.insert(3, _kpi(
            key='revenue_month', label='Billed this month',
            # Stringify Decimal — frontend formats with currency util.
            value=str(m['revenue_month']), icon='receipt',
            tone='success', href='/invoices',
        ))

    return {
        'kpis': kpis,
        'actions': [
            _action(key='bottlenecks', title='Bottlenecks',
                    count=m['requests_awaiting_review'] + m['requests_ready_for_release'],
                    description='Requests stuck in the review pipeline.',
                    cta='Investigate',
                    href='/requests?status=AWAITING_REVIEW', tone='warning'),
            _action(key='alerts', title='Operational alerts',
                    count=m['alerts_open'],
                    description='Stock and inventory alerts that need attention.',
                    cta='Open alerts', href='/alerts',
                    tone='danger' if m['alerts_critical'] else 'warning'),
            _action(
                key='billing' if include_revenue else 'notify_patients',
                title='Billing' if include_revenue else 'Patient notifications',
                count=0 if include_revenue else m['requests_ready_to_notify'],
                description=(
                    'Generate and review partner invoices.'
                    if include_revenue
                    else 'Validated results not yet delivered to the patient.'
                ),
                cta='Open' if include_revenue else 'Open queue',
                href='/invoices' if include_revenue else '/requests?status=VALIDATED',
                tone='primary',
            ),
        ],
    }


def _inventory(m: Dict[str, Any]) -> Dict[str, List]:
    return {
        'kpis': [
            _kpi(key='alerts', label='Open alerts',
                 value=m['alerts_open'], icon='bell',
                 tone='danger' if m['alerts_critical'] else 'warning', href='/alerts'),
            _kpi(key='critical', label='Critical alerts',
                 value=m['alerts_critical'], icon='alert-triangle',
                 tone='danger', href='/alerts'),
            _kpi(key='active_requests', label='Active requests',
                 value=m['requests_active'], icon='clipboard-list',
                 tone='neutral', href='/requests'),
            _kpi(key='delivered_today', label='Delivered today',
                 value=m['requests_delivered_today'], icon='package-check',
                 tone='neutral', href='/requests?lifecycle=delivered'),
        ],
        'actions': [
            _action(key='alerts', title='Stock alerts',
                    count=m['alerts_open'],
                    description='Inventory items below threshold or expiring soon.',
                    cta='Review alerts', href='/alerts',
                    tone='danger' if m['alerts_critical'] else 'warning'),
            _action(key='stock', title='Stock overview',
                    count=0,
                    description='See inventory levels, lots and movements.',
                    cta='Open stock', href='/stock', tone='primary'),
            _action(key='procurement', title='Procurement',
                    count=0,
                    description='Manage suppliers and purchase orders.',
                    cta='Open procurement', href='/procurement', tone='primary'),
        ],
    }


def _default(m: Dict[str, Any]) -> Dict[str, List]:
    """Fallback for VIEWER_AUDITOR or any unmapped role."""
    return {
        'kpis': [
            _kpi(key='active_requests', label='Active requests',
                 value=m['requests_active'], icon='clipboard-list',
                 tone='primary', href='/requests'),
            _kpi(key='pending_validation', label='Pending validation',
                 value=m['results_pending_validation'], icon='clipboard-check',
                 tone='warning', href='/results?status=SUBMITTED'),
            _kpi(key='ready_to_notify', label='Results ready',
                 value=m['requests_ready_to_notify'], icon='mail',
                 tone='success', href='/requests?status=VALIDATED'),
            _kpi(key='alerts', label='Open alerts',
                 value=m['alerts_open'], icon='bell',
                 tone='danger' if m['alerts_critical'] else 'warning', href='/alerts'),
        ],
        'actions': [
            _action(key='requests', title='View requests',
                    count=m['requests_active'],
                    description='Browse all active analysis requests.',
                    cta='Open requests', href='/requests', tone='primary'),
            _action(key='results', title='View results',
                    count=m['results_pending_validation'],
                    description='Inspect validation pipeline.',
                    cta='Open results', href='/results', tone='primary'),
        ],
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Roles allowed to see revenue-tinged KPIs.
_REVENUE_ROLES = frozenset({'LAB_ADMIN', 'BILLING_OFFICER'})


def build_cockpit(user) -> Dict[str, Any]:
    """Return the role-adapted cockpit payload for ``user``."""
    metrics = _compute_metrics()
    role = getattr(user, 'role', None) or ''

    if role == 'RECEPTIONIST':
        composed = _receptionist(metrics)
    elif role == 'TECHNICIAN':
        composed = _technician(metrics)
    elif role == 'BIOLOGIST':
        composed = _biologist(metrics)
    elif role in ('LAB_ADMIN', 'BILLING_OFFICER'):
        composed = _lab_admin(metrics, include_revenue=role in _REVENUE_ROLES)
    elif role == 'INVENTORY_MANAGER':
        composed = _inventory(metrics)
    else:  # VIEWER_AUDITOR + any future / unmapped role
        composed = _default(metrics)

    return {
        'role': role,
        'greeting_name': (user.first_name or '').strip() or user.email,
        'kpis': composed['kpis'],
        'actions': composed['actions'],
        'charts': {
            'requests_over_time': metrics['requests_over_time'],
            'requests_by_status': metrics['requests_by_status'],
            'requests_by_source': metrics['requests_by_source'],
            'results_pipeline':   metrics['results_pipeline'],
        },
    }
