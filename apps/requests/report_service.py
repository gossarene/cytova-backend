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

Lifecycle
    - ``generate_or_get``  — idempotent. If a current version exists it is
      returned verbatim; otherwise version 1 is produced.
    - ``regenerate``       — explicit. Creates the next version
      (``last + 1``), marks the previous current as non-current, and
      switches the pointer. Historical versions are preserved.

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
from apps.requests.branding import ReportBranding, resolve_result_report_branding
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

    # --- Public API ---------------------------------------------------------

    @staticmethod
    def get_current(analysis_request: AnalysisRequest):
        """Return the current report version for a request, or ``None``."""
        return (
            AnalysisRequestReport.objects
            .filter(analysis_request=analysis_request, is_current=True)
            .first()
        )

    @staticmethod
    @transaction.atomic
    def generate_or_get(
        analysis_request: AnalysisRequest,
        generated_by,
        request,
    ) -> AnalysisRequestReport:
        """
        Produce version 1 if no current version exists yet; otherwise
        return the current version unchanged. Only VALIDATED requests
        can be reported.

        Idempotent on purpose: a UI refresh or a re-click should not
        produce a silent new version — that is what ``regenerate`` is
        for.
        """
        _assert_validated(analysis_request)

        current = RequestReportService.get_current(analysis_request)
        if current is not None and current.pdf_file_key:
            return current

        return _create_version(
            analysis_request=analysis_request,
            version_number=1,
            generated_by=generated_by,
            request=request,
            audit_action=AuditAction.CREATE,
        )

    @staticmethod
    @transaction.atomic
    def regenerate(
        analysis_request: AnalysisRequest,
        generated_by,
        request,
    ) -> AnalysisRequestReport:
        """
        Create a new report version and switch the current pointer.

        - Requires at least one previous version (use ``generate_or_get``
          to produce v1).
        - Requires VALIDATED status.
        - Marks the old current as ``is_current=False`` and persists the
          new one with ``is_current=True`` in the same transaction so
          the unique partial constraint is never violated.
        - Writes an ``UPDATE`` audit entry — distinct from the CREATE
          action used on first generation — so the audit log is
          unambiguous about version lineage.

        Refuses outright once the request has reached
        ``RESULT_ISSUED`` — the lab must reopen the result first.
        """
        _assert_report_writable(analysis_request)

        # Lock the existing versions for the duration of the transaction
        # to serialise concurrent regenerate calls.
        existing = list(
            AnalysisRequestReport.objects
            .select_for_update()
            .filter(analysis_request=analysis_request)
            .order_by('-version_number')
        )
        if not existing:
            raise ValidationError(
                'No report has been generated yet — use generate first.'
            )

        current = next((r for r in existing if r.is_current), None)
        next_version = existing[0].version_number + 1

        if current is not None:
            current.is_current = False
            current.save(update_fields=['is_current', 'updated_at'])

        return _create_version(
            analysis_request=analysis_request,
            version_number=next_version,
            generated_by=generated_by,
            request=request,
            audit_action=AuditAction.UPDATE,
        )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _assert_validated(analysis_request: AnalysisRequest) -> None:
    """The original gate said "VALIDATED only". After the issuance
    lifecycle landed, a request that has reached ``RESULT_ISSUED`` has
    by definition already had a report generated — the patient is
    reading it — so READ paths must not refuse.

    Write paths (``regenerate``) keep the stricter rule via the
    ``_assert_report_writable`` helper below: regenerating a report on
    an issued request would silently swap what the patient sees, which
    is exactly what the issuance lock exists to prevent.
    """
    if analysis_request.status not in (
        RequestStatus.VALIDATED, RequestStatus.RESULT_ISSUED,
    ):
        raise ValidationError(
            'Final report can only be generated for validated requests '
            f'(current status: {analysis_request.status}).'
        )


def _assert_report_writable(analysis_request: AnalysisRequest) -> None:
    """Stricter gate for actions that PRODUCE a new report version
    (``regenerate``). Refuses once the result has been officially
    issued — the lab must walk the request back through
    ``reopen-result`` first, which transitions to VALIDATED and
    legitimises the new version.

    Refreshes status from the DB so a stale in-memory handle (e.g.
    when a parallel request flipped the row to RESULT_ISSUED between
    the caller's load and this check) doesn't slip past the gate.
    """
    analysis_request.refresh_from_db(fields=['status'])
    if analysis_request.status == RequestStatus.RESULT_ISSUED:
        raise ValidationError(
            'This result has already been issued and is locked. '
            'Reopen the result to generate a new report version.'
        )
    if analysis_request.status != RequestStatus.VALIDATED:
        raise ValidationError(
            'Final report can only be generated for validated requests '
            f'(current status: {analysis_request.status}).'
        )


