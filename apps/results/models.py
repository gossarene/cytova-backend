"""
Cytova — Result Models

ExamResult
    One result per AnalysisRequestItem (OneToOneField).
    Lifecycle: DRAFT → PENDING_VALIDATION → VALIDATED → PUBLISHED.
    PUBLISHED is terminal and irreversible — enforced by the state machine
    and at the service layer. Hard delete is blocked at the model level.

ResultFile
    A PDF or image file attached to an ExamResult. The internal storage key
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
    DRAFT              = 'DRAFT',              'Draft'
    PENDING_VALIDATION = 'PENDING_VALIDATION', 'Pending Validation'
    VALIDATED          = 'VALIDATED',          'Validated'
    PUBLISHED          = 'PUBLISHED',          'Published'


class ExamResult(BaseModel):
    """
    The laboratory finding for a single AnalysisRequestItem.

    result_value  — primary result (numeric string, text, "See attached", …)
    result_unit   — optional unit (e.g. "g/dL")
    reference_range — normal range for this exam; defaults to LabExamSettings
                      value when the result is created, but can be overridden.
    is_abnormal   — explicit abnormality flag; set by the entering technician.
    comments      — visible on the result document delivered to the patient.
    internal_notes — lab-internal notes; NOT visible externally.
    validation_notes — biologist's review notes (used for both approval and
                        rejection feedback).

    PUBLISHED results are fully immutable. The state machine blocks further
    transitions; the service layer rejects any update attempt.
    """
    item = models.OneToOneField(
        'analysis_requests.AnalysisRequestItem',
        on_delete=models.PROTECT,
        related_name='result',
    )
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

    # Biologist review notes — populated at validate or reject_validation
    validation_notes = models.TextField(blank=True, default='')

    # Validation trail
    validated_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='validated_results',
    )
    validated_at = models.DateTimeField(null=True, blank=True)

    # Publish trail
    published_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='published_results',
    )
    published_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_results',
    )

    class Meta:
        verbose_name = 'Exam Result'
        verbose_name_plural = 'Exam Results'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'is_abnormal']),
        ]

    def __str__(self):
        return (
            f'Result for {self.item} '
            f'[{self.get_status_display()}]'
        )

    def delete(self, *args, **kwargs):
        """Exam results are medical records — hard delete is permanently blocked."""
        raise PermissionError(
            'Exam results cannot be deleted. '
            'Published results are immutable; draft results can be superseded.'
        )


class ResultFile(models.Model):
    """
    A file (PDF or image scan) attached to an ExamResult.

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
        ExamResult,
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
        """
        Hard delete is blocked at the model level.
        Use ResultFileService.delete() which validates result state first,
        then removes both the DB record and the stored object via queryset.delete().
        """
        raise PermissionError(
            'ResultFile.delete() is blocked. '
            'Use ResultFileService.delete() to remove files from DRAFT results.'
        )
