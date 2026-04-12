"""
Cytova — Result Models

ResultVersion
    A versioned result for a single AnalysisRequestItem. One item can have
    many result versions; exactly one is marked ``is_current=True`` at any
    time.

    Version lifecycle: DRAFT → SUBMITTED → VALIDATED → PUBLISHED  (happy path)
                       DRAFT → SUBMITTED → REJECTED                (terminal for version)

    When a version is REJECTED, the technician creates a new version (higher
    version_number, is_current=True) while the rejected version remains as a
    permanent historical record.

    PUBLISHED is terminal and irreversible — enforced by the state machine
    and at the service layer. Hard delete is blocked at the model level.

ResultFile
    A PDF or image file attached to a ResultVersion. The internal storage key
    (file_key) is never exposed to API clients; access is always mediated
    through apps/files/signed_urls.py.
    Files can only be deleted from DRAFT results. Hard delete is blocked;
    the service uses queryset.delete() with prior state validation.
"""
import uuid

from django.db import models
from django.utils import timezone

from common.models import BaseModel


class ResultStatus(models.TextChoices):
    DRAFT     = 'DRAFT',     'Draft'
    SUBMITTED = 'SUBMITTED', 'Submitted'
    REJECTED  = 'REJECTED',  'Rejected'
    VALIDATED = 'VALIDATED',  'Validated'
    PUBLISHED = 'PUBLISHED', 'Published'



class ResultVersion(BaseModel):
    """
    A single versioned result entry for an AnalysisRequestItem.

    Design rationale: results are never overwritten. Each edit cycle
    (enter → submit → review) produces a new version. Rejected versions
    remain as immutable historical records. Only one version per item
    is ``is_current=True`` at any given time — this is the version
    biologists see and review.

    result_value  — primary result (numeric string, text, "See attached", …)
    result_unit   — optional unit (e.g. "g/dL")
    reference_range — normal range; defaults from LabExamSettings.
    is_abnormal   — explicit abnormality flag set by the entering technician.
    comments      — visible on the result document delivered to the patient.
    internal_notes — lab-internal notes; NOT visible externally.
    """
    item = models.ForeignKey(
        'analysis_requests.AnalysisRequestItem',
        on_delete=models.PROTECT,
        related_name='result_versions',
    )
    version_number = models.PositiveIntegerField(default=1)
    is_current = models.BooleanField(default=True, db_index=True)

    status = models.CharField(
        max_length=25,
        choices=ResultStatus.choices,
        default=ResultStatus.DRAFT,
        db_index=True,
    )

    # Core result data
    result_value = models.TextField(blank=True, default='')
    result_unit = models.CharField(max_length=50, blank=True, default='')
    reference_range = models.CharField(max_length=100, blank=True, default='')
    is_abnormal = models.BooleanField(default=False, db_index=True)

    # Document-visible remarks
    comments = models.TextField(
        blank=True,
        default='',
        help_text='Visible on the result document delivered to the patient/doctor.',
    )

    # Internal lab notes (not in patient-facing document)
    internal_notes = models.TextField(blank=True, default='')

    # Entry trail
    entered_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        related_name='entered_results',
    )
    entered_at = models.DateTimeField(default=timezone.now)

    # Submission trail
    submitted_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='submitted_results',
    )
    submitted_at = models.DateTimeField(null=True, blank=True)

    # Biologist review trail — populated at validate or reject
    validation_notes = models.TextField(blank=True, default='')
    validated_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='validated_results',
    )
    validated_at = models.DateTimeField(null=True, blank=True)

    # Rejection trail — populated when biologist rejects
    rejected_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='rejected_results',
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejection_notes = models.TextField(blank=True, default='')

    # Publish trail
    published_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='published_results',
    )
    published_at = models.DateTimeField(null=True, blank=True)

    notes = models.TextField(
        blank=True,
        default='',
        help_text='Free-form notes about this result version.',
    )

    class Meta:
        verbose_name = 'Result Version'
        verbose_name_plural = 'Result Versions'
        ordering = ['-version_number']
        constraints = [
            models.UniqueConstraint(
                fields=['item', 'version_number'],
                name='unique_item_version_number',
            ),
            models.UniqueConstraint(
                fields=['item'],
                condition=models.Q(is_current=True),
                name='unique_current_version_per_item',
            ),
        ]
        indexes = [
            models.Index(fields=['item', 'is_current']),
            models.Index(fields=['status', 'is_abnormal']),
        ]

    def __str__(self):
        return (
            f'Result v{self.version_number} for {self.item} '
            f'[{self.get_status_display()}]'
        )

    def delete(self, *args, **kwargs):
        raise PermissionError(
            'Result versions are medical records — hard delete is permanently blocked.'
        )


class ResultFile(models.Model):
    """
    A file (PDF or image scan) attached to a ResultVersion.

    file_key stores the internal S3/MinIO object key. It is NEVER returned
    to API clients. Clients obtain time-limited signed URLs via the dedicated
    download endpoint which calls apps.files.signed_urls.generate_download_url().

    original_filename is the name uploaded by the user, kept for display only.
    """
    ALLOWED_MIME_TYPES = frozenset({
        'application/pdf',
        'image/jpeg',
        'image/png',
        'image/tiff',
    })

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    result = models.ForeignKey(
        ResultVersion,
        on_delete=models.CASCADE,
        related_name='files',
    )
    file_key = models.CharField(
        max_length=500,
        help_text='Internal storage key — never exposed to clients.',
    )
    original_filename = models.CharField(
        max_length=255,
        help_text='Display name only. The stored file name is a UUID-based key.',
    )
    file_size = models.PositiveIntegerField(help_text='Size in bytes.')
    mime_type = models.CharField(max_length=100)
    uploaded_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        related_name='uploaded_result_files',
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = 'Result File'
        verbose_name_plural = 'Result Files'
        ordering = ['created_at']

    def __str__(self):
        return f'{self.original_filename} → {self.result}'

    def delete(self, *args, **kwargs):
        raise PermissionError(
            'ResultFile.delete() is blocked. '
            'Use ResultFileService.delete() to remove files from DRAFT results.'
        )
