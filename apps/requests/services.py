"""
Cytova — Analysis Request Service

All write operations that carry business logic live here.
Views are thin: validate input → delegate to service.

AnalysisRequestService:
    create           — draft request + optional inline items + traceability stubs
    add_item         — append an item to a DRAFT request + traceability stub
    remove_item      — delete an item from a DRAFT request
    update           — update notes on a DRAFT request
    confirm          — lock items, transition request to CONFIRMED
    cancel           — cancel a DRAFT or CONFIRMED request

AnalysisRequestItemService:
    update           — update operational metadata on a DRAFT item
    start            — PENDING → IN_PROGRESS; populate traceability receipt fields
    complete         — IN_PROGRESS → COMPLETED; populate traceability completion fields
    reject           — PENDING|IN_PROGRESS → REJECTED; record rejection reason
    _auto_advance    — (internal) advance request status after item terminal transition
"""
import logging

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.audit.models import AuditLog, AuditAction, ActorType
from apps.catalog.models import ExamDefinition
from apps.users.models import StaffUser
from .models import (
    AnalysisRequest, AnalysisRequestItem, ExamTraceability,
    RequestLabelBatch, RequestReferenceSequence,
    RequestStatus, ItemStatus, ExecutionMode, PriceSource,
)
from .pricing import RequestPricingResolver
from .state_machine import RequestStateMachine, ItemStateMachine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public reference allocator
# ---------------------------------------------------------------------------

PUBLIC_REFERENCE_WIDTH = 6
PUBLIC_REFERENCE_MAX = 10 ** PUBLIC_REFERENCE_WIDTH - 1


def _allocate_public_reference(day) -> str:
    """
    Allocate one patient-facing reference of the form ``YYYYMMDD-NNNNNN``.

    Caller must be inside ``transaction.atomic`` — the ``select_for_update``
    on ``RequestReferenceSequence`` serialises overlapping creates for
    the same day so the sequence never collides.
    """
    RequestReferenceSequence.objects.get_or_create(date=day)
    seq = RequestReferenceSequence.objects.select_for_update().get(date=day)
    next_value = seq.last_value + 1
    if next_value > PUBLIC_REFERENCE_MAX:
        raise ValidationError(
            'Daily request reference sequence exhausted for this tenant.'
        )
    seq.last_value = next_value
    seq.save(update_fields=['last_value', 'updated_at'])
    return f'{day.strftime("%Y%m%d")}-{next_value:0{PUBLIC_REFERENCE_WIDTH}d}'


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _audit(*, actor: StaffUser, action: str, entity_type: str, entity_id,
           diff: dict, request) -> None:
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


def _resolve_item_pricing(item: AnalysisRequestItem, analysis_request: AnalysisRequest,
                           manual_billed_price=None) -> None:
    """
    Set pricing fields on a newly created or updated item.

    The decision tree for the 3-step request workflow is:

    1. ``execution_mode == REJECTED`` → zero prices, source=DEFAULT_PRICE.
       Rejected items bill zero regardless of any other configuration.
    2. ``manual_billed_price`` provided (legacy draft-edit escape hatch) →
       use it, source=MANUAL_OVERRIDE. The new 3-step flow does not pass
       this path; it exists only for the legacy direct-item-edit API.
    3. Otherwise → delegate to ``RequestPricingResolver``. The resolver
       enforces the authoritative rules:
           - DIRECT_PATIENT      → billed = unit_price
           - PARTNER_ORGANIZATION → billed = agreed_price if any else unit_price

    Note: ``PricingRule`` (catalog-level rule system) is intentionally NOT
    consulted here. The new workflow treats ``PartnerExamPrice`` as the
    single commercial-pricing reference for partner sources.
    """
    exam = item.exam_definition

    if item.execution_mode == ExecutionMode.REJECTED:
        item.unit_price = 0
        item.billed_price = 0
        item.pricing_rule = None
        item.price_source = PriceSource.DEFAULT_PRICE
        return

    if manual_billed_price is not None:
        item.unit_price = exam.unit_price
        item.billed_price = manual_billed_price
        item.pricing_rule = None
        item.price_source = PriceSource.MANUAL_OVERRIDE
        return

    # Default path — the new resolver. One resolver call per item is fine
    # here; bulk paths (preview, inline create) batch their lookups at the
    # service-method level.
    [resolved] = RequestPricingResolver.resolve(
        source_type=analysis_request.source_type,
        partner=analysis_request.partner_organization,
        exams=[exam],
    )
    item.unit_price = resolved.unit_price
    item.billed_price = resolved.billed_price
    item.pricing_rule = None
    item.price_source = resolved.price_source


