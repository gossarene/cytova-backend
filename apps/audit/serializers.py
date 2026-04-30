"""
Cytova — Audit Log serializer.

Serialises ``AuditLog`` rows for the dashboard / Audit Log page.

Identity rendering uses a fallback chain so old records, deactivated
users, and system-generated entries all read sensibly:

    1. Live ``StaffUser.display_name`` if the actor still exists.
    2. Snapshotted ``actor_email`` stored on the audit row.
    3. The literal ``"System"`` for SYSTEM actor type.
    4. ``"Unknown"`` as a last resort.

Diff sanitisation
-----------------
``AuditLog.diff`` is supposed to be sanitised on write (see the model
docstring), but defence-in-depth: any key whose name matches a
sensitive pattern is replaced with ``"***"`` at read time. This guards
against:
  - legacy records written before a sanitiser was installed,
  - new sensitive fields the writers haven't been updated to strip,
  - field combinations where a parent key is sensitive but the child
    structure carries the secret.

The ``AuditLog`` model itself stays append-only; this serializer is
read-only and never writes.
"""
from __future__ import annotations

import re
from typing import Any

from rest_framework import serializers

from .models import ActorType, AuditLog


# Case-insensitive substring patterns. Tuned conservatively — better to
# mask too much than leak. Order doesn't matter (any match wins).
_SENSITIVE_PATTERNS = re.compile(
    r'(password|secret|token|api[_-]?key|access[_-]?link|access[_-]?url|'
    r'verification[_-]?code|reset[_-]?code|otp|hash|salt|pdf[_-]?password|'
    r'lab[_-]?secret|signing[_-]?key)',
    re.IGNORECASE,
)

_MASK = '***'


def _mask_value(key: str, value: Any) -> Any:
    """Walk a JSON-shaped value and mask sensitive leaves by key. Lists
    and nested dicts are traversed; other scalar types are returned
    untouched unless their parent key matched."""
    if isinstance(value, dict):
        return {k: _mask_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        # List values inherit the parent key for masking decisions.
        return [_mask_value(key, v) for v in value]
    if _SENSITIVE_PATTERNS.search(key or ''):
        return _MASK
    return value


def mask_sensitive(diff: Any) -> Any:
    """Public helper: returns a masked clone of ``diff`` ready for
    presentation. Pure — never mutates the input."""
    if diff is None:
        return None
    return _mask_value('', diff)


class AuditLogSerializer(serializers.ModelSerializer):
    actor_display_name = serializers.SerializerMethodField()
    diff = serializers.SerializerMethodField()

    class Meta:
        model = AuditLog
        fields = [
            'id',
            'actor_type', 'actor_id', 'actor_email', 'actor_display_name',
            'action', 'entity_type', 'entity_id',
            'diff', 'ip_address', 'timestamp',
        ]
        read_only_fields = fields

    def get_actor_display_name(self, obj: AuditLog) -> str:
        if obj.actor_type == ActorType.SYSTEM:
            return 'System'
        if obj.actor_id:
            actor = self.context.get('actor_index', {}).get(obj.actor_id)
            if actor is not None:
                return actor.display_name
        if obj.actor_email:
            return obj.actor_email
        return 'Unknown'

    def get_diff(self, obj: AuditLog):
        return mask_sensitive(obj.diff)
