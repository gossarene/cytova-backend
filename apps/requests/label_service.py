"""
Cytova — Request Label Generation Service

Business logic that turns a confirmed analysis request into a printable
batch of specimen labels. Layout primitives live in ``apps.labels``;
this module wires tenant + request + storage + audit around them.

Responsibilities
----------------
- Decide how many labels a request needs (``LabelCountStrategy``).
- Allocate numeric label codes from the tenant's monthly sequence
  (``_allocate_numeric_code``).
- Persist the batch + label rows atomically.
- Render the PDF using the lab's effective label config and the
  appropriate renderer (A4 sheet or thermal roll).
- Write an AuditLog entry so generation events are traceable.
- Enforce the lifecycle rule "generate once and reuse".
"""
import logging
from datetime import date

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import connection, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.audit.models import ActorType, AuditAction, AuditLog
from apps.labels.renderers import LabelPayload, render_labels
from apps.lab_settings.models import LabSettings
from apps.tenants.models import Tenant
from apps.users.models import StaffUser
from .models import (
    AnalysisRequest, ItemStatus, LabelSequence,
    RequestLabel, RequestLabelBatch, RequestStatus,
)

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Business rule — isolated constants + strategy class
# -----------------------------------------------------------------------------

# Pre-Phase-1 hard-coded extras count. Kept as a module-level
# constant for two reasons:
#   1. ``LabSettings.extra_label_count`` defaults to this value, so
#      tenants on the default config still produce the historical
#      five-label batches (2 extras on top of every 3-family
#      request).
#   2. Existing tests reference it as the back-compat baseline — no
#      need to update them when the source of truth moved into
#      lab settings.
# Code consumers should read ``LabSettings.extra_label_count`` —
# this constant is the documented default, not the runtime source.
EXTRA_LABELS_BONUS = 2


class LabelCountStrategy:
    """Computes how many labels to produce for a given analysis request.

    Phase 4 wires the count to ``LabSettings.extra_label_count``.
    The strategy stays a pure function of (request, extras): callers
    that want the lab default pass ``extra_count=None`` and the
    strategy reads the setting; callers that already have the value
    in hand (the service does, since it reads settings once per
    batch) pass it explicitly to avoid a second ``get_solo`` trip.
    """

    @staticmethod
    def compute(
        request: AnalysisRequest,
        extra_count: int | None = None,
    ) -> tuple[int, list[str]]:
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

        if extra_count is None:
            extra_count = LabSettings.get_solo().extra_label_count

        return len(ordered_names) + extra_count, ordered_names


# -----------------------------------------------------------------------------
# Numeric label code allocation
# -----------------------------------------------------------------------------

LABEL_SEQUENCE_WIDTH = 6
LABEL_SEQUENCE_MAX = 10 ** LABEL_SEQUENCE_WIDTH - 1


def _current_tenant_numeric_code() -> str:
    """
    Resolve the current tenant's 4-digit numeric code.

    django-tenants' ``TenantMiddleware`` attaches ``tenant`` to the
    connection in production; ``schema_context`` (used in tests) only
    sets the search path. We fall back to a lookup by schema_name so
    both code paths behave identically.
    """
    tenant = getattr(connection, 'tenant', None)
    if tenant is not None and getattr(tenant, 'numeric_code', None):
        return tenant.numeric_code
    schema_name = getattr(connection, 'schema_name', None)
    if not schema_name or schema_name == 'public':
        raise ValidationError('Label generation requires a tenant context.')
    try:
        return Tenant.objects.only('numeric_code').get(schema_name=schema_name).numeric_code
    except Tenant.DoesNotExist as exc:
        raise ValidationError(
            f'No tenant registered for schema {schema_name!r}.'
        ) from exc


