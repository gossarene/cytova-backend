"""
Cytova — Platform Audit Logger

Platform-level audit for actions on the public schema (tenants,
subscriptions, plans). Stored in a dedicated PlatformAuditLog model
in the public schema — separate from the per-tenant AuditLog.
"""
import uuid

from django.db import models
from django.utils import timezone

from common.utils.serialization import json_safe


class PlatformAction(models.TextChoices):
    CREATE     = 'CREATE',     'Create'
    UPDATE     = 'UPDATE',     'Update'
    ACTIVATE   = 'ACTIVATE',   'Activate'
    SUSPEND    = 'SUSPEND',    'Suspend'
    CANCEL     = 'CANCEL',     'Cancel'
    DEACTIVATE = 'DEACTIVATE', 'Deactivate'
    PLAN_CHANGE = 'PLAN_CHANGE', 'Plan Change'


class PlatformAuditLog(models.Model):
    """
    Immutable audit trail for platform-level actions.
    Lives in the public schema (apps.tenants is in SHARED_APPS).

    actor is either a PlatformAdmin or 'system' for automated actions.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor_email = models.CharField(max_length=255)
    action = models.CharField(max_length=20, choices=PlatformAction.choices, db_index=True)
    entity_type = models.CharField(max_length=50, db_index=True)
    entity_id = models.UUIDField(null=True, blank=True, db_index=True)
    diff = models.JSONField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        verbose_name = 'Platform Audit Log'
        verbose_name_plural = 'Platform Audit Logs'
        ordering = ['-timestamp']

    def __str__(self):
        return f'[{self.action}] {self.entity_type}({self.entity_id}) by {self.actor_email}'

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise PermissionError('Platform audit logs are immutable.')
        if self.diff is not None:
            self.diff = json_safe(self.diff)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError('Platform audit logs cannot be deleted.')


def log_platform_action(*, request, action: str, entity_type: str,
                        entity_id, diff: dict) -> PlatformAuditLog:
    """Helper to create a platform audit log entry."""
    actor_email = 'system'
    if hasattr(request, 'user') and request.user and hasattr(request.user, 'email'):
        actor_email = request.user.email

    ip_address = None
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        ip_address = forwarded.split(',')[0].strip()
    else:
        ip_address = request.META.get('REMOTE_ADDR')

    return PlatformAuditLog.objects.create(
        actor_email=actor_email,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        diff=diff,
        ip_address=ip_address,
    )
