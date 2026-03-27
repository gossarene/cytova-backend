"""
Cytova — Analysis Request State Machine

Enforces legal status transitions for AnalysisRequest and AnalysisRequestItem.
All status changes MUST go through these guards — never set .status directly
in views or serializers.

Transition graphs
─────────────────
AnalysisRequest:
    DRAFT → CONFIRMED → IN_PROGRESS → COMPLETED
    DRAFT → CANCELLED
    CONFIRMED → CANCELLED

AnalysisRequestItem:
    PENDING → IN_PROGRESS → COMPLETED
    PENDING → REJECTED
    IN_PROGRESS → REJECTED
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
            RequestStatus.IN_PROGRESS,
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
            ItemStatus.IN_PROGRESS,
            ItemStatus.REJECTED,
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
