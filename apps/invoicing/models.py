"""
Cytova — Partner Invoicing Models

Invoice
    A partner billing document covering a date period. Lifecycle:
    DRAFT → CONFIRMED (locked) or DRAFT → CANCELLED.
    Once CONFIRMED, no second invoice for the same partner + period
    can be confirmed (partial unique constraint).

InvoiceLine
    One row per billable request item. All financially relevant data
    is snapshotted at generation time so the invoice remains a
    self-contained record regardless of later catalog or pricing changes.

InvoiceNumberSequence
    Per-tenant monthly counter for professional invoice numbering
    (``INV-YYYYMM-NNNNNN``). Same atomic pattern as LabelSequence.
"""
from django.db import models
from django.utils import timezone

from common.models import BaseModel


class InvoiceStatus(models.TextChoices):
    DRAFT = 'DRAFT', 'Draft'
    CONFIRMED = 'CONFIRMED', 'Confirmed'
    CANCELLED = 'CANCELLED', 'Cancelled'


class Invoice(BaseModel):
    partner = models.ForeignKey(
        'partners.PartnerOrganization',
        on_delete=models.PROTECT,
        related_name='invoices',
    )
    invoice_number = models.CharField(
        max_length=25,
        unique=True,
        db_index=True,
        help_text='Professional invoice number: INV-YYYYMM-NNNNNN.',
    )
    status = models.CharField(
        max_length=12,
        choices=InvoiceStatus.choices,
        default=InvoiceStatus.DRAFT,
        db_index=True,
    )

    # Period covered
    period_start = models.DateField()
    period_end = models.DateField()

    # Financial snapshots — frozen at generation time
    currency = models.CharField(max_length=3, default='XOF')
    gross_total = models.DecimalField(max_digits=14, decimal_places=2)
    discount_rate_snapshot = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text='Partner discount % snapshotted at generation.',
    )
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    subtotal_after_discount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    vat_rate_snapshot = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text='VAT % snapshotted at generation.',
    )
    vat_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    net_total = models.DecimalField(max_digits=14, decimal_places=2)

    # Lifecycle tracking
    generated_by = models.ForeignKey(
        'users.StaffUser', on_delete=models.SET_NULL,
        null=True, related_name='generated_invoices',
    )
    generated_at = models.DateTimeField(default=timezone.now)
    confirmed_by = models.ForeignKey(
        'users.StaffUser', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='confirmed_invoices',
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    notes = models.TextField(blank=True, default='')
    pdf_file_key = models.CharField(
        max_length=500, blank=True, default='',
        help_text='Internal storage key for the rendered invoice PDF.',
    )
    statement_file_key = models.CharField(
        max_length=500, blank=True, default='',
        help_text='Internal storage key for the rendered financial statement PDF.',
    )

    class Meta:
        verbose_name = 'Invoice'
        verbose_name_plural = 'Invoices'
        ordering = ['-generated_at']
        constraints = [
            models.UniqueConstraint(
                fields=['partner', 'period_start', 'period_end'],
                condition=models.Q(status='CONFIRMED'),
                name='unique_confirmed_invoice_per_partner_period',
            ),
        ]
        indexes = [
            models.Index(fields=['partner', '-generated_at']),
            models.Index(fields=['status', '-generated_at']),
        ]

    def __str__(self):
        return f'{self.invoice_number} — {self.partner.name}'

    def delete(self, *args, **kwargs):
        raise PermissionError(
            'Invoices cannot be deleted — they are financial records.'
        )


class InvoiceLine(BaseModel):
    invoice = models.ForeignKey(
        Invoice, on_delete=models.CASCADE, related_name='lines',
    )
    analysis_request = models.ForeignKey(
        'analysis_requests.AnalysisRequest',
        on_delete=models.PROTECT,
        related_name='invoice_lines',
    )
    request_item = models.ForeignKey(
        'analysis_requests.AnalysisRequestItem',
        on_delete=models.PROTECT,
        related_name='invoice_lines',
    )

    # Snapshots — frozen at invoice generation
    request_number_snapshot = models.CharField(max_length=30)
    public_reference_snapshot = models.CharField(max_length=20)
    patient_display_name = models.CharField(max_length=255)
    exam_code_snapshot = models.CharField(max_length=30)
    exam_name_snapshot = models.CharField(max_length=255)
    performed_date = models.DateField(
        null=True, blank=True,
        help_text='Date the exam was collected/performed.',
    )

    unit_price_snapshot = models.DecimalField(max_digits=12, decimal_places=4)
    billed_price_snapshot = models.DecimalField(max_digits=12, decimal_places=4)
    line_amount = models.DecimalField(
        max_digits=12, decimal_places=2,
        help_text='Billed amount for this line (= billed_price_snapshot).',
    )

    class Meta:
        verbose_name = 'Invoice Line'
        verbose_name_plural = 'Invoice Lines'
        ordering = ['patient_display_name', 'performed_date']
        constraints = [
            models.UniqueConstraint(
                fields=['invoice', 'request_item'],
                name='unique_line_per_item_per_invoice',
            ),
        ]

    def __str__(self):
        return f'{self.exam_code_snapshot} — {self.patient_display_name}'


class InvoiceNumberSequence(models.Model):
    """Per-tenant monthly counter for invoice numbering."""
    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField()
    last_value = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Invoice Number Sequence'
        verbose_name_plural = 'Invoice Number Sequences'
        constraints = [
            models.UniqueConstraint(
                fields=['year', 'month'],
                name='unique_invoice_seq_year_month',
            ),
        ]

    def __str__(self):
        return f'InvoiceSeq({self.year:04d}-{self.month:02d} @ {self.last_value})'