def _create_item_with_traceability(
    analysis_request: AnalysisRequest,
    item_data: dict,
) -> AnalysisRequestItem:
    """
    Create an AnalysisRequestItem and its mandatory ExamTraceability stub
    in a single operation. Pricing is resolved immediately.
    Must be called inside an atomic transaction.
    """
    item = AnalysisRequestItem(
        analysis_request=analysis_request,
        exam_definition_id=item_data['exam_definition_id'],
        execution_mode=item_data.get('execution_mode', ExecutionMode.INTERNAL),
        rejection_reason=item_data.get('rejection_reason', ''),
        external_partner_name=item_data.get('external_partner_name', ''),
        notes=item_data.get('notes', ''),
    )
    # Need the exam_definition loaded for pricing
    if not hasattr(item, '_exam_definition_cache'):
        item.exam_definition = ExamDefinition.objects.get(id=item_data['exam_definition_id'])

    manual_billed = item_data.get('billed_price')
    _resolve_item_pricing(item, analysis_request, manual_billed_price=manual_billed)
    item.save()
    ExamTraceability.objects.create(item=item)
    return item


# ---------------------------------------------------------------------------
# AnalysisRequestService
# ---------------------------------------------------------------------------

class AnalysisRequestService:

    @staticmethod
    def preview_pricing(
        source_type: str,
        partner,
        exam_ids: list,
    ) -> list:
        """
        Resolve pricing for a set of exams under a given source WITHOUT
        persisting anything. Used by the Step 3 recap to surface the same
        values the final ``create`` call will snapshot.

        The guarantee "preview matches final" is structural: both code
        paths call ``RequestPricingResolver`` with the same inputs, so
        they cannot drift.
        """
        return RequestPricingResolver.resolve_for_ids(
            source_type=source_type,
            partner=partner,
            exam_ids=exam_ids,
        )

    @staticmethod
    @transaction.atomic
    def create(
        validated_data: dict,
        created_by: StaffUser,
        request,
        confirm_after: bool = False,
    ) -> AnalysisRequest:
        """
        Create an analysis request with optional inline items.

        By default the result is in ``DRAFT`` status — compatible with
        legacy draft-edit flows where items are added incrementally and
        the client confirms later via ``POST /requests/:id/confirm/``.

        When ``confirm_after=True`` the request is created AND transitioned
        to ``CONFIRMED`` in the same atomic transaction, reusing the
        existing ``AnalysisRequestService.confirm`` code path. This is the
        mode used by the 3-step creation wizard whose final button
        semantically means "commit this request". The decorator's
        atomicity guarantees that a failure during confirmation rolls
        back the whole create+confirm pair — there is no half-created
        draft orphan if the confirm step raises.
        """
        items_data = validated_data.pop('items', [])

        ar = AnalysisRequest(
            patient_id=validated_data['patient_id'],
            notes=validated_data.get('notes', ''),
            source_type=validated_data.get('source_type', 'DIRECT_PATIENT'),
            partner_organization_id=validated_data.get('partner_organization_id'),
            external_reference=validated_data.get('external_reference', ''),
            billing_mode=validated_data.get('billing_mode', 'DIRECT_PAYMENT'),
            source_notes=validated_data.get('source_notes', ''),
            created_by=created_by,
        )
        ar.save()

        # Assign the internal request_number (unchanged: REQ-YYYY-XXXXXXXX)
        # and the patient-facing public_reference (YYYYMMDD-NNNNNN). The
        # two live side by side: the UUID-derived number is stable and
        # useful for audit log grep, the public reference is the clean
        # one printed on final reports.
        uid_part = str(ar.id).replace('-', '')[:8].upper()
        ar.request_number = f'REQ-{ar.created_at.year}-{uid_part}'
        ar.public_reference = _allocate_public_reference(ar.created_at.date())
        ar.save(update_fields=['request_number', 'public_reference'])

        for item_data in items_data:
            _create_item_with_traceability(ar, item_data)

        _audit(
            actor=created_by,
            action=AuditAction.CREATE,
            entity_type='AnalysisRequest',
            entity_id=ar.id,
            diff={'after': {
                'request_number': ar.request_number,
                'patient_id': str(ar.patient_id),
                'source_type': ar.source_type,
                'partner_organization_id': str(ar.partner_organization_id) if ar.partner_organization_id else None,
                'billing_mode': ar.billing_mode,
                'items_count': len(items_data),
            }},
            request=request,
        )

        if confirm_after:
            # Reuse the existing confirm service verbatim so the audit
            # trail contains one CREATE entry followed by one CONFIRM
            # entry, and so any future change to confirm's rules (item
            # locking, state-machine coherence, auto-complete, etc.)
            # automatically applies to the 3-step flow without needing
            # a second code path to keep in sync.
            ar = AnalysisRequestService.confirm(
                analysis_request=ar,
                confirmed_by=created_by,
                request=request,
            )

        return ar

    @staticmethod
    @transaction.atomic
    def add_item(
        analysis_request: AnalysisRequest,
        validated_data: dict,
        added_by: StaffUser,
        request,
    ) -> AnalysisRequestItem:
        if analysis_request.status != RequestStatus.DRAFT:
            raise ValidationError('Items can only be added to a DRAFT request.')

        exam_id = validated_data['exam_definition_id']
        if analysis_request.items.filter(exam_definition_id=exam_id).exists():
            raise ValidationError('This exam is already in the request.')

        item = _create_item_with_traceability(analysis_request, validated_data)

        _audit(
            actor=added_by,
            action=AuditAction.UPDATE,
            entity_type='AnalysisRequest',
            entity_id=analysis_request.id,
            diff={'after': {
                'action': 'item_added',
                'exam_definition_id': str(exam_id),
                'item_id': str(item.id),
            }},
            request=request,
        )

        return item

    @staticmethod
    @transaction.atomic
    def remove_item(
        analysis_request: AnalysisRequest,
        item: AnalysisRequestItem,
        removed_by: StaffUser,
        request,
    ) -> None:
        if analysis_request.status != RequestStatus.DRAFT:
            raise ValidationError('Items can only be removed from a DRAFT request.')

        exam_id = item.exam_definition_id
        item_id = item.id

        # Bypass model-level delete guard via queryset
        AnalysisRequestItem.objects.filter(id=item.id).delete()

        _audit(
            actor=removed_by,
            action=AuditAction.UPDATE,
            entity_type='AnalysisRequest',
            entity_id=analysis_request.id,
            diff={'after': {
                'action': 'item_removed',
                'exam_definition_id': str(exam_id),
                'item_id': str(item_id),
            }},
            request=request,
        )

    @staticmethod
    def update(
        analysis_request: AnalysisRequest,
        validated_data: dict,
        updated_by: StaffUser,
        request,
    ) -> AnalysisRequest:
        if analysis_request.status != RequestStatus.DRAFT:
            raise ValidationError('Only DRAFT requests can be updated.')
        if not validated_data:
            return analysis_request

        before = {k: getattr(analysis_request, k) for k in validated_data}
        for field, value in validated_data.items():
            setattr(analysis_request, field, value)
        analysis_request.save(update_fields=list(validated_data.keys()) + ['updated_at'])
        after = {k: getattr(analysis_request, k) for k in validated_data}

        # Ensure all values are JSON-serializable (UUIDs → str)
        def _safe(v):
            from uuid import UUID
            return str(v) if isinstance(v, UUID) else v

        _audit(
            actor=updated_by,
            action=AuditAction.UPDATE,
            entity_type='AnalysisRequest',
            entity_id=analysis_request.id,
            diff={
                'before': {k: _safe(v) for k, v in before.items()},
                'after': {k: _safe(v) for k, v in after.items()},
            },
            request=request,
        )

        return analysis_request

    @staticmethod
    @transaction.atomic
    def confirm(
        analysis_request: AnalysisRequest,
        confirmed_by: StaffUser,
        request,
    ) -> AnalysisRequest:
        """
        Lock the request: transition DRAFT → CONFIRMED.

        Pricing is already set at item creation time. Confirmation:
        - Validates at least one item exists.
        - Transitions REJECTED-mode items to status=REJECTED with zero prices.
        - Locks all item prices (no further changes allowed).
        - Auto-completes the request if every item is terminal.
        """
        RequestStateMachine.transition(analysis_request, RequestStatus.CONFIRMED)

        items = list(analysis_request.items.select_related('exam_definition').all())
        if not items:
            raise ValidationError(
                'Cannot confirm a request with no items. Add at least one exam.'
            )

        for item in items:
            if item.execution_mode == ExecutionMode.REJECTED:
                item.status = ItemStatus.REJECTED
                item.unit_price = 0
                item.billed_price = 0
                item.price_source = PriceSource.DEFAULT_PRICE
                item.pricing_rule = None
                item.save(update_fields=[
                    'status', 'unit_price', 'billed_price', 'price_source',
                    'pricing_rule', 'updated_at',
                ])

        analysis_request.confirmed_at = timezone.now()
        analysis_request.confirmed_by = confirmed_by

        # Auto-complete if every item was pre-rejected
        all_terminal = not analysis_request.items.filter(
            status__in=[ItemStatus.PENDING, ItemStatus.IN_PROGRESS]
        ).exists()
        if all_terminal:
            RequestStateMachine.transition(analysis_request, RequestStatus.COMPLETED)

        analysis_request.save(update_fields=[
            'status', 'confirmed_at', 'confirmed_by', 'updated_at',
        ])

        _audit(
            actor=confirmed_by,
            action=AuditAction.CONFIRM,
            entity_type='AnalysisRequest',
            entity_id=analysis_request.id,
            diff={'after': {
                'status': analysis_request.status,
                'items_confirmed': len(
                    [i for i in items if i.execution_mode != ExecutionMode.REJECTED]
                ),
                'items_auto_rejected': len(
                    [i for i in items if i.execution_mode == ExecutionMode.REJECTED]
                ),
            }},
            request=request,
        )

        return analysis_request

    @staticmethod
    @transaction.atomic
    def cancel(
        analysis_request: AnalysisRequest,
        cancelled_by: StaffUser,
        request,
    ) -> AnalysisRequest:
        RequestStateMachine.transition(analysis_request, RequestStatus.CANCELLED)

        analysis_request.cancelled_at = timezone.now()
        analysis_request.cancelled_by = cancelled_by
        analysis_request.save(update_fields=[
            'status', 'cancelled_at', 'cancelled_by', 'updated_at',
        ])

        _audit(
            actor=cancelled_by,
            action=AuditAction.CANCEL,
            entity_type='AnalysisRequest',
            entity_id=analysis_request.id,
            diff={'after': {'status': RequestStatus.CANCELLED}},
            request=request,
        )

        return analysis_request

    @staticmethod
    @transaction.atomic
    def finalize_validation(
        analysis_request: AnalysisRequest,
        finalized_by: StaffUser,
        request,
    ) -> AnalysisRequest:
        """
        Explicit biologist action to finalize the request after all
        items have been individually validated.

        Only allowed when the request is in READY_FOR_RELEASE.
        Transitions the request to VALIDATED, which locks it against
        further review modifications.
        """
        if analysis_request.status != RequestStatus.READY_FOR_RELEASE:
            raise ValidationError(
                'Request can only be finalized when all items are '
                'validated and the request is in Ready For Release state '
                f'(current: {analysis_request.status}).'
            )

        RequestStateMachine.transition(
            analysis_request, RequestStatus.VALIDATED,
        )
        analysis_request.save(update_fields=['status', 'updated_at'])

        _audit(
            actor=finalized_by,
            action=AuditAction.VALIDATE,
            entity_type='AnalysisRequest',
            entity_id=analysis_request.id,
            diff={
                'before': {'status': RequestStatus.READY_FOR_RELEASE},
                'after': {
                    'status': RequestStatus.VALIDATED,
                    'finalized_by': str(finalized_by.id),
                },
            },
            request=request,
        )

        return analysis_request

    @staticmethod
    @transaction.atomic
    def mark_delivered(
        analysis_request: AnalysisRequest,
        actor: StaffUser,
        request,
    ) -> AnalysisRequest:
        """Mark a request's closure as DELIVERED. Workflow ``status`` is
        deliberately untouched — billing keeps querying the same VALIDATED
        rows it always did.

        Idempotent on already-delivered requests; once ARCHIVED, a request
        cannot be re-delivered (closure is a terminal forward sequence:
        OPEN → DELIVERED → ARCHIVED).
        """
        from .models import ClosureStatus

        if analysis_request.closure_status == ClosureStatus.DELIVERED:
            return analysis_request
        if analysis_request.closure_status == ClosureStatus.ARCHIVED:
            raise ValidationError(
                'Cannot mark an archived request as delivered.'
            )

        # Report must exist before closure transitions. The PDF is what the
        # patient ultimately receives, so closing the request without one
        # would leave the workflow in a half-finished state. Idempotent
        # short-circuit above means already-delivered rows aren't penalised
        # by this check (their report was required at the original action).
        if not analysis_request.reports.filter(is_current=True).exists():
            raise ValidationError(
                'Generate the report before marking this request as '
                'delivered or archived.'
            )

        previous = analysis_request.closure_status
        analysis_request.closure_status = ClosureStatus.DELIVERED
        analysis_request.delivered_at = timezone.now()
        analysis_request.delivered_by = actor
        analysis_request.save(update_fields=[
            'closure_status', 'delivered_at', 'delivered_by', 'updated_at',
        ])

        _audit(
            actor=actor,
            action=AuditAction.UPDATE,
            entity_type='AnalysisRequest',
            entity_id=analysis_request.id,
            diff={
                'closure_from': previous,
                'closure_to': ClosureStatus.DELIVERED.value,
                'reason': 'manual_mark_delivered',
            },
            request=request,
        )
        return analysis_request

    @staticmethod
    @transaction.atomic
    def archive(
        analysis_request: AnalysisRequest,
        actor: StaffUser,
        request,
    ) -> AnalysisRequest:
        """Set a request's closure to ARCHIVED. Workflow ``status`` is
        deliberately untouched.

        Idempotent on already-archived requests; ARCHIVED is terminal.
        """
        from .models import ClosureStatus

        if analysis_request.closure_status == ClosureStatus.ARCHIVED:
            return analysis_request

        # Same gate as ``mark_delivered``: a request without a generated
        # report is not in a state that should be archived. See the
        # docstring on ``mark_delivered`` for the rationale.
        if not analysis_request.reports.filter(is_current=True).exists():
            raise ValidationError(
                'Generate the report before marking this request as '
                'delivered or archived.'
            )

        previous = analysis_request.closure_status
        analysis_request.closure_status = ClosureStatus.ARCHIVED
        analysis_request.archived_at = timezone.now()
        analysis_request.archived_by = actor
        analysis_request.save(update_fields=[
            'closure_status', 'archived_at', 'archived_by', 'updated_at',
        ])

        _audit(
            actor=actor,
            action=AuditAction.UPDATE,
            entity_type='AnalysisRequest',
            entity_id=analysis_request.id,
            diff={
                'closure_from': previous,
                'closure_to': ClosureStatus.ARCHIVED.value,
                'reason': 'manual_archive',
            },
            request=request,
        )
        return analysis_request


