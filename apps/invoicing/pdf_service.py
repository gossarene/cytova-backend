"""
Cytova — Invoice PDF Rendering Service

Produces a professional invoice PDF from snapshot data stored on
``Invoice`` and ``InvoiceLine``. No mutable request/pricing data is
read — the PDF is a self-contained document derived entirely from the
frozen invoice record.

Layout:
    1. Lab header (logo + identity from LabSettings)
    2. Invoice metadata block (number, date, status, partner, period)
    3. Lines table grouped by Date > Patient > Exam with amounts
    4. Totals block (gross, discount, subtotal, VAT, net)
    5. Footer (legal text + page numbering)

Grouped rendering:
    Lines are sorted by (performed_date, patient_display_name, exam).
    Consecutive rows sharing the same date suppress the date cell;
    consecutive rows sharing the same date+patient suppress both.
    This produces a visually merged effect without actual cell spans.
"""
import io
import logging
from itertools import groupby

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone

from reportlab.lib.colors import HexColor, black, grey
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from apps.lab_settings.models import LabSettings
from .models import Invoice, InvoiceStatus

logger = logging.getLogger(__name__)

PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN_LEFT = 20 * mm
MARGIN_RIGHT = 20 * mm
MARGIN_TOP = 20 * mm
MARGIN_BOTTOM = 20 * mm
CONTENT_WIDTH = PAGE_WIDTH - MARGIN_LEFT - MARGIN_RIGHT
FOOTER_ZONE = 18 * mm
LABEL_COLOR = HexColor('#475569')

# Table column positions (left edge of each column)
COL_DATE_X = MARGIN_LEFT
COL_DATE_W = 22 * mm
COL_PATIENT_X = COL_DATE_X + COL_DATE_W
COL_PATIENT_W = 55 * mm
COL_EXAM_X = COL_PATIENT_X + COL_PATIENT_W
COL_EXAM_W = 55 * mm
COL_AMOUNT_X = PAGE_WIDTH - MARGIN_RIGHT
ROW_H = 4 * mm


DOC_INVOICE = 'invoice'
DOC_STATEMENT = 'statement'


class InvoicePdfService:

    @staticmethod
    def generate_or_get(
        invoice: Invoice, doc_type: str = DOC_INVOICE,
    ) -> Invoice:
        """Generate (or return existing) PDF for the given document type."""
        key_field = 'pdf_file_key' if doc_type == DOC_INVOICE else 'statement_file_key'
        if getattr(invoice, key_field):
            return invoice
        return InvoicePdfService._render_and_store(invoice, doc_type)

    @staticmethod
    def regenerate(
        invoice: Invoice, doc_type: str = DOC_INVOICE,
    ) -> Invoice:
        """Force-regenerate the PDF for the given document type."""
        return InvoicePdfService._render_and_store(invoice, doc_type)

    @staticmethod
    def _render_and_store(invoice: Invoice, doc_type: str) -> Invoice:
        key_field = 'pdf_file_key' if doc_type == DOC_INVOICE else 'statement_file_key'
        suffix = 'invoice' if doc_type == DOC_INVOICE else 'statement'

        pdf_bytes = _render_invoice_pdf(invoice, doc_type=doc_type)
        file_key = f'invoices/{invoice.id}/{invoice.invoice_number}_{suffix}.pdf'

        old_key = getattr(invoice, key_field)
        if old_key and old_key != file_key:
            try:
                default_storage.delete(old_key)
            except Exception:  # noqa: BLE001
                pass

        default_storage.save(file_key, ContentFile(pdf_bytes))
        setattr(invoice, key_field, file_key)
        invoice.save(update_fields=[key_field, 'updated_at'])

        logger.info('%s PDF generated: %s', suffix.title(), invoice.invoice_number)
        return invoice


# ---------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------

def _render_invoice_pdf(invoice: Invoice, doc_type: str = DOC_INVOICE) -> bytes:
    settings = LabSettings.get_solo()
    lines = list(
        invoice.lines
        .order_by('performed_date', 'patient_display_name', 'exam_name_snapshot')
    )
    is_invoice = doc_type == DOC_INVOICE

    # Two-pass for page numbering
    ctx = {'current_page': 1, 'total_pages': 0}
    buf_dry = io.BytesIO()
    c_dry = canvas.Canvas(buf_dry, pagesize=A4)
    _draw_invoice(c_dry, invoice, settings, lines, ctx, is_invoice)
    c_dry.save()

    ctx2 = {'current_page': 1, 'total_pages': ctx['current_page']}
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _draw_invoice(c, invoice, settings, lines, ctx2, is_invoice)
    c.save()
    return buf.getvalue()


