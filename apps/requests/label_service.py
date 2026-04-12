"""
Cytova — Request Label Generation Service

Isolated business logic for producing printable specimen labels from a
confirmed analysis request. Kept out of ``services.py`` on purpose so
the label rules can evolve without touching the main request service.

Responsibilities
----------------
- Decide how many labels a request needs (``LabelCountStrategy``).
- Allocate system-wide unique barcodes (``_generate_unique_barcode``).
- Persist the batch + label rows atomically.
- Render a single printable PDF for the whole batch (``_render_batch_pdf``).
- Write an AuditLog entry so generation events are traceable.
- Enforce the lifecycle rule "generate once and reuse".
"""
import io
import logging
import secrets

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from reportlab.graphics.barcode.code128 import Code128
from reportlab.lib.colors import HexColor, black
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from apps.audit.models import ActorType, AuditAction, AuditLog
from apps.users.models import StaffUser
from .models import (
    AnalysisRequest, ItemStatus, RequestLabel, RequestLabelBatch, RequestStatus,
)

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Business rule — isolated constants + strategy class
# -----------------------------------------------------------------------------

# Current operational rule: ``distinct exam families in the request`` + this
# fixed bonus. Kept here as a single named constant so that turning it into
# a per-tenant config value in a future step is a one-line swap without
# touching the strategy logic, the view, or the tests.
EXTRA_LABELS_BONUS = 2


class LabelCountStrategy:
    """
    Computes how many labels to produce for a given analysis request.

    Intentionally shaped as a strategy class (not a free function) so
    future per-tenant overrides can subclass or replace it via dependency
    injection without having to monkey-patch a module-level symbol.

    Current rule
    ------------
    ``N`` = number of distinct non-null exam families among the
    request's non-rejected items.
    ``total`` = ``N + EXTRA_LABELS_BONUS``.

    Rejected items contribute no labels — they are not collected and
    therefore do not need a tube sticker. Items without an exam family
    (legacy or edge cases) also contribute no family entry but still
    benefit from the fixed extras.
    """

    @staticmethod
    def compute(request: AnalysisRequest) -> tuple[int, list[str]]:
        """
        Return ``(total_label_count, ordered_family_names)``.

        ``ordered_family_names`` drives label-to-family pinning: the
        first ``len(ordered_family_names)`` labels get a family name
        stamped on them, the remaining ``EXTRA_LABELS_BONUS`` labels
        are unpinned "extras".
        """
        seen_ids: set = set()
        ordered_names: list[str] = []
        items = (
            request.items
            .select_related('exam_definition__family')
            .all()
        )
        for item in items:
            if item.status == ItemStatus.REJECTED:
                continue
            exam = item.exam_definition
            family = getattr(exam, 'family', None)
            if family is None:
                continue
            if family.id in seen_ids:
                continue
            seen_ids.add(family.id)
            ordered_names.append(family.name)

        return len(ordered_names) + EXTRA_LABELS_BONUS, ordered_names


# -----------------------------------------------------------------------------
# Barcode generation
# -----------------------------------------------------------------------------

_BARCODE_RETRY_LIMIT = 8


def _generate_unique_barcode() -> str:
    """
    Produce a system-wide unique barcode value.

    Shape: ``LBL-YYYYMMDD-XXXXXXXXXXXX``
        - ``LBL-`` prefix identifies the source table at a glance
        - ``YYYYMMDD`` makes the barcode self-describing
        - 12 hex chars = 48 bits of entropy — collision probability is
          < 1 in 280 trillion per day

    A DB uniqueness check + bounded retry loop covers the
    astronomically unlikely collision case. The bound prevents infinite
    spinning in pathological situations (broken RNG, etc.) — eight
    retries is more than enough margin for 48-bit random values.
    """
    today = timezone.now().strftime('%Y%m%d')
    for _ in range(_BARCODE_RETRY_LIMIT):
        candidate = f'LBL-{today}-{secrets.token_hex(6).upper()}'
        if not RequestLabel.objects.filter(barcode_value=candidate).exists():
            return candidate
    raise ValidationError(
        'Failed to generate a unique barcode after several attempts.'
    )


# -----------------------------------------------------------------------------
# PDF rendering
# -----------------------------------------------------------------------------