def _create_version(
    *,
    analysis_request: AnalysisRequest,
    version_number: int,
    generated_by,
    request,
    audit_action: str,
) -> AnalysisRequestReport:
    """
    Render a new PDF and persist a fresh ``AnalysisRequestReport`` row
    as the new current version. Caller is responsible for marking any
    previous current as non-current BEFORE this runs, so the partial
    unique constraint holds throughout the transaction.
    """
    settings = LabSettings.get_solo()
    # Optional partner-specific branding (header / logo / footer). Falls
    # back field-by-field to the lab's own values, so behaviour for any
    # request without partner branding is unchanged. Behaviour toggles
    # (``show_logo``, ``logo_position``, etc.) and PDF password
    # protection still come from ``LabSettings``.
    branding = resolve_result_report_branding(analysis_request)
    sections = _collect_sections(analysis_request)

    # Two-pass rendering: first pass counts pages so the second pass
    # can draw "Page X / Y" on every page.
    ctx_dry = _RenderContext()
    buf_dry = io.BytesIO()
    c_dry = canvas.Canvas(buf_dry, pagesize=A4)
    _render_report(c_dry, analysis_request, settings, branding, sections, ctx_dry)
    c_dry.save()

    ctx = _RenderContext(total_pages=ctx_dry.current_page)
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    _render_report(c, analysis_request, settings, branding, sections, ctx)
    c.save()
    pdf_bytes = buffer.getvalue()

    # Apply password protection if enabled in lab settings
    from .pdf_protection import protect_if_enabled
    pdf_bytes = protect_if_enabled(pdf_bytes, analysis_request, settings)

    report = AnalysisRequestReport.objects.create(
        analysis_request=analysis_request,
        version_number=version_number,
        is_current=True,
        generated_by=generated_by,
        generated_at=timezone.now(),
        pdf_file_key='',
    )

    file_key = f'request-reports/{analysis_request.id}/v{version_number}_{report.id}.pdf'
    default_storage.save(file_key, ContentFile(pdf_bytes))
    report.pdf_file_key = file_key
    report.save(update_fields=['pdf_file_key', 'updated_at'])

    AuditLog.objects.create(
        actor_type=ActorType.STAFF_USER,
        actor_id=getattr(generated_by, 'id', None),
        actor_email=getattr(generated_by, 'email', ''),
        action=audit_action,
        entity_type='AnalysisRequestReport',
        entity_id=report.id,
        diff={'after': {
            'analysis_request_id': str(analysis_request.id),
            'request_number': analysis_request.request_number,
            'version_number': version_number,
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
            'values': [ResultValue],          (annotated with previous_*)
            'previous_value': str | None,     (SINGLE_VALUE only)
            'previous_date':  str | None,     (SINGLE_VALUE only)
        }],
    }

    For MULTI_PARAMETER exams, each ``ResultValue`` in ``values`` carries
    ``previous_value`` and ``previous_date`` attributes attached in-memory
    (Python attribute assignment on model instances). The renderer reads
    these via ``getattr(v, 'previous_value', None)``.

    Only items whose current result version is VALIDATED contribute. Items
    with REJECTED execution mode, or without a validated current version,
    are silently skipped — the report reflects the validated state only.
    """
    items = list(
        analysis_request.items
        .exclude(execution_mode='REJECTED')
        .select_related('exam_definition__family', 'exam_definition__technique')
        .order_by('exam_definition__family__display_order', 'exam_definition__name')
    )

    # Batch-load current validated versions for every item in one query
    item_ids = [i.id for i in items]
    current_versions = {
        v.item_id: v
        for v in ResultVersion.objects.filter(
            item_id__in=item_ids,
            is_current=True,
            status=ResultStatus.VALIDATED,
        ).prefetch_related('values')
    }

    # Collect the exam_definition IDs that have a current result, then
    # batch-fetch previous results for the same patient + exam combos.
    exam_def_ids = {
        i.exam_definition_id
        for i in items
        if i.id in current_versions
    }
    previous_lookup = _build_previous_lookup(
        analysis_request, exam_def_ids,
    )

    families: dict[str, dict] = {}
    for item in items:
        version = current_versions.get(item.id)
        if version is None:
            continue

        exam_def = item.exam_definition
        family = exam_def.family
        family_name = family.name if family else 'Uncategorized'
        family_order = family.display_order if family else 9999

        values = list(version.values.order_by('display_order'))
        prev_version = previous_lookup.get(exam_def.id)

        # Attach previous data to each value or the exam dict
        prev_value_single = None
        prev_date_single = None
        if prev_version is not None:
            prev_vals = {v.parameter_id: v for v in prev_version.values.all()}
            if exam_def.result_structure == ResultStructure.MULTI_PARAMETER:
                for val in values:
                    pv = prev_vals.get(val.parameter_id)
                    val.previous_value = pv.value if pv else None
                    val.previous_date = (
                        prev_version.validated_at.strftime('%Y-%m-%d')
                        if pv and prev_version.validated_at else None
                    )
            else:
                # SINGLE_VALUE: previous is parameter_id=None row
                pv = prev_vals.get(None)
                prev_value_single = pv.value if pv else (
                    prev_version.result_value or None
                )
                prev_date_single = (
                    prev_version.validated_at.strftime('%Y-%m-%d')
                    if prev_version.validated_at else None
                )
        else:
            for val in values:
                val.previous_value = None
                val.previous_date = None

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
            'version': version,
            'values': values,
            'previous_value': prev_value_single,
            'previous_date': prev_date_single,
        })

    return sorted(
        families.values(),
        key=lambda s: (s['family_order'], s['family_name']),
    )


def _build_previous_lookup(
    current_request: AnalysisRequest,
    exam_def_ids: set,
) -> dict:
    """
    Batch-fetch the most recent previous validated result for each exam
    definition, for the same patient.

    Returns ``{exam_definition_id: ResultVersion}`` with values
    pre-loaded. At most **2 queries** (versions + prefetched values)
    regardless of how many exam definitions are in the set.

    Rules:
    - Same patient.
    - Same exam_definition.
    - Different request, created strictly before the current one.
    - Result version is the item's current version and is VALIDATED.
    - Among candidates, the one from the most recently created request
      wins (closest temporal comparison).
    """
    if not exam_def_ids:
        return {}

    patient = current_request.patient

    candidates = list(
        ResultVersion.objects.filter(
            item__analysis_request__patient=patient,
            item__exam_definition_id__in=exam_def_ids,
            is_current=True,
            status=ResultStatus.VALIDATED,
            item__analysis_request__created_at__lt=current_request.created_at,
        ).exclude(
            item__analysis_request=current_request,
        ).select_related(
            'item__exam_definition',
            'item__analysis_request',
        ).prefetch_related(
            'values',
        ).order_by(
            'item__exam_definition_id',
            '-item__analysis_request__created_at',
        )
    )

    # De-duplicate: keep only the first (most recent) per exam_definition
    lookup: dict = {}
    for v in candidates:
        eid = v.item.exam_definition_id
        if eid not in lookup:
            lookup[eid] = v
    return lookup


# ---------------------------------------------------------------------------
# PDF rendering (ReportLab canvas)
# ---------------------------------------------------------------------------

FOOTER_ZONE = 20 * mm
HEADER_ZONE = 0  # computed dynamically per render
LABEL_COLOR = HexColor('#475569')
COL_GAP = 10 * mm
LEFT_COL_W = (CONTENT_WIDTH - COL_GAP) / 2
RIGHT_COL_X = MARGIN_LEFT + LEFT_COL_W + COL_GAP
FIELD_LABEL_W = 30 * mm

SIG_MAX_W = 30 * mm
SIG_MAX_H = 15 * mm


class _RenderContext:
    __slots__ = ('current_page', 'total_pages')

    def __init__(self, total_pages: int = 0):
        self.current_page = 1
        self.total_pages = total_pages


def _accent(settings: LabSettings):
    try:
        return HexColor(settings.report_accent_color or '#0f172a')
    except Exception:  # noqa: BLE001
        return HexColor('#0f172a')


def _render_report(
    c: canvas.Canvas, ar: AnalysisRequest, settings: LabSettings,
    branding: ReportBranding, sections: list[dict], ctx: '_RenderContext',
):
    def _on_page_break():
        _draw_page_footer(c, ar, settings, branding, ctx)
        ctx.current_page += 1

    def _on_new_page(cursor: 'PageCursor'):
        _draw_header(cursor, settings, branding)
        cursor.gap(2 * mm)
        _draw_continuation_line(cursor, ar)
        cursor.gap(4 * mm)

    cursor = PageCursor(
        c,
        top=PAGE_HEIGHT - MARGIN_TOP,
        bottom=MARGIN_BOTTOM + FOOTER_ZONE,
        on_page_break=_on_page_break,
        on_new_page=_on_new_page,
    )

    # Page 1: full header + patient/request block
    _draw_header(cursor, settings, branding)
    cursor.gap(5 * mm)
    _draw_patient_request_block(cursor, ar, settings)
    cursor.gap(8 * mm)

    # Exam sections
    if not sections:
        cursor.text('No validated results to report.', font_size=10, colour=grey)
    for section in sections:
        _draw_section(cursor, section, settings)
        cursor.gap(6 * mm)

    # Final conclusion
    if settings.show_final_conclusion and ar.final_conclusion:
        cursor.gap(4 * mm)
        cursor.text('Final Conclusion', font_size=11, bold=True)
        cursor.gap(2 * mm)
        cursor.paragraph(ar.final_conclusion, font_size=9)

    # Validation block — last business content on the final page,
    # visually separate from the repeating footer (pagination + legal).
    if settings.show_signature and ar.confirmed_by:
        _draw_validation_block(cursor, ar)

    # Last page footer (pagination + legal only)
    _draw_page_footer(c, ar, settings, branding, ctx)


# ---------------------------------------------------------------------------
# Logo
# ---------------------------------------------------------------------------

def _draw_logo(
    c: canvas.Canvas, cursor: 'PageCursor', settings: LabSettings,
    branding: ReportBranding,
):
    """
    Render the logo inside a fixed bounding box anchored to the page
    top-right (or top-left / top-center per ``logo_position``).

    The box origin is ``PAGE_HEIGHT - MARGIN_TOP`` so the logo sits at a
    stable position regardless of where the cursor is. The image is
    centered both horizontally and vertically inside the box, scaled
    down to fit, with aspect ratio preserved.

    Logo *source* comes from ``branding`` (lab or partner depending on
    request); position + max box size still come from ``LabSettings``.
    """
    logo_key = branding.logo_file_key
    if not logo_key:
        return
    try:
        from reportlab.lib.utils import ImageReader
        from django.core.files.storage import default_storage

        with default_storage.open(logo_key, 'rb') as f:
            img = ImageReader(f)

        max_w = settings.logo_max_width_mm * mm
        max_h = settings.logo_max_height_mm * mm
        iw, ih = img.getSize()
        if ih <= 0 or iw <= 0:
            return
        scale = min(max_w / iw, max_h / ih, 1.0)
        w = iw * scale
        h = ih * scale

        # Box horizontal position
        pos = settings.logo_position
        if pos == 'LEFT':
            box_x = MARGIN_LEFT
        elif pos == 'CENTER':
            box_x = MARGIN_LEFT + (CONTENT_WIDTH - max_w) / 2
        else:
            box_x = PAGE_WIDTH - MARGIN_RIGHT - max_w

        # Anchored to page top — independent of cursor position
        box_top = PAGE_HEIGHT - MARGIN_TOP
        x = box_x + (max_w - w) / 2
        y = box_top - max_h + (max_h - h) / 2

        c.drawImage(img, x, y, width=w, height=h,
                    preserveAspectRatio=True, mask='auto')
    except Exception as e:  # noqa: BLE001
        logger.warning('Failed to render report logo: %s', e)


# ---------------------------------------------------------------------------
# Header — side-by-side logo zone + text zone
# ---------------------------------------------------------------------------

_HEADER_GAP = 5 * mm  # horizontal gap between logo box and text zone


def _draw_header(
    cursor: 'PageCursor', settings: LabSettings, branding: ReportBranding,
):
    """
    Two-zone header layout.

    The header reserves a fixed vertical band equal to the logo's
    ``max_height`` (or a sensible minimum if no logo is configured).
    Inside that band:

    - **Logo zone** — positioned per ``logo_position`` (LEFT / CENTER
      / RIGHT), image centered inside its bounding box.
    - **Text zone** — occupies the remaining width, top-aligned to the
      same starting Y as the logo zone. Contains: header name, subtitle,
      address, contact info.

    Both zones share the same top anchor (``cursor.y``) and the cursor
    advances past the taller of the two once drawing is complete.

    Identity data (name/subtitle/address/contact/logo source) comes
    from ``branding`` so that partner overrides apply transparently.
    Display toggles (``show_logo``, ``show_lab_address``,
    ``logo_position``, ``logo_max_*``) still come from ``settings`` —
    partners don't override layout decisions.
    """
    c = cursor.canvas
    has_logo = settings.show_logo and bool(branding.logo_file_key)

    logo_box_w = settings.logo_max_width_mm * mm if has_logo else 0
    logo_box_h = settings.logo_max_height_mm * mm if has_logo else 0
    header_top = cursor.y

    # -- Determine text zone boundaries --
    pos = getattr(settings, 'logo_position', 'RIGHT') if has_logo else 'RIGHT'
    if pos == 'LEFT':
        text_x = MARGIN_LEFT + logo_box_w + _HEADER_GAP
        text_w = CONTENT_WIDTH - logo_box_w - _HEADER_GAP
    elif pos == 'CENTER':
        # Logo centered → text goes full width underneath (fallback to
        # stacked layout when logo is centered — side-by-side doesn't
        # make sense visually).
        text_x = MARGIN_LEFT
        text_w = CONTENT_WIDTH
    else:
        text_x = MARGIN_LEFT
        text_w = CONTENT_WIDTH - logo_box_w - _HEADER_GAP

    # -- Draw logo in its zone --
    if has_logo:
        _draw_logo(c, cursor, settings, branding)

    # -- Draw text in its zone (top-aligned to header_top) --
    text_y = header_top
    line_h = 0  # tracks how far down the text extends

    if branding.name:
        c.setFont('Helvetica-Bold', 16)
        c.setFillColor(black)
        c.drawString(text_x, text_y, branding.name)
        text_y -= 18
        line_h += 18

    if branding.subtitle:
        text_y -= 1
        c.setFont('Helvetica', 9)
        c.setFillColor(grey)
        c.drawString(text_x, text_y, branding.subtitle)
        text_y -= 11
        line_h += 12

    if settings.show_lab_address:
        text_y -= 3
        line_h += 3
        if branding.address:
            c.setFont('Helvetica', 8)
            c.setFillColor(grey)
            for line in branding.address.splitlines():
                c.drawString(text_x, text_y, line)
                text_y -= 10
                line_h += 10
        contact_parts = []
        if branding.phone:
            contact_parts.append(f'Tel: {branding.phone}')
        if branding.email:
            contact_parts.append(branding.email)
        if branding.website:
            contact_parts.append(branding.website)
        if contact_parts:
            c.setFont('Helvetica', 8)
            c.setFillColor(grey)
            c.drawString(text_x, text_y, ' · '.join(contact_parts))
            text_y -= 10
            line_h += 10

    c.setFillColor(black)

    # -- Advance cursor past whichever zone is taller --
    used_h = max(logo_box_h, line_h)
    cursor.y = header_top - used_h
    cursor.gap(3 * mm)
    cursor.hline()


# ---------------------------------------------------------------------------
# Patient / Request — two-column layout
# ---------------------------------------------------------------------------

def _draw_patient_request_block(
    cursor: 'PageCursor', ar: AnalysisRequest, settings: LabSettings,
):
    patient = ar.patient
    reference = ar.public_reference or ar.request_number
    c = cursor.canvas

    # -- Collect fields for each column --
    left_fields: list[tuple[str, str]] = []
    left_fields.append(('Last Name', patient.last_name))
    left_fields.append(('First Name', patient.first_name))
    if settings.show_patient_sex:
        left_fields.append(('Sex', getattr(patient, 'gender', '') or ''))
    if settings.show_patient_age and getattr(patient, 'date_of_birth', None):
        left_fields.append(('Date of Birth', patient.date_of_birth.isoformat()))

    right_fields: list[tuple[str, str]] = []
    right_fields.append(('Request #', reference))
    if settings.show_collection_datetime:
        first_collected = (
            ar.items.exclude(collected_at__isnull=True)
            .order_by('collected_at').first()
        )
        if first_collected and first_collected.collected_at:
            right_fields.append(('Collected', first_collected.collected_at.strftime('%Y-%m-%d %H:%M')))
    right_fields.append(('Report date', timezone.now().strftime('%Y-%m-%d %H:%M')))
    if settings.show_prescriber and ar.external_reference:
        right_fields.append(('Ext. Ref', ar.external_reference))

    row_h = 13
    rows = max(len(left_fields), len(right_fields))
    cursor._check_break(rows * row_h + 4 * mm)

    start_y = cursor.y
    for i, (label, value) in enumerate(left_fields):
        y = start_y - i * row_h
        c.setFont('Helvetica-Bold', 9)
        c.setFillColor(LABEL_COLOR)
        c.drawString(MARGIN_LEFT, y, label)
        c.setFont('Helvetica', 9)
        c.setFillColor(black)
        c.drawString(MARGIN_LEFT + FIELD_LABEL_W, y, value)

    for i, (label, value) in enumerate(right_fields):
        y = start_y - i * row_h
        c.setFont('Helvetica-Bold', 9)
        c.setFillColor(LABEL_COLOR)
        c.drawString(RIGHT_COL_X, y, label)
        c.setFont('Helvetica', 9)
        c.setFillColor(black)
        c.drawString(RIGHT_COL_X + FIELD_LABEL_W, y, value)

    c.setFillColor(black)
    cursor.y = start_y - rows * row_h
    cursor.gap(2 * mm)
    cursor.hline()


def _draw_continuation_line(cursor: 'PageCursor', ar: AnalysisRequest):
    """Compact identifier line for continuation pages (page 2+)."""
    patient = ar.patient
    reference = ar.public_reference or ar.request_number
    cursor.text(
        f'{patient.last_name}, {patient.first_name}  ·  Request #{reference}',
        font_size=8, colour=grey,
    )
    cursor.hline()


# ---------------------------------------------------------------------------
# Validation block — rendered once on the final page as business content
# ---------------------------------------------------------------------------

def _draw_validation_block(cursor: 'PageCursor', ar: AnalysisRequest):
    """
    Right-aligned validation identity + optional biologist signature.

    This is final **document body content**, not footer material —
    deliberately rendered via the cursor so it flows with content,
    participates in page-break logic, and appears once on the last
    page. No delimiter line above it; spacing alone separates it
    from the preceding result section so it reads as the natural
    end of the clinical document.

    The signature image is read from the validating biologist's own
    ``StaffUser.signature_file_key`` — NOT from LabSettings (which
    holds a separate lab-wide stamp, unrelated to per-biologist
    identity).
    """
    c = cursor.canvas
    right_x = PAGE_WIDTH - MARGIN_RIGHT

    cursor.gap(12 * mm)

    validator = ar.confirmed_by
    sig_key = getattr(validator, 'signature_file_key', '')
    if sig_key:
        try:
            from reportlab.lib.utils import ImageReader
            with default_storage.open(sig_key, 'rb') as f:
                sig = ImageReader(f)
            iw, ih = sig.getSize()
            if iw > 0 and ih > 0:
                scale = min(SIG_MAX_W / iw, SIG_MAX_H / ih, 1.0)
                sw, sh = iw * scale, ih * scale
                cursor._check_break(sh + 16)
                sig_x = right_x - sw
                c.drawImage(sig, sig_x, cursor.y - sh,
                            width=sw, height=sh,
                            preserveAspectRatio=True, mask='auto')
                cursor.y -= sh + 2
        except Exception:  # noqa: BLE001
            pass

    cursor.gap(4 * mm)
    name = validator.get_display_name()
    cursor._check_break(14)
    c.setFont('Helvetica', 9)
    c.setFillColor(black)
    c.drawRightString(right_x, cursor.y, f'Validated by: {name}')
    cursor.y -= 14


# ---------------------------------------------------------------------------
# Family section + exams
# ---------------------------------------------------------------------------

def _draw_section(cursor: 'PageCursor', section: dict, settings: LabSettings):
    accent = _accent(settings)
    cursor.gap(4 * mm)
    cursor.text_centered(
        section['family_name'].upper(),
        font_size=12, bold=True, colour=accent,
    )
    if settings.show_family_divider_line:
        cursor.hline()
    cursor.gap(3 * mm)

    for i, exam in enumerate(section['exams']):
        _draw_exam(cursor, exam, settings)
        if i < len(section['exams']) - 1:
            cursor.gap(3 * mm)


def _draw_exam(cursor: 'PageCursor', exam: dict, settings: LabSettings):
    cursor.text(f'{exam["code"]}  ·  {exam["name"]}', font_size=10, bold=True)

    tech = exam['technique']
    if settings.show_exam_technique and tech and tech.lower() not in ('unspecified', ''):
        cursor.text(f'Technique: {tech}', font_size=7, italic=True, colour=grey)
    cursor.gap(1 * mm)

    version = exam['version']
    values = exam['values']

    show_prev = settings.show_previous_results

    if exam['structure'] == ResultStructure.MULTI_PARAMETER:
        _draw_param_table(cursor, values, settings, show_prev)
    else:
        _draw_single_value_table(cursor, exam, settings, show_prev)

    if settings.show_patient_comments and version.comments:
        cursor.gap(1 * mm)
        cursor.paragraph(f'Comment: {version.comments}', font_size=8, italic=True, colour=grey)


# ---------------------------------------------------------------------------
# SINGLE_VALUE table
# ---------------------------------------------------------------------------

def _draw_single_value_table(
    cursor: 'PageCursor', exam: dict, settings: LabSettings,
    show_prev: bool,
) -> None:
    version = exam['version']
    values = exam['values']
    single = values[0] if values else None

    value_str = single.value if single else version.result_value
    unit_str = (single.unit_snapshot if single else version.result_unit) or ''
    ref_str = (single.reference_range_snapshot if single else version.reference_range) or ''
    is_abn = (single.is_abnormal if single else version.is_abnormal)

    prev_val = exam.get('previous_value') if show_prev else None
    prev_date = exam.get('previous_date') if show_prev else None
    has_prev = prev_val is not None

    if has_prev:
        col_name = CONTENT_WIDTH * 0.30
        col_val = CONTENT_WIDTH * 0.22
        col_ref = CONTENT_WIDTH * 0.22
        col_prev = CONTENT_WIDTH * 0.26
    else:
        col_name = CONTENT_WIDTH * 0.40
        col_val = CONTENT_WIDTH * 0.28
        col_ref = CONTENT_WIDTH * 0.32
        col_prev = 0

    c = cursor.canvas

    # Column headers
    y = cursor.y
    c.setFont('Helvetica-Bold', 7.5)
    c.setFillColor(grey)
    c.drawString(MARGIN_LEFT, y, 'Test Name')
    c.drawRightString(MARGIN_LEFT + col_name + col_val, y, 'Current Value')
    if settings.show_reference_ranges:
        c.drawString(MARGIN_LEFT + col_name + col_val + 4, y, 'Ref. Range')
    if has_prev:
        header = f'Previous ({prev_date})' if prev_date else 'Previous'
        c.drawRightString(
            MARGIN_LEFT + col_name + col_val + col_ref + col_prev, y, header,
        )
    c.setFillColor(black)
    cursor.advance(4 * mm)

    # Data row
    abn_color = HexColor('#b91c1c')
    colour = abn_color if (settings.show_abnormal_flags and is_abn) else black
    y = cursor.y
    c.setFont('Helvetica', 9)
    c.setFillColor(colour)
    c.drawString(MARGIN_LEFT, y, _truncate(exam['name'], 35))
    c.setFont('Helvetica-Bold', 9)
    current_display = f'{value_str} {unit_str}'.strip()
    c.drawRightString(MARGIN_LEFT + col_name + col_val, y, _truncate(current_display, 22))
    c.setFont('Helvetica', 9)
    if settings.show_reference_ranges:
        c.drawString(MARGIN_LEFT + col_name + col_val + 4, y, _truncate(ref_str, 22))
    if has_prev:
        prev_display = f'{prev_val} {unit_str}'.strip() if prev_val else '—'
        c.setFont('Helvetica', 8)
        c.setFillColor(grey)
        c.drawRightString(
            MARGIN_LEFT + col_name + col_val + col_ref + col_prev, y,
            _truncate(prev_display, 22),
        )
    c.setFillColor(black)
    cursor.advance(4.5 * mm)


# ---------------------------------------------------------------------------
# MULTI_PARAMETER table
# ---------------------------------------------------------------------------

def _draw_param_table(
    cursor: 'PageCursor', values, settings: LabSettings,
    show_prev: bool,
):
    has_prev = show_prev and any(
        getattr(v, 'previous_value', None) is not None for v in values
    )
    prev_date = None
    if has_prev:
        prev_date = next(
            (getattr(v, 'previous_date', None) for v in values
             if getattr(v, 'previous_value', None) is not None),
            None,
        )

    if has_prev:
        col_param = CONTENT_WIDTH * 0.28
        col_value = CONTENT_WIDTH * 0.17
        col_unit = CONTENT_WIDTH * 0.12
        col_ref = CONTENT_WIDTH * 0.18
    else:
        col_param = CONTENT_WIDTH * 0.40
        col_value = CONTENT_WIDTH * 0.20
        col_unit = CONTENT_WIDTH * 0.12
        col_ref = CONTENT_WIDTH * 0.28

    c = cursor.canvas
    x = MARGIN_LEFT
    right_edge = PAGE_WIDTH - MARGIN_RIGHT

    # Column headers
    y = cursor.y
    c.setFont('Helvetica-Bold', 7.5)
    c.setFillColor(grey)
    c.drawString(x, y, 'Parameter Name')
    c.drawRightString(x + col_param + col_value, y, 'Current Value')
    c.drawString(x + col_param + col_value + 4, y, 'Unit')
    if settings.show_reference_ranges:
        c.drawString(x + col_param + col_value + col_unit + 4, y, 'Ref. Range')
    if has_prev:
        header = f'Previous ({prev_date})' if prev_date else 'Previous'
        c.drawRightString(right_edge, y, header)
    c.setFillColor(black)
    cursor.advance(4 * mm)

    # Data rows
    abn_color = HexColor('#b91c1c')
    for v in values:
        is_abn = settings.show_abnormal_flags and v.is_abnormal
        colour = abn_color if is_abn else black
        y = cursor.y
        c.setFont('Helvetica', 9)
        c.setFillColor(colour)
        c.drawString(x, y, _truncate(v.name_snapshot, 35))
        c.setFont('Helvetica-Bold', 9)
        c.drawRightString(x + col_param + col_value, y, _truncate(v.value, 18))
        c.setFont('Helvetica', 9)
        c.drawString(x + col_param + col_value + 4, y, _truncate(v.unit_snapshot, 14))
        if settings.show_reference_ranges:
            c.drawString(
                x + col_param + col_value + col_unit + 4, y,
                _truncate(v.reference_range_snapshot, 22),
            )
        if has_prev:
            pv = getattr(v, 'previous_value', None) or '—'
            c.setFont('Helvetica', 8)
            c.setFillColor(grey)
            c.drawRightString(right_edge, y, _truncate(pv, 18))
        c.setFillColor(black)
        cursor.advance(4 * mm)


# ---------------------------------------------------------------------------
# Fixed page footer
# ---------------------------------------------------------------------------

def _draw_page_footer(
    c: canvas.Canvas, ar: AnalysisRequest, settings: LabSettings,
    branding: ReportBranding, ctx: '_RenderContext',
):
    """
    Repeating footer drawn at a fixed Y on every page.

    Contains only pagination and legal text — the validation block is
    rendered as final business content via the cursor (see
    ``_draw_validation_block``), not repeated per page.

    The legal text source comes from ``branding`` so a partner-supplied
    footer overrides the lab's; the toggle ``show_legal_footer`` stays
    on ``LabSettings``.
    """
    y = MARGIN_BOTTOM + FOOTER_ZONE - 4 * mm

    c.setStrokeColor(HexColor('#c4c9d0'))
    c.setLineWidth(0.5)
    c.line(MARGIN_LEFT, y, PAGE_WIDTH - MARGIN_RIGHT, y)

    # -- Pagination (centered) --
    if ctx.total_pages > 0:
        c.setFont('Helvetica', 8)
        c.setFillColor(grey)
        c.drawCentredString(
            PAGE_WIDTH / 2, y - 4 * mm,
            f'Page {ctx.current_page} / {ctx.total_pages}',
        )

    # -- Legal text (bottom, centered) --
    if settings.show_legal_footer and branding.legal_footer:
        c.setFont('Helvetica-Oblique', 7)
        c.setFillColor(grey)
        legal_y = MARGIN_BOTTOM
        for i, line in enumerate(branding.legal_footer.splitlines()[:3]):
            c.drawCentredString(
                PAGE_WIDTH / 2, legal_y + (2 - i) * 3 * mm, line,
            )

    c.setFillColor(black)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate(s: str, max_chars: int) -> str:
    if not s:
        return ''
    return s if len(s) <= max_chars else s[: max_chars - 1] + '…'


# ---------------------------------------------------------------------------
# Page cursor
# ---------------------------------------------------------------------------

class PageCursor:
    def __init__(self, c: canvas.Canvas, top: float, bottom: float,
                 on_page_break=None, on_new_page=None):
        self.canvas = c
        self.top = top
        self.bottom = bottom
        self.y = top
        self._on_page_break = on_page_break
        self._on_new_page = on_new_page

    def _check_break(self, needed: float):
        if self.y - needed < self.bottom:
            if self._on_page_break:
                self._on_page_break()
            self.canvas.showPage()
            self.y = self.top
            if self._on_new_page:
                self._on_new_page(self)

    def advance(self, dy: float):
        self.y -= dy

    def gap(self, dy: float):
        self._check_break(dy)
        self.y -= dy

    def hline(self, colour=None):
        self._check_break(1 * mm)
        self.canvas.setStrokeColor(colour or HexColor('#c4c9d0'))
        self.canvas.setLineWidth(0.5)
        self.canvas.line(MARGIN_LEFT, self.y, PAGE_WIDTH - MARGIN_RIGHT, self.y)
        self.y -= 1 * mm

    def text(self, s: str, font_size: int = 10, bold: bool = False,
             italic: bool = False, colour=black):
        font = _resolve_font(bold, italic)
        self._check_break(font_size * 0.4 * mm + 1 * mm)
        self.canvas.setFont(font, font_size)
        self.canvas.setFillColor(colour)
        self.canvas.drawString(MARGIN_LEFT, self.y, s)
        self.canvas.setFillColor(black)
        self.y -= (font_size + 2)

    def text_centered(self, s: str, font_size: int = 10, bold: bool = False,
                      italic: bool = False, colour=black):
        font = _resolve_font(bold, italic)
        self._check_break(font_size * 0.4 * mm + 1 * mm)
        self.canvas.setFont(font, font_size)
        self.canvas.setFillColor(colour)
        self.canvas.drawCentredString(PAGE_WIDTH / 2, self.y, s)
        self.canvas.setFillColor(black)
        self.y -= (font_size + 2)

    def paragraph(self, s: str, font_size: int = 9, italic: bool = False,
                  colour=black):
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


def _resolve_font(bold: bool, italic: bool) -> str:
    if bold and italic:
        return 'Helvetica-BoldOblique'
    if bold:
        return 'Helvetica-Bold'
    if italic:
        return 'Helvetica-Oblique'
    return 'Helvetica'