def _draw_invoice(c, invoice, settings, lines, ctx, is_invoice=True):
    bottom = MARGIN_BOTTOM + FOOTER_ZONE

    def on_page_break():
        _draw_footer(c, settings, ctx)
        ctx['current_page'] += 1

    y = PAGE_HEIGHT - MARGIN_TOP

    # Header
    y = _draw_header(c, y, settings)
    y -= 6 * mm

    # Document metadata + partner
    y = _draw_invoice_block(c, y, invoice, is_invoice)
    y -= 6 * mm

    # Lines table
    y = _draw_lines_table(c, y, lines, bottom, on_page_break, ctx, settings)
    y -= 6 * mm

    # Totals
    y = _draw_totals(c, y, invoice, bottom, on_page_break, ctx, settings)

    # Footer on last page
    _draw_footer(c, settings, ctx)


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

def _draw_header(c, y, settings):
    if settings.show_logo and settings.logo_file_key:
        try:
            from reportlab.lib.utils import ImageReader
            with default_storage.open(settings.logo_file_key, 'rb') as f:
                img = ImageReader(f)
            max_w = settings.logo_max_width_mm * mm
            max_h = settings.logo_max_height_mm * mm
            iw, ih = img.getSize()
            if iw > 0 and ih > 0:
                scale = min(max_w / iw, max_h / ih, 1.0)
                w, h = iw * scale, ih * scale
                pos = settings.logo_position
                if pos == 'LEFT':
                    lx = MARGIN_LEFT
                elif pos == 'CENTER':
                    lx = MARGIN_LEFT + (CONTENT_WIDTH - w) / 2
                else:
                    lx = PAGE_WIDTH - MARGIN_RIGHT - w
                c.drawImage(img, lx, y - h, width=w, height=h,
                            preserveAspectRatio=True, mask='auto')
        except Exception:  # noqa: BLE001
            pass

    if settings.lab_name:
        c.setFont('Helvetica-Bold', 14)
        c.drawString(MARGIN_LEFT, y, settings.lab_name)
        y -= 16
    if settings.lab_subtitle:
        c.setFont('Helvetica', 9)
        c.setFillColor(grey)
        c.drawString(MARGIN_LEFT, y, settings.lab_subtitle)
        y -= 11
    if settings.show_lab_address and settings.address:
        c.setFont('Helvetica', 8)
        c.setFillColor(grey)
        for line in settings.address.splitlines()[:3]:
            c.drawString(MARGIN_LEFT, y, line)
            y -= 10
    c.setFillColor(black)
    y -= 2 * mm
    c.setStrokeColor(HexColor('#c4c9d0'))
    c.setLineWidth(0.5)
    c.line(MARGIN_LEFT, y, PAGE_WIDTH - MARGIN_RIGHT, y)
    y -= 1 * mm
    return y


# ---------------------------------------------------------------------------
# Invoice metadata
# ---------------------------------------------------------------------------

def _draw_invoice_block(c, y, invoice, is_invoice=True):
    title = 'INVOICE' if is_invoice else 'FINANCIAL STATEMENT'
    c.setFont('Helvetica-Bold', 16)
    c.drawString(MARGIN_LEFT, y, title)
    y -= 20

    fields_left = []
    if is_invoice:
        fields_left.append(('Invoice #', invoice.invoice_number))
    fields_left.append(('Date', invoice.generated_at.strftime('%Y-%m-%d')))

    fields_right = [
        ('Partner', invoice.partner.name),
        ('Period', f'{invoice.period_start} — {invoice.period_end}'),
    ]

    mid_x = MARGIN_LEFT + CONTENT_WIDTH / 2

    row_h = 13
    for i, (label, value) in enumerate(fields_left):
        ry = y - i * row_h
        c.setFont('Helvetica-Bold', 9)
        c.setFillColor(LABEL_COLOR)
        c.drawString(MARGIN_LEFT, ry, label)
        c.setFont('Helvetica', 9)
        c.setFillColor(black)
        c.drawString(MARGIN_LEFT + 25 * mm, ry, str(value))

    for i, (label, value) in enumerate(fields_right):
        ry = y - i * row_h
        c.setFont('Helvetica-Bold', 9)
        c.setFillColor(LABEL_COLOR)
        c.drawString(mid_x, ry, label)
        c.setFont('Helvetica', 9)
        c.setFillColor(black)
        c.drawString(mid_x + 28 * mm, ry, str(value))

    rows = max(len(fields_left), len(fields_right))
    y -= rows * row_h + 2 * mm

    c.setStrokeColor(HexColor('#c4c9d0'))
    c.setLineWidth(0.5)
    c.line(MARGIN_LEFT, y, PAGE_WIDTH - MARGIN_RIGHT, y)
    y -= 1 * mm
    return y


