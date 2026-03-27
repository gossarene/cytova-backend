"""
Cytova — Base Models

All tenant-scoped domain models should extend BaseModel or SoftDeleteModel.
These are abstract and create no database tables themselves.
"""
import uuid
from django.db import models
from django.utils import timezone


class BaseModel(models.Model):
    """
    Abstract base for all tenant-scoped domain entities.

    Provides:
    - UUID primary key (not sequential integers — prevents enumeration)
    - created_at with db_index (used by cursor pagination default ordering)
    - updated_at (auto-managed)
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def __repr__(self):
        return f'<{self.__class__.__name__} id={self.id}>'


# ---------------------------------------------------------------------------
# Soft Delete
# ---------------------------------------------------------------------------

class SoftDeleteQuerySet(models.QuerySet):
    def delete(self):
        """Soft-delete all records in the queryset."""
        return self.update(deleted_at=timezone.now())

    def hard_delete(self):
        """Permanently delete all records (use with caution)."""
        return super().delete()

    def alive(self):
        """Records that have NOT been soft-deleted."""
        return self.filter(deleted_at__isnull=True)

    def deleted(self):
        """Records that HAVE been soft-deleted."""
        return self.filter(deleted_at__isnull=False)


class SoftDeleteManager(models.Manager):
    """
    Default manager: returns only non-deleted records.
    Use .with_deleted() to include soft-deleted records.
    """

    def get_queryset(self):
        return SoftDeleteQuerySet(self.model, using=self._db).alive()

    def with_deleted(self):
        """Return all records including soft-deleted ones."""
        return SoftDeleteQuerySet(self.model, using=self._db)


class SoftDeleteModel(BaseModel):
    """
    Abstract model with soft-delete support.

    Records are never hard-deleted by default. A deleted_at timestamp is set
    instead, and the default manager filters them out automatically.

    Usage:
        instance.delete()          → sets deleted_at, hides from queries
        instance.hard_delete()     → permanent removal (rare)
        instance.restore()         → clears deleted_at, makes visible again
        Model.objects.with_deleted().filter(...) → include deleted records
    """
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects = SoftDeleteManager()
    all_objects = models.Manager()  # Unfiltered access when explicitly needed

    class Meta:
        abstract = True

    def delete(self, using=None, keep_parents=False):
        self.deleted_at = timezone.now()
        self.save(update_fields=['deleted_at', 'updated_at'])

    def hard_delete(self, using=None, keep_parents=False):
        return super().delete(using=using, keep_parents=keep_parents)

    def restore(self):
        self.deleted_at = None
        self.save(update_fields=['deleted_at', 'updated_at'])

    @property
    def is_deleted(self):
        return self.deleted_at is not None
