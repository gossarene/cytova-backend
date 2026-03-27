"""
Cytova — Analysis Request Service

All write operations that carry business logic live here.
Views are thin: validate input → delegate to service.

AnalysisRequestService:
    create           — draft request + optional inline items + traceability stubs
    add_item         — append an item to a DRAFT request + traceability stub
    remove_item      — delete an item from a DRAFT request
    update           — update notes on a DRAFT request
    confirm          — snapshot prices, transition items/request to CONFIRMED
    cancel           — cancel a DRAFT or CONFIRMED request

AnalysisRequestItemService:
    update           — update operational metadata on a DRAFT item
    start            — PENDING → IN_PROGRESS; populate traceability receipt fields
    complete         — IN_PROGRESS → COMPLETED; populate traceability completion fields
    reject           — PENDING|IN_PROGRESS → REJECTED; record rejection reason
    _auto_advance    — (internal) advance request status after item terminal transition
"""
import logging
from datetime import date

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.audit.models import AuditLog, AuditAction, ActorType
from apps.catalog.models import PricingRule
from apps.users.models import StaffUser
from .models import (
    AnalysisRequest, AnalysisRequestItem, ExamTraceability,
    RequestStatus, ItemStatus, ExecutionMode,
)
from .state_machine import RequestStateMachine, ItemStateMachine

logger = logging.getLogger(__name__)


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


def _active_pricing_rule(exam_definition) -> PricingRule | None:
    """
    Return the currently active pricing rule for an exam definition:
      effective_from <= today  AND  (effective_to IS NULL OR effective_to > today)
    Returns None if no such rule exists.
    """
    today = date.today()
    return (
        PricingRule.objects
        .filter(exam_definition=exam_definition, effective_from__lte=today)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=today))
        .order_by('-effective_from')
        .first()
    )


def _create_item_with_traceability(
    analysis_request: AnalysisRequest,
    item_data: dict,
) -> AnalysisRequestItem:
    """
    Create an AnalysisRequestItem and its mandatory ExamTraceability stub
    in a single operation. Must be called inside an atomic transaction.
    """
    item = AnalysisRequestItem(
        analysis_request=analysis_request,
        exam_definition_id=item_data['exam_definition_id'],
        execution_mode=item_data.get('execution_mode', ExecutionMode.INTERNAL),
        rejection_reason=item_data.get('rejection_reason', ''),
        external_partner_name=item_data.get('external_partner_name', ''),
        notes=item_data.get('notes', ''),
    )
    item.save()
    ExamTraceability.objects.create(item=item)
    return item


# ---------------------------------------------------------------------------
# AnalysisRequestService
# ---------------------------------------------------------------------------

class AnalysisRequestService:

    @staticmethod
    @transaction.atomic
    def create(
        validated_data: dict,
        created_by: StaffUser,
        request,
    ) -> AnalysisRequest:
        """
        Create a DRAFT analysis request with optional inline items.
        request_number is assigned immediately after the first save.
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

        # Assign human-readable request number using the first 8 chars of the UUID
        uid_part = str(ar.id).replace('-', '')[:8].upper()
        ar.request_number = f'REQ-{ar.created_at.year}-{uid_part}'
        ar.save(update_fields=['request_number'])

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
        Lock the request and snapshot prices from active pricing rules.

        Rules:
        - Request must be DRAFT with at least one item.
        - Every INTERNAL/SUBCONTRACTED item must have an active pricing rule.
        - Items with execution_mode=REJECTED receive zero prices and are
          immediately transitioned to status=REJECTED.
        - If every item ends up REJECTED the request is auto-completed.
        """
        RequestStateMachine.transition(analysis_request, RequestStatus.CONFIRMED)

        items = list(analysis_request.items.select_related('exam_definition').all())
        if not items:
            raise ValidationError(
                'Cannot confirm a request with no items. Add at least one exam.'
            )

        missing_prices = []
        for item in items:
            if item.execution_mode == ExecutionMode.REJECTED:
                item.status = ItemStatus.REJECTED
                item.unit_price = 0
                item.billed_price = 0
                item.save(update_fields=['status', 'unit_price', 'billed_price', 'updated_at'])
                continue

            rule = _active_pricing_rule(item.exam_definition)
            if rule is None:
                missing_prices.append(item.exam_definition.code)
                continue

            item.unit_price = rule.unit_price
            item.billed_price = rule.billed_price
            item.pricing_rule = rule
            item.status = ItemStatus.PENDING
            item.save(update_fields=[
                'unit_price', 'billed_price', 'pricing_rule', 'status', 'updated_at',
            ])

        if missing_prices:
            raise ValidationError(
                f'No active pricing rule found for: {", ".join(missing_prices)}. '
                f'Add a pricing rule before confirming.'
            )

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

        before = {k: getattr(item, k) for k in validated_data}
        for field, value in validated_data.items():
            setattr(item, field, value)
        item.save(update_fields=list(validated_data.keys()) + ['updated_at'])
        after = {k: getattr(item, k) for k in validated_data}

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
        return item

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
            status__in=[ItemStatus.PENDING, ItemStatus.IN_PROGRESS]
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
