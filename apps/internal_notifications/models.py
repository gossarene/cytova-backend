"""
Cytova — InternalNotificationLog model.

One row per *attempt* to deliver an internal-workflow email.
Designed so the unique ``dedupe_key`` is the race guard: two
parallel submits both computing the same key collide on insert,
and the second caller treats the IntegrityError as "already
queued by someone else, nothing to do".

Rows are written BEFORE the send (with ``status='PENDING'``) so a
hard crash in the email provider leaves a trail. The send path
then flips the row to ``SENT`` or ``FAILED`` based on the
``EmailResult`` returned by the provider.

Privacy contract
----------------
``recipient_email`` is the staff user's work address — never a
patient address. The recipient resolver
(``services.resolve_*_recipients``) is the single source of
truth and queries only ``apps.users.StaffUser`` /
``ResultVersion.submitted_by``; it does NOT touch
``apps.patients`` or ``apps.patient_portal``. The model
deliberately has no ``payload`` / ``body`` JSON field so a
buggy caller can't write clinical content into the audit trail.
"""
from __future__ import annotations

import uuid

from django.db import models


class InternalNotificationEvent(models.TextChoices):
    """Event-type taxonomy. The dedupe key is derived from event +
    primary entity + cycle, so adding a new event here requires
    deciding the corresponding key shape too."""
    REQUEST_READY_FOR_REVIEW = (
        'REQUEST_READY_FOR_REVIEW',
        'Request ready for biological review',
    )
    RESULT_REJECTED = (
        'RESULT_REJECTED',
        'Result rejected by biologist',
    )


class InternalNotificationStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending delivery'
    SENT = 'SENT', 'Delivered'
    FAILED = 'FAILED', 'Delivery failed'


class InternalNotificationLog(models.Model):
    """Append-style record of one notification-attempt.

    The ``dedupe_key`` UNIQUE constraint is the load-bearing
    safety property: it prevents two simultaneous submits / two
    racing transactions from producing two emails. Keys are
    constructed by the service layer (never by the caller) so
    the format stays consistent.

    Why the model lives in the tenant schema:
      - ``request`` is a tenant-local FK (a request never
        crosses tenants).
      - The dedupe key embeds an opaque request_id; collisions
        across tenants are impossible because the schemas are
        physically separate.
      - The recipient resolver also queries tenant tables only.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    event_type = models.CharField(
        max_length=40,
        choices=InternalNotificationEvent.choices,
        db_index=True,
    )

    # Strong refs — both FKs are PROTECT'd so deleting a request or
    # a result version cannot leave dangling notification rows. In
    # practice both targets are themselves PROTECT-blocked from
    # deletion, but the explicit constraint matches the
    # append-only audit philosophy.
    request = models.ForeignKey(
        'analysis_requests.AnalysisRequest',
        on_delete=models.PROTECT,
        related_name='internal_notifications',
    )
    result_version = models.ForeignKey(
        'results.ResultVersion',
        on_delete=models.PROTECT,
        null=True, blank=True,
        related_name='internal_notifications',
        help_text='Only set on RESULT_REJECTED events; null for '
                  'request-level events.',
    )

    # Recipient identity — store both the FK (when resolvable to a
    # current StaffUser row) AND the email snapshot. The FK can
    # become null if the user is later deleted; the snapshot keeps
    # the audit trail readable.
    recipient_user = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='received_internal_notifications',
    )
    recipient_email = models.EmailField(
        help_text='Email-at-attempt-time snapshot. NEVER a patient address — '
                  'recipients are resolved exclusively from StaffUser.',
    )

    status = models.CharField(
        max_length=10,
        choices=InternalNotificationStatus.choices,
        default=InternalNotificationStatus.PENDING,
        db_index=True,
    )

    # ``dedupe_key`` is unique within the tenant schema. The format
    # is documented in ``services.build_dedupe_key``; do not write
    # arbitrary strings here at call sites.
    dedupe_key = models.CharField(max_length=200, unique=True, db_index=True)

    error_message = models.TextField(blank=True, default='')

    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Internal Notification Log'
        verbose_name_plural = 'Internal Notification Logs'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['event_type', 'created_at']),
            models.Index(fields=['request', 'event_type']),
        ]

    def __str__(self) -> str:
        return (
            f'[{self.event_type}] → {self.recipient_email} '
            f'({self.status})'
        )

    def delete(self, *args, **kwargs):  # noqa: D401
        # Notification logs are evidence of an attempted
        # delivery; deletion would hide ops history. Mirror the
        # pattern used by AuditLog / PlatformAdminAuditLog.
        raise PermissionError(
            'Internal notification logs cannot be deleted.'
        )
