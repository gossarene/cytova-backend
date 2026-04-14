"""
Cytova — Request Report Service

Generates the final patient result PDF for a validated analysis request.

Architecture:
- Only VALIDATED requests can be reported — stale or in-progress results
  never appear on a final document.
- Groups results by exam family (deterministic order: family.display_order,
  then family.name).
- Per exam, renders:
    * SINGLE_VALUE: one line with value + unit_snapshot + reference_range_snapshot
    * MULTI_PARAMETER: one row per ResultValue (name, value, unit, ref range)
- Uses snapshotted metadata on ResultValue rows so future catalog changes
  do not alter historical reports.
- Reads ``LabSettings`` for lab identity and display toggles.

Lifecycle: generate-or-get (idempotent). If a report exists, it is returned
verbatim. Regeneration is deliberately a separate action (not in scope here)
to keep the audit trail simple and reports immutable once produced.

Security: the PDF file key is never exposed. Download goes through the
protected view ``requests/<id>/report/download/`` which streams via
``FileResponse``.
"""
import io
import logging

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from reportlab.lib.colors import HexColor, black, grey
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from apps.audit.models import ActorType, AuditAction, AuditLog
from apps.catalog.models import ResultStructure
from apps.lab_settings.models import LabSettings
from apps.requests.models import (
    AnalysisRequest, AnalysisRequestReport, RequestStatus,
)
from apps.results.models import ResultStatus, ResultVersion

logger = logging.getLogger(__name__)

PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN_LEFT = 20 * mm
MARGIN_RIGHT = 20 * mm
MARGIN_TOP = 20 * mm
MARGIN_BOTTOM = 20 * mm
CONTENT_WIDTH = PAGE_WIDTH - MARGIN_LEFT - MARGIN_RIGHT


# ---------------------------------------------------------------------------
# Public service
# ---------------------------------------------------------------------------

class RequestReportService:

    @staticmethod
    @transaction.atomic
    def generate_or_get(
        analysis_request: AnalysisRequest,
        generated_by,
        request,
    ) -> AnalysisRequestReport:
        """
        Generate the final report PDF for a validated request, or return the
        existing one if already generated. Only VALIDATED requests can be
        reported.
        """
        if analysis_request.status != RequestStatus.VALIDATED:
            raise ValidationError(
                'Final report can only be generated for validated requests '
                f'(current status: {analysis_request.status}).'
            )

        existing = getattr(analysis_request, 'report', None)
        if existing is not None and existing.pdf_file_key:
            return existing

        # Build the structured context
        settings = LabSettings.get_solo()
        sections = _collect_sections(analysis_request)

        # Render PDF
        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=A4)
        _render_report(c, analysis_request, settings, sections)
        c.save()
        pdf_bytes = buffer.getvalue()

        # Create or reuse the record, replace file_key
        if existing is None:
            report = AnalysisRequestReport.objects.create(
                analysis_request=analysis_request,
                generated_by=generated_by,
                generated_at=timezone.now(),
                pdf_file_key='',
            )
        else:
            report = existing

        file_key = f'request-reports/{analysis_request.id}/{report.id}.pdf'
        default_storage.save(file_key, ContentFile(pdf_bytes))
        report.pdf_file_key = file_key
        report.generated_by = generated_by
        report.generated_at = timezone.now()
        report.save(update_fields=['pdf_file_key', 'generated_by', 'generated_at', 'updated_at'])

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=getattr(generated_by, 'id', None),
            actor_email=getattr(generated_by, 'email', ''),
            action=AuditAction.CREATE,
            entity_type='AnalysisRequestReport',
            entity_id=report.id,
            diff={'after': {
                'analysis_request_id': str(analysis_request.id),
                'request_number': analysis_request.request_number,
            }},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return report


# ---------------------------------------------------------------------------
# Data collection — grouped by family
# ---------------------------------------------------------------------------

