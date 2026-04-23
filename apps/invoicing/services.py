"""
Cytova — Invoice Service

Handles the full partner invoicing lifecycle:

    preview   — compute invoice totals without persisting
    generate  — create a DRAFT invoice + snapshotted lines
    confirm   — lock the invoice, enforce period uniqueness
    cancel    — void a DRAFT invoice

Source data rules:
    Billable items are AnalysisRequestItems where:
    - parent request has partner_organization = selected partner
    - parent request source_type = PARTNER_ORGANIZATION
    - parent request status = VALIDATED (finalized)
    - item execution_mode ≠ REJECTED
    - parent request confirmed_at falls within [period_start, period_end]
    - item is not already on a CONFIRMED invoice (prevents double-billing)

Snapshotting:
    All financially relevant data is frozen at generation time.
    Invoice totals, line amounts, patient names, exam names, and
    pricing are immutable after creation.
"""
import logging
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.audit.models import ActorType, AuditAction, AuditLog
from apps.partners.models import PartnerOrganization
from apps.requests.models import (
    AnalysisRequestItem, ItemStatus, RequestStatus,
)
from apps.users.models import StaffUser
from .models import (
    Invoice, InvoiceLine, InvoiceNumberSequence, InvoiceStatus,
)

logger = logging.getLogger(__name__)

INVOICE_SEQ_WIDTH = 6


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class InvoiceService:

    @staticmethod
    def preview(
        partner: PartnerOrganization,
        period_start: date,
        period_end: date,
    ) -> dict:
        """
        Compute preview data for a potential invoice without persisting.

        Returns a dict with ``lines``, ``gross_total``, ``discount_rate``,
        ``discount_amount``, ``net_total``, and summary counts. The
        shape matches what the generate endpoint produces so the frontend
        can render a preview card identically.
        """
        items = _billable_items(partner, period_start, period_end)
        lines = [_build_line_snapshot(item) for item in items]
        return _compute_totals(partner, lines)

    @staticmethod
    @transaction.atomic
    def generate(
        partner: PartnerOrganization,
        period_start: date,
        period_end: date,
        generated_by: StaffUser,
        request,
        notes: str = '',
    ) -> Invoice:
        """
        Create a DRAFT invoice with snapshotted lines.

        Raises if a CONFIRMED invoice already covers the same partner
        + period (duplicate prevention). DRAFT duplicates are allowed
        so operators can regenerate a preview correction.
        """
        _check_no_confirmed_duplicate(partner, period_start, period_end)

        items = _billable_items(partner, period_start, period_end)
        if not items:
            raise ValidationError(
                'No billable items found for this partner and period.'
            )

        lines_data = [_build_line_snapshot(item) for item in items]
        totals = _compute_totals(partner, lines_data)

        invoice_number = _allocate_invoice_number(period_start)

        invoice = Invoice.objects.create(
            partner=partner,
            invoice_number=invoice_number,
            status=InvoiceStatus.DRAFT,
            period_start=period_start,
            period_end=period_end,
            gross_total=totals['gross_total'],
            discount_rate_snapshot=totals['discount_rate'],
            discount_amount=totals['discount_amount'],
            subtotal_after_discount=totals['subtotal_after_discount'],
            vat_rate_snapshot=totals['vat_rate'],
            vat_amount=totals['vat_amount'],
            net_total=totals['net_total'],
            generated_by=generated_by,
            generated_at=timezone.now(),
            notes=notes,
        )

        for ld in lines_data:
            InvoiceLine.objects.create(invoice=invoice, **ld)

        _audit(
            actor=generated_by,
            action=AuditAction.CREATE,
            entity_type='Invoice',
            entity_id=invoice.id,
            diff={'after': {
                'invoice_number': invoice_number,
                'partner': partner.name,
                'period': f'{period_start} — {period_end}',
                'gross_total': str(totals['gross_total']),
                'discount_rate': str(totals['discount_rate']),
                'vat_rate': str(totals['vat_rate']),
                'net_total': str(totals['net_total']),
                'line_count': len(lines_data),
            }},
            request=request,
        )

        logger.info(
            'Invoice %s generated for %s (%s → %s), %d lines, net %s',
            invoice_number, partner.name, period_start, period_end,
            len(lines_data), totals['net_total'],
        )
        return invoice

    @staticmethod
    @transaction.atomic
    def confirm(
        invoice: Invoice,
        confirmed_by: StaffUser,
        request,
    ) -> Invoice:
        """Lock a DRAFT invoice. Enforces period uniqueness at DB level."""
        if invoice.status != InvoiceStatus.DRAFT:
            raise ValidationError(
                f'Only DRAFT invoices can be confirmed '
                f'(current status: {invoice.status}).'
            )

        _check_no_confirmed_duplicate(
            invoice.partner, invoice.period_start, invoice.period_end,
        )

        invoice.status = InvoiceStatus.CONFIRMED
        invoice.confirmed_by = confirmed_by
        invoice.confirmed_at = timezone.now()
        invoice.save(update_fields=[
            'status', 'confirmed_by', 'confirmed_at', 'updated_at',
        ])

        _audit(
            actor=confirmed_by,
            action=AuditAction.CONFIRM,
            entity_type='Invoice',
            entity_id=invoice.id,
            diff={'after': {
                'status': InvoiceStatus.CONFIRMED,
                'invoice_number': invoice.invoice_number,
                'net_total': str(invoice.net_total),
            }},
            request=request,
        )
        return invoice

    @staticmethod
    @transaction.atomic
    def cancel(
        invoice: Invoice,
        cancelled_by: StaffUser,
        request,
    ) -> Invoice:
        """Void a DRAFT invoice. Confirmed invoices cannot be cancelled."""
        if invoice.status != InvoiceStatus.DRAFT:
            raise ValidationError(
                f'Only DRAFT invoices can be cancelled '
                f'(current status: {invoice.status}).'
            )

        invoice.status = InvoiceStatus.CANCELLED
        invoice.cancelled_at = timezone.now()
        invoice.save(update_fields=['status', 'cancelled_at', 'updated_at'])

        _audit(
            actor=cancelled_by,
            action=AuditAction.CANCEL,
            entity_type='Invoice',
            entity_id=invoice.id,
            diff={'after': {
                'status': InvoiceStatus.CANCELLED,
                'invoice_number': invoice.invoice_number,
            }},
            request=request,
        )
        return invoice


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _billable_items(
    partner: PartnerOrganization,
    period_start: date,
    period_end: date,
):
    """
    Return the queryset of request items eligible for invoicing.

    Items already on a CONFIRMED invoice are excluded to prevent
    double-billing. Items on DRAFT or CANCELLED invoices are still
    eligible (those invoices haven't been locked).
    """
    already_invoiced = InvoiceLine.objects.filter(
        invoice__status=InvoiceStatus.CONFIRMED,
    ).values_list('request_item_id', flat=True)

    return list(
        AnalysisRequestItem.objects
        .filter(
            analysis_request__partner_organization=partner,
            analysis_request__source_type='PARTNER_ORGANIZATION',
            analysis_request__status=RequestStatus.VALIDATED,
            analysis_request__confirmed_at__date__gte=period_start,
            analysis_request__confirmed_at__date__lte=period_end,
        )
        .exclude(execution_mode='REJECTED')
        .exclude(id__in=already_invoiced)
        .select_related(
            'analysis_request__patient',
            'exam_definition',
        )
        .order_by(
            'analysis_request__patient__last_name',
            'analysis_request__confirmed_at',
        )
    )