def period_key_for(today: date, mode: str = 'MONTHLY') -> str:
    """Compute the ``LabelSequence.period_key`` for the supplied date.

    The shape depends on the tenant's reset mode (Phase 1 setting
    ``LabSettings.label_sequence_reset_period``):

      - ``MONTHLY`` (default) → ``"YYYY-MM"``. The pre-Phase-2
        behaviour. Each month starts a fresh sequence at 1.
      - ``YEARLY``            → ``"YYYY"``. The sequence runs
        continuously from January 1 to December 31, useful for
        labs whose monthly throughput is too low to justify the
        per-month reset.

    Default ``mode='MONTHLY'`` keeps the helper safe to call from
    legacy code paths without forcing them to thread the setting
    through. The allocator (the only real consumer) always passes
    the resolved tenant setting explicitly.

    Unknown modes fall through to ``MONTHLY`` so a future enum value
    that ships in a settings migration before the helper is updated
    doesn't crash label generation — the worst case is "monthly
    sequence behaviour" until the helper catches up.
    """
    if mode == 'YEARLY':
        return f'{today.year:04d}'
    return f'{today.year:04d}-{today.month:02d}'


def _allocate_numeric_code(
    tenant_numeric_code: str, today: date, reset_mode: str = 'MONTHLY',
) -> str:
    """
    Allocate one numeric label code ``TTTTYYMMSSSSSS``.

    Must be called inside ``transaction.atomic`` — the caller's
    ``select_for_update`` on the ``LabelSequence`` row serialises
    concurrent allocations for the same tenant + period. See the
    "Concurrency" section of this module for the lock contract.

    ``reset_mode`` selects the sequence reset cadence (Phase 3
    integration with ``LabSettings.label_sequence_reset_period``):

      - ``MONTHLY`` (default) → period_key ``"YYYY-MM"``; sequence
        resets the 1st of every month. Pre-Phase-3 behaviour.
      - ``YEARLY``            → period_key ``"YYYY"``; sequence
        runs continuously through the year.

    The barcode body still embeds ``YYMM`` for human readability;
    the *sequence reset* is determined by ``period_key_for``. For
    monthly reset the two align by definition; for yearly reset,
    the YYMM in the barcode is purely informational while
    ``SSSSSS`` is unique within the full year.

    The reset mode is passed in (rather than re-fetched here) so the
    allocator stays a pure function of its inputs and so the caller
    pays the ``LabSettings.get_solo()`` cost once per batch instead
    of once per label.
    """
    period_key = period_key_for(today, reset_mode)
    # The two-step lookup (get_or_create then select_for_update) is
    # the canonical Django pattern for "lock-or-create" against a
    # single-row counter. The first call ensures the row exists; the
    # second call takes the row lock under the caller's atomic
    # block. Concurrent allocators serialise on the row lock, so no
    # two transactions can read the same ``last_value``.
    LabelSequence.objects.get_or_create(period_key=period_key)
    seq = LabelSequence.objects.select_for_update().get(
        period_key=period_key,
    )
    next_value = seq.last_value + 1
    if next_value > LABEL_SEQUENCE_MAX:
        raise ValidationError(
            'Label sequence exhausted for this tenant period.'
        )
    seq.last_value = next_value
    seq.save(update_fields=['last_value', 'updated_at'])
    yy = today.year % 100
    return (
        f'{tenant_numeric_code}'
        f'{yy:02d}{today.month:02d}'
        f'{next_value:0{LABEL_SEQUENCE_WIDTH}d}'
    )


# -----------------------------------------------------------------------------
# Payload builder
# -----------------------------------------------------------------------------

def _build_payloads(
    batch: RequestLabelBatch,
) -> list[LabelPayload]:
    request = batch.analysis_request
    patient = request.patient
    collection = (request.confirmed_at or batch.generated_at).date()
    labels = list(batch.labels.order_by('label_index'))
    return [
        LabelPayload(
            numeric_code=label.barcode_value,
            patient_name=patient.full_name,
            patient_dob=patient.date_of_birth.isoformat(),
            collection_date=collection.isoformat(),
            request_number=request.request_number,
            family_name=label.family_name,
            label_index=label.label_index,
            label_total=batch.label_count,
        )
        for label in labels
    ]


# -----------------------------------------------------------------------------
# Service entry point
# -----------------------------------------------------------------------------

