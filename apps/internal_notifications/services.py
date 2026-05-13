"""
Cytova — Internal-workflow notification service.

Two public entry points hook into ``ResultVersionService``:

  - ``notify_request_ready_for_review(request, *, actor)`` fires
    when an analysis request has all its required exam results
    submitted (i.e. the request just landed in
    ``AWAITING_REVIEW`` and is genuinely ready for the biologist).
    One email per active biologist + LAB_ADMIN, deduped by
    request id + review cycle.

  - ``notify_technician_result_rejected(version, *, actor)`` fires
    when a biologist rejects a submitted result. One email to the
    technician who submitted the rejected version, deduped by
    version id + technician id.

Both paths obey three load-bearing invariants:

  1. **Dedupe via DB unique constraint** — every send writes one
     ``InternalNotificationLog`` row whose ``dedupe_key`` is
     UNIQUE in the tenant schema. A racing submit that produces
     the same key gets an ``IntegrityError`` and silently skips
     the send — there is no "check-then-insert" window.

  2. **Send AFTER transaction commit** — the actual provider call
     is wrapped in ``transaction.on_commit(...)`` so a rollback in
     the surrounding result-workflow transaction never produces a
     ghost email. If we sent inline and then the parent rolled
     back, the email would be already gone.

  3. **Email failure must not break the workflow** — provider
     errors flip the log row to ``FAILED`` with ``error_message``
     populated, and the caller continues. The staff submission
     transaction has already committed by the time the send
     fires (on_commit), so there is no rollback path anyway.

The recipient resolvers read ONLY from ``apps.users.StaffUser``
and ``ResultVersion.submitted_by``. They do not touch any
patient-side table; a refactor that added one would be flagged
by the tests' grep-for-medical-vocabulary checks.
"""
from __future__ import annotations