def _build_line_snapshot(item: AnalysisRequestItem) -> dict:
    """Build the snapshot dict for one invoice line."""
    ar = item.analysis_request
    patient = ar.patient
    return {
        'analysis_request': ar,
        'request_item': item,
        'request_number_snapshot': ar.request_number,
        'public_reference_snapshot': ar.public_reference or ar.request_number,
        'patient_display_name': f'{patient.last_name}, {patient.first_name}',
        'exam_code_snapshot': item.exam_definition.code if item.exam_definition else '',
        'exam_name_snapshot': item.exam_definition.name if item.exam_definition else '',
        'performed_date': (
            item.collected_at.date() if item.collected_at else
            (ar.confirmed_at.date() if ar.confirmed_at else None)
        ),
        'unit_price_snapshot': item.unit_price,
        'billed_price_snapshot': item.billed_price,
        'line_amount': item.billed_price.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
    }


def _compute_totals(
    partner: PartnerOrganization,
    lines_data: list[dict],
) -> dict:
    """
    Compute financial totals using the canonical order:

        1. gross_total           = sum(line_amounts)
        2. discount_amount       = gross_total × discount_rate / 100
        3. subtotal_after_discount = gross_total − discount_amount
        4. vat_amount            = subtotal_after_discount × vat_rate / 100
        5. net_total             = subtotal_after_discount + vat_amount

    Ownership:
        - discount_rate comes from ``PartnerOrganization.invoice_discount_rate``
        - vat_rate comes from ``LabSettings.default_invoice_vat_rate``

    All intermediate values are quantized to 2 decimal places (half-up)
    so the snapshotted totals are stable and reproducible.
    """
    from apps.lab_settings.models import LabSettings

    q = Decimal('0.01')
    gross = sum(ld['line_amount'] for ld in lines_data)

    discount_rate = partner.invoice_discount_rate or Decimal('0')
    discount_amount = (gross * discount_rate / Decimal('100')).quantize(q, rounding=ROUND_HALF_UP)
    subtotal = gross - discount_amount

    lab = LabSettings.get_solo()
    vat_rate = lab.default_invoice_vat_rate or Decimal('0')
    vat_amount = (subtotal * vat_rate / Decimal('100')).quantize(q, rounding=ROUND_HALF_UP)
    net = subtotal + vat_amount

    return {
        'lines': lines_data,
        'line_count': len(lines_data),
        'gross_total': gross,
        'discount_rate': discount_rate,
        'discount_amount': discount_amount,
        'subtotal_after_discount': subtotal,
        'vat_rate': vat_rate,
        'vat_amount': vat_amount,
        'net_total': net,
    }


def _check_no_confirmed_duplicate(
    partner: PartnerOrganization,
    period_start: date,
    period_end: date,
):
    exists = Invoice.objects.filter(
        partner=partner,
        period_start=period_start,
        period_end=period_end,
        status=InvoiceStatus.CONFIRMED,
    ).exists()
    if exists:
        raise ValidationError(
            'A confirmed invoice already exists for this partner and period.'
        )


def _allocate_invoice_number(ref_date: date) -> str:
    InvoiceNumberSequence.objects.get_or_create(
        year=ref_date.year, month=ref_date.month,
    )
    seq = InvoiceNumberSequence.objects.select_for_update().get(
        year=ref_date.year, month=ref_date.month,
    )
    seq.last_value += 1
    seq.save(update_fields=['last_value', 'updated_at'])
    return f'INV-{ref_date.year}{ref_date.month:02d}-{seq.last_value:0{INVOICE_SEQ_WIDTH}d}'


def _audit(*, actor, action, entity_type, entity_id, diff, request):
    AuditLog.objects.create(
        actor_type=ActorType.STAFF_USER,
        actor_id=actor.id,
        actor_email=actor.email,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        diff=diff,
        ip_address=getattr(request, 'audit_ip', None),
        user_agent=getattr(request, 'audit_user_agent', ''),
    )
