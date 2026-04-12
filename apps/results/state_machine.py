"""
Cytova — Result Version State Machine

Enforces legal status transitions for ResultVersion.
All status changes MUST go through ResultStateMachine.transition() —
never set .status directly in views or services.

Transition graph:
    DRAFT → SUBMITTED          (technician submits for review)
    SUBMITTED → VALIDATED      (biologist approves)
    SUBMITTED → REJECTED       (biologist rejects — terminal for this version)
    VALIDATED → PUBLISHED      (publish — IRREVERSIBLE)
    REJECTED → ∅               (terminal — create a new version instead)
    PUBLISHED → ∅              (terminal, no further transitions)

CLAUDE.md constraint:
    "Result publishing is irreversible. Once an ExamResult is published,
     it cannot be edited. Enforce this in the status machine."
"""
from rest_framework.exceptions import ValidationError

from .models import ResultStatus


class ResultStateMachine:
    _TRANSITIONS: dict[str, set[str]] = {
        ResultStatus.DRAFT: {
            ResultStatus.SUBMITTED,
        },
        ResultStatus.SUBMITTED: {
            ResultStatus.VALIDATED,
            ResultStatus.REJECTED,
        },
        ResultStatus.VALIDATED: {
            ResultStatus.PUBLISHED,
        },
        ResultStatus.REJECTED:  set(),
        ResultStatus.PUBLISHED: set(),
    }

    @classmethod
    def transition(cls, result, new_status: str) -> None:
        """
        Validate and apply a status transition on a ResultVersion instance.
        Raises ValidationError if the transition is illegal.
        The caller is responsible for saving the instance after this call.
        """
        if result.status == ResultStatus.PUBLISHED:
            raise ValidationError(
                'Published results are immutable. '
                'No further status changes are permitted.'
            )

        allowed = cls._TRANSITIONS.get(result.status, set())
        if new_status not in allowed:
            raise ValidationError(
                f"Cannot transition result from '{result.status}' to '{new_status}'. "
                f"Allowed: {sorted(allowed) or 'none'}."
            )

        result.status = new_status
