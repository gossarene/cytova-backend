"""
Cytova — Exam Result State Machine

Enforces legal status transitions for ExamResult.
All status changes MUST go through ResultStateMachine.transition() —
never set .status directly in views or services.

Transition graph:
    DRAFT → PENDING_VALIDATION  (submit for validation)
    PENDING_VALIDATION → VALIDATED   (biologist approves)
    PENDING_VALIDATION → DRAFT       (biologist rejects → back for revision)
    VALIDATED → PUBLISHED            (publish — IRREVERSIBLE)
    PUBLISHED → ∅                    (terminal, no further transitions)

CLAUDE.md constraint:
    "Result publishing is irreversible. Once an ExamResult is published,
     it cannot be edited. Enforce this in the status machine."
"""
from rest_framework.exceptions import ValidationError

from .models import ResultStatus


class ResultStateMachine:
    _TRANSITIONS: dict[str, set[str]] = {
        ResultStatus.DRAFT: {
            ResultStatus.PENDING_VALIDATION,
        },
        ResultStatus.PENDING_VALIDATION: {
            ResultStatus.VALIDATED,
            ResultStatus.DRAFT,
        },
        ResultStatus.VALIDATED: {
            ResultStatus.PUBLISHED,
        },
        ResultStatus.PUBLISHED: set(),  # TERMINAL — no outgoing transitions
    }

    @classmethod
    def transition(cls, result, new_status: str) -> None:
        """
        Validate and apply a status transition on an ExamResult instance.
        Raises ValidationError if the transition is illegal.

        Publishing to PUBLISHED is checked explicitly to surface a clear error
        message (rather than the generic "no allowed transitions" message).

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
