"""
Cytova — Notify-Cytova service.

Bridges the lab tenant ``AnalysisRequest`` flow with the global
patient portal. The lab user supplies a Cytova Patient ID + claimed
identity; on a successful match we snapshot the request + report
metadata into the public-schema ``PatientSharedResult`` /
``PatientSharedResultFile`` tables so the patient sees the share in
their portal — independently of the lab tenant's schema lifecycle.

Snapshot rationale
------------------
Patient portal rows live in the ``public`` schema and have NO foreign
keys into tenant tables (cross-schema FKs aren't supported by
django-tenants and would be a layering violation anyway). Every field
the patient needs to see is captured at share time as a plain string
or date. A future tenant rename, deletion, or settings change never
rewrites the patient's visible history.

The original PDF blob is NOT physically duplicated. The shared file
row carries:
  - ``file_token``: a random opaque identifier the future patient
    portal download endpoint will quote;
  - ``storage_key``: a snapshot of the underlying file path, so the
    download endpoint can stream from the original storage without
    crossing into a tenant database table.

Security
--------
- Identity verification is delegated to ``apps.patient_portal.lookup``,
  which never reveals which field failed.
- We refuse to share unless the request is VALIDATED and a current
  report PDF exists.
- A generic ``IdentityVerificationFailed`` is the ONLY failure surface
  for verification problems — lab user sees one message regardless of
  whether the Cytova ID is wrong, the name is wrong, or the DOB is
  wrong. Audit log records the attempt with no patient PII.
"""
from __future__ import annotations

import logging
import secrets
from typing import Optional

from django.conf import settings as django_settings
from django.db import connection, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.audit.models import ActorType, AuditAction, AuditLog
from apps.lab_settings.models import LabSettings
from apps.patient_portal.audit import write_event as write_patient_audit
from apps.patient_portal.lookup import verify_patient_identity
from apps.patient_portal.models import (
    PatientPortalAuditAction,
    PatientSharedChannel,
    PatientSharedResult,
    PatientSharedResultFile,
    SharedResultSourceType,
)
from apps.requests.models import (
    AnalysisRequest, AnalysisRequestReport, RequestStatus,
)
from common.email.service import get_email_service

logger = logging.getLogger(__name__)


class NotifyCytovaError(Exception):
    """Base for the narrow exception types the view maps to HTTP errors."""
    code: str = 'NOTIFY_CYTOVA_ERROR'
    message: str = 'Could not share result with patient portal.'


class RequestNotValidated(NotifyCytovaError):
    code = 'REQUEST_NOT_VALIDATED'
    message = 'Only validated requests with a generated report can be shared.'


class ReportNotAvailable(NotifyCytovaError):
    code = 'REPORT_NOT_AVAILABLE'
    message = 'Generate the result report before sharing it with the patient.'


class IdentityVerificationFailed(NotifyCytovaError):
    """Single non-distinguishing failure for any verification mismatch
    (unknown Cytova ID, wrong name, wrong DOB, inactive account).
    Mirrors the spec: never tell the lab user which field failed.

    Phase D extension: also raised when a previously-linked patient's
    global account has been deactivated since the link was created.
    The lab user sees the same message in either path — the audit
    metadata distinguishes the two via ``notify_cytova_outcome``
    (``IDENTITY_MISMATCH`` for the explicit-payload path,
    ``LINKED_ACCOUNT_INACTIVE`` for the linked path) so an audit
    reader can still tell what happened without leaking it to the UI."""
    code = 'IDENTITY_VERIFICATION_FAILED'
    message = (
        'Identity verification failed. Please check the Cytova ID or '
        'patient information.'
    )


class MissingIdentity(NotifyCytovaError):
    """Raised when the call shape doesn't carry an identity claim AND
    the local patient has no Cytova link to fall back on. The UI should
    drive the operator to link the patient first (Phase E) — this
    error is the safety net for callers that bypassed the UX hint."""
    code = 'MISSING_IDENTITY'
    message = (
        'Patient identity is required. Either link this patient to a '
        'Cytova account first, or supply the Cytova ID and patient '
        'information.'
    )


