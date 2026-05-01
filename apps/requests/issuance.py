"""
Cytova — Result issuance helpers.

Centralises the lifecycle hooks that turn a VALIDATED request into a
RESULT_ISSUED one. Every patient-facing notification path (email blast,
secure access-link generation, Cytova share) calls into this module so
the rules stay enforced in one place:

  - The first patient-facing notification of any channel transitions
    the request from VALIDATED → RESULT_ISSUED. Subsequent
    notifications stay in RESULT_ISSUED — the helper is idempotent.

  - After issuance, every re-notification must carry an explicit
    ``force_resend=True`` flag. The default ``force_resend=False``
    raises ``AlreadyIssued`` so the lab UI can surface the
    confirmation modal.

  - ``mark_request_issued`` writes a single ``RESULT_ISSUED`` audit
    row; ``enforce_resend_gate`` (when allowing a forced resend)
    writes ``RESULT_REISSUED``.

The audit log NEVER carries patient PII or storage paths — only the
request id, channel name, and actor.
"""
from __future__ import annotations

import logging
from typing import Optional

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.audit.models import ActorType, AuditAction, AuditLog

from .models import AnalysisRequest, RequestStatus
from .state_machine import RequestStateMachine

logger = logging.getLogger(__name__)


# Channel names recorded in the audit metadata. Free-form strings —
# kept as constants here so the four call sites can't drift.
CHANNEL_EMAIL = 'EMAIL'
CHANNEL_SHARE_LINK = 'SHARE_LINK'
CHANNEL_CYTOVA = 'CYTOVA'


class AlreadyIssued(ValidationError):
    """Raised when a re-notification is attempted on an already-issued
    request without ``force_resend=True``. Inherits from DRF's
    ``ValidationError`` so the existing 400 envelope path serialises
    it cleanly without view-level wrapping. The view layer can still
    intercept by ``isinstance`` to swap the status code (the spec
    suggests 409, but we keep 400 here for envelope-shape consistency
    with every other rejection in the requests viewset)."""

    code = 'ALREADY_ISSUED'

    def __init__(self):
        super().__init__({
            'code': self.code,
            'message': 'This result has already been issued. '
                       'Pass ``force_resend=true`` to re-send.',
        })


def mark_request_issued(
    *,
    analysis_request: AnalysisRequest,
    channel: str,
    actor=None,
    request=None,
) -> bool:
    """Promote the request to RESULT_ISSUED on the FIRST patient-facing
    notification. Idempotent — subsequent calls return ``False`` and
    leave the row alone.

    Returns ``True`` when the transition actually fired, ``False``
    when the request was already issued (or never reached VALIDATED in
    the first place — defensive: the caller's notify path should have
    refused earlier).
    """
    # Re-fetch status to avoid acting on stale in-memory state when a
    # parallel request may have already issued.
    analysis_request.refresh_from_db(fields=['status'])

    if analysis_request.status == RequestStatus.RESULT_ISSUED:
        return False

    if analysis_request.status != RequestStatus.VALIDATED:
        # Defensive — issuance must follow validation. The notify
        # paths already enforce VALIDATED; if we get here from
        # elsewhere we want a loud no-op rather than a silent
        # transition that violates the state machine.
        logger.warning(
            'mark_request_issued skipped — unexpected source status: '
            'request_id=%s status=%s channel=%s',
            analysis_request.id, analysis_request.status, channel,
        )
        return False

    RequestStateMachine.transition(
        analysis_request, RequestStatus.RESULT_ISSUED,
    )
    actor_pk = (
        actor.pk if actor and getattr(actor, 'is_authenticated', True)
        else None
    )
    analysis_request.issued_at = timezone.now()
    analysis_request.issued_by_id = actor_pk
    analysis_request.save(update_fields=[
        'status', 'issued_at', 'issued_by', 'updated_at',
    ])

    _write_audit(
        analysis_request=analysis_request,
        action=AuditAction.RESULT_ISSUED,
        channel=channel,
        actor=actor,
        request=request,
    )
    logger.info(
        'Result issued: request_id=%s channel=%s',
        analysis_request.id, channel,
    )
    return True


def enforce_resend_gate(
    *,
    analysis_request: AnalysisRequest,
    channel: str,
    force_resend: bool,
    actor=None,
    request=None,
) -> bool:
    """Gatekeeper for re-notification on an already-issued request.

    Returns
    -------
    ``True`` if a re-notification should proceed (either the request
    isn't issued yet, or the caller explicitly forced).
    ``True`` *also* when the request isn't issued — this helper is the
    single decision point so callers don't reproduce the rule.

    Raises
    ------
    ``AlreadyIssued`` when the request is RESULT_ISSUED and the caller
    didn't pass ``force_resend=True``.

    Side effect
    -----------
    On a forced resend, writes one ``RESULT_REISSUED`` audit row. The
    actual notification is the caller's responsibility — this helper
    only verifies the policy and stamps the resend event.
    """
    analysis_request.refresh_from_db(fields=['status'])
    if analysis_request.status != RequestStatus.RESULT_ISSUED:
        return True
    if not force_resend:
        raise AlreadyIssued()

    _write_audit(
        analysis_request=analysis_request,
        action=AuditAction.RESULT_REISSUED,
        channel=channel,
        actor=actor,
        request=request,
    )
    logger.info(
        'Result reissued via force_resend: request_id=%s channel=%s',
        analysis_request.id, channel,
    )
    return True


def _write_audit(
    *,
    analysis_request: AnalysisRequest,
    action: str,
    channel: str,
    actor=None,
    request=None,
    extra: Optional[dict] = None,
) -> None:
    """Single audit-write site for both the issued and reissued
    transitions. Metadata is deliberately narrow: request id, channel,
    and the actor identity already known to both sides — no patient
    PII, no storage keys, no tokens."""
    diff_after = {
        'request_number': analysis_request.request_number,
        'channel': channel,
    }
    if extra:
        diff_after.update(extra)
    AuditLog.objects.create(
        actor_type=ActorType.STAFF_USER,
        actor_id=getattr(actor, 'id', None),
        actor_email=getattr(actor, 'email', '') or '',
        action=action,
        entity_type='AnalysisRequest',
        entity_id=analysis_request.id,
        diff={'after': diff_after},
        ip_address=getattr(request, 'audit_ip', None),
        user_agent=getattr(request, 'audit_user_agent', '') or '',
    )
