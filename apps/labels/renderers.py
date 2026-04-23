"""
Cytova — Label Rendering Engine

Pure-layout renderers that produce a PDF document from a list of
``LabelPayload`` records and a ``LabelLayoutConfig``. The renderers
know nothing about tenants, analysis requests, or storage — that
integration lives in ``apps.requests.label_service``. This separation
lets each renderer be unit-tested in isolation with a dict config.

Two modes:

- ``A4SheetRenderer``  — grid layout on configurable page size.
  Auto-paginates when more labels are needed than fit on a page.
- ``ThermalRollRenderer`` — one label per page, page size equals
  label dimensions plus a configurable inter-label gap.

Both produce Code128 barcodes (universal scanner support) and draw
the fields: numeric code, patient name, DOB, collection date, request
number, family name (when pinned), and label position.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Iterable

from reportlab.graphics.barcode.code128 import Code128
from reportlab.lib.colors import HexColor, black
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas


@dataclass(frozen=True)
class LabelPayload:
    """Minimal data a renderer needs for one label. Framework-agnostic."""
    numeric_code: str
    patient_name: str
    patient_dob: str           # ISO date string
    collection_date: str       # ISO date string
    request_number: str
    family_name: str           # '' if extra / unpinned
    label_index: int
    label_total: int


@dataclass(frozen=True)
class LabelLayoutConfig:
    """Effective layout config, usually copied from LabSettings."""
    print_mode: str            # 'A4_SHEET' | 'THERMAL_ROLL'
    page_width_mm: int
    page_height_mm: int
    label_width_mm: int
    label_height_mm: int
    margin_top_mm: int
    margin_left_mm: int
    horizontal_gap_mm: int
    vertical_gap_mm: int
    thermal_gap_mm: int
    show_barcode: bool
    show_numeric_code: bool


# ---------------------------------------------------------------------------
# Shared drawing primitive
# ---------------------------------------------------------------------------

def _draw_label_content(
    c: canvas.Canvas,
    x: float, y: float,
    width: float, height: float,
    payload: LabelPayload,
    config: LabelLayoutConfig,
) -> None:
    """
    Draw one label's content at the given bottom-left origin. A light
    border is drawn so slots are visible on A4 proofs; on thermal
    output it is typically clipped by the physical label edge anyway.
    """
    c.setStrokeColor(HexColor('#CCCCCC'))
    c.setLineWidth(0.5)
    c.rect(x, y, width, height)

    text_x = x + 2 * mm
    line_y = y + height - 4 * mm

    c.setFillColor(black)
    c.setFont('Helvetica-Bold', 8)
    c.drawString(text_x, line_y, _truncate(payload.patient_name, width))
    line_y -= 3.2 * mm

    c.setFont('Helvetica', 6.5)
    c.drawString(text_x, line_y, f'DOB: {payload.patient_dob}')
    line_y -= 2.8 * mm
    c.drawString(text_x, line_y, f'Coll: {payload.collection_date}')
    line_y -= 2.8 * mm
    c.drawString(text_x, line_y, f'Req: {payload.request_number}')
    line_y -= 2.8 * mm

    if payload.family_name:
        c.drawString(text_x, line_y, _truncate(payload.family_name, width))
        line_y -= 2.8 * mm

    c.drawString(text_x, line_y, f'{payload.label_index}/{payload.label_total}')

    # Numeric code + barcode at the bottom of the slot.
    baseline_y = y + 1.5 * mm

    if config.show_barcode:
        barcode = Code128(payload.numeric_code, barHeight=7 * mm, barWidth=0.33)
        bc_x = x + max(0, (width - barcode.width) / 2)
        barcode.drawOn(c, bc_x, baseline_y + 3 * mm)

    if config.show_numeric_code:
        c.setFont('Helvetica', 6)
        c.drawCentredString(x + width / 2, baseline_y, payload.numeric_code)


def _truncate(text: str, width_mm_value: float) -> str:
    """Rough truncation to avoid overflowing narrow labels."""
    max_chars = max(8, int((width_mm_value / mm) / 1.8))
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + '…'


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

class A4SheetRenderer:
    """Grid layout for A4 (or any paper size) adhesive label sheets."""

    @staticmethod
    def render(payloads: Iterable[LabelPayload], config: LabelLayoutConfig) -> bytes:
        buffer = io.BytesIO()
        page_w = config.page_width_mm * mm
        page_h = config.page_height_mm * mm
        c = canvas.Canvas(buffer, pagesize=(page_w, page_h))

        margin_left = config.margin_left_mm * mm
        margin_top = config.margin_top_mm * mm
        h_gap = config.horizontal_gap_mm * mm
        v_gap = config.vertical_gap_mm * mm
        label_w = config.label_width_mm * mm
        label_h = config.label_height_mm * mm

        usable_w = page_w - margin_left
        usable_h = page_h - margin_top

        cols = max(1, int((usable_w + h_gap) // (label_w + h_gap)))
        rows = max(1, int((usable_h + v_gap) // (label_h + v_gap)))
        per_page = cols * rows

        for i, payload in enumerate(list(payloads)):
            slot = i % per_page
            if i > 0 and slot == 0:
                c.showPage()
            col = slot % cols
            row = slot // cols

            x = margin_left + col * (label_w + h_gap)
            y = page_h - margin_top - (row + 1) * label_h - row * v_gap
            _draw_label_content(c, x, y, label_w, label_h, payload, config)

        c.save()
        return buffer.getvalue()


class ThermalRollRenderer:
    """
    One label per page, page size = (label_width, label_height + thermal_gap).

    The extra gap is included in the page height so thermal printers
    advance the roll by exactly one slot per "page" — matching how
    most thermal drivers interpret multi-page PDFs.
    """

    @staticmethod
    def render(payloads: Iterable[LabelPayload], config: LabelLayoutConfig) -> bytes:
        buffer = io.BytesIO()
        label_w = config.label_width_mm * mm
        label_h = config.label_height_mm * mm
        gap = config.thermal_gap_mm * mm
        page_w = label_w
        page_h = label_h + gap
        c = canvas.Canvas(buffer, pagesize=(page_w, page_h))

        payloads = list(payloads)
        for i, payload in enumerate(payloads):
            if i > 0:
                c.showPage()
                c.setPageSize((page_w, page_h))
            # Draw at the top of the page — the gap sits at the bottom
            # so the roll advances past the label to the next slot.
            _draw_label_content(c, 0, gap, label_w, label_h, payload, config)

        c.save()
        return buffer.getvalue()


RENDERERS = {
    'A4_SHEET': A4SheetRenderer,
    'THERMAL_ROLL': ThermalRollRenderer,
}


def render_labels(payloads: Iterable[LabelPayload], config: LabelLayoutConfig) -> bytes:
    """Dispatch to the right renderer by print_mode."""
    try:
        renderer = RENDERERS[config.print_mode]
    except KeyError as exc:
        raise ValueError(f'Unsupported print_mode: {config.print_mode!r}') from exc
    return renderer.render(payloads, config)
