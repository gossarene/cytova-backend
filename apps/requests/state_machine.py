"""
Cytova — Analysis Request State Machine

Enforces legal status transitions for AnalysisRequest and AnalysisRequestItem.
All status changes MUST go through these guards — never set .status directly
in views or serializers.

Transition graphs
─────────────────
AnalysisRequest:
    DRAFT → CONFIRMED → COLLECTION_IN_PROGRESS → IN_ANALYSIS → AWAITING_REVIEW
                     ↘─────────────────────────────↗               ↓
                     ↘ IN_PROGRESS → COMPLETED       READY_FOR_RELEASE ← all items validated
                                                     RETEST_REQUIRED   ← some items rejected
    READY_FOR_RELEASE → VALIDATED (explicit finalize-validation action)
    READY_FOR_RELEASE → AWAITING_REVIEW / RETEST_REQUIRED (item rejected before finalization)
    RETEST_REQUIRED → IN_ANALYSIS / AWAITING_REVIEW (re-entry cycle)
    VALIDATED → COMPLETED (future: after publishing)
    DRAFT → CANCELLED
    Most active states → CANCELLED

AnalysisRequestItem:
    PENDING → COLLECTED → RESULT_ENTERED → UNDER_REVIEW → VALIDATED
            ↘──────────↗                                ↘ RESULT_ENTERED (rejection re-entry)
            ↘ REJECTED                  ↘ IN_PROGRESS → COMPLETED (legacy)
    VALIDATED → COMPLETED (future: after publishing)
    PENDING / COLLECTED / RESULT_ENTERED / UNDER_REVIEW / IN_PROGRESS → REJECTED
"""
from rest_framework.exceptions import ValidationError

from .models import RequestStatus, ItemStatus


class RequestStateMachine:
    _TRANSITIONS: dict[str, set[str]] = {
        RequestStatus.DRAFT: {
            RequestStatus.CONFIRMED,
            RequestStatus.CANCELLED,
        },
        RequestStatus.CONFIRMED: {
            # New collection-driven path
            RequestStatus.COLLECTION_IN_PROGRESS,
            # Single-item requests transition straight to IN_ANALYSIS
            # when their only item is collected — the intermediate
            # "some collected, some pending" state does not exist.
            RequestStatus.IN_ANALYSIS,
            # Legacy direct-to-processing path, kept for backward compat
            # with the existing ``AnalysisRequestItemService.start`` flow.
            RequestStatus.IN_PROGRESS,
            # All-rejected-on-creation path: when every item is
            # rejected before collection starts, the auto-advance
            # helper collapses the request to COMPLETED (there is
            # nothing to analyse).
            RequestStatus.COMPLETED,
            RequestStatus.CANCELLED,
        },
        RequestStatus.COLLECTION_IN_PROGRESS: {
            RequestStatus.IN_ANALYSIS,
            RequestStatus.CANCELLED,
        },
        RequestStatus.IN_ANALYSIS: {
            RequestStatus.AWAITING_REVIEW,
            RequestStatus.CANCELLED,
        },
        RequestStatus.AWAITING_REVIEW: {
            RequestStatus.READY_FOR_RELEASE,
            RequestStatus.RETEST_REQUIRED,
            RequestStatus.IN_ANALYSIS,
            RequestStatus.COMPLETED,
            RequestStatus.CANCELLED,
        },
        RequestStatus.RETEST_REQUIRED: {
            RequestStatus.IN_ANALYSIS,
            RequestStatus.AWAITING_REVIEW,
            RequestStatus.CANCELLED,
        },
        RequestStatus.READY_FOR_RELEASE: {
            RequestStatus.VALIDATED,
            RequestStatus.AWAITING_REVIEW,
            RequestStatus.RETEST_REQUIRED,
            RequestStatus.CANCELLED,
        },
        RequestStatus.VALIDATED: {
            RequestStatus.COMPLETED,
            RequestStatus.CANCELLED,
        },
        RequestStatus.IN_PROGRESS: {
            RequestStatus.COMPLETED,
        },
        RequestStatus.COMPLETED: set(),
        RequestStatus.CANCELLED:  set(),
    }

    @classmethod
    def transition(cls, request, new_status: str) -> None:
        """
        Validate and apply a status transition on an AnalysisRequest instance.
        Raises ValidationError if the transition is illegal.
        The caller is responsible for saving the instance.
        """
        allowed = cls._TRANSITIONS.get(request.status, set())
        if new_status not in allowed:
            raise ValidationError(
                f"Cannot transition analysis request from "
                f"'{request.status}' to '{new_status}'. "
                f"Allowed transitions: {sorted(allowed) or 'none'}."
            )
        request.status = new_status


class ItemStateMachine:
    _TRANSITIONS: dict[str, set[str]] = {
        ItemStatus.PENDING: {
            # New collection path
            ItemStatus.COLLECTED,
            # Legacy direct-to-processing path, kept for backward compat.
            ItemStatus.IN_PROGRESS,
            ItemStatus.REJECTED,
        },
        ItemStatus.COLLECTED: {
            ItemStatus.RESULT_ENTERED,
            ItemStatus.IN_PROGRESS,
            ItemStatus.REJECTED,
        },
        ItemStatus.RESULT_ENTERED: {
            ItemStatus.UNDER_REVIEW,
            ItemStatus.REJECTED,
        },
        ItemStatus.UNDER_REVIEW: {
            ItemStatus.VALIDATED,
            ItemStatus.RESULT_ENTERED,
            ItemStatus.COMPLETED,
            ItemStatus.REJECTED,
        },
        ItemStatus.VALIDATED: {
            ItemStatus.COMPLETED,
        },
        ItemStatus.IN_PROGRESS: {
            ItemStatus.COMPLETED,
            ItemStatus.REJECTED,
        },
        ItemStatus.COMPLETED: set(),
        ItemStatus.REJECTED:  set(),
    }

    @classmethod
    def transition(cls, item, new_status: str) -> None:
        """
        Validate and apply a status transition on an AnalysisRequestItem.
        Raises ValidationError if the transition is illegal.
        The caller is responsible for saving the instance.
        """
        allowed = cls._TRANSITIONS.get(item.status, set())
        if new_status not in allowed:
            raise ValidationError(
                f"Cannot transition item from "
                f"'{item.status}' to '{new_status}'. "
                f"Allowed transitions: {sorted(allowed) or 'none'}."
            )
        item.status = new_status
