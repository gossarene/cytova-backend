"""
Cytova — Exam reports views.

Two endpoints, both POST so the filter payload can carry rich
structured data without trying to cram lists into a query string:

  POST /api/v1/exam-reports/exams-by-partner/preview/  → JSON pivot
  POST /api/v1/exam-reports/exams-by-partner/export/   → XLSX download

Permission gate: ``reports.view`` if defined; otherwise we ride on
``billing.view`` (same audience as the financial reports surface
— LAB_ADMIN + BILLING_OFFICER). The two reports are operationally
adjacent; a single role gate keeps the matrix small.
"""
from __future__ import annotations

import logging

from django.http import HttpResponse
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.models import ActorType, AuditAction, AuditLog
from common.permissions import RequiresPermission

from .serializers import ExamsByPartnerFiltersSerializer
from .services import build_exams_by_partner_report
from .xlsx_service import render_exams_by_partner_xlsx


logger = logging.getLogger(__name__)


# Mirrors the financial-reports gate; the two surfaces share an audience.
_ViewPermission = RequiresPermission('billing.view')


class ExamsByPartnerPreviewView(APIView):
    permission_classes = [_ViewPermission]

    def post(self, request):
        ser = ExamsByPartnerFiltersSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        report = build_exams_by_partner_report(ser.to_filters())
        return Response(report)


class ExamsByPartnerExportView(APIView):
    """Build the pivot and stream it back as an XLSX download.

    A short audit row records the export event — useful for operators
    who later need to confirm a report was actually pulled from the
    system (vs. typed in by hand into an email). The audit captures
    only the period + filter shape; no patient data ever lands in
    the diff.
    """
    permission_classes = [_ViewPermission]

    def post(self, request):
        ser = ExamsByPartnerFiltersSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        filters = ser.to_filters()
        report = build_exams_by_partner_report(filters)
        xlsx_bytes = render_exams_by_partner_xlsx(report)

        # Audit — operator name + applied filters (no patient data,
        # no clinical content). Matches the existing catalog/audit
        # convention of using ActorType.STAFF_USER + AuditAction.VIEW.
        try:
            AuditLog.objects.create(
                actor_type=ActorType.STAFF_USER,
                actor_id=getattr(request.user, 'id', None),
                actor_email=getattr(request.user, 'email', ''),
                action=AuditAction.VIEW,
                entity_type='ExamsByPartnerReport',
                entity_id=None,
                diff={'export': report['filters_applied']},
                ip_address=getattr(request, 'audit_ip', None),
                user_agent=getattr(request, 'audit_user_agent', ''),
            )
        except Exception:
            # Audit failure must not break the download path —
            # operators rely on the export under time pressure.
            logger.exception('Failed to audit exams-by-partner export')

        filename = (
            f'exams_by_partner_{filters.period_start.strftime("%Y%m%d")}'
            f'_{filters.period_end.strftime("%Y%m%d")}.xlsx'
        )
        response = HttpResponse(
            xlsx_bytes,
            content_type=(
                'application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.sheet'
            ),
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