def _collect_sections(analysis_request: AnalysisRequest) -> list[dict]:
    """
    Build the deterministic section list grouped by exam family.

    Each section: {
        'family_name': str,
        'family_order': int,
        'exams': [{
            'code': str, 'name': str, 'technique': str,
            'structure': 'SINGLE_VALUE' | 'MULTI_PARAMETER',
            'version': ResultVersion,
            'values': [ResultValue],
        }],
    }

    Only items whose current result version is VALIDATED contribute. Items
    with REJECTED execution mode, or without a validated current version,
    are silently skipped — the report reflects the validated state only.
    """
    # Pull all items with eager-loaded FKs needed downstream
    items = (
        analysis_request.items
        .exclude(execution_mode='REJECTED')
        .select_related('exam_definition__family', 'exam_definition__technique')
        .order_by('exam_definition__family__display_order', 'exam_definition__name')
    )

    families: dict[str, dict] = {}
    for item in items:
        exam_def = item.exam_definition
        family = exam_def.family
        family_name = family.name if family else 'Uncategorized'
        family_order = family.display_order if family else 9999

        current = ResultVersion.objects.filter(
            item=item,
            is_current=True,
            status=ResultStatus.VALIDATED,
        ).first()
        if current is None:
            continue

        values = list(current.values.order_by('display_order'))

        section = families.setdefault(family_name, {
            'family_name': family_name,
            'family_order': family_order,
            'exams': [],
        })
        section['exams'].append({
            'code': exam_def.code,
            'name': exam_def.name,
            'technique': exam_def.technique.name if exam_def.technique else '',
            'structure': exam_def.result_structure,
            'version': current,
            'values': values,
        })

    # Sort family sections deterministically
    return sorted(
        families.values(),
        key=lambda s: (s['family_order'], s['family_name']),
    )


# ---------------------------------------------------------------------------
# PDF rendering (ReportLab canvas)
# ---------------------------------------------------------------------------

def _render_report(c: canvas.Canvas, ar: AnalysisRequest, settings: LabSettings, sections: list[dict]):
    """
    Render the full report on the supplied canvas.

    Layout:
        1. Laboratory header (name, subtitle, address, contact)
        2. Patient / request context
        3. Grouped result sections by family
        4. Final conclusion
        5. Signature area
        6. Legal footer
    """
    cursor = PageCursor(c, top=PAGE_HEIGHT - MARGIN_TOP, bottom=MARGIN_BOTTOM)

    _draw_header(cursor, settings)
    cursor.gap(4 * mm)
    _draw_patient_block(cursor, ar, settings)
    cursor.gap(6 * mm)

    if not sections:
        cursor.text('No validated results to report.', font_size=10, colour=grey)
    for section in sections:
        _draw_section(cursor, section, settings)
        cursor.gap(4 * mm)

    if settings.show_final_conclusion and ar.final_conclusion:
        cursor.gap(4 * mm)
        cursor.text('Final Conclusion', font_size=11, bold=True)
        cursor.gap(1 * mm)
        cursor.paragraph(ar.final_conclusion, font_size=9)

    if settings.show_signature:
        cursor.gap(10 * mm)
        _draw_signature(cursor, ar, settings)

    if settings.show_legal_footer and settings.legal_footer:
        _draw_legal_footer(c, settings.legal_footer)


def _draw_logo(c: canvas.Canvas, cursor: 'PageCursor', settings: LabSettings):
    """
    Render the laboratory logo in the top-right of the header area.

    Precedence:
        1. Uploaded file (``logo_file_key``) — rendered from storage
        2. External URL (``logo_url``) — NOT rendered in PDF (display-only
           in UI; fetching arbitrary URLs during PDF gen is an SSRF risk
           and unreliable). Admins who want a logo in reports must upload.
    """
    if not settings.logo_file_key:
        return
    try:
        from reportlab.lib.utils import ImageReader
        from django.core.files.storage import default_storage

        with default_storage.open(settings.logo_file_key, 'rb') as f:
            img = ImageReader(f)

        # Size: max 25mm tall, proportional width, top-right corner
        max_h = 20 * mm
        iw, ih = img.getSize()
        if ih <= 0:
            return
        scale = min(max_h / ih, 1.0)
        w = iw * scale
        h = ih * scale
        x = PAGE_WIDTH - MARGIN_RIGHT - w
        y = cursor.y - h + 3 * mm
        c.drawImage(img, x, y, width=w, height=h, preserveAspectRatio=True, mask='auto')
    except Exception as e:  # noqa: BLE001 — logo is optional, never block PDF
        logger.warning('Failed to render lab logo: %s', e)


def _draw_header(cursor: 'PageCursor', settings: LabSettings):
    c = cursor.canvas
    if settings.show_logo:
        _draw_logo(c, cursor, settings)
    if settings.lab_name:
        cursor.text(settings.lab_name, font_size=14, bold=True)
    if settings.lab_subtitle:
        cursor.gap(1 * mm)
        cursor.text(settings.lab_subtitle, font_size=9, colour=grey)

    if settings.show_lab_address:
        cursor.gap(2 * mm)
        if settings.address:
            for line in settings.address.splitlines():
                cursor.text(line, font_size=8, colour=grey)
        contact_parts = []
        if settings.phone:
            contact_parts.append(f'Tel: {settings.phone}')
        if settings.email:
            contact_parts.append(settings.email)
        if settings.website:
            contact_parts.append(settings.website)
        if contact_parts:
            cursor.text(' · '.join(contact_parts), font_size=8, colour=grey)

    cursor.gap(2 * mm)
    cursor.hline()


