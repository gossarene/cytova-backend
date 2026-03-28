"""
Cytova — Dashboard Views

Read-only aggregation endpoints for the frontend dashboard.
Each view runs a small number of bulk queries with annotations —
no N+1 patterns, no per-row service calls.

    GET /overview/      — lightweight summary across all domains
    GET /patients/      — patient registration metrics
    GET /requests/      — request lifecycle + execution mode stats
    GET /partners/      — partner organization analytics + revenue
    GET /results/       — result validation / publication pipeline
    GET /stock/         — inventory health indicators
    GET /alerts/        — open alert counts by type and severity
    GET /procurement/   — purchase order + reception indicators
"""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import (
    Count,
    DecimalField,
    F,
    Q,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from common.permissions import IsAnyStaff


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _period_boundaries():
    """Returns (today, start_of_week, start_of_month) as dates."""
    now = timezone.now()
    today = now.date()
    start_of_week = today - timedelta(days=today.weekday())  # Monday
    start_of_month = today.replace(day=1)
    return today, start_of_week, start_of_month


def _status_breakdown(qs):
    """Turn .values('status').annotate(count=Count('id')) into a dict."""
    return dict(
        qs.values_list('status', 'count')
    )


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

class DashboardOverviewView(APIView):
    """
    Lightweight cross-domain summary. One or two key numbers per domain —
    use the individual endpoints for detailed breakdowns.
    """
    permission_classes = [IsAnyStaff]

    def get(self, request):
        from apps.alerts.models import AlertSeverity, InventoryAlert, OPEN_STATUSES
        from apps.patients.models import Patient
        from apps.requests.models import AnalysisRequest, RequestStatus, SourceType
        from apps.results.models import ExamResult, ResultStatus
        from apps.stock.models import StockItem, StockLot
        from apps.suppliers.models import PurchaseOrder, RECEIVABLE_STATUSES

        today, _, start_of_month = _period_boundaries()
        warning_days = getattr(settings, 'ALERT_EXPIRY_WARNING_DAYS', 30)
        expiry_cutoff = today + timedelta(days=warning_days)

        # ---- patients ----
        patient_qs = Patient.objects.filter(is_active=True)
        patients_total = patient_qs.count()
        patients_month = patient_qs.filter(created_at__date__gte=start_of_month).count()

        # ---- requests ----
        req_active = AnalysisRequest.objects.filter(
            status__in=[
                RequestStatus.CONFIRMED,
                RequestStatus.IN_PROGRESS,
            ]
        ).count()
        req_month = AnalysisRequest.objects.filter(
            created_at__date__gte=start_of_month,
        ).count()
        req_total = AnalysisRequest.objects.count()
        req_partner = AnalysisRequest.objects.filter(
            source_type=SourceType.PARTNER_ORGANIZATION,
        ).count()

        # ---- results ----
        pending_validation = ExamResult.objects.filter(
            status=ResultStatus.PENDING_VALIDATION,
        ).count()
        published_month = ExamResult.objects.filter(
            published_at__date__gte=start_of_month,
        ).count()

        # ---- stock ----
        items_with_qty = StockItem.objects.filter(is_active=True).annotate(
            available_qty=Coalesce(
                Sum('lots__current_quantity', filter=Q(lots__is_exhausted=False)),
                Value(Decimal('0')),
                output_field=DecimalField(),
            ),
        )
        below_threshold = items_with_qty.filter(
            minimum_threshold__gt=0,
            available_qty__lt=F('minimum_threshold'),
            available_qty__gt=0,
        ).count()
        out_of_stock = items_with_qty.filter(available_qty__lte=0).count()
        expiring_soon = StockLot.objects.filter(
            is_exhausted=False,
            expiry_date__isnull=False,
            expiry_date__gt=today,
            expiry_date__lte=expiry_cutoff,
        ).count()

        # ---- alerts ----
        alert_open = InventoryAlert.objects.filter(
            status__in=OPEN_STATUSES,
        )
        alerts_total = alert_open.count()
        alerts_critical = alert_open.filter(
            severity=AlertSeverity.CRITICAL,
        ).count()

        # ---- procurement ----
        pending_orders = PurchaseOrder.objects.filter(
            status__in=RECEIVABLE_STATUSES,
        ).count()

        return Response({
            'patients': {
                'total_active': patients_total,
                'registered_this_month': patients_month,
            },
            'requests': {
                'active': req_active,
                'created_this_month': req_month,
                'total': req_total,
                'from_partners': req_partner,
                'from_direct': req_total - req_partner,
            },
            'results': {
                'pending_validation': pending_validation,
                'published_this_month': published_month,
            },
            'stock': {
                'below_threshold': below_threshold,
                'out_of_stock': out_of_stock,
                'expiring_soon': expiring_soon,
            },
            'alerts': {
                'total_open': alerts_total,
                'critical': alerts_critical,
            },
            'procurement': {
                'pending_orders': pending_orders,
            },
        })


# ---------------------------------------------------------------------------
# Patients
# ---------------------------------------------------------------------------

class DashboardPatientsView(APIView):
    permission_classes = [IsAnyStaff]

    def get(self, request):
        from apps.patients.models import Patient

        today, start_of_week, start_of_month = _period_boundaries()

        total = Patient.objects.count()
        active = Patient.objects.filter(is_active=True).count()

        base_qs = Patient.objects.all()

        return Response({
            'total': total,
            'active': active,
            'inactive': total - active,
            'registered_today': base_qs.filter(
                created_at__date=today,
            ).count(),
            'registered_this_week': base_qs.filter(
                created_at__date__gte=start_of_week,
            ).count(),
            'registered_this_month': base_qs.filter(
                created_at__date__gte=start_of_month,
            ).count(),
        })


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------

class DashboardRequestsView(APIView):
    permission_classes = [IsAnyStaff]

    def get(self, request):
        from apps.requests.models import (
            AnalysisRequest,
            AnalysisRequestItem,
        )

        today, start_of_week, start_of_month = _period_boundaries()

        # Request status breakdown
        req_status = _status_breakdown(
            AnalysisRequest.objects
            .values('status')
            .annotate(count=Count('id'))
        )

        req_base = AnalysisRequest.objects.all()
        total = sum(req_status.values())

        # Execution mode breakdown across all items
        exec_mode = dict(
            AnalysisRequestItem.objects
            .values('execution_mode')
            .annotate(count=Count('id'))
            .values_list('execution_mode', 'count')
        )

        # Item status breakdown
        item_status = _status_breakdown(
            AnalysisRequestItem.objects
            .values('status')
            .annotate(count=Count('id'))
        )

        # Source type breakdown
        by_source_type = dict(
            req_base
            .values('source_type')
            .annotate(count=Count('id'))
            .values_list('source_type', 'count')
        )

        # Billing mode breakdown
        by_billing_mode = dict(
            req_base
            .values('billing_mode')
            .annotate(count=Count('id'))
            .values_list('billing_mode', 'count')
        )

        return Response({
            'by_status': req_status,
            'total': total,
            'created_today': req_base.filter(
                created_at__date=today,
            ).count(),
            'created_this_week': req_base.filter(
                created_at__date__gte=start_of_week,
            ).count(),
            'created_this_month': req_base.filter(
                created_at__date__gte=start_of_month,
            ).count(),
            'by_source_type': by_source_type,
            'by_billing_mode': by_billing_mode,
            'items': {
                'by_status': item_status,
                'by_execution_mode': exec_mode,
            },
        })


# ---------------------------------------------------------------------------
# Partners
# ---------------------------------------------------------------------------

class DashboardPartnersView(APIView):
    """
    Partner organization analytics: request volume, exam volume, revenue,
    and the direct-vs-partner ratio.
    """
    permission_classes = [IsAnyStaff]

    def get(self, request):
        from django.conf import settings as django_settings
        from apps.partners.models import PartnerOrganization
        from apps.requests.models import (
            AnalysisRequest,
            AnalysisRequestItem,
            RequestStatus,
            SourceType,
        )

        today, _, start_of_month = _period_boundaries()
        top_n = getattr(django_settings, 'DASHBOARD_TOP_N_LIMIT', 20)
        confirmed_statuses = [
            RequestStatus.CONFIRMED,
            RequestStatus.IN_PROGRESS,
            RequestStatus.COMPLETED,
        ]

        # ---- ratio: direct vs partner (all-time, confirmed+) ----
        confirmed_qs = AnalysisRequest.objects.filter(
            status__in=confirmed_statuses,
        )
        total_confirmed = confirmed_qs.count()
        partner_confirmed = confirmed_qs.filter(
            source_type=SourceType.PARTNER_ORGANIZATION,
        ).count()
        direct_confirmed = total_confirmed - partner_confirmed

        # ---- top partners by request volume (this month) ----
        partner_requests_month = list(
            AnalysisRequest.objects
            .filter(
                source_type=SourceType.PARTNER_ORGANIZATION,
                partner_organization__isnull=False,
                created_at__date__gte=start_of_month,
            )
            .values(
                'partner_organization_id',
                partner_code=F('partner_organization__code'),
                partner_name=F('partner_organization__name'),
            )
            .annotate(request_count=Count('id'))
            .order_by('-request_count')[:top_n]
        )

        # ---- exam volume per partner (confirmed+ items, this month) ----
        partner_items_month = list(
            AnalysisRequestItem.objects
            .filter(
                analysis_request__source_type=SourceType.PARTNER_ORGANIZATION,
                analysis_request__partner_organization__isnull=False,
                analysis_request__status__in=confirmed_statuses,
                analysis_request__created_at__date__gte=start_of_month,
            )
            .values(
                partner_id=F('analysis_request__partner_organization_id'),
                partner_code=F('analysis_request__partner_organization__code'),
                partner_name=F('analysis_request__partner_organization__name'),
            )
            .annotate(
                exam_count=Count('id'),
                total_billed=Coalesce(
                    Sum('billed_price'),
                    Value(Decimal('0')),
                    output_field=DecimalField(),
                ),
            )
            .order_by('-exam_count')[:top_n]
        )

        # ---- aggregate revenue by partner (confirmed+, all-time) ----
        partner_revenue = list(
            AnalysisRequestItem.objects
            .filter(
                analysis_request__source_type=SourceType.PARTNER_ORGANIZATION,
                analysis_request__partner_organization__isnull=False,
                analysis_request__status__in=confirmed_statuses,
                billed_price__isnull=False,
            )
            .values(
                partner_id=F('analysis_request__partner_organization_id'),
                partner_code=F('analysis_request__partner_organization__code'),
                partner_name=F('analysis_request__partner_organization__name'),
            )
            .annotate(
                total_billed=Coalesce(
                    Sum('billed_price'),
                    Value(Decimal('0')),
                    output_field=DecimalField(),
                ),
                exam_count=Count('id'),
            )
            .order_by('-total_billed')[:top_n]
        )

        # Serialize Decimals to strings for JSON
        for row in partner_items_month:
            row['total_billed'] = str(row['total_billed'])
        for row in partner_revenue:
            row['total_billed'] = str(row['total_billed'])

        return Response({
            'ratio': {
                'total_confirmed': total_confirmed,
                'direct': direct_confirmed,
                'partner': partner_confirmed,
            },
            'active_partners': PartnerOrganization.objects.filter(
                is_active=True,
            ).count(),
            'requests_by_partner_this_month': partner_requests_month,
            'exams_by_partner_this_month': partner_items_month,
            'revenue_by_partner': partner_revenue,
        })


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

class DashboardResultsView(APIView):
    permission_classes = [IsAnyStaff]

    def get(self, request):
        from apps.results.models import ExamResult, ResultStatus

        today, start_of_week, start_of_month = _period_boundaries()

        status_breakdown = _status_breakdown(
            ExamResult.objects
            .values('status')
            .annotate(count=Count('id'))
        )

        abnormal_published = ExamResult.objects.filter(
            status=ResultStatus.PUBLISHED,
            is_abnormal=True,
        ).count()

        pub_base = ExamResult.objects.filter(
            status=ResultStatus.PUBLISHED,
        )

        return Response({
            'by_status': status_breakdown,
            'total': sum(status_breakdown.values()),
            'abnormal_published': abnormal_published,
            'published_today': pub_base.filter(
                published_at__date=today,
            ).count(),
            'published_this_week': pub_base.filter(
                published_at__date__gte=start_of_week,
            ).count(),
            'published_this_month': pub_base.filter(
                published_at__date__gte=start_of_month,
            ).count(),
        })


# ---------------------------------------------------------------------------
# Stock
# ---------------------------------------------------------------------------

class DashboardStockView(APIView):
    permission_classes = [IsAnyStaff]

    def get(self, request):
        from apps.stock.models import StockItem, StockLot

        today = timezone.now().date()
        warning_days = getattr(settings, 'ALERT_EXPIRY_WARNING_DAYS', 30)
        expiry_cutoff = today + timedelta(days=warning_days)

        total_items = StockItem.objects.filter(is_active=True).count()

        items_with_qty = StockItem.objects.filter(is_active=True).annotate(
            available_qty=Coalesce(
                Sum('lots__current_quantity', filter=Q(lots__is_exhausted=False)),
                Value(Decimal('0')),
                output_field=DecimalField(),
            ),
        )

        below_threshold = items_with_qty.filter(
            minimum_threshold__gt=0,
            available_qty__lt=F('minimum_threshold'),
            available_qty__gt=0,
        ).count()

        out_of_stock = items_with_qty.filter(available_qty__lte=0).count()

        # Lot-level expiry metrics
        active_lots = StockLot.objects.filter(is_exhausted=False)
        lots_total = active_lots.count()

        expiring_soon = active_lots.filter(
            expiry_date__isnull=False,
            expiry_date__gt=today,
            expiry_date__lte=expiry_cutoff,
        ).count()

        expired = active_lots.filter(
            expiry_date__isnull=False,
            expiry_date__lte=today,
        ).count()

        return Response({
            'total_active_items': total_items,
            'below_threshold': below_threshold,
            'out_of_stock': out_of_stock,
            'total_active_lots': lots_total,
            'expiring_soon': expiring_soon,
            'expiring_soon_window_days': warning_days,
            'expired': expired,
        })


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

class DashboardAlertsView(APIView):
    permission_classes = [IsAnyStaff]

    def get(self, request):
        from apps.alerts.models import (
            AlertSeverity,
            AlertStatus,
            InventoryAlert,
            OPEN_STATUSES,
        )

        open_qs = InventoryAlert.objects.filter(status__in=OPEN_STATUSES)

        by_type = dict(
            open_qs
            .values('alert_type')
            .annotate(count=Count('id'))
            .values_list('alert_type', 'count')
        )

        by_severity = dict(
            open_qs
            .values('severity')
            .annotate(count=Count('id'))
            .values_list('severity', 'count')
        )

        by_status = dict(
            open_qs
            .values('status')
            .annotate(count=Count('id'))
            .values_list('status', 'count')
        )

        return Response({
            'total_open': open_qs.count(),
            'by_type': by_type,
            'by_severity': by_severity,
            'by_status': by_status,
        })


# ---------------------------------------------------------------------------
# Procurement
# ---------------------------------------------------------------------------

class DashboardProcurementView(APIView):
    permission_classes = [IsAnyStaff]

    def get(self, request):
        from apps.suppliers.models import (
            PurchaseOrder,
            PurchaseOrderStatus,
            Reception,
            RECEIVABLE_STATUSES,
        )

        today, start_of_week, start_of_month = _period_boundaries()

        order_status = _status_breakdown(
            PurchaseOrder.objects
            .values('status')
            .annotate(count=Count('id'))
        )

        pending_reception = PurchaseOrder.objects.filter(
            status__in=RECEIVABLE_STATUSES,
        ).count()

        overdue = PurchaseOrder.objects.filter(
            status__in=RECEIVABLE_STATUSES,
            expected_delivery_date__isnull=False,
            expected_delivery_date__lt=today,
        ).count()

        receptions_month = Reception.objects.filter(
            received_at__gte=start_of_month,
        ).count()

        with_discrepancy = Reception.objects.filter(
            has_discrepancy=True,
            received_at__gte=start_of_month,
        ).count()

        return Response({
            'orders_by_status': order_status,
            'orders_total': sum(order_status.values()),
            'pending_reception': pending_reception,
            'overdue': overdue,
            'receptions_this_month': receptions_month,
            'receptions_with_discrepancy_this_month': with_discrepancy,
        })
