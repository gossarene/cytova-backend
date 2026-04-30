"""
Cytova — Financial Reports views.

Two endpoints:

    POST /api/v1/financial-reports/preview/   → JSON simulation payload
    POST /api/v1/financial-reports/export/    → Financial Statement PDF

Both run the same composer (``build_financial_report``) — the export
endpoint additionally feeds the result through the dedicated PDF renderer
and streams the bytes back. **Neither endpoint creates or mutates any
``Invoice`` record**: the financial-reports surface is strictly read-only.

Permission gate: ``billing.view``. A dedicated ``financial_reports.view``
permission may follow once we have role variants that need different
gating — TODO marker below.
"""
from __future__ import annotations

from django.http import HttpResponse
from rest_framework.response import Response
from rest_framework.views import APIView

from common.permissions import RequiresPermission

from .serializers import FinancialReportFiltersSerializer
from .services import FinancialReportFilters, build_financial_report
from .pdf_service import render_financial_statement_pdf


# TODO(perms): add a dedicated ``financial_reports.view`` permission once
# we want to grant access without exposing the full invoicing surface.
# For now we ride on ``billing.view`` so the existing BILLING_OFFICER /
# LAB_ADMIN roles get it without an RBAC change.
_BillingView = RequiresPermission('billing.view')


def _filters_from(serializer: FinancialReportFiltersSerializer) -> FinancialReportFilters:
    data = serializer.validated_data
    return FinancialReportFilters(
        period_start=data['period_start'],
        period_end=data['period_end'],
        source_type=data['source_type'],
        partner_ids=tuple(str(p) for p in data['partner_ids']),
    )


class FinancialReportPreviewView(APIView):
    permission_classes = [_BillingView]

    def post(self, request):
        ser = FinancialReportFiltersSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        report = build_financial_report(_filters_from(ser))
        return Response(report)


class FinancialReportExportView(APIView):
    """Generates the Financial Statement PDF in memory and returns it as
    a download. Does NOT persist the PDF or create any Invoice record —
    re-running the same period produces a fresh, identical PDF."""
    permission_classes = [_BillingView]

    def post(self, request):
        ser = FinancialReportFiltersSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        report = build_financial_report(_filters_from(ser))
        pdf_bytes = render_financial_statement_pdf(report)
        filename = (
            f'financial-statement-{report["filters_applied"]["period_start"]}'
            f'_{report["filters_applied"]["period_end"]}.pdf'
        )
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
