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

EXTRA_LABELS_BONUS = 2


class LabelCountStrategy:
    """Computes how many labels to produce for a given analysis request."""

    @staticmethod
    def compute(request: AnalysisRequest) -> tuple[int, list[str]]:
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


def _allocate_numeric_code(
    tenant_numeric_code: str, today: date,
) -> str:
    """
    Allocate one numeric label code ``TTTTYYMMSSSSSS``.

    Must be called inside ``transaction.atomic`` — the caller's
    ``select_for_update`` on the ``LabelSequence`` row serialises
    concurrent allocations for the same tenant+month.
    """
    LabelSequence.objects.get_or_create(year=today.year, month=today.month)
    seq = LabelSequence.objects.select_for_update().get(
        year=today.year, month=today.month,
    )
    next_value = seq.last_value + 1
    if next_value > LABEL_SEQUENCE_MAX:
        raise ValidationError(
            'Monthly label sequence exhausted for this tenant.'
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

        label_count, family_names = LabelCountStrategy.compute(analysis_request)

        batch = RequestLabelBatch.objects.create(
            analysis_request=analysis_request,
            generated_by=generated_by,
            label_count=label_count,
            family_count=len(family_names),
            pdf_file_key='',
        )

        tenant_code = _current_tenant_numeric_code()
        today = timezone.now().date()
        for idx in range(1, label_count + 1):
            numeric_code = _allocate_numeric_code(tenant_code, today)
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
        lab_settings = LabSettings.get_solo()
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
            diff={'after': {
                'analysis_request_id': str(analysis_request.id),
                'request_number': analysis_request.request_number,
                'label_count': label_count,
                'family_count': len(family_names),
                'print_mode': layout.print_mode,
            }},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        logger.info(
            'Generated %d labels for request %s (mode=%s)',
            label_count, analysis_request.request_number, layout.print_mode,
        )
        return batch
