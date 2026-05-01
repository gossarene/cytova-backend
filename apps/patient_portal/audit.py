"""
Cytova — Patient Portal audit helper.

Single entry point for writing ``PatientPortalAuditLog`` rows. The
helper enforces a small allow-list of metadata keys per action so
callers can't accidentally leak storage paths, file tokens, or
medical content into the audit JSON. The contract documented on the
model's docstring is *enforced* here, not just hoped for.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional
from uuid import UUID

from .models import (
    PatientAccount, PatientPortalAuditAction, PatientPortalAuditLog,
)

logger = logging.getLogger(__name__)


# Allow-list of metadata keys per action. Anything not in this set
# is silently dropped before the row is written — defence in depth
# against future callers passing the wrong dict.
#
# Keys in this allow-list MUST be:
#   - either an ID known to both sides (UUID surfaces fine to JSON)
#   - or a human-readable counter (string / int)
#
# Specifically NOT allowed:
#   - storage_key, file_token, file path
#   - patient PII (name, email, DOB, phone)
#   - medical content (exam codes, values)
_ALLOWED_METADATA_KEYS: dict[str, frozenset[str]] = {
    PatientPortalAuditAction.PATIENT_RESULT_SHARED.value: frozenset({
        'shared_result_id', 'source_request_reference', 'source_name',
        'email_notification_status',
    }),
    PatientPortalAuditAction.PATIENT_RESULT_DOWNLOADED.value: frozenset({
        'shared_result_id', 'file_id', 'download_count_after',
    }),
    PatientPortalAuditAction.PATIENT_RESULT_HIDDEN_BY_PATIENT.value: frozenset({
        'shared_result_id',
    }),
    PatientPortalAuditAction.PATIENT_RESULT_REVOKED_BY_LAB.value: frozenset({
        'shared_result_id', 'source_request_reference', 'revoked_by_lab',
    }),
    # Version-aware events. ``report_version_number`` is the lab's
    # internal version index — already public to the patient via the
    # "Version N" badge, so it's safe to log here. ``shared_channel``
    # is the enum value (CYTOVA / EMAIL / …); never the recipient
    # email or any token.
    PatientPortalAuditAction.PATIENT_VERSION_SHARED.value: frozenset({
        'shared_result_id', 'source_request_reference',
        'report_version_number', 'shared_channel',
    }),
    PatientPortalAuditAction.PATIENT_VERSION_SUPERSEDED.value: frozenset({
        'shared_result_id', 'source_request_reference',
        'report_version_number', 'superseded_by_shared_result_id',
    }),
}


def _filter_metadata(action: str, metadata: Optional[Mapping[str, Any]]) -> dict:
    """Drop any metadata key not on the allow-list for this action and
    coerce UUIDs to strings so the JSON serializer stays happy. Returns
    a fresh dict — callers may keep mutating their original."""
    if not metadata:
        return {}
    allowed = _ALLOWED_METADATA_KEYS.get(action, frozenset())
    cleaned: dict[str, Any] = {}
    for key, value in metadata.items():
        if key not in allowed:
            continue
        cleaned[key] = str(value) if isinstance(value, UUID) else value
    return cleaned


def write_event(
    *,
    action: str,
    entity_type: str,
    entity_id: Optional[UUID] = None,
    patient_account: Optional[PatientAccount] = None,
    request=None,
    metadata: Optional[Mapping[str, Any]] = None,
) -> PatientPortalAuditLog:
    """Persist one ``PatientPortalAuditLog`` row.

    All callers go through this helper so the metadata allow-list is
    enforced uniformly and the (request → IP/UA) extraction lives in
    one place. Failure to write is logged but never raised — patient
    workflows must not fail because the audit table is briefly
    unavailable.
    """
    safe_metadata = _filter_metadata(action, metadata)
    try:
        return PatientPortalAuditLog.objects.create(
            patient_account=patient_account,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            ip_address=getattr(request, 'audit_ip', None) if request else None,
            user_agent=(
                (getattr(request, 'audit_user_agent', '') or '')[:500]
                if request else ''
            ),
            metadata=safe_metadata,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            'PatientPortalAuditLog write failed: action=%s entity_id=%s err=%s',
            action, entity_id, exc,
        )
        # Return a transient unsaved instance so type-conscious callers
        # don't have to handle ``None`` — they shouldn't be reading the
        # PK anyway.
        return PatientPortalAuditLog(
            action=action, entity_type=entity_type, entity_id=entity_id,
            patient_account=patient_account, metadata=safe_metadata,
        )