import logging
from typing import Iterable

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from common.email.service import get_email_service
from .models import (
    InternalNotificationEvent, InternalNotificationLog,
    InternalNotificationStatus,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Recipient resolution
# ---------------------------------------------------------------------------

def resolve_review_ready_recipients() -> list:
    """Return the list of active staff users who opted in to
    review-ready emails.

    Roles are no longer the authority — only the explicit
    ``receive_review_ready_notifications`` flag is. The flag's
    default is False at the schema level, but the data migration
    + ``StaffUserManager.create_user`` apply role-derived smart
    defaults (BIOLOGIST + LAB_ADMIN → True) so the pre-config
    behaviour is preserved.

    Local import keeps this module free of a hard dependency on
    ``apps.users`` at module-load time (matters for migrations /
    tooling that loads this file before all apps are registered).
    """
    from apps.users.models import StaffUser
    return list(
        StaffUser.objects
        .filter(
            is_active=True,
            receive_review_ready_notifications=True,
        )
        .exclude(email='')
    )


def resolve_technician_recipient(version):
    """The technician for a rejection email is the user who
    submitted the rejected version, gated by their per-user
    notification preference.

    Returns None when:
      - submitted_by is null (legacy / out-of-band entries),
      - the user is inactive,
      - the user has no email on file,
      - the user opted out of rejection notifications.
    """
    user = version.submitted_by
    if user is None:
        return None
    if not user.is_active:
        return None
    if not user.email:
        return None
    # Per-user opt-in — a technician who turned this off (or whose
    # role-default never flipped it on) should not be notified.
    if not getattr(user, 'receive_result_rejection_notifications', False):
        return None
    return user


# Back-compat alias — the previous name is still imported by the
# existing call sites in the test suite. New code should use
# ``resolve_review_ready_recipients`` directly.
def resolve_biologist_recipients() -> list:  # noqa: D401
    """Deprecated alias for ``resolve_review_ready_recipients``."""
    return resolve_review_ready_recipients()


# ---------------------------------------------------------------------------
# Dedupe-key construction
# ---------------------------------------------------------------------------

def build_request_ready_key(*, request_id, review_cycle: int, recipient_id) -> str:
    """One key per (request, cycle, recipient). Including the
    recipient id means N biologists get N rows but the SAME
    biologist can't get the same email twice for the same cycle.

    ``review_cycle`` is the ordinal of the current review round —
    increments whenever a rejection forces a new draft. The hook
    site derives it from ``max(version_number)`` across the
    request's items.
    """
    return f'REQUEST_READY_FOR_REVIEW:{request_id}:{review_cycle}:{recipient_id}'


def build_result_rejected_key(*, version_id, technician_id) -> str:
    """One key per (rejected version, technician). A version can
    be rejected only once (the model state machine refuses re-
    rejecting an already-rejected version), so the technician id
    is included for symmetry with the other event type."""
    return f'RESULT_REJECTED:{version_id}:{technician_id}'


# ---------------------------------------------------------------------------
# Cycle inference
# ---------------------------------------------------------------------------

def current_review_cycle(analysis_request) -> int:
    """Return the current review-round ordinal for ``analysis_request``.

    Implementation: the highest ``version_number`` across the
    request's items, defaulting to 1 when no result version
    exists yet. The rationale:

      - First round of submissions for an item produces
        ``version_number=1``.
      - After a rejection, ``create_draft`` reads
        ``MAX(version_number) + 1`` so the second draft is
        version 2.
      - When all items have been resubmitted at least once, the
        max across the request is 2 → a new dedupe key.

    The cycle therefore monotonically increases per rejection
    round, which is exactly what we want for "let the biologist
    know there's something new to look at".
    """
    from apps.results.models import ResultVersion
    max_v = (
        ResultVersion.objects
        .filter(item__analysis_request=analysis_request)
        .order_by('-version_number')
        .values_list('version_number', flat=True)
        .first()
    )
    return int(max_v or 1)


# ---------------------------------------------------------------------------
# CTA link builders
# ---------------------------------------------------------------------------

def _frontend_request_url(analysis_request) -> str:
    """Build the deep-link URL that operators click through to in
    the email. Reads ``CYTOVA_FRONTEND_BASE_URL`` from settings —
    the env var that already drives every other staff-side email.

    Falls back to the bare request path if the setting is empty;
    that produces a relative link, which is uglier but not
    broken on a single-tenant dev setup.
    """
    base = (getattr(settings, 'CYTOVA_FRONTEND_BASE_URL', '') or '').rstrip('/')
    path = f'/requests/{analysis_request.id}'
    return f'{base}{path}' if base else path


# ---------------------------------------------------------------------------
# Core send orchestration
# ---------------------------------------------------------------------------

def _dispatch_after_commit(log_id) -> None:
    """Find the pending row by id, render + send, and stamp the
    outcome. Runs OUTSIDE the parent transaction (scheduled via
    ``transaction.on_commit``) so a rollback in the workflow
    transaction never produces a ghost email.

    Provider failures flip ``status=FAILED`` with the error
    message captured, and never raise into the caller."""
    try:
        row = InternalNotificationLog.objects.get(pk=log_id)
    except InternalNotificationLog.DoesNotExist:
        # Row was rolled back with the parent transaction. Nothing
        # to do — the workflow rolled back too, so there is no
        # event to notify about.
        return

    try:
        if row.event_type == InternalNotificationEvent.REQUEST_READY_FOR_REVIEW:
            result = _send_request_ready(row)
        elif row.event_type == InternalNotificationEvent.RESULT_REJECTED:
            result = _send_result_rejected(row)
        else:
            logger.error(
                'Unknown internal-notification event_type=%r on row %s',
                row.event_type, row.id,
            )
            return

        if result.ok:
            row.status = InternalNotificationStatus.SENT
            row.sent_at = timezone.now()
            row.error_message = ''
        else:
            row.status = InternalNotificationStatus.FAILED
            row.error_message = (result.error or '')[:5000]
    except Exception as exc:  # noqa: BLE001 — provider may raise; never propagate
        logger.exception('Internal notification send failed for row %s', row.id)
        row.status = InternalNotificationStatus.FAILED
        row.error_message = repr(exc)[:5000]

    row.save(update_fields=['status', 'sent_at', 'error_message', 'updated_at'])


def _send_request_ready(row: InternalNotificationLog):
    """Render + send the biologist review-ready email for ``row``.

    Pulls metadata from the linked request: reference + exam list.
    Both are non-clinical fields by construction; the template
    refuses to render anything beyond them.
    """
    ar = row.request
    exam_names = list(
        ar.items.select_related('exam_definition')
        .order_by('exam_definition__code')
        .values_list('exam_definition__name', flat=True)
    )
    review_url = _frontend_request_url(ar)
    recipient_name = (
        getattr(row.recipient_user, 'first_name', '') or ''
    )
    return get_email_service().send_biologist_review_ready_email(
        recipient_email=row.recipient_email,
        recipient_name=recipient_name,
        request_reference=ar.public_reference or ar.request_number,
        exam_names=exam_names,
        review_url=review_url,
    )


def _send_result_rejected(row: InternalNotificationLog):
    """Render + send the technician rejection email for ``row``.

    The rejection note is read from the ``ResultVersion`` at send
    time, not snapshotted onto the log — keeping the log content
    minimal. Any later edit to the note (which is also blocked
    by the model contract) would surface through this path, but
    the spec rejects-are-immutable contract means that never
    happens in practice.
    """
    version = row.result_version
    if version is None:
        return _EmailResultSentinel(
            ok=False, error='ResultVersion missing on log row',
        )
    item = version.item
    ar = item.analysis_request
    review_url = _frontend_request_url(ar)
    recipient_name = (
        getattr(row.recipient_user, 'first_name', '') or ''
    )
    return get_email_service().send_technician_result_rejected_email(
        recipient_email=row.recipient_email,
        recipient_name=recipient_name,
        request_reference=ar.public_reference or ar.request_number,
        exam_name=item.exam_definition.name,
        rejection_notes=version.rejection_notes or '',
        review_url=review_url,
    )


class _EmailResultSentinel:
    """Tiny duck-typed stand-in for the provider's EmailResult so
    the dispatcher can record a "not even attempted" failure
    without importing the provider type."""
    def __init__(self, *, ok: bool, error: str = '', provider_message_id: str = ''):
        self.ok = ok
        self.error = error
        self.provider_message_id = provider_message_id


# ---------------------------------------------------------------------------
# Public API — hook these from ResultVersionService
# ---------------------------------------------------------------------------

def _settings_allow(event_attr: str) -> bool:
    """Return True when the active tenant's LabSettings permit the
    given event channel.

    Checks both the master switch and the per-event flag. Reads
    the singleton inside a try/except so a missing or corrupt
    settings row never breaks the workflow — we err on the side
    of "allow" so a fresh tenant doesn't lose review-ready
    emails before its admin has visited the settings page.

    ``event_attr`` is one of the per-event field names on
    ``LabSettings`` (``notify_review_ready_enabled``,
    ``notify_result_rejected_enabled``).
    """
    try:
        from apps.lab_settings.models import LabSettings
        settings_row = LabSettings.get_solo()
    except Exception:  # noqa: BLE001 — never break on a settings read
        return True
    if not getattr(settings_row, 'internal_notifications_enabled', True):
        return False
    return bool(getattr(settings_row, event_attr, True))


def notify_request_ready_for_review(analysis_request, *, actor=None) -> int:
    """Schedule the "ready for review" email for every recipient
    who explicitly opted in. Returns the number of NEW (deduped)
    rows queued — duplicates are silently skipped, which is the
    whole point.

    Always safe to call repeatedly; the unique ``dedupe_key``
    constraint handles the race.

    Short-circuits to 0 when the tenant disabled the master
    switch OR the per-event flag.
    """
    if not _settings_allow('notify_review_ready_enabled'):
        return 0
    recipients = resolve_review_ready_recipients()
    if not recipients:
        return 0

    review_cycle = current_review_cycle(analysis_request)
    queued = 0
    for user in recipients:
        key = build_request_ready_key(
            request_id=analysis_request.id,
            review_cycle=review_cycle,
            recipient_id=user.id,
        )
        log_id = _try_insert_log(
            event_type=InternalNotificationEvent.REQUEST_READY_FOR_REVIEW,
            analysis_request=analysis_request,
            result_version=None,
            recipient_user=user,
            recipient_email=user.email,
            dedupe_key=key,
        )
        if log_id is not None:
            queued += 1
            transaction.on_commit(
                lambda lid=log_id: _dispatch_after_commit(lid),
            )

    if queued == 0:
        logger.debug(
            'notify_request_ready_for_review: no new recipients for '
            'request=%s cycle=%d (all already deduped)',
            analysis_request.id, review_cycle,
        )
    return queued


def notify_technician_result_rejected(version, *, actor=None) -> int:
    """Schedule the rejection email for the technician who
    submitted ``version``. Returns 1 when a new row is queued, 0
    when deduped (already sent for the same version) or when no
    eligible recipient is recoverable.

    Short-circuits to 0 when the tenant disabled the master
    switch OR the per-event flag.
    """
    if not _settings_allow('notify_result_rejected_enabled'):
        return 0
    technician = resolve_technician_recipient(version)
    if technician is None or not technician.email:
        return 0

    key = build_result_rejected_key(
        version_id=version.id,
        technician_id=technician.id,
    )
    log_id = _try_insert_log(
        event_type=InternalNotificationEvent.RESULT_REJECTED,
        analysis_request=version.item.analysis_request,
        result_version=version,
        recipient_user=technician,
        recipient_email=technician.email,
        dedupe_key=key,
    )
    if log_id is None:
        return 0
    transaction.on_commit(
        lambda lid=log_id: _dispatch_after_commit(lid),
    )
    return 1


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _try_insert_log(
    *,
    event_type: str,
    analysis_request,
    result_version,
    recipient_user,
    recipient_email: str,
    dedupe_key: str,
):
    """Best-effort insert of a PENDING log row.

    The UNIQUE constraint on ``dedupe_key`` is the race guard. A
    parallel call producing the same key fails with
    ``IntegrityError`` — we catch it and return None so the
    caller knows to skip the dispatch. Wrapped in
    ``transaction.atomic`` so the inner failure doesn't poison
    the surrounding transaction (Django's default ATOMIC_REQUESTS
    behaviour otherwise marks the whole transaction broken).
    """
    try:
        with transaction.atomic():
            row = InternalNotificationLog.objects.create(
                event_type=event_type,
                request=analysis_request,
                result_version=result_version,
                recipient_user=recipient_user,
                recipient_email=recipient_email,
                dedupe_key=dedupe_key,
                status=InternalNotificationStatus.PENDING,
            )
        return row.id
    except IntegrityError:
        return None
