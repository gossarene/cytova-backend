"""
Cytova — Patient result notification orchestration.

Wraps the secure-link lifecycle and the configured patient-channel toggles
in a single tenant-aware service. Currently V1 supports email only:

  - reuses the existing active ResultAccessToken for the request (or creates
    one if none exists) so we never produce duplicate links
  - resolves enabled channels from LabSettings (per-tenant)
  - dispatches via the existing EmailService → EmailProvider abstraction
    (so dev/console and prod/Brevo both work transparently)
  - writes an AuditLog entry recording the attempt + provider result;
    never logs medical data, the secure URL, or PDF content

WhatsApp share remains a manual frontend action — this service does not
produce WhatsApp links. SMS is intentionally NOT implemented yet (the
LabSettings flag exists for future use).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from django.utils import timezone

from apps.audit.models import ActorType, AuditAction, AuditLog
from apps.lab_settings.models import LabSettings
from apps.requests.models import (
    AnalysisRequest, ClosureStatus, RequestStatus, ResultAccessToken,
)
from apps.requests.patient_access import ResultAccessService
from common.email import get_email_service
from common.utils.url import build_tenant_frontend_url

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class NotificationError(Exception):
    """Base for notification orchestration errors. Translated to API
    responses by the view layer."""
    code = 'NOTIFICATION_ERROR'
    message = 'Could not send the patient notification.'


class PatientEmailMissing(NotificationError):
    code = 'PATIENT_EMAIL_MISSING'
    message = 'Patient email is required to send an email notification.'


class EmailChannelDisabled(NotificationError):
    code = 'EMAIL_CHANNEL_DISABLED'
    message = 'Email notifications are disabled in lab settings.'


class NoChannelsRequested(NotificationError):
    code = 'NO_CHANNELS_REQUESTED'
    message = 'No notification channels are enabled or requested.'


# ---------------------------------------------------------------------------
# Result value object
# ---------------------------------------------------------------------------

EMAIL_CHANNEL = 'EMAIL'

# Channels this service knows how to dispatch in V1. WhatsApp share remains
# a manual frontend action; SMS is intentionally not implemented yet (the
# LabSettings flag exists for future wiring).
SUPPORTED_CHANNELS = (EMAIL_CHANNEL,)


@dataclass
class ChannelOutcome:
    channel: str
    status: str           # 'SENT' | 'FAILED'
    provider: Optional[str] = None
    error: Optional[str] = None


@dataclass
class NotificationResult:
    secure_link: str
    expires_at: str           # ISO 8601 timestamp
    channels_attempted: List[str] = field(default_factory=list)
    channels_succeeded: List[str] = field(default_factory=list)
    channels_failed: List[ChannelOutcome] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class RequestNotificationService:

    PATIENT_RESULT_FRONTEND_PATH = '/results/access'

    @classmethod
    def notify_patient(
        cls,
        analysis_request: AnalysisRequest,
        request,
        *,
        channels: Optional[List[str]] = None,
    ) -> NotificationResult:
        """Send patient notifications for ``analysis_request`` over the
        requested channels (default: every channel enabled in LabSettings
        that this service supports — currently only EMAIL).

        Tenant scope: the caller is expected to be on a tenant-routed view,
        so ``LabSettings.get_solo()``, ``StaffUser.objects``, and the
        ResultAccessToken lookup all resolve inside the active tenant
        schema. The reset link is built from the request host so it
        cannot point at a different tenant.

        Side effects on hit:
          - active access token reused, or new one created (single source
            of truth via ResultAccessService)
          - email dispatched per enabled channel
          - audit log row written per channel attempted, capturing provider
            outcome (no medical data, no secure link in the diff)

        Channel selection:
          - ``channels=None``  → use the lab's enabled channels
          - ``channels=[...]`` → use explicit channels (still gated by
                                 LabSettings flags so a disabled channel
                                 cannot be force-fired by request)
        """
        lab_settings = LabSettings.get_solo()
        enabled = cls._enabled_channels(lab_settings)

        # Default to the V1 supported channels (currently email only).
        # Caller can pass an explicit list to constrain — never to expand
        # beyond what the service supports.
        requested = [
            c.upper() for c in (channels if channels is not None else SUPPORTED_CHANNELS)
        ]
        requested = [c for c in requested if c in SUPPORTED_CHANNELS]
        # Intersection with the lab's enabled set — never bypass policy.
        active_channels = [c for c in requested if c in enabled]

        if not active_channels:
            # Distinguish "channel disabled in settings" from "no supported
            # channel requested" so the frontend can render the right hint.
            if EMAIL_CHANNEL in requested and EMAIL_CHANNEL not in enabled:
                raise EmailChannelDisabled()
            raise NoChannelsRequested()

        # Resolve the secure access URL once — every channel shares it.
        token = ResultAccessService.get_or_create_token(analysis_request)
        secure_link = build_tenant_frontend_url(
            request, f'{cls.PATIENT_RESULT_FRONTEND_PATH}/{token.token}',
        )

        result = NotificationResult(
            secure_link=secure_link,
            expires_at=token.expires_at.isoformat(),
        )

        if EMAIL_CHANNEL in active_channels:
            cls._dispatch_email(
                analysis_request=analysis_request,
                token=token,
                secure_link=secure_link,
                lab_settings=lab_settings,
                request=request,
                result=result,
            )

        return result

    # ----- Channel handlers -------------------------------------------

    @classmethod
    def _dispatch_email(
        cls,
        *,
        analysis_request: AnalysisRequest,
        token: ResultAccessToken,
        secure_link: str,
        lab_settings: LabSettings,
        request,
        result: NotificationResult,
    ) -> None:
        result.channels_attempted.append(EMAIL_CHANNEL)
        patient = analysis_request.patient
        recipient_email = (patient.email or '').strip()

        if not recipient_email:
            # Channel-specific precondition — distinct from a provider failure.
            cls._audit(
                analysis_request=analysis_request,
                channel=EMAIL_CHANNEL,
                status='FAILED',
                provider=None,
                error='patient_email_missing',
                request=request,
            )
            raise PatientEmailMissing()

        service = get_email_service()
        # Phase 2 of the customisable-templates rollout: thread the
        # operator-customised subject + body templates through to
        # the renderer. Empty strings (the migration default) make
        # the renderer fall back to the canonical hard-coded copy,
        # so tenants that haven't touched the fields experience
        # zero behavioural drift. ``request_reference`` populates
        # the operator's optional ``{{ request_reference }}``
        # placeholder; we pass the public-facing value (matches
        # what the operator sees on receipts), falling back to the
        # internal request_number so the variable is never empty.
        delivery = service.send_patient_result_ready_email(
            recipient_email=recipient_email,
            recipient_name=patient.first_name,
            secure_link=secure_link,
            lab_name=lab_settings.lab_name or '',
            request_reference=(
                analysis_request.public_reference
                or analysis_request.request_number
                or ''
            ),
            subject_template=lab_settings.patient_result_email_subject_template,
            body_template=lab_settings.patient_result_email_body_template,
        )

        provider_name = service.provider.name
        if delivery.ok:
            result.channels_succeeded.append(EMAIL_CHANNEL)
            logger.info(
                'Patient notification sent: channel=EMAIL provider=%s request_id=%s '
                'patient_id=%s token_id=%s recipient_domain=%s',
                provider_name, analysis_request.id, patient.id, token.id,
                _email_domain(recipient_email),
            )

            # Persist tracking + (when applicable) auto-advance to DELIVERED.
            cls._persist_email_notification(analysis_request, request)
            cls._maybe_auto_deliver(analysis_request, request)

            cls._audit(
                analysis_request=analysis_request,
                channel=EMAIL_CHANNEL,
                status='SENT',
                provider=provider_name,
                error=None,
                request=request,
                notification_count=analysis_request.notification_count,
            )
        else:
            result.channels_failed.append(ChannelOutcome(
                channel=EMAIL_CHANNEL,
                status='FAILED',
                provider=provider_name,
                error=delivery.error or 'unknown_error',
            ))
            logger.error(
                'Patient notification FAILED: channel=EMAIL provider=%s request_id=%s '
                'patient_id=%s token_id=%s recipient_domain=%s provider_error=%s',
                provider_name, analysis_request.id, patient.id, token.id,
                _email_domain(recipient_email), delivery.error,
            )
            cls._audit(
                analysis_request=analysis_request,
                channel=EMAIL_CHANNEL,
                status='FAILED',
                provider=provider_name,
                error=delivery.error or 'unknown_error',
                request=request,
            )

    # ----- Tracking + lifecycle hooks ---------------------------------

    @staticmethod
    def _persist_email_notification(analysis_request: AnalysisRequest, request) -> None:
        """Stamp per-request notification tracking after a successful send.
        Idempotent on a single call — only updates fields, no transitions."""
        actor = getattr(request, 'user', None)
        actor_pk = actor.pk if actor and getattr(actor, 'is_authenticated', False) else None

        analysis_request.notified_by_email_at = timezone.now()
        analysis_request.notified_by_email_by_id = actor_pk
        analysis_request.notification_count = (analysis_request.notification_count or 0) + 1
        analysis_request.last_patient_notification_channel = EMAIL_CHANNEL
        analysis_request.save(update_fields=[
            'notified_by_email_at',
            'notified_by_email_by',
            'notification_count',
            'last_patient_notification_channel',
            'updated_at',
        ])

    @staticmethod
    def _maybe_auto_deliver(analysis_request: AnalysisRequest, request) -> None:
        """A successful patient notification promotes closure_status to
        DELIVERED when the workflow has reached VALIDATED (or a downstream
        terminal state). Workflow ``status`` is NEVER changed — billing
        queries that look at status=VALIDATED keep working unchanged.

        Already-DELIVERED rows: idempotent (no-op).
        Already-ARCHIVED rows:  preserved (closure is forward-only;
                                we don't reopen a manually archived row
                                because email happened to fire again).
        Earlier workflow states (e.g. AWAITING_REVIEW): left as OPEN —
                                a notification at this stage is a courtesy
                                and shouldn't pre-close the request.
        """
        if analysis_request.closure_status != ClosureStatus.OPEN:
            return
        if analysis_request.status not in (
            RequestStatus.VALIDATED,
            RequestStatus.COMPLETED,
        ):
            return

        actor = getattr(request, 'user', None)
        actor_pk = actor.pk if actor and getattr(actor, 'is_authenticated', False) else None

        previous_closure = analysis_request.closure_status
        analysis_request.closure_status = ClosureStatus.DELIVERED
        analysis_request.delivered_at = timezone.now()
        analysis_request.delivered_by_id = actor_pk
        analysis_request.save(update_fields=[
            'closure_status', 'delivered_at', 'delivered_by', 'updated_at',
        ])

        # Separate audit row so the closure transition shows up alongside
        # other lifecycle events, not buried inside a notification log.
        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=actor_pk,
            actor_email=actor.email if actor_pk else None,
            action=AuditAction.UPDATE,
            entity_type='AnalysisRequest',
            entity_id=analysis_request.id,
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
            diff={
                'closure_from': previous_closure,
                'closure_to': ClosureStatus.DELIVERED.value,
                'reason': 'auto_delivered_on_email_notification',
            },
        )

    # ----- Channel resolution -----------------------------------------

    @staticmethod
    def _enabled_channels(lab_settings: LabSettings) -> set[str]:
        enabled: set[str] = set()
        if lab_settings.notification_enable_email:
            enabled.add(EMAIL_CHANNEL)
        # SMS intentionally excluded in V1 — the LabSettings flag exists
        # for future wiring but the service does not act on it yet.
        return enabled

    # ----- Audit ------------------------------------------------------

    @staticmethod
    def _audit(
        *,
        analysis_request: AnalysisRequest,
        channel: str,
        status: str,
        provider: Optional[str],
        error: Optional[str],
        request,
        notification_count: Optional[int] = None,
    ) -> None:
        """Record one notification attempt. Diff payload deliberately
        excludes the secure link, the recipient email address (only its
        domain is logged via the service's logger above), and any
        medical data.

        ``notification_count`` distinguishes first-send from resends: a
        value > 1 means this attempt is a resend, since the field is
        incremented inside ``_persist_email_notification`` before the
        audit row is written.
        """
        actor = getattr(request, 'user', None)
        actor_id = actor.id if actor and getattr(actor, 'is_authenticated', False) else None
        actor_email = actor.email if actor and getattr(actor, 'is_authenticated', False) else None

        diff = {
            'channel': channel,
            'status': status,
            'provider': provider,
            'error': error,
        }
        if notification_count is not None:
            diff['notification_count'] = notification_count
            diff['is_resend'] = notification_count > 1

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=actor_id,
            actor_email=actor_email,
            action=AuditAction.UPDATE,
            entity_type='PatientResultNotification',
            entity_id=analysis_request.id,
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
            diff=diff,
        )


def _email_domain(email: str) -> str:
    return email.rsplit('@', 1)[-1] if '@' in email else 'unknown'