def _draw_patient_block(cursor: 'PageCursor', ar: AnalysisRequest, settings: LabSettings):
    patient = ar.patient
    cursor.text(f'Request #{ar.request_number}', font_size=10, bold=True)
    cursor.gap(1 * mm)

    fields: list[tuple[str, str]] = []
    fields.append(('Patient', f'{patient.first_name} {patient.last_name}'))
    if settings.show_patient_sex:
        fields.append(('Sex', getattr(patient, 'gender', '') or ''))
    if settings.show_patient_age and getattr(patient, 'date_of_birth', None):
        fields.append(('DOB', patient.date_of_birth.isoformat()))
    if settings.show_collection_datetime:
        first_collected = ar.items.exclude(collected_at__isnull=True).order_by('collected_at').first()
        if first_collected and first_collected.collected_at:
            fields.append(('Collected', first_collected.collected_at.strftime('%Y-%m-%d %H:%M')))
    if settings.show_prescriber:
        ref = ar.external_reference or ''
        if ref:
            fields.append(('Ext. Ref', ref))

    for label, value in fields:
        cursor.text(f'{label}: {value}', font_size=9)

    cursor.gap(2 * mm)
    cursor.hline()


def _draw_section(cursor: 'PageCursor', section: dict, settings: LabSettings):
    cursor.gap(2 * mm)
    cursor.text(section['family_name'].upper(), font_size=11, bold=True, colour=HexColor('#0f172a'))
    cursor.gap(1 * mm)

    for exam in section['exams']:
        _draw_exam(cursor, exam, settings)
        cursor.gap(2 * mm)


def _draw_exam(cursor: 'PageCursor', exam: dict, settings: LabSettings):
    cursor.text(f'{exam["code"]}  ·  {exam["name"]}', font_size=10, bold=True)

    if settings.show_exam_technique and exam['technique']:
        cursor.text(f'Technique: {exam["technique"]}', font_size=8, italic=True, colour=grey)

    version = exam['version']
    values = exam['values']

    if exam['structure'] == ResultStructure.MULTI_PARAMETER:
        _draw_param_table(cursor, values, settings)
    else:
        single = values[0] if values else None
        value_str = single.value if single else version.result_value
        unit_str = (single.unit_snapshot if single else version.result_unit) or ''
        ref_str = (single.reference_range_snapshot if single else version.reference_range) or ''
        is_abn = (single.is_abnormal if single else version.is_abnormal)

        line = f'  {value_str} {unit_str}'.rstrip()
        if settings.show_reference_ranges and ref_str:
            line += f'   (Ref: {ref_str})'
        if settings.show_abnormal_flags and is_abn:
            line += '   ⚠ ABNORMAL'
        cursor.text(line, font_size=10, colour=(HexColor('#b91c1c') if (settings.show_abnormal_flags and is_abn) else black))

    if settings.show_patient_comments and version.comments:
        cursor.gap(1 * mm)
        cursor.paragraph(f'Comment: {version.comments}', font_size=8, italic=True, colour=grey)