class CytovaChannelDisabled(NotifyCytovaError):
    """Lab toggled ``LabSettings.notification_enable_cytova`` off.
    Mirrors the ``EmailChannelDisabled`` shape used by notify-by-email
    so the frontend can branch on the same error code shape across
    channels."""
    code = 'CYTOVA_CHANNEL_DISABLED'
    message = (
        'Cytova patient-portal sharing is disabled in lab settings.'
    )


def _copy_pdf_to_patient_storage(*, sfile, profile) -> None:
    """Copy the lab-side PDF to a patient-owned storage path so the
    portal's access doesn't depend on the lab tenant's storage
    lifecycle.

    On success: stamps ``patient_storage_key`` + ``storage_origin='PATIENT'``.
    On failure: logs a warning and leaves the row with
    ``storage_origin='LAB'`` so the download endpoint's fallback keeps
    serving the file from the original lab key. The share itself
    proceeds either way — the snapshot row is the canonical artefact;
    the storage copy is a privacy/lifecycle improvement that must
    never gate the share.
    """
    from apps.files.storage import copy_file
    from apps.patient_portal.models import PatientSharedResultFile
    import uuid as _uuid

    if not sfile.storage_key:
        return

    # Stable destination path under a patient-owned namespace. The
    # ``shared_result_id`` is part of the path so listing the bucket
    # makes the lineage obvious; the trailing UUID prevents accidental
    # collision if the share row is ever recreated.
    destination_key = (
        f'patient-results/{profile.account_id}/'
        f'{sfile.shared_result_id}/{_uuid.uuid4().hex}.pdf'
    )
    if copy_file(sfile.storage_key, destination_key):
        # Update without refresh — the row was just created and
        # nothing else races us on these columns.
        PatientSharedResultFile.objects.filter(pk=sfile.pk).update(
            patient_storage_key=destination_key,
            storage_origin='PATIENT',
        )
        logger.info(
            'Patient PDF copied: shared_result_id=%s file_id=%s',
            sfile.shared_result_id, sfile.id,
        )
    else:
        # Already logged inside copy_file. Row stays with
        # storage_origin='LAB' which the download endpoint's fallback
        # honours — patient still sees the file.
        logger.warning(
            'Patient PDF copy fell back to lab storage: shared_result_id=%s '
            'file_id=%s', sfile.shared_result_id, sfile.id,
        )


def _resolve_linked_profile(patient):
    """Re-fetch the global ``PatientProfile`` for a patient who carries
    a verified Cytova link snapshot, but only if the underlying
    ``PatientAccount`` is still active.

    Returns the profile on success, or ``None`` when the link is no
    longer usable (account deactivated, account UUID orphaned, or the
    profile row was removed). The caller maps ``None`` to
    ``IdentityVerificationFailed`` — the UI sees the same generic
    message either way; the audit metadata is what distinguishes
    "the operator typed the wrong DOB" from "the global account
    was deactivated since you linked" so an audit reader can still
    tell what happened.

    Cross-schema rule: this is the only legitimate use of the stored
    ``cytova_patient_account_id`` snapshot — re-checking validity at
    use time. The snapshot is never published to clients (Phase C);
    the read is server-side only.
    """
    if not getattr(patient, 'has_cytova_identity', False):
        return None
    from apps.patient_portal.models import PatientAccount, PatientProfile
    try:
        account = PatientAccount.objects.get(
            id=patient.cytova_patient_account_id,
            is_active=True,
        )
    except PatientAccount.DoesNotExist:
        return None
    try:
        return (
            PatientProfile.objects
            .select_related('account')
            .get(account=account)
        )
    except PatientProfile.DoesNotExist:
        return None