class RequestLabelService:
    """
    Single public entry point for label generation.

    Views stay thin — they call ``generate_or_get`` and serialise the
    returned batch. All business rules (count, numeric codes, lifecycle,
    PDF layout, audit) live inside this module.
    """

    @staticmethod
    @transaction.atomic
    def generate_or_get(
        analysis_request: AnalysisRequest,
        generated_by: StaffUser,
        request,
    ) -> RequestLabelBatch:
        if analysis_request.status == RequestStatus.DRAFT:
            raise ValidationError(
                'Labels can only be generated for a confirmed analysis request.'
            )

        existing = getattr(analysis_request, 'label_batch', None)
        if existing is not None:
            return existing

        # Resolve the lab settings ONCE per batch — both the count
        # strategy, the allocator (per-label), and the PDF renderer
        # need them, and ``get_solo`` is cheap but doing it inside
        # the per-label loop would multiply the trip by label_count.
        lab_settings = LabSettings.get_solo()
        reset_mode = lab_settings.label_sequence_reset_period
        numbering_mode = lab_settings.label_numbering_mode
        extra_count = lab_settings.extra_label_count

        label_count, family_names = LabelCountStrategy.compute(
            analysis_request, extra_count=extra_count,
        )

        # Refuse to materialise an empty batch — there's nothing
        # operational to print, and a 0-count row would silently
        # confuse every downstream surface (audit, PDF, scan
        # workflow). The combination "no families AND zero extras"
        # is only reachable when the lab explicitly opts out of
        # extras AND the request carries no exam family — caller
        # should add an exam item with a family or restore extras.
        if label_count == 0:
            raise ValidationError(
                'No labels to generate: the request has no exam family '
                'and the lab is configured for zero extra labels.'
            )

        batch = RequestLabelBatch.objects.create(
            analysis_request=analysis_request,
            generated_by=generated_by,
            label_count=label_count,
            family_count=len(family_names),
            pdf_file_key='',
        )

        tenant_code = _current_tenant_numeric_code()
        today = timezone.now().date()

        # Numbering-mode dispatch (Phase 4).
        #
        # PER_FAMILY  — pre-Phase-4 behaviour: one fresh sequence
        #               value per label. The allocator runs N times
        #               and every barcode is unique within the
        #               batch (and across batches, by the
        #               LabelSequence row lock).
        # SAME_REQUEST_NUMBER — one allocation per BATCH, reused
        #               on every RequestLabel row. Within-batch
        #               duplicates are intentional; cross-batch
        #               uniqueness still holds because each
        #               allocator call advances the sequence
        #               exactly once.
        #
        # The two modes share the same family-name assignment
        # rule: indices [0..family_count-1] take the family name in
        # order; the trailing extras carry the empty string so the
        # PDF renderer skips the family caption on those tubes.
        if numbering_mode == 'SAME_REQUEST_NUMBER':
            shared_code = _allocate_numeric_code(
                tenant_code, today, reset_mode=reset_mode,
            )
            for idx in range(1, label_count + 1):
                family_name = (
                    family_names[idx - 1]
                    if idx - 1 < len(family_names)
                    else ''
                )
                RequestLabel.objects.create(
                    batch=batch,
                    barcode_value=shared_code,
                    label_index=idx,
                    family_name=family_name,
                )
        else:  # PER_FAMILY (default)
            for idx in range(1, label_count + 1):
                numeric_code = _allocate_numeric_code(
                    tenant_code, today, reset_mode=reset_mode,
                )
                family_name = (
                    family_names[idx - 1]
                    if idx - 1 < len(family_names)
                    else ''
                )
                RequestLabel.objects.create(
                    batch=batch,
                    barcode_value=numeric_code,
                    label_index=idx,
                    family_name=family_name,
                )

        # Render PDF using the laboratory's effective label config.
        layout = lab_settings.to_label_layout_config()
        payloads = _build_payloads(batch)
        pdf_bytes = render_labels(payloads, layout)

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
            # Snapshot the labelling configuration that was in
            # effect at generation time. An audit reader can then
            # correlate any historical batch back to the exact
            # mode + extras + reset cadence the lab had configured
            # — useful when a tenant changes settings and operators
            # need to explain why a printed label looks different
            # from a sibling batch generated the day before.
            diff={'after': {
                'analysis_request_id': str(analysis_request.id),
                'request_number': analysis_request.request_number,
                'label_count': label_count,
                'family_count': len(family_names),
                'print_mode': layout.print_mode,
                'numbering_mode': numbering_mode,
                'extra_label_count': extra_count,
                'reset_period': reset_mode,
            }},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        logger.info(
            'Generated %d labels for request %s (mode=%s)',
            label_count, analysis_request.request_number, layout.print_mode,
        )
        return batch
