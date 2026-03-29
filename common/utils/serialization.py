"""
Cytova — JSON Serialization Utility

Provides ``json_safe()`` to recursively convert Python objects into
JSON-serializable values before storing them in ``JSONField`` columns.

Why this matters
~~~~~~~~~~~~~~~~
Django's ``JSONField`` calls ``json.dumps()`` under the hood. Several
common Python types are NOT natively JSON-serializable:

    Decimal, UUID, datetime, date, model instances, …

If any of these leak into a ``JSONField`` value, the save will either
raise ``TypeError`` or — worse — silently produce a broken string via
``DjangoJSONEncoder`` without the developer realising the format is
inconsistent.

This utility guarantees a **uniform, predictable** format:

    Decimal   → ``str``  (preserves financial precision: ``"142.5000"``)
    UUID      → ``str``  (lowercase hyphenated: ``"a1b2c3d4-…"``)
    datetime  → ``str``  (ISO 8601 UTC: ``"2026-03-01T10:00:00+00:00"``)
    date      → ``str``  (ISO 8601: ``"2026-03-01"``)
    Model     → ``str``  (its primary key)
    Enum      → ``str``  (its ``.value``)
    set       → ``list``
    other     → ``str()`` fallback

Usage
~~~~~
::

    from common.utils.serialization import json_safe

    AuditLog.objects.create(
        …,
        diff=json_safe({'before': before, 'after': after}),
    )

In practice **you rarely need to call this manually** because
``AuditLog.save()`` and ``PlatformAuditLog.save()`` auto-sanitize
the ``diff`` field on every insert.
"""
from __future__ import annotations

import datetime
import enum
from decimal import Decimal
from uuid import UUID

from django.db import models


def json_safe(value):
    """
    Recursively convert *value* into a JSON-serializable structure.

    Handles ``dict``, ``list``, ``tuple``, ``set``, and all the common
    Django / Python types that are not natively JSON-safe.

    Returns the converted value — always a combination of ``dict``,
    ``list``, ``str``, ``int``, ``float``, ``bool``, and ``None``.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value

    if isinstance(value, dict):
        return {json_safe(k): json_safe(v) for k, v in value.items()}

    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]

    if isinstance(value, set):
        return [json_safe(item) for item in sorted(value, key=str)]

    if isinstance(value, Decimal):
        return str(value)

    if isinstance(value, UUID):
        return str(value)

    if isinstance(value, datetime.datetime):
        return value.isoformat()

    if isinstance(value, datetime.date):
        return value.isoformat()

    if isinstance(value, enum.Enum):
        return value.value

    if isinstance(value, models.Model):
        return str(value.pk)

    # Fallback — str() is always safe
    return str(value)