def _suggested_filename(ar: AnalysisRequest, report: AnalysisRequestReport) -> str:
    """Build a human-friendly download filename. Mirrors what the
    lab-side ``report_download`` endpoint surfaces."""
    ref = ar.public_reference or ar.request_number or str(ar.id)
    return f'report_{ref}_v{report.version_number}.pdf'


def _resolve_lab_name(request) -> str:
    """Snapshot the active lab's display name. Prefers the
    ``LabSettings`` text the lab uses on its own report header
    (single source of truth across the rest of the app); falls back
    to ``request.tenant.name`` when ``LabSettings`` hasn't been
    populated yet (early-onboarding tenants)."""
    try:
        settings = LabSettings.get_solo()
        if settings.lab_name:
            return settings.lab_name
    except Exception:  # noqa: BLE001
        # LabSettings should always exist (singleton helper), but in
        # the unlikely event it can't be read, fall through to the
        # tenant fallback rather than failing the share.
        pass
    tenant = getattr(request, 'tenant', None)
    return getattr(tenant, 'name', '') or ''


def _patient_results_url() -> str:
    """Build the patient-portal results URL the email CTA points at.
    Reads ``PATIENT_PORTAL_FRONTEND_BASE_URL`` from settings so dev /
    staging / prod deployments each link to their own frontend."""
    base = getattr(
        django_settings, 'PATIENT_PORTAL_FRONTEND_BASE_URL',
        'https://www.cytova.io',
    ).rstrip('/')
    return f'{base}/patient/results'


