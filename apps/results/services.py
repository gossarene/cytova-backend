"""
Cytova — Result Service

All write operations that carry business logic live here.
Views are thin: validate input → delegate to service.

ResultService:
    create              — create a DRAFT ExamResult for an item
    update              — update result data (DRAFT only; PUBLISHED is immutable)
    submit              — DRAFT → PENDING_VALIDATION
    validate            — PENDING_VALIDATION → VALIDATED
    reject_validation   — PENDING_VALIDATION → DRAFT (back for revision)
    publish             — VALIDATED → PUBLISHED (IRREVERSIBLE)

ResultFileService:
    upload              — validate, store, and record a file on a non-PUBLISHED result
    get_download_url    — generate a signed URL for a result file
    delete              — remove a file from a DRAFT result (storage + DB record)

Security invariants:
    - PUBLISHED results reject all update, submit, validate, reject, and file-delete
      operations at the service level (state machine enforces the same at model transitions).
    - file_key is never returned by any service method — callers use get_download_url().
    - Physical storage deletion failure is logged but does not abort the DB transaction
      (orphaned storage objects are preferable to orphaned DB records).
"""
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.audit.models import AuditLog, AuditAction, ActorType
from apps.files.signed_urls import generate_download_url
from apps.files.storage import delete_stored_file, store_result_file
from apps.users.models import StaffUser
from .models import ExamResult, ResultFile, ResultStatus
from .state_machine import ResultStateMachine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _audit(*, actor: StaffUser, action: str, entity_type: str, entity_id,
           diff: dict, request) -> None:
    AuditLog.objects.create(
        actor_type=ActorType.STAFF_USER,
        actor_id=actor.id,
        actor_email=actor.email,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        diff=diff,
        ip_address=getattr(request, 'audit_ip', None),
        user_agent=getattr(request, 'audit_user_agent', ''),
    )


# ---------------------------------------------------------------------------
# ResultService
# ---------------------------------------------------------------------------

class ResultService:

    @staticmethod
    def create(
        validated_data: dict,
        created_by: StaffUser,
        request,
    ) -> ExamResult:
        """
        Create a DRAFT ExamResult for the given item.
        If reference_range is not supplied, it is auto-populated from the item's
        LabExamSettings (if configured), then from ExamDefinition (no override).
        """
        item_id = validated_data.pop('item_id')

        # Resolve reference_range default from lab settings → exam definition
        reference_range = validated_data.get('reference_range', '')
        if not reference_range:
            from apps.requests.models import AnalysisRequestItem
            item = AnalysisRequestItem.objects.select_related(
                'exam_definition__lab_settings'
            ).get(pk=item_id)
            try:
                reference_range = item.exam_definition.lab_settings.reference_range
            except Exception:
                reference_range = ''
            validated_data['reference_range'] = reference_range

        result = ExamResult(
            item_id=item_id,
            created_by=created_by,
            **validated_data,
        )
        result.save()

        _audit(
            actor=created_by,
            action=AuditAction.CREATE,
            entity_type='ExamResult',
            entity_id=result.id,
            diff={'after': {
                'item_id': str(item_id),
                'status': ResultStatus.DRAFT,
            }},
            request=request,
        )

        return result

    @staticmethod
    def update(
        result: ExamResult,
        validated_data: dict,
        updated_by: StaffUser,
        request,
    ) -> ExamResult:
        """Update result data. Only allowed in DRAFT state."""
        if result.status == ResultStatus.PUBLISHED:
            raise ValidationError('Published results are immutable.')
        if result.status != ResultStatus.DRAFT:
            raise ValidationError(
                f'Results can only be edited in DRAFT state '
                f'(current: {result.status}).'
            )
        if not validated_data:
            return result

        before = {k: getattr(result, k) for k in validated_data}
        for field, value in validated_data.items():
            setattr(result, field, value)
        result.save(update_fields=list(validated_data.keys()) + ['updated_at'])
        after = {k: getattr(result, k) for k in validated_data}

        _audit(
            actor=updated_by,
            action=AuditAction.UPDATE,
            entity_type='ExamResult',
            entity_id=result.id,
            diff={'before': before, 'after': after},
            request=request,
        )

        return result

    @staticmethod
    def submit(
        result: ExamResult,
        submitted_by: StaffUser,
        request,
    ) -> ExamResult:
        """
        Transition DRAFT → PENDING_VALIDATION.
        The result_value must be non-empty before submission.
        """
        if not result.result_value.strip():
            raise ValidationError(
                'result_value must be set before submitting for validation.'
            )

        ResultStateMachine.transition(result, ResultStatus.PENDING_VALIDATION)
        result.save(update_fields=['status', 'updated_at'])

        _audit(
            actor=submitted_by,
            action=AuditAction.UPDATE,
            entity_type='ExamResult',
            entity_id=result.id,
            diff={'before': {'status': ResultStatus.DRAFT},
                  'after': {'status': ResultStatus.PENDING_VALIDATION}},
            request=request,
        )

        return result

    @staticmethod
    def validate(
        result: ExamResult,
        validation_notes: str,
        validated_by: StaffUser,
        request,
    ) -> ExamResult:
        """Transition PENDING_VALIDATION → VALIDATED."""
        ResultStateMachine.transition(result, ResultStatus.VALIDATED)

        result.validated_by = validated_by
        result.validated_at = timezone.now()
        result.validation_notes = validation_notes
        result.save(update_fields=[
            'status', 'validated_by', 'validated_at', 'validation_notes', 'updated_at',
        ])

        _audit(
            actor=validated_by,
            action=AuditAction.VALIDATE,
            entity_type='ExamResult',
            entity_id=result.id,
            diff={'before': {'status': ResultStatus.PENDING_VALIDATION},
                  'after': {'status': ResultStatus.VALIDATED,
                             'validated_by': str(validated_by.id)}},
            request=request,
        )

        return result

    @staticmethod
    def reject_validation(
        result: ExamResult,
        validation_notes: str,
        rejected_by: StaffUser,
        request,
    ) -> ExamResult:
        """
        Transition PENDING_VALIDATION → DRAFT (back for revision).
        Clears validation timestamps. validation_notes captures the rejection reason.
        """
        ResultStateMachine.transition(result, ResultStatus.DRAFT)

        result.validation_notes = validation_notes
        result.validated_by = None
        result.validated_at = None
        result.save(update_fields=[
            'status', 'validation_notes', 'validated_by', 'validated_at', 'updated_at',
        ])

        _audit(
            actor=rejected_by,
            action=AuditAction.UPDATE,
            entity_type='ExamResult',
            entity_id=result.id,
            diff={'before': {'status': ResultStatus.PENDING_VALIDATION},
                  'after': {'status': ResultStatus.DRAFT,
                             'validation_notes': validation_notes}},
            request=request,
        )

        return result

    @staticmethod
    def publish(
        result: ExamResult,
        published_by: StaffUser,
        request,
    ) -> ExamResult:
        """
        Transition VALIDATED → PUBLISHED.

        This transition is IRREVERSIBLE. The state machine permanently blocks
        any further transitions from PUBLISHED, and the model-level delete()
        is also blocked.
        """
        ResultStateMachine.transition(result, ResultStatus.PUBLISHED)

        result.published_by = published_by
        result.published_at = timezone.now()
        result.save(update_fields=[
            'status', 'published_by', 'published_at', 'updated_at',
        ])

        _audit(
            actor=published_by,
            action=AuditAction.PUBLISH,
            entity_type='ExamResult',
            entity_id=result.id,
            diff={'before': {'status': ResultStatus.VALIDATED},
                  'after': {'status': ResultStatus.PUBLISHED,
                             'published_by': str(published_by.id),
                             'published_at': str(result.published_at)}},
            request=request,
        )

        return result