def _draw_param_table(cursor: 'PageCursor', values, settings: LabSettings):
    # Columns: Parameter (45%), Value (20%), Unit (15%), Ref. Range (20%)
    col_param = CONTENT_WIDTH * 0.45
    col_value = CONTENT_WIDTH * 0.20
    col_unit = CONTENT_WIDTH * 0.15
    col_ref = CONTENT_WIDTH * 0.20

    cursor.gap(1 * mm)
    # Header row
    c = cursor.canvas
    y = cursor.y
    c.setFont('Helvetica-Bold', 8)
    c.setFillColor(grey)
    x = MARGIN_LEFT
    c.drawString(x, y, 'Parameter')
    c.drawString(x + col_param, y, 'Value')
    c.drawString(x + col_param + col_value, y, 'Unit')
    if settings.show_reference_ranges:
        c.drawString(x + col_param + col_value + col_unit, y, 'Ref. Range')
    c.setFillColor(black)
    cursor.advance(4 * mm)

    for v in values:
        is_abn = settings.show_abnormal_flags and v.is_abnormal
        colour = HexColor('#b91c1c') if is_abn else black
        y = cursor.y
        c.setFont('Helvetica', 9)
        c.setFillColor(colour)
        c.drawString(MARGIN_LEFT, y, _truncate(v.name_snapshot, 50))
        c.setFont('Helvetica-Bold', 9)
        c.drawString(MARGIN_LEFT + col_param, y, _truncate(v.value, 18))
        c.setFont('Helvetica', 9)
        c.drawString(MARGIN_LEFT + col_param + col_value, y, _truncate(v.unit_snapshot, 14))
        if settings.show_reference_ranges:
            c.drawString(
                MARGIN_LEFT + col_param + col_value + col_unit,
                y,
                _truncate(v.reference_range_snapshot, 22),
            )
        if is_abn:
            c.setFillColor(HexColor('#b91c1c'))
            c.drawString(
                MARGIN_LEFT + col_param + col_value + col_unit + col_ref,
                y,
                '⚠',
            )
        c.setFillColor(black)
        cursor.advance(4 * mm)


def _draw_signature(cursor: 'PageCursor', ar: AnalysisRequest, settings: LabSettings):
    cursor.hline()
    cursor.gap(2 * mm)
    if ar.confirmed_by:
        cursor.text(
            f'Validated by: {ar.confirmed_by.email}',
            font_size=9, italic=True,
        )
    cursor.text(
        f'Report generated: {timezone.now().strftime("%Y-%m-%d %H:%M")}',
        font_size=8, colour=grey,
    )


def _draw_legal_footer(c: canvas.Canvas, footer: str):
    c.setFont('Helvetica-Oblique', 7)
    c.setFillColor(grey)
    y = MARGIN_BOTTOM - 4 * mm
    for i, line in enumerate(footer.splitlines()[:3]):
        c.drawCentredString(PAGE_WIDTH / 2, y - (i * 3 * mm), line)
    c.setFillColor(black)


def _truncate(s: str, max_chars: int) -> str:
    if not s:
        return ''
    return s if len(s) <= max_chars else s[: max_chars - 1] + '…'


# ---------------------------------------------------------------------------
# Simple cursor helper for flowing top-down layout with auto page breaks
# ---------------------------------------------------------------------------

class PageCursor:
    def __init__(self, c: canvas.Canvas, top: float, bottom: float):
        self.canvas = c
        self.top = top
        self.bottom = bottom
        self.y = top

    def _check_break(self, needed: float):
        if self.y - needed < self.bottom:
            self.canvas.showPage()
            self.y = self.top

    def advance(self, dy: float):
        self.y -= dy

    def gap(self, dy: float):
        self._check_break(dy)
        self.y -= dy

    def hline(self):
        self._check_break(1 * mm)
        self.canvas.setStrokeColor(HexColor('#e2e8f0'))
        self.canvas.line(MARGIN_LEFT, self.y, PAGE_WIDTH - MARGIN_RIGHT, self.y)
        self.y -= 1 * mm

    def text(self, s: str, font_size: int = 10, bold: bool = False, italic: bool = False, colour=black):
        font = 'Helvetica'
        if bold and italic:
            font = 'Helvetica-BoldOblique'
        elif bold:
            font = 'Helvetica-Bold'
        elif italic:
            font = 'Helvetica-Oblique'
        self._check_break(font_size * 0.4 * mm + 1 * mm)
        self.canvas.setFont(font, font_size)
        self.canvas.setFillColor(colour)
        self.canvas.drawString(MARGIN_LEFT, self.y, s)
        self.canvas.setFillColor(black)
        self.y -= (font_size + 2)

    def paragraph(self, s: str, font_size: int = 9, italic: bool = False, colour=black):
        # Simple word-wrap within CONTENT_WIDTH
        font = 'Helvetica-Oblique' if italic else 'Helvetica'
        self.canvas.setFont(font, font_size)
        self.canvas.setFillColor(colour)
        words = s.split()
        line = ''
        for w in words:
            trial = f'{line} {w}'.strip()
            if self.canvas.stringWidth(trial, font, font_size) > CONTENT_WIDTH:
                self._check_break(font_size + 2)
                self.canvas.drawString(MARGIN_LEFT, self.y, line)
                self.y -= (font_size + 2)
                line = w
            else:
                line = trial
        if line:
            self._check_break(font_size + 2)
            self.canvas.drawString(MARGIN_LEFT, self.y, line)
            self.y -= (font_size + 2)
        self.canvas.setFillColor(black)
