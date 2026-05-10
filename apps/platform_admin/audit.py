"""
Platform-admin audit helper.

Centralises the audit-row construction so callers don't repeat the
``request.audit_ip`` / ``request.audit_user_agent`` extraction. Those
attributes are set by ``common.middleware`` on every authenticated
request — see the existing usage in ``apps.authentication.services``
for the convention.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from .models import (
    PlatformAdminAuditLog, PlatformAdminUser, PlatformAuditAction,
)


def log_platform_admin_action(
    *,
    request,
    action: PlatformAuditAction | str,
    actor: Optional[PlatformAdminUser] = None,
    actor_email: str = '',
    entity_type: str = '',
    entity_id: Optional[UUID] = None,
    metadata: Optional[dict] = None,
) -> PlatformAdminAuditLog:
    """Write a single audit row.

    ``actor`` is optional because the helper also serves the failed-
    login path where no user row matches the supplied email.
    ``actor_email`` is captured separately as an immutable snapshot
    so the audit row stays meaningful after the user is deleted.

    The helper does not catch DB exceptions — audit failure during a
    login attempt should surface, not be silently swallowed. The
    login service wraps the call in a transaction so a failed audit
    rolls back the ``last_login`` bump and the operation is visibly
    failed.
    """
    return PlatformAdminAuditLog.objects.create(
        actor=actor,
        actor_email=actor_email or (actor.email if actor else ''),
        action=action.value if hasattr(action, 'value') else action,
        entity_type=entity_type,
        entity_id=entity_id,
        ip_address=getattr(request, 'audit_ip', None),
        user_agent=getattr(request, 'audit_user_agent', '') or '',
        metadata=metadata or {},
    )