# ---------------------------------------------------------------------------
# AnalysisRequestItemService
# ---------------------------------------------------------------------------

class AnalysisRequestItemService:

    @staticmethod
    def update(
        item: AnalysisRequestItem,
        validated_data: dict,
        updated_by: StaffUser,
        request,
    ) -> AnalysisRequestItem:
        if item.analysis_request.status != RequestStatus.DRAFT:
            raise ValidationError(
                'Items can only be updated while the request is in DRAFT.'
            )
        if not validated_data:
            return item

        before = {k: str(getattr(item, k)) for k in validated_data}

        # Extract billed_price for special handling
        manual_billed = validated_data.pop('billed_price', _UNSET)

        # Apply simple field updates
        for field, value in validated_data.items():
            setattr(item, field, value)

        # Re-resolve pricing if execution_mode changed, or handle manual override
        needs_reprice = 'execution_mode' in validated_data
        if manual_billed is not _UNSET:
            # Explicit manual override (or null = re-resolve)
            _resolve_item_pricing(
                item, item.analysis_request,
                manual_billed_price=manual_billed,
            )
        elif needs_reprice:
            # execution_mode changed — re-resolve automatically
            _resolve_item_pricing(item, item.analysis_request)

        item.save()

        after = {}
        for k in list(validated_data.keys()) + (['billed_price'] if manual_billed is not _UNSET else []):
            after[k] = str(getattr(item, k))

        _audit(
            actor=updated_by,
            action=AuditAction.UPDATE,
            entity_type='AnalysisRequestItem',
            entity_id=item.id,
            diff={'before': before, 'after': after},
            request=request,
        )

        return item

    @staticmethod
    @transaction.atomic
    def start(
        item: AnalysisRequestItem,
        started_by: StaffUser,
        request,
    ) -> AnalysisRequestItem:
        """
        Transition item PENDING → IN_PROGRESS.
        Populates traceability with sample receipt timestamp and receiver.
        Advances parent request CONFIRMED → IN_PROGRESS if it is the first item starting.
        """
        ar = item.analysis_request
        if ar.status not in {RequestStatus.CONFIRMED, RequestStatus.IN_PROGRESS}:
            raise ValidationError(
                'Item processing can only start when the request is CONFIRMED or IN_PROGRESS.'
            )

        ItemStateMachine.transition(item, ItemStatus.IN_PROGRESS)
        item.save(update_fields=['status', 'updated_at'])

        traceability = item.traceability
        traceability.sample_received_at = timezone.now()
        traceability.sample_received_by = started_by
        traceability.save(update_fields=['sample_received_at', 'sample_received_by'])

        if ar.status == RequestStatus.CONFIRMED:
            RequestStateMachine.transition(ar, RequestStatus.IN_PROGRESS)
            ar.save(update_fields=['status', 'updated_at'])

        _audit(
            actor=started_by,
            action=AuditAction.UPDATE,
            entity_type='AnalysisRequestItem',
            entity_id=item.id,
            diff={'before': {'status': ItemStatus.PENDING},
                  'after': {'status': ItemStatus.IN_PROGRESS}},
            request=request,
        )

        return item

    @staticmethod
    @transaction.atomic
    def complete(
        item: AnalysisRequestItem,
        completed_by: StaffUser,
        request,
    ) -> AnalysisRequestItem:
        """
        Transition item IN_PROGRESS → COMPLETED.
        Populates traceability with completion timestamp and performer.
        Auto-completes the parent request if all items are terminal.
        """
        ItemStateMachine.transition(item, ItemStatus.COMPLETED)
        item.save(update_fields=['status', 'updated_at'])

        traceability = item.traceability
        traceability.analysis_completed_at = timezone.now()
        traceability.performed_by = completed_by
        traceability.save(update_fields=['analysis_completed_at', 'performed_by'])

        _audit(
            actor=completed_by,
            action=AuditAction.UPDATE,
            entity_type='AnalysisRequestItem',
            entity_id=item.id,
            diff={'before': {'status': ItemStatus.IN_PROGRESS},
                  'after': {'status': ItemStatus.COMPLETED}},
            request=request,
        )

        AnalysisRequestItemService._auto_advance(item.analysis_request, completed_by, request)
        return item

    @staticmethod
    @transaction.atomic
    def reject(
        item: AnalysisRequestItem,
        rejection_reason: str,
        rejected_by: StaffUser,
        request,
    ) -> AnalysisRequestItem:
        """
        Transition item PENDING|IN_PROGRESS → REJECTED.
        Auto-completes the parent request if all items are terminal.
        """
        prev_status = item.status
        ItemStateMachine.transition(item, ItemStatus.REJECTED)
        item.rejection_reason = rejection_reason
        item.save(update_fields=['status', 'rejection_reason', 'updated_at'])

        _audit(
            actor=rejected_by,
            action=AuditAction.UPDATE,
            entity_type='AnalysisRequestItem',
            entity_id=item.id,
            diff={'before': {'status': prev_status},
                  'after': {'status': ItemStatus.REJECTED,
                             'rejection_reason': rejection_reason}},
            request=request,
        )

        AnalysisRequestItemService._auto_advance(item.analysis_request, rejected_by, request)
        # A rejection before collection can change the "all active
        # items collected" count, so re-derive the request-level
        # status the same way mark_collected would.
        AnalysisRequestItemService._refresh_collection_status(
            item.analysis_request, actor=rejected_by, request=request,
        )
        return item

    @staticmethod
    @transaction.atomic
    def mark_collected(
        item: AnalysisRequestItem,
        collected_by: StaffUser,
        request,
        collection_notes: str = '',
    ) -> AnalysisRequestItem:
        """
        Mark an analysis request item as collected.

        Conceptual permission: ``requests.collection_mark`` — enforced
        in the view layer by ``IsTechnicianOrAbove``, the class-based
        equivalent in this project's RBAC system.

        Semantics:
            - PENDING → COLLECTED (via the state machine)
            - Writes ``collected_at`` + ``collected_by`` as a permanent
              traceability record.
            - Triggers a centralized refresh of the parent request's
              status via ``_refresh_collection_status``.

        Idempotency:
            - If the item is already COLLECTED, the call is a safe
              no-op and returns the item unchanged. No duplicate audit
              entry, no overwrite of the original ``collected_at`` /
              ``collected_by``.

        Guardrails:
            - Only items whose parent request is in a collection-eligible
              state (CONFIRMED or COLLECTION_IN_PROGRESS) may be
              collected. Drafts, cancelled, already-in-analysis, or
              fully-completed requests reject.
            - Only items in PENDING status may be marked collected.
              Items already in IN_PROGRESS, COMPLETED, or REJECTED
              cannot be walked back to COLLECTED — that would rewrite
              the traceability chain.
        """
        ar = item.analysis_request

        # Idempotency check FIRST — a re-invocation on an
        # already-collected item must be a safe no-op regardless of
        # the current request status. For a single-item request the
        # first call transitions the parent to IN_ANALYSIS, so without
        # this ordering the second call would fail the request-state
        # guard below even though the item itself has not moved.
        if item.status == ItemStatus.COLLECTED:
            return item

        if ar.status not in {
            RequestStatus.CONFIRMED,
            RequestStatus.COLLECTION_IN_PROGRESS,
        }:
            raise ValidationError(
                'Specimens can only be collected while the request is '
                'CONFIRMED or COLLECTION_IN_PROGRESS '
                f"(current status: {ar.status}).",
            )

        # Traceability gate — two-stage check.
        #
        #   1. Labels must EXIST. Without a generated batch the
        #      operator has no barcoded tubes, so collection has
        #      nothing to scan against.
        #   2. The PDF must have been DOWNLOADED at least once.
        #      Generation alone doesn't put labels on physical
        #      tubes — the operator has to print them. Without the
        #      download we'd be marking specimens "collected" on
        #      tubes that nobody has the labels for, breaking the
        #      scan-based traceability chain at the next step.
        #
        # Both checks raise ``ValidationError`` with the exact
        # phrasing the frontend's helper text quotes — so the toast
        # the operator sees on a backend-rejected attempt matches
        # the inline guidance under the disabled CTA. The two
        # messages are deliberately distinct so the operator knows
        # whether the next step is "generate labels" or "download
        # labels".
        batch = RequestLabelBatch.objects.filter(
            analysis_request=ar,
        ).only('download_count').first()
        if batch is None:
            raise ValidationError(
                'Labels must be generated for this request before specimens '
                'can be marked as collected.'
            )
        if batch.download_count == 0:
            raise ValidationError(
                'Labels must be downloaded before specimens can be marked '
                'as collected.'
            )

        if item.status != ItemStatus.PENDING:
            raise ValidationError(
                f'Only pending items can be marked as collected '
                f"(item status: {item.status}).",
            )

        ItemStateMachine.transition(item, ItemStatus.COLLECTED)
        item.collected_at = timezone.now()
        item.collected_by = collected_by
        if collection_notes:
            item.collection_notes = collection_notes
        item.save(update_fields=[
            'status', 'collected_at', 'collected_by',
            'collection_notes', 'updated_at',
        ])

        _audit(
            actor=collected_by,
            action=AuditAction.UPDATE,
            entity_type='AnalysisRequestItem',
            entity_id=item.id,
            diff={
                'before': {'status': ItemStatus.PENDING},
                'after': {
                    'status': ItemStatus.COLLECTED,
                    'collected_at': item.collected_at.isoformat(),
                    'collected_by': str(collected_by.id),
                },
            },
            request=request,
        )

        AnalysisRequestItemService._refresh_collection_status(
            ar, actor=collected_by, request=request,
        )
        return item

    @staticmethod
    def _refresh_collection_status(
        analysis_request: AnalysisRequest,
        actor: StaffUser,
        request,
    ) -> None:
        """
        Derive the parent request's status from the current collection
        progress of its items. This is the **single place** where the
        request lifecycle rule
        "`CONFIRMED` ↔ `COLLECTION_IN_PROGRESS` ↔ `IN_ANALYSIS`" lives,
        so the rule cannot drift between code paths.

        Rules (rejected items are excluded from both numerator and
        denominator — they are operationally "done" and do not block
        progress):

            active = items where status != REJECTED
            collected = items where status in (COLLECTED, IN_PROGRESS, COMPLETED)

            if not active          → no change (nothing to analyse)
            if all active collected → request → IN_ANALYSIS
            if some collected      → request → COLLECTION_IN_PROGRESS
            else                    → no change

        A transition is only attempted if the target status is different
        from the current one, so repeated calls on a stable population
        are cheap no-ops.
        """
        items = list(analysis_request.items.all())
        active = [i for i in items if i.status != ItemStatus.REJECTED]
        if not active:
            return

        # Items that have reached or passed the collection milestone.
        # All states beyond PENDING satisfy the "has been collected" predicate.
        collected_or_beyond = {
            ItemStatus.COLLECTED, ItemStatus.RESULT_ENTERED,
            ItemStatus.UNDER_REVIEW, ItemStatus.VALIDATED,
            ItemStatus.IN_PROGRESS, ItemStatus.COMPLETED,
        }
        collected = [i for i in active if i.status in collected_or_beyond]

        def _transition_to(target: str) -> None:
            if analysis_request.status == target:
                return
            prev = analysis_request.status
            RequestStateMachine.transition(analysis_request, target)
            analysis_request.save(update_fields=['status', 'updated_at'])
            _audit(
                actor=actor,
                action=AuditAction.UPDATE,
                entity_type='AnalysisRequest',
                entity_id=analysis_request.id,
                diff={
                    'before': {'status': prev},
                    'after': {
                        'status': target,
                        'reason': 'collection_progress',
                    },
                },
                request=request,
            )

        if len(collected) == len(active):
            _transition_to(RequestStatus.IN_ANALYSIS)
        elif collected:
            _transition_to(RequestStatus.COLLECTION_IN_PROGRESS)
        # else: zero collected → stay put (CONFIRMED by contract)

    @staticmethod
    def _auto_advance(
        analysis_request: AnalysisRequest,
        actor: StaffUser,
        request,
    ) -> None:
        """
        Complete the parent request when all items have reached a terminal
        status (COMPLETED or REJECTED). No-op if any item is still active.
        """
        if analysis_request.status not in {
            RequestStatus.IN_PROGRESS, RequestStatus.CONFIRMED,
        }:
            return

        has_active = analysis_request.items.filter(
            status__in=[
                ItemStatus.PENDING, ItemStatus.COLLECTED,
                ItemStatus.RESULT_ENTERED, ItemStatus.UNDER_REVIEW,
                ItemStatus.VALIDATED, ItemStatus.IN_PROGRESS,
            ]
        ).exists()
        if has_active:
            return

        RequestStateMachine.transition(analysis_request, RequestStatus.COMPLETED)
        analysis_request.save(update_fields=['status', 'updated_at'])

        _audit(
            actor=actor,
            action=AuditAction.UPDATE,
            entity_type='AnalysisRequest',
            entity_id=analysis_request.id,
            diff={'after': {'status': RequestStatus.COMPLETED,
                            'reason': 'all_items_terminal'}},
            request=request,
        )


# Sentinel for distinguishing "not in payload" from explicit null
_UNSET = object()
