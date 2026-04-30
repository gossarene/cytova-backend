"""
Cytova — Financial Statement PDF rendering for the Financial Reports app.

The actual rendering is delegated to ``apps.invoicing.pdf_service`` so the
Financial Statement PDF in this module is visually IDENTICAL to the
existing FINANCIAL STATEMENT mode of the invoicing renderer — a single
template, no divergent layouts. We provide only:

  - a thin in-memory shim ("statement context") that satisfies the
    attribute contract the invoicing renderer expects (no Invoice row
    is ever created), and
  - a small filter-label helper for the document's "Partner" field.

Strict guarantees:
  - **Never** writes to ``Invoice`` / ``InvoiceLine`` / any invoicing
    table. The shim is a Python dataclass that exists for the duration
    of the request only.
  - **Never** assigns an invoice number. ``invoice_number`` is unused
    in DOC_STATEMENT mode but we still leave it empty for safety.
  - **Never** locks a period. There is no period_lock model touch.

TODO: Excel export (``.xlsx``) — same dataset, different writer.
TODO: introduce FinancialReportSnapshot for immutable saved reports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Optional

from django.utils import timezone

from apps.invoicing.pdf_service import (
    DOC_STATEMENT,
    draw_document_body,
)
from apps.lab_settings.models import LabSettings


# ---------------------------------------------------------------------------
# Statement context — duck-typed Invoice + InvoiceLine
# ---------------------------------------------------------------------------

@dataclass
class _StatementPartner:
    """Shim for the renderer's ``invoice.partner.name`` access."""
    name: str


@dataclass
class _StatementInvoice:
    """Duck-typed Invoice. Field names match what the invoicing renderer
    reads in DOC_STATEMENT mode — see ``_draw_invoice_block`` and
    ``_draw_totals`` in ``apps.invoicing.pdf_service``."""
    generated_at: datetime
    partner: _StatementPartner
    period_start: date
    period_end: date

    gross_total: Decimal
    discount_rate_snapshot: Decimal
    discount_amount: Decimal
    subtotal_after_discount: Decimal
    net_total: Decimal

    # Statement mode never shows VAT, but the renderer reads these
    # attributes; zero them out and they're suppressed.
    vat_rate_snapshot: Decimal = field(default_factory=lambda: Decimal('0'))
    vat_amount: Decimal = field(default_factory=lambda: Decimal('0'))

    # Unused in DOC_STATEMENT mode, but referenced if the renderer is
    # ever called with ``is_invoice=True`` (it isn't here). Empty string
    # is intentional — there is no invoice number for a financial report.
    invoice_number: str = ''


@dataclass
class _StatementLine:
    """Duck-typed InvoiceLine. One per request × exam grouping in the
    drill-down dataset; the renderer groups consecutive identical
    date/patient cells visually so we don't suppress them here."""
    performed_date: Optional[date]
    patient_display_name: str
    exam_code_snapshot: str
    exam_name_snapshot: str
    line_amount: Decimal


# ---------------------------------------------------------------------------
# Filter → header label
# ---------------------------------------------------------------------------

def _resolve_partner_label(filters_applied: dict[str, Any]) -> str:
    """Translate the applied filters into the single-line label the
    invoicing renderer prints in the "Partner" slot of the metadata block.
    Matches the spec's required vocabulary."""
    source = filters_applied.get('source_type', 'ALL')
    partner_ids = filters_applied.get('partner_ids') or []

    if source == 'ALL':
        return 'All sources'
    if source == 'DIRECT_PATIENT':
        return 'Direct patients'
    if source == 'PARTNER':
        if not partner_ids:
            return 'All partners'
        if len(partner_ids) == 1:
            # Single partner — fetch the name from the active tenant
            # schema so the document shows the actual organization
            # rather than "Selected partners (1)". Lazy import avoids
            # circular module loads at app start.
            from apps.partners.models import PartnerOrganization
            p = PartnerOrganization.objects.filter(
                pk=partner_ids[0],
            ).only('name').first()
            return p.name if p else 'Selected partner'
        return f'Selected partners ({len(partner_ids)})'
    return source  # fallback — never hit in practice


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def render_financial_statement_pdf(report: dict[str, Any]) -> bytes:
    """Render the Financial Statement PDF for a preview ``report`` payload.

    Reuses the invoicing module's ``draw_document_body`` in DOC_STATEMENT
    mode by constructing in-memory shim objects — no ``Invoice`` row is
    created, no invoice number is allocated, no period is locked.
    """
    from reportlab.pdfgen import canvas
    import io
    from reportlab.lib.pagesizes import A4

    settings = LabSettings.get_solo()

    # Build the synthetic invoice + lines from the preview payload.
    invoice_shim = _build_invoice_shim(report)
    lines = list(_iter_statement_lines(report))

    # Two-pass for accurate page numbering — same shape as the invoicing
    # renderer's own driver. We re-implement the loop here rather than
    # call into the invoicing helper because the invoicing helper
    # signature is keyed on a real Invoice instance.
    ctx = {'current_page': 1, 'total_pages': 0}
    buf_dry = io.BytesIO()
    c_dry = canvas.Canvas(buf_dry, pagesize=A4)
    draw_document_body(c_dry, invoice_shim, settings, lines, ctx, is_invoice=False)
    c_dry.save()

    ctx2 = {'current_page': 1, 'total_pages': ctx['current_page']}
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    draw_document_body(c, invoice_shim, settings, lines, ctx2, is_invoice=False)
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_invoice_shim(report: dict[str, Any]) -> _StatementInvoice:
    summary = report['summary']
    fa = report['filters_applied']

    gross    = Decimal(summary['gross_total'])
    discount = Decimal(summary['discount_total'])
    net      = Decimal(summary['net_total'])

    # Effective rate for the totals block. When mixed sources produce a
    # discount > 0 we expose the *implied* aggregate rate so the existing
    # renderer's "Discount (X%)" line stays meaningful — and so callers
    # don't accidentally see "Discount (0%)" on a non-zero figure.
    if gross > 0 and discount > 0:
        rate = (discount / gross * Decimal('100')).quantize(Decimal('0.01'))
    else:
        rate = Decimal('0')

    return _StatementInvoice(
        generated_at=timezone.now(),
        partner=_StatementPartner(name=_resolve_partner_label(fa)),
        period_start=date.fromisoformat(fa['period_start']),
        period_end=date.fromisoformat(fa['period_end']),
        gross_total=gross,
        discount_rate_snapshot=rate,
        discount_amount=discount,
        subtotal_after_discount=gross - discount,
        net_total=net,
    )


def _iter_statement_lines(report: dict[str, Any]) -> Iterable[_StatementLine]:
    """Expand each request row into one statement line per (exam) group.
    Grouping is identical to the drill-down ``exams`` array, so the PDF
    table mirrors what the user sees expanded in the UI."""
    for row in report['rows']:
        performed = (
            date.fromisoformat(row['date']) if row.get('date') else None
        )
        patient_name = row.get('patient_name') or ''
        for exam in row.get('exams') or []:
            yield _StatementLine(
                performed_date=performed,
                patient_display_name=patient_name,
                exam_code_snapshot=exam.get('code') or '',
                exam_name_snapshot=exam.get('name') or '',
                line_amount=Decimal(exam.get('gross_amount') or '0'),
            )