# ---------------------------------------------------------------------------
# ResultFileService
# ---------------------------------------------------------------------------

class ResultFileService:

    @staticmethod
    @transaction.atomic
    def upload(
        result: ExamResult,
        file,
        uploaded_by: StaffUser,
        request,
    ) -> ResultFile:
        """
        Store a file and attach it to the given ExamResult.
        Rejected for PUBLISHED results — the document set is immutable once published.
        """
        if result.status == ResultStatus.PUBLISHED:
            raise ValidationError(
                'Files cannot be added to a PUBLISHED result.'
            )

        file_key, file_size = store_result_file(file, str(result.id))

        result_file = ResultFile(
            result=result,
            file_key=file_key,
            original_filename=getattr(file, 'name', 'file'),
            file_size=file_size,
            mime_type=getattr(file, 'content_type', 'application/octet-stream'),
            uploaded_by=uploaded_by,
        )
        result_file.save()

        _audit(
            actor=uploaded_by,
            action=AuditAction.CREATE,
            entity_type='ResultFile',
            entity_id=result_file.id,
            diff={'after': {
                'result_id': str(result.id),
                'original_filename': result_file.original_filename,
                'file_size': file_size,
                'mime_type': result_file.mime_type,
            }},
            request=request,
        )

        return result_file

    @staticmethod
    def get_download_url(result_file: ResultFile) -> dict:
        """
        Generate a time-limited signed URL for downloading a result file.
        The file_key is consumed internally and never returned to callers.

        Returns a dict suitable for SignedDownloadURLSerializer.
        """
        expires_in = getattr(settings, 'RESULT_FILE_SIGNED_URL_EXPIRY', 900)
        url = generate_download_url(result_file.file_key, expires_in=expires_in)
        return {
            'url': url,
            'expires_in': expires_in,
            'filename': result_file.original_filename,
        }

    @staticmethod
    @transaction.atomic
    def delete(
        result_file: ResultFile,
        deleted_by: StaffUser,
        request,
    ) -> None:
        """
        Remove a ResultFile.

        Only allowed on DRAFT results — files are locked after submission
        to prevent the evidence trail from being modified during review.
        PUBLISHED result files are permanently immutable.
        """
        result = result_file.result
        if result.status != ResultStatus.DRAFT:
            raise ValidationError(
                f'Files can only be deleted from DRAFT results '
                f'(current status: {result.status}).'
            )

        file_key = result_file.file_key
        file_id = result_file.id
        original_filename = result_file.original_filename

        # Remove DB record first (bypasses model-level guard)
        ResultFile.objects.filter(id=result_file.id).delete()

        # Remove from storage — failure is logged but does not abort
        delete_stored_file(file_key)

        _audit(
            actor=deleted_by,
            action=AuditAction.DELETE,
            entity_type='ResultFile',
            entity_id=file_id,
            diff={'before': {
                'result_id': str(result.id),
                'original_filename': original_filename,
            }},
            request=request,
        )
