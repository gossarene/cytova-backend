import uuid
from django.db import models
from django.utils import timezone

from common.utils.serialization import json_safe


class ActorType(models.TextChoices):
    STAFF_USER = 'STAFF_USER', 'Staff User'
    PATIENT_PORTAL = 'PATIENT_PORTAL', 'Patient Portal'
    SYSTEM = 'SYSTEM', 'System'
    PLATFORM_ADMIN = 'PLATFORM_ADMIN', 'Platform Admin'


class AuditAction(models.TextChoices):
    CREATE = 'CREATE', 'Create'
    UPDATE = 'UPDATE', 'Update'
    DELETE = 'DELETE', 'Delete'
    LOGIN = 'LOGIN', 'Login'
    LOGIN_FAILED = 'LOGIN_FAILED', 'Login Failed'
    LOGOUT = 'LOGOUT', 'Logout'
    VIEW = 'VIEW', 'View'
    VALIDATE = 'VALIDATE', 'Validate'
    PUBLISH = 'PUBLISH', 'Publish'
    CONFIRM = 'CONFIRM', 'Confirm'
    CANCEL = 'CANCEL', 'Cancel'
    DEACTIVATE = 'DEACTIVATE', 'Deactivate'
    TOKEN_REVOKED = 'TOKEN_REVOKED', 'Token Revoked'
    PASSWORD_RESET = 'PASSWORD_RESET', 'Password Reset'


class AuditLog(models.Model):
    """
    Immutable audit trail for all significant events within a tenant.
    Lives in each tenant's private schema.

    Security invariants:
    - Append-only: save() raises PermissionError if the record already exists.
    - delete() is permanently blocked at the model level.
    - The application DB user is granted INSERT + SELECT only on this table.
    - actor_email is snapshotted (not a FK) so records survive user deactivation.
    - Sensitive fields (password_hash, etc.) must be excluded before writing diff.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    actor_type = models.CharField(
        max_length=20, choices=ActorType.choices, db_index=True
    )
    # actor_id is a soft reference (not a FK) — users may be deactivated later.
    actor_id = models.UUIDField(null=True, blank=True, db_index=True)
    actor_email = models.CharField(max_length=255, null=True, blank=True)

    action = models.CharField(
        max_length=20, choices=AuditAction.choices, db_index=True
    )
    entity_type = models.CharField(max_length=100, db_index=True)
    entity_id = models.UUIDField(null=True, blank=True, db_index=True)

    # For UPDATE actions: {"before": {...}, "after": {...}}
    # Sensitive fields are stripped before storage.
    diff = models.JSONField(null=True, blank=True)

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, null=True, blank=True)
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        verbose_name = 'Audit Log'
        verbose_name_plural = 'Audit Logs'
        ordering = ['-timestamp']

    def __str__(self):
        return (
            f'[{self.action}] {self.entity_type}({self.entity_id}) '
            f'by {self.actor_email} at {self.timestamp:%Y-%m-%d %H:%M:%S}'
        )

    def save(self, *args, **kwargs):
        """Block updates. AuditLog entries are write-once. Auto-sanitize diff."""
        if self._state.adding is False:
            raise PermissionError('AuditLog entries are immutable and cannot be updated.')
        if self.diff is not None:
            self.diff = json_safe(self.diff)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Block all deletions."""
        raise PermissionError('AuditLog entries cannot be deleted.')
