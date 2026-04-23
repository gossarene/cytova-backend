"""
Cytova — Invoice Views

    POST   /invoicing/preview/              — preview totals without persisting
    POST   /invoicing/                      — generate a DRAFT invoice
    GET    /invoicing/                      — list invoices
    GET    /invoicing/{id}/                 — retrieve invoice detail
    POST   /invoicing/{id}/confirm/         — lock invoice
    POST   /invoicing/{id}/cancel/          — void draft invoice
    POST   /invoicing/{id}/generate-pdf/    — generate invoice PDF
    GET    /invoicing/{id}/download/        — stream invoice PDF
"""
from django.core.files.storage import default_storage
from django.http import FileResponse
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from common.permissions import IsLabAdmin, IsAnyStaff
from apps.partners.models import PartnerOrganization
from .models import Invoice
from .serializers import (
    InvoiceDetailSerializer,
    InvoiceGenerateRequestSerializer,
    InvoiceListSerializer,
    InvoicePreviewRequestSerializer,
)
from .services import InvoiceService


class InvoiceViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    ordering_fields = ['generated_at', 'net_total', 'status']

    def get_queryset(self):
        return (
            Invoice.objects
            .select_related('partner', 'generated_by', 'confirmed_by')
            .prefetch_related('lines')
            .all()
        )

    def get_serializer_class(self):
        if self.action == 'list':
            return InvoiceListSerializer
        return InvoiceDetailSerializer

    def get_permissions(self):
        if self.action in ('list', 'retrieve', 'download', 'download_statement'):
            return [IsAnyStaff()]
        return [IsLabAdmin()]

    def create(self, request):
        """Generate a DRAFT invoice for a partner + period."""
        serializer = InvoiceGenerateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data
        partner = PartnerOrganization.objects.get(id=d['partner_id'])
        invoice = InvoiceService.generate(
            partner=partner,
            period_start=d['period_start'],
            period_end=d['period_end'],
            generated_by=request.user,
            request=request,
            notes=d.get('notes', ''),
        )
        return Response(InvoiceDetailSerializer(invoice).data)

    @action(detail=False, methods=['post'])
    def preview(self, request):
        """Preview invoice totals without creating a record."""
        serializer = InvoicePreviewRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data
        partner = PartnerOrganization.objects.get(id=d['partner_id'])
        result = InvoiceService.preview(
            partner=partner,
            period_start=d['period_start'],
            period_end=d['period_end'],
        )
        lines = [
            {
                'patient_display_name': ld['patient_display_name'],
                'public_reference_snapshot': ld['public_reference_snapshot'],
                'exam_code_snapshot': ld['exam_code_snapshot'],
                'exam_name_snapshot': ld['exam_name_snapshot'],
                'performed_date': ld['performed_date'],
                'unit_price_snapshot': str(ld['unit_price_snapshot']),
                'billed_price_snapshot': str(ld['billed_price_snapshot']),
                'line_amount': str(ld['line_amount']),
            }
            for ld in result['lines']
        ]
        return Response({
            'partner_id': str(partner.id),
            'partner_name': partner.name,
            'period_start': str(d['period_start']),
            'period_end': str(d['period_end']),
            'line_count': result['line_count'],
            'lines': lines,
            'gross_total': str(result['gross_total']),
            'discount_rate': str(result['discount_rate']),
            'discount_amount': str(result['discount_amount']),
            'subtotal_after_discount': str(result['subtotal_after_discount']),
            'vat_rate': str(result['vat_rate']),
            'vat_amount': str(result['vat_amount']),
            'net_total': str(result['net_total']),
        })

    @action(detail=True, methods=['post'])
    def confirm(self, request, pk=None):
        """Lock a DRAFT invoice."""
        invoice = self._get_or_404(pk)
        invoice = InvoiceService.confirm(
            invoice=invoice,
            confirmed_by=request.user,
            request=request,
        )
        return Response(InvoiceDetailSerializer(invoice).data)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Void a DRAFT invoice."""
        invoice = self._get_or_404(pk)
        invoice = InvoiceService.cancel(
            invoice=invoice,
            cancelled_by=request.user,
            request=request,
        )
        return Response(InvoiceDetailSerializer(invoice).data)

    @action(detail=True, methods=['post'], url_path='generate-pdf')
    def generate_pdf(self, request, pk=None):
        """Generate invoice PDF (or return existing)."""
        from .pdf_service import InvoicePdfService, DOC_INVOICE
        invoice = self._get_or_404(pk)
        invoice = InvoicePdfService.generate_or_get(invoice, DOC_INVOICE)
        return Response(InvoiceDetailSerializer(invoice).data)

    @action(detail=True, methods=['post'], url_path='generate-statement')
    def generate_statement(self, request, pk=None):
        """Generate financial statement PDF (or return existing)."""
        from .pdf_service import InvoicePdfService, DOC_STATEMENT
        invoice = self._get_or_404(pk)
        invoice = InvoicePdfService.generate_or_get(invoice, DOC_STATEMENT)
        return Response(InvoiceDetailSerializer(invoice).data)

    @action(detail=True, methods=['get'])
    def download(self, request, pk=None):
        """Stream the invoice PDF securely."""
        invoice = self._get_or_404(pk)
        if not invoice.pdf_file_key:
            raise NotFound('No invoice PDF has been generated.')
        file_obj = default_storage.open(invoice.pdf_file_key, 'rb')
        return FileResponse(
            file_obj,
            content_type='application/pdf',
            as_attachment=True,
            filename=f'{invoice.invoice_number}.pdf',
        )

    @action(detail=True, methods=['get'], url_path='download-statement')
    def download_statement(self, request, pk=None):
        """Stream the financial statement PDF securely."""
        invoice = self._get_or_404(pk)
        if not invoice.statement_file_key:
            raise NotFound('No financial statement PDF has been generated.')
        file_obj = default_storage.open(invoice.statement_file_key, 'rb')
        return FileResponse(
            file_obj,
            content_type='application/pdf',
            as_attachment=True,
            filename=f'statement_{invoice.invoice_number}.pdf',
        )

    def _get_or_404(self, pk):
        try:
            return Invoice.objects.select_related(
                'partner', 'generated_by', 'confirmed_by',
            ).get(pk=pk)
        except Invoice.DoesNotExist:
            raise NotFound('Invoice not found.')