def _send_share_notification(
    *, shared: PatientSharedResult, account_email: str,
) -> str:
    """Dispatch the "result shared with you" email + record outcome on
    the snapshot row. Returns ``'SENT'`` or ``'FAILED'`` so the HTTP
    layer can surface the status to the lab user without breaking the
    share. Email content carries no medical detail (see template
    docstring); ``account_email`` flows from the patient portal
    account, not the lab side, so the lab never gets to peek at it.

    Email-delivery failures are NEVER raised from this function —
    sharing is the contract; notification is best-effort. Returning
    ``'FAILED'`` (and stamping the row) is enough for the UI to nudge
    the operator to resend or follow up out-of-band.
    """
    try:
        result = get_email_service().send_patient_shared_result_email(
            recipient_email=account_email,
            view_url=_patient_results_url(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            'Patient shared-result email crashed: shared_result_id=%s err=%s',
            shared.id, exc,
        )
        result = None

    sent = bool(result and result.ok)
    status_value = 'SENT' if sent else 'FAILED'
    PatientSharedResult.objects.filter(pk=shared.id).update(
        email_notification_status=status_value,
        email_notification_sent_at=timezone.now() if sent else None,
    )
    if sent:
        # Don't log the patient's email — only the IDs already known.
        logger.info(
            'Patient share email sent: shared_result_id=%s', shared.id,
        )
    else:
        err = getattr(result, 'error', '') if result else 'crash'
        logger.warning(
            'Patient share email NOT delivered: shared_result_id=%s error=%s',
            shared.id, err,
        )
    return status_value


def notify_cytova(
    *,
    analysis_request: AnalysisRequest,
    cytova_patient_id: str = '',
    first_name: str = '',
    last_name: str = '',
    date_of_birth=None,
    actor,
    request,
) -> tuple[PatientSharedResult, str]:
    """Share an analysis request's current report with a patient
    portal account.

    Two valid call shapes (Phase D):

    1. **Linked-patient call (default)** — empty identity payload.
       The service consults
       ``analysis_request.patient.has_cytova_identity`` and
       re-verifies the linked ``PatientAccount`` is still active
       (``_resolve_linked_profile``). The link itself was already
       verified once at link time (Phase B), so we don't re-run the
       interactive name/DOB match — re-checking ``is_active`` is
       enough to catch a global-account deactivation since the link.

    2. **Explicit-identity call (back-compat)** — all four identity
       fields supplied. The service runs the original
       ``verify_patient_identity`` path. Kept for unlinked patients
       and for any external caller still on the pre-Phase-D contract.

    Failure modes:
      - ``MissingIdentity``           — neither path applies (no link
        AND no identity payload).
      - ``IdentityVerificationFailed``— either the explicit-payload
        verification failed OR the linked account is no longer
        active. Single non-distinguishing message; audit metadata
        carries the distinguishing ``notify_cytova_outcome`` marker.

    Returns
    -------
    ``(shared_result, email_notification_status)`` where the status is
    one of ``'SENT'`` / ``'FAILED'``. Email failures do NOT roll back
    the share; the snapshot row is the canonical artefact and the
    notification is best-effort.

    Transaction structure
    ---------------------
    The verification + failed-attempt audit live OUTSIDE any atomic
    block — otherwise raising ``IdentityVerificationFailed`` from
    inside ``@transaction.atomic`` would roll back the audit row we
    just wrote (defeating the brute-force-detection purpose). On a
    successful match, the snapshot writes + the SHARED tenant audit
    row are wrapped in a single atomic block so a failure halfway
    through can never leave a half-shared state. The email + the
    patient-portal audit row run AFTER commit so a slow / failing
    SMTP provider can't hold the row lock.

    Raises one of the ``NotifyCytovaError`` subclasses on failure.
    Writes a tenant-side AuditLog row recording the outcome. Patient
    PII is never written to the audit log; only the (already-public)
    Cytova ID is captured for traceability.
    """
    # --- Step 1: verify the request can be shared at all -----------
    # Both VALIDATED and RESULT_ISSUED are acceptable: RESULT_ISSUED
    # means an earlier notification already occurred (the view's
    # one-shot guard handles whether re-sharing is allowed); the
    # underlying report still exists either way.
    if analysis_request.status not in (
        RequestStatus.VALIDATED, RequestStatus.RESULT_ISSUED,
    ):
        raise RequestNotValidated()

    report = (
        AnalysisRequestReport.objects
        .filter(analysis_request=analysis_request, is_current=True)
        .first()
    )
    if report is None or not report.pdf_file_key:
        raise ReportNotAvailable()

    # --- Step 2: identity resolution (NOT in atomic) ---------------
    # Two paths converge here. The explicit-payload path is kept for
    # back-compat with the pre-Phase-D contract; the linked path is
    # the new default that the Phase E UI will exercise.
    explicit_supplied = bool(
        cytova_patient_id or first_name or last_name or date_of_birth
    )
    patient = analysis_request.patient

    if explicit_supplied:
        # Back-compat path. Run the original interactive verification
        # against the supplied claim — same code site Notify-Cytova
        # used pre-Phase-D, no behaviour change.
        profile = verify_patient_identity(
            cytova_patient_id, first_name, last_name, date_of_birth,
        )
        if profile is None:
            # Audit the *failed* attempt with the supplied Cytova ID
            # only — name/DOB never recorded. Helps detect
            # brute-force patterns against a single known ID without
            # storing PII per attempt.
            AuditLog.objects.create(
                actor_type=ActorType.STAFF_USER,
                actor_id=getattr(actor, 'id', None),
                actor_email=getattr(actor, 'email', ''),
                action=AuditAction.UPDATE,
                entity_type='AnalysisRequest',
                entity_id=analysis_request.id,
                diff={'after': {
                    'notify_cytova_outcome': 'IDENTITY_MISMATCH',
                    'cytova_patient_id_attempted': (cytova_patient_id or '').strip()[:32],
                }},
                ip_address=getattr(request, 'audit_ip', None),
                user_agent=getattr(request, 'audit_user_agent', ''),
            )
            raise IdentityVerificationFailed()
    elif patient.has_cytova_identity:
        # Linked path. The link was already verified at link time
        # (Phase B), so we don't re-run the interactive name/DOB
        # match — re-checking ``is_active`` on the global account is
        # enough to catch a deactivation since the link.
        profile = _resolve_linked_profile(patient)
        if profile is None:
            # Audit with a distinct outcome marker so an audit reader
            # can tell "linked-but-now-inactive" apart from a fresh
            # interactive mismatch. The lab user sees the same
            # generic message either way.
            AuditLog.objects.create(
                actor_type=ActorType.STAFF_USER,
                actor_id=getattr(actor, 'id', None),
                actor_email=getattr(actor, 'email', ''),
                action=AuditAction.UPDATE,
                entity_type='AnalysisRequest',
                entity_id=analysis_request.id,
                diff={'after': {
                    'notify_cytova_outcome': 'LINKED_ACCOUNT_INACTIVE',
                    'patient_id': str(patient.id),
                }},
                ip_address=getattr(request, 'audit_ip', None),
                user_agent=getattr(request, 'audit_user_agent', ''),
            )
            raise IdentityVerificationFailed()
    else:
        # Neither path applies — the operator hit the endpoint on an
        # unlinked patient without supplying identity. The Phase E UI
        # routes around this (it links first), so this is the safety
        # net for callers that bypassed the UX hint.
        raise MissingIdentity()

    # --- Step 3 + 4: snapshot + audit in a single atomic block -----
    lab_name = _resolve_lab_name(request)
    actor_email = getattr(actor, 'email', '') or ''

    # Snapshot the originating tenant schema so the lab-side revoke
    # endpoint can scope its lookup safely. ``connection.schema_name``
    # is set by the django-tenants middleware for the active request;
    # falling back to the empty string keeps tests that bypass the
    # middleware happy (the revoke endpoint scopes to the same value
    # so empty-schema rows can still be revoked from an empty-schema
    # context).
    tenant_schema = getattr(connection, 'schema_name', '') or ''

    with transaction.atomic():
        # --- Step 3a: demote any prior versions current for this
        # patient + this source request so the supersession invariant
        # holds before the new row is inserted.
        #
        # Scope: (patient_account, source_tenant_schema, source_request_id).
        # Tenant schema is included so a hypothetical UUID collision
        # across tenants could never bleed supersession across labs.
        # ``status`` is intentionally NOT in the filter — a row that
        # was REVOKED still owned the "current for patient" flag, and
        # demoting it on a subsequent share keeps the bookkeeping
        # honest even though the patient view filters revoked rows
        # out separately.
        prior_current = list(
            PatientSharedResult.objects
            .select_for_update()
            .filter(
                patient_account=profile.account,
                source_tenant_schema=tenant_schema,
                source_request_id=analysis_request.id,
                is_current_for_patient=True,
            )
            .values_list('id', flat=True)
        )
        if prior_current:
            PatientSharedResult.objects.filter(
                id__in=prior_current,
            ).update(is_current_for_patient=False)

        # --- Step 3b: insert the new shared row with full version
        # metadata. ``shared_at`` matches ``created_at`` here (new row,
        # same instant) and is duplicated as a semantic field so the
        # patient versions API can expose a channel-agnostic
        # "when did the patient gain access?" timestamp without
        # leaking the model's row-creation column.
        now = timezone.now()
        shared = PatientSharedResult.objects.create(
            patient_account=profile.account,
            source_type=SharedResultSourceType.DIRECT,
            source_name=lab_name,
            request_reference=(
                analysis_request.public_reference
                or analysis_request.request_number
                or str(analysis_request.id)
            )[:64],
            request_date=analysis_request.created_at.date(),
            result_available_date=report.generated_at.date(),
            created_by_lab=lab_name or actor_email,
            # Immutable lab linkage for revoke. The pair
            # (tenant_schema, source_request_id) is what the lab-side
            # revoke endpoint matches on — never the lab patient row,
            # never the report file key.
            source_tenant_schema=tenant_schema,
            source_request_id=analysis_request.id,
            # Patient-facing version metadata snapshotted at share time.
            report_version_number=report.version_number,
            report_generated_at=report.generated_at,
            shared_at=now,
            shared_channel=PatientSharedChannel.CYTOVA,
            is_current_for_patient=True,
        )
        # Random URL-safe opaque token. Distinct from the lab's
        # ``ResultAccessToken`` (48h TTL, tenant-patient FK) — the
        # patient portal needs a stable reference that doesn't expire
        # just because the lab token rotated.
        sfile = PatientSharedResultFile.objects.create(
            shared_result=shared,
            file_token=secrets.token_urlsafe(32)[:64],
            filename=_suggested_filename(analysis_request, report),
            storage_key=report.pdf_file_key,
        )

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=getattr(actor, 'id', None),
            actor_email=actor_email,
            # Dedicated lifecycle action so the audit reader can
            # distinguish a share from a generic UPDATE. The diff
            # still carries ``notify_cytova_outcome=SHARED`` for
            # back-compat with existing audit tests + dashboards.
            action=AuditAction.RESULT_SHARED_CYTOVA,
            entity_type='AnalysisRequest',
            entity_id=analysis_request.id,
            diff={'after': {
                'notify_cytova_outcome': 'SHARED',
                'request_number': analysis_request.request_number,
                'patient_account_id': str(profile.account_id),
                'shared_result_id': str(shared.id),
            }},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

    logger.info(
        'Notify Cytova: request_id=%s patient_account_id=%s shared_result_id=%s',
        analysis_request.id, profile.account_id, shared.id,
    )

    # Post-commit side effects. The order matters:
    #   1. Copy the PDF into patient-owned storage so the patient's
    #      access doesn't depend on the lab tenant's storage lifecycle.
    #      Failure is logged + we keep the LAB-side reference as
    #      fallback (the download endpoint reads from the patient key
    #      first, then falls through).
    #   2. Send the notification email. Email failure must not roll
    #      back the share — the snapshot row is the canonical artefact.
    #   3. Write the patient-side audit row.
    _copy_pdf_to_patient_storage(sfile=sfile, profile=profile)
    notification_status = _send_share_notification(
        shared=shared, account_email=profile.account.email,
    )
    write_patient_audit(
        action=PatientPortalAuditAction.PATIENT_RESULT_SHARED.value,
        entity_type='PatientSharedResult',
        entity_id=shared.id,
        patient_account=profile.account,
        request=request,
        metadata={
            'shared_result_id': shared.id,
            'source_request_reference': shared.request_reference,
            'source_name': shared.source_name,
            'email_notification_status': notification_status,
        },
    )

    # Version-aware audit events. The legacy PATIENT_RESULT_SHARED
    # row above is kept untouched for back-compat; these two carry
    # the version-line semantics (which version is now current for
    # the patient, which prior versions were demoted) so a future
    # patient-portal versions reader can reconstruct the timeline
    # without inspecting the snapshot row directly.
    write_patient_audit(
        action=PatientPortalAuditAction.PATIENT_VERSION_SHARED.value,
        entity_type='PatientSharedResult',
        entity_id=shared.id,
        patient_account=profile.account,
        request=request,
        metadata={
            'shared_result_id': shared.id,
            'source_request_reference': shared.request_reference,
            'report_version_number': report.version_number,
            'shared_channel': PatientSharedChannel.CYTOVA.value,
        },
    )
    for prior_id in prior_current:
        write_patient_audit(
            action=PatientPortalAuditAction.PATIENT_VERSION_SUPERSEDED.value,
            entity_type='PatientSharedResult',
            entity_id=prior_id,
            patient_account=profile.account,
            request=request,
            metadata={
                'shared_result_id': prior_id,
                'source_request_reference': shared.request_reference,
                'superseded_by_shared_result_id': shared.id,
            },
        )

    return shared, notification_status