# ---------------------------------------------------------------------------
# Lines table with grouped Date > Patient rendering
# ---------------------------------------------------------------------------

def _check_page(c, y, needed, bottom, on_page_break, ctx, settings):
    if y - needed < bottom:
        on_page_break()
        c.showPage()
        y = PAGE_HEIGHT - MARGIN_TOP
    return y


_GRID_COLOR = HexColor('#c4c9d0')
_GRID_WIDTH = 0.4
_TEXT_PAD = 1.5 * mm  # horizontal padding inside cells
_HEADER_BG = HexColor('#f1f5f9')


def _grid_hline(c, y):
    c.setStrokeColor(_GRID_COLOR)
    c.setLineWidth(_GRID_WIDTH)
    c.line(MARGIN_LEFT, y, PAGE_WIDTH - MARGIN_RIGHT, y)


def _grid_vlines(c, y_top, y_bottom):
    c.setStrokeColor(_GRID_COLOR)
    c.setLineWidth(_GRID_WIDTH)
    for x in (MARGIN_LEFT, COL_PATIENT_X, COL_EXAM_X,
              COL_EXAM_X + COL_EXAM_W, PAGE_WIDTH - MARGIN_RIGHT):
        c.line(x, y_top, x, y_bottom)


def _draw_lines_table(c, y, lines, bottom, on_page_break, ctx, settings):
    right_x = PAGE_WIDTH - MARGIN_RIGHT

    # -- Pre-compute group boundaries for merged-cell border logic --
    n = len(lines)
    group_info = []
    for i, line in enumerate(lines):
        cur_date = line.performed_date.isoformat() if line.performed_date else '—'
        cur_patient = line.patient_display_name
        next_date = (
            (lines[i + 1].performed_date.isoformat()
             if lines[i + 1].performed_date else '—')
            if i + 1 < n else None
        )
        next_patient = lines[i + 1].patient_display_name if i + 1 < n else None
        group_info.append({
            'date': cur_date,
            'patient': cur_patient,
            'is_last_in_date': next_date != cur_date,
            'is_last_in_patient': (
                next_date != cur_date or next_patient != cur_patient
            ),
        })

    # -- Header row with background fill --
    header_h = ROW_H + 1.5 * mm
    y = _check_page(c, y, header_h + 2, bottom, on_page_break, ctx, settings)

    header_top = y + 2.5 * mm
    header_bot = header_top - header_h
    c.setFillColor(_HEADER_BG)
    c.rect(MARGIN_LEFT, header_bot, CONTENT_WIDTH, header_h, fill=1, stroke=0)

    c.setFont('Helvetica-Bold', 7.5)
    c.setFillColor(HexColor('#334155'))
    text_y = header_bot + 2 * mm
    c.drawString(COL_DATE_X + _TEXT_PAD, text_y, 'Date')
    c.drawString(COL_PATIENT_X + _TEXT_PAD, text_y, 'Patient')
    c.drawString(COL_EXAM_X + _TEXT_PAD, text_y, 'Exam')
    c.drawRightString(right_x - _TEXT_PAD, text_y, 'Amount')
    c.setFillColor(black)

    _grid_hline(c, header_top)
    _grid_hline(c, header_bot)
    _grid_vlines(c, header_top, header_bot)

    y = header_bot

    # -- Data rows --
    prev_date = None
    prev_patient = None

    for i, line in enumerate(lines):
        y = _check_page(c, y, ROW_H + 1, bottom, on_page_break, ctx, settings)

        row_bot = y - ROW_H
        text_y = row_bot + 1.2 * mm
        gi = group_info[i]

        # Date text — only on first row of date group
        if gi['date'] != prev_date:
            c.setFont('Helvetica-Bold', 7.5)
            c.setFillColor(black)
            c.drawString(COL_DATE_X + _TEXT_PAD, text_y, gi['date'])
            prev_date = gi['date']
            prev_patient = None

        # Patient text — only on first row of patient group
        if gi['patient'] != prev_patient:
            c.setFont('Helvetica', 7.5)
            c.setFillColor(black)
            c.drawString(COL_PATIENT_X + _TEXT_PAD, text_y,
                         _truncate(gi['patient'], 35))
            prev_patient = gi['patient']

        # Exam + Amount — every row
        c.setFont('Helvetica', 7.5)
        c.setFillColor(black)
        exam_text = f'{line.exam_code_snapshot}  {line.exam_name_snapshot}'
        c.drawString(COL_EXAM_X + _TEXT_PAD, text_y, _truncate(exam_text, 38))
        c.drawRightString(right_x - _TEXT_PAD, text_y, f'{line.line_amount:,.2f}')

        # -- Borders --
        c.setStrokeColor(_GRID_COLOR)
        c.setLineWidth(_GRID_WIDTH)

        # Exam + Amount columns: always draw bottom hline
        c.line(COL_EXAM_X, row_bot, right_x, row_bot)

        # Patient column: bottom hline only at patient-group boundary
        if gi['is_last_in_patient']:
            c.line(COL_PATIENT_X, row_bot, COL_EXAM_X, row_bot)

        # Date column: bottom hline only at date-group boundary
        if gi['is_last_in_date']:
            c.line(MARGIN_LEFT, row_bot, COL_PATIENT_X, row_bot)

        # All vertical separators for this row
        _grid_vlines(c, y, row_bot)

        y = row_bot

    return y