def _draw_single_label(
    c, x: float, y: float, width: float, height: float,
    label: RequestLabel, request: AnalysisRequest,
) -> None:
    """
    Draw one label at the given origin (bottom-left of the slot).
    Deterministic layout, minimal styling — good enough for operational
    printing; the visual design can be elevated in a later pass.
    """
    # Light border so slots are visible on the print-out
    c.setStrokeColor(HexColor('#CCCCCC'))
    c.setLineWidth(0.5)
    c.rect(x, y, width, height)

    text_x = x + 4 * mm
    line_y = y + height - 6 * mm

    patient = request.patient

    c.setFillColor(black)
    c.setFont('Helvetica-Bold', 10)
    c.drawString(text_x, line_y, patient.full_name)
    line_y -= 4 * mm

    c.setFont('Helvetica', 8)
    c.drawString(text_x, line_y, f'DOB: {patient.date_of_birth.isoformat()}')
    line_y -= 3.5 * mm

    collection = (request.confirmed_at or label.batch.generated_at).date()
    c.drawString(text_x, line_y, f'Collected: {collection.isoformat()}')
    line_y -= 3.5 * mm

    c.drawString(text_x, line_y, f'Req: {request.request_number}')
    line_y -= 3.5 * mm

    if label.family_name:
        c.drawString(text_x, line_y, f'Family: {label.family_name}')
        line_y -= 3.5 * mm

    c.drawString(text_x, line_y, f'Label {label.label_index}/{label.batch.label_count}')

    # Code 128 barcode at the bottom of the slot. reportlab's Code128
    # class renders the bars plus a human-readable baseline by default.
    barcode = Code128(label.barcode_value, barHeight=10 * mm, barWidth=0.4)
    bc_x = x + (width - barcode.width) / 2
    barcode.drawOn(c, bc_x, y + 2 * mm)


def _render_batch_pdf(batch: RequestLabelBatch) -> bytes:
    """
    Produce the full PDF bytes for a batch.

    Layout: A4 portrait, 2 columns × 5 rows per page → up to 10 labels
    per page. Batches larger than 10 paginate automatically.
    """
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    page_w, page_h = A4

    cols, rows = 2, 5
    per_page = cols * rows
    margin_x = 10 * mm
    margin_y = 10 * mm
    grid_gap = 5 * mm
    label_w = (page_w - 2 * margin_x - (cols - 1) * grid_gap) / cols
    label_h = (page_h - 2 * margin_y - (rows - 1) * grid_gap) / rows

    labels = list(
        batch.labels
        .select_related('batch')
        .order_by('label_index')
    )
    request = batch.analysis_request

    for i, label in enumerate(labels):
        slot = i % per_page
        if i > 0 and slot == 0:
            c.showPage()
        col = slot % cols
        row = slot // cols

        x = margin_x + col * (label_w + grid_gap)
        y = page_h - margin_y - (row + 1) * label_h - row * grid_gap
        _draw_single_label(c, x, y, label_w, label_h, label, request)

    c.save()
    return buffer.getvalue()


# -----------------------------------------------------------------------------
# Service entry point
# -----------------------------------------------------------------------------

class RequestLabelService:
    """
    Single public entry point for label generation.

    Views stay thin — they call ``generate_or_get`` and serialise the
    returned batch. All business rules (count, barcodes, lifecycle,
    PDF layout, audit) live inside this module.
    """

    @staticmethod
    @transaction.atomic
    def generate_or_get(
        analysis_request: AnalysisRequest,
        generated_by: StaffUser,
        request,
    ) -> RequestLabelBatch:
        """
        Return the label batch for a post-draft analysis request.

        Idempotent: if a batch already exists for this request, it is
        returned unchanged — no new barcodes, no new PDF, no new audit
        entry. This is the "generate once and reuse" rule documented on
        ``RequestLabelBatch``.

        Raises ``ValidationError`` if the request is still in DRAFT —
        labels are printed for confirmed requests only, since a draft
        represents a specimen that has not yet been committed.
        """
        if analysis_request.status == RequestStatus.DRAFT:
            raise ValidationError(
                'Labels can only be generated for a confirmed analysis request.'
            )

        existing = getattr(analysis_request, 'label_batch', None)
        if existing is not None:
            return existing

        label_count, family_names = LabelCountStrategy.compute(analysis_request)

        batch = RequestLabelBatch.objects.create(
            analysis_request=analysis_request,
            generated_by=generated_by,
            label_count=label_count,
            family_count=len(family_names),
            pdf_file_key='',  # filled in after PDF render
        )

        # First N labels are pinned to each distinct family, the
        # remaining EXTRA_LABELS_BONUS are unpinned extras.
        for idx in range(1, label_count + 1):
            family_name = (
                family_names[idx - 1]
                if idx - 1 < len(family_names)
                else ''
            )
            RequestLabel.objects.create(
                batch=batch,
                barcode_value=_generate_unique_barcode(),
                label_index=idx,
                family_name=family_name,
            )

        # Render the PDF only after every label row exists so the draw
        # loop can iterate from the DB in order.
        pdf_bytes = _render_batch_pdf(batch)
        file_key = f'request-labels/{analysis_request.id}/{batch.id}.pdf'
        default_storage.save(file_key, ContentFile(pdf_bytes))

        batch.pdf_file_key = file_key
        batch.save(update_fields=['pdf_file_key', 'updated_at'])

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=generated_by.id,
            actor_email=generated_by.email,
            action=AuditAction.CREATE,
            entity_type='RequestLabelBatch',
            entity_id=batch.id,
            diff={'after': {
                'analysis_request_id': str(analysis_request.id),
                'request_number': analysis_request.request_number,
                'label_count': label_count,
                'family_count': len(family_names),
            }},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        logger.info(
            'Generated %d labels for request %s',
            label_count, analysis_request.request_number,
        )
        return batch