# ---------------------------------------------------------------------------
# Totals block
# ---------------------------------------------------------------------------

def _draw_totals(c, y, invoice, bottom, on_page_break, ctx, settings):
    """Right-aligned totals table with cell borders."""
    right_x = PAGE_WIDTH - MARGIN_RIGHT
    label_x = right_x - 60 * mm
    split_x = right_x - 25 * mm
    row_h = 4.5 * mm

    rows: list[tuple[str, str, bool]] = []
    rows.append(('Gross total', f'{invoice.gross_total:,.2f}', True))
    if invoice.discount_rate_snapshot > 0:
        rows.append((
            f'Discount ({invoice.discount_rate_snapshot}%)',
            f'- {invoice.discount_amount:,.2f}', False,
        ))
    rows.append(('Subtotal', f'{invoice.subtotal_after_discount:,.2f}', False))
    if invoice.vat_rate_snapshot > 0:
        rows.append((
            f'VAT ({invoice.vat_rate_snapshot}%)',
            f'+ {invoice.vat_amount:,.2f}', False,
        ))
    rows.append(('Net total', f'{invoice.net_total:,.2f}', True))

    total_h = len(rows) * row_h + 2 * mm
    y = _check_page(c, y, total_h + 4 * mm, bottom, on_page_break, ctx, settings)
    y -= 4 * mm

    table_top = y
    for label, value, bold in rows:
        row_bot = y - row_h
        text_y = row_bot + 1.2 * mm

        font = 'Helvetica-Bold' if bold else 'Helvetica'
        c.setFont(font, 8.5)
        c.setFillColor(black)
        c.drawString(label_x + _TEXT_PAD, text_y, label)
        c.drawRightString(right_x - _TEXT_PAD, text_y, value)

        _grid_color = _GRID_COLOR
        c.setStrokeColor(_grid_color)
        c.setLineWidth(_GRID_WIDTH)
        c.line(label_x, row_bot, right_x, row_bot)

        y = row_bot

    # Outer border + vertical split
    c.setStrokeColor(_GRID_COLOR)
    c.setLineWidth(_GRID_WIDTH)
    c.line(label_x, table_top, right_x, table_top)
    c.line(label_x, table_top, label_x, y)
    c.line(right_x, table_top, right_x, y)
    c.line(split_x, table_top, split_x, y)

    # Bold bottom border on net total
    c.setStrokeColor(HexColor('#0f172a'))
    c.setLineWidth(1.2)
    c.line(label_x, y, right_x, y)

    return y - 2 * mm


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

def _draw_footer(c, settings, ctx):
    y = MARGIN_BOTTOM + FOOTER_ZONE - 3 * mm
    c.setStrokeColor(HexColor('#c4c9d0'))
    c.setLineWidth(0.5)
    c.line(MARGIN_LEFT, y, PAGE_WIDTH - MARGIN_RIGHT, y)

    if ctx['total_pages'] > 0:
        c.setFont('Helvetica', 8)
        c.setFillColor(grey)
        c.drawCentredString(
            PAGE_WIDTH / 2, y - 4 * mm,
            f'Page {ctx["current_page"]} / {ctx["total_pages"]}',
        )

    if settings.show_legal_footer and settings.legal_footer:
        c.setFont('Helvetica-Oblique', 7)
        c.setFillColor(grey)
        legal_y = MARGIN_BOTTOM
        for i, line in enumerate(settings.legal_footer.splitlines()[:3]):
            c.drawCentredString(PAGE_WIDTH / 2, legal_y + (2 - i) * 3 * mm, line)

    c.setFillColor(black)


def _truncate(s, max_chars):
    if not s:
        return ''
    return s if len(s) <= max_chars else s[:max_chars - 1] + '…'
