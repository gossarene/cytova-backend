"""
Cytova — Result Service

All write operations that carry business logic live here.
Views are thin: validate input → delegate to service.

ResultVersionService:
    create_draft        — create a new DRAFT version for an item
    update_draft        — update result data (DRAFT + is_current only)
    submit              — DRAFT → SUBMITTED; item → UNDER_REVIEW
    validate            — SUBMITTED → VALIDATED (biologist)
    reject              — SUBMITTED → REJECTED; item → RESULT_ENTERED (biologist)
    publish             — VALIDATED → PUBLISHED (IRREVERSIBLE)

ResultFileService:
    upload              — validate, store, and record a file on a non-PUBLISHED result
    get_download_url    — generate a signed URL for a result file
    delete              — remove a file from a DRAFT result (storage + DB record)

Security invariants:
    - PUBLISHED results reject all update, submit, validate, reject, and file-delete
      operations at the service level (state machine enforces the same).
    - file_key is never returned by any service method — callers use get_download_url().
    - Physical storage deletion failure is logged but does not abort the DB transaction.
"""
import logging

from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.audit.models import AuditLog, AuditAction, ActorType
from apps.files.signed_urls import generate_download_url
from apps.files.storage import delete_stored_file, store_result_file
from apps.requests.models import (
    AnalysisRequestItem, ItemStatus, RequestStatus,
)
from apps.requests.state_machine import RequestStateMachine, ItemStateMachine
from apps.users.models import StaffUser
from .models import ResultVersion, ResultValue, ResultFile, ResultStatus
from .state_machine import ResultStateMachine as VersionStateMachine

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


_RESULT_ENTRY_ELIGIBLE = {
    ItemStatus.COLLECTED,
    ItemStatus.RESULT_ENTERED,
}


def _create_result_values(version: ResultVersion, values_input: list, exam_def) -> list:
    """
    Create ResultValue rows for a version based on structured input.
    Snapshots metadata from the catalog at creation time.
    Returns the created rows.
    """
    from apps.catalog.models import ResultStructure, ExamParameter

    structure = exam_def.result_structure

    if structure == ResultStructure.SINGLE_VALUE:
        if len(values_input) > 1:
            raise ValidationError(
                'Single-value exams expect at most one value entry.'
            )
        row = values_input[0] if values_input else {}
        return [ResultValue.objects.create(
            result_version=version,
            parameter=None,
            name_snapshot='',
            value=row.get('value', version.result_value),
            unit_snapshot=exam_def.unit,
            reference_range_snapshot=exam_def.reference_range,
            is_abnormal=row.get('is_abnormal', version.is_abnormal),
            display_order=0,
        )]

    # MULTI_PARAMETER
    if not values_input:
        return []

    param_ids = [v['parameter_id'] for v in values_input if v.get('parameter_id')]
    if len(param_ids) != len(set(param_ids)):
        raise ValidationError('Duplicate parameter entries are not allowed.')

    valid_params = {
        str(p.id): p
        for p in ExamParameter.objects.filter(
            exam_definition=exam_def, is_active=True,
        )
    }
    for pid in param_ids:
        if str(pid) not in valid_params:
            raise ValidationError(
                f'Parameter {pid} is not a valid active parameter '
                f'for exam {exam_def.code}.'
            )

    created = []
    for v in values_input:
        pid = v.get('parameter_id')
        if not pid:
            raise ValidationError(
                'parameter_id is required for multi-parameter exam values.'
            )
        param = valid_params[str(pid)]
        created.append(ResultValue.objects.create(
            result_version=version,
            parameter=param,
            name_snapshot=param.name,
            value=v.get('value', ''),
            unit_snapshot=param.unit,
            reference_range_snapshot=param.reference_range,
            is_abnormal=v.get('is_abnormal', False),
            display_order=param.display_order,
        ))
    return created


def _replace_result_values(version: ResultVersion, values_input: list, exam_def) -> list:
    """Replace all value rows on a DRAFT version (for update)."""
    ResultValue.objects.filter(result_version=version).delete()
    return _create_result_values(version, values_input, exam_def)


# ---------------------------------------------------------------------------
# ResultVersionService
# ---------------------------------------------------------------------------

class ResultVersionService:

    @staticmethod
    @transaction.atomic
    def create_draft(
        item: AnalysisRequestItem,
        entered_by: StaffUser,
        request,
        result_value: str = '',
        result_unit: str = '',
        reference_range: str = '',
        is_abnormal: bool = False,
        comments: str = '',
        internal_notes: str = '',
        notes: str = '',
        values: list | None = None,
    ) -> ResultVersion:
        """
        Create a new DRAFT result version for the given item.

        For SINGLE_VALUE exams, accepts either the legacy flat fields
        (result_value, result_unit, etc.) or a ``values`` list with one
        entry. For MULTI_PARAMETER exams, ``values`` must contain
        parameter_id + value pairs — the backend snapshots metadata.
        """
        if item.status not in _RESULT_ENTRY_ELIGIBLE:
            raise ValidationError(
                f'Result entry requires item status COLLECTED or RESULT_ENTERED '
                f'(current: {item.status}).'
            )

        current = item.result_versions.filter(is_current=True).first()
        if current and current.status not in {ResultStatus.REJECTED}:
            raise ValidationError(
                f'A {current.status} result version already exists for this item. '
                f'Edit or submit the existing version before creating a new one.'
            )

        exam_def = item.exam_definition

        if not reference_range:
            try:
                reference_range = exam_def.lab_settings.reference_range
            except Exception:
                reference_range = ''

        if current:
            current.is_current = False
            current.save(update_fields=['is_current', 'updated_at'])

        max_version = (
            item.result_versions.aggregate(m=Max('version_number'))['m'] or 0
        )

        version = ResultVersion(
            item=item,
            version_number=max_version + 1,
            is_current=True,
            status=ResultStatus.DRAFT,
            result_value=result_value,
            result_unit=result_unit,
            reference_range=reference_range,
            is_abnormal=is_abnormal,
            comments=comments,
            internal_notes=internal_notes,
            notes=notes,
            entered_by=entered_by,
            entered_at=timezone.now(),
        )
        version.save()

        _create_result_values(
            version,
            values if values is not None else [],
            exam_def,
        )

        if item.status == ItemStatus.COLLECTED:
            ItemStateMachine.transition(item, ItemStatus.RESULT_ENTERED)
            item.save(update_fields=['status', 'updated_at'])

        _audit(
            actor=entered_by,
            action=AuditAction.CREATE,
            entity_type='ResultVersion',
            entity_id=version.id,
            diff={'after': {
                'item_id': str(item.id),
                'version_number': version.version_number,
                'status': ResultStatus.DRAFT,
            }},
            request=request,
        )

        return version

    @staticmethod
    def update_draft(
        version: ResultVersion,
        validated_data: dict,
        updated_by: StaffUser,
        request,
    ) -> ResultVersion:
        """Update result data. Only allowed on current DRAFT versions."""
        if version.status != ResultStatus.DRAFT:
            raise ValidationError(
                f'Results can only be edited in DRAFT state '
                f'(current: {version.status}).'
            )
        if not version.is_current:
            raise ValidationError('Only the current version can be edited.')

        values_input = validated_data.pop('values', None)

        if validated_data:
            before = {k: getattr(version, k) for k in validated_data}
            for field, value in validated_data.items():
                setattr(version, field, value)
            version.save(update_fields=list(validated_data.keys()) + ['updated_at'])
            after = {k: getattr(version, k) for k in validated_data}

            _audit(
                actor=updated_by,
                action=AuditAction.UPDATE,
                entity_type='ResultVersion',
                entity_id=version.id,
                diff={'before': before, 'after': after},
                request=request,
            )

        if values_input is not None:
            exam_def = version.item.exam_definition
            _replace_result_values(version, values_input, exam_def)

        return version

    @staticmethod
    def update_review_comments(
        version: ResultVersion,
        validated_data: dict,
        updated_by: StaffUser,
        request,
    ) -> ResultVersion:
        """
        Update the patient-facing ``comments`` field (and optionally
        ``validation_notes``) on a current result version that is under
        biologist review.

        Allowed on SUBMITTED or VALIDATED versions while the parent
        request has not been finalized. Once the request reaches
        VALIDATED, the comments are locked.
        """
        if version.status not in {ResultStatus.SUBMITTED, ResultStatus.VALIDATED}:
            raise ValidationError(
                'Comments can only be edited on SUBMITTED or VALIDATED results.'
            )
        if not version.is_current:
            raise ValidationError('Only the current version can be edited.')

        ar = version.item.analysis_request
        if ar.status == RequestStatus.VALIDATED:
            raise ValidationError(
                'The request has been finalized. Comments can no longer '
                'be edited.'
            )

        allowed_fields = {'comments', 'validation_notes'}
        update_fields = {k: v for k, v in validated_data.items()
                         if k in allowed_fields}
        if not update_fields:
            return version

        before = {k: getattr(version, k) for k in update_fields}
        for field, value in update_fields.items():
            setattr(version, field, value)
        version.save(update_fields=list(update_fields.keys()) + ['updated_at'])
        after = {k: getattr(version, k) for k in update_fields}

        _audit(
            actor=updated_by,
            action=AuditAction.UPDATE,
            entity_type='ResultVersion',
            entity_id=version.id,
            diff={'before': before, 'after': after},
            request=request,
        )

        return version

    @staticmethod
    @transaction.atomic
    def submit(
        version: ResultVersion,
        submitted_by: StaffUser,
        request,
    ) -> ResultVersion:
        """
        Transition DRAFT → SUBMITTED.

        Completeness check depends on the exam's result_structure:
        - SINGLE_VALUE: the legacy result_value field must be non-empty
        - MULTI_PARAMETER: every active parameter must have a non-empty
          ResultValue row
        """
        from apps.catalog.models import ResultStructure

        if not version.is_current:
            raise ValidationError('Only the current version can be submitted.')

        exam_def = version.item.exam_definition

        if exam_def.result_structure == ResultStructure.MULTI_PARAMETER:
            active_param_ids = set(
                exam_def.parameters
                .filter(is_active=True)
                .values_list('id', flat=True)
            )
            filled_param_ids = set(
                version.values
                .filter(parameter_id__isnull=False)
                .exclude(value='')
                .values_list('parameter_id', flat=True)
            )
            missing = active_param_ids - filled_param_ids
            if missing:
                raise ValidationError(
                    f'{len(missing)} parameter(s) still need a value '
                    f'before submitting for review.'
                )
        else:
            if not version.result_value.strip():
                raise ValidationError(
                    'result_value must be set before submitting for review.'
                )

        VersionStateMachine.transition(version, ResultStatus.SUBMITTED)
        version.submitted_by = submitted_by
        version.submitted_at = timezone.now()
        version.save(update_fields=[
            'status', 'submitted_by', 'submitted_at', 'updated_at',
        ])

        item = version.item
        if item.status == ItemStatus.RESULT_ENTERED:
            ItemStateMachine.transition(item, ItemStatus.UNDER_REVIEW)
            item.save(update_fields=['status', 'updated_at'])

        _audit(
            actor=submitted_by,
            action=AuditAction.SUBMIT,
            entity_type='ResultVersion',
            entity_id=version.id,
            diff={
                'before': {'status': ResultStatus.DRAFT},
                'after': {
                    'status': ResultStatus.SUBMITTED,
                    'submitted_by': str(submitted_by.id),
                },
            },
            request=request,
        )

        ResultVersionService._refresh_review_status(
            item.analysis_request, actor=submitted_by, request=request,
        )

        return version

    @staticmethod
    @transaction.atomic
    def validate(
        version: ResultVersion,
        validation_notes: str,
        validated_by: StaffUser,
        request,
    ) -> ResultVersion:
        """
        Transition SUBMITTED → VALIDATED.
        Item transitions to VALIDATED. Request may advance to
        READY_FOR_RELEASE if all active items are validated.
        """
        if not version.is_current:
            raise ValidationError('Only the current version can be validated.')

        ar = version.item.analysis_request
        if ar.status == RequestStatus.VALIDATED:
            raise ValidationError(
                'The request has been finalized. No further review '
                'modifications are allowed.'
            )

        VersionStateMachine.transition(version, ResultStatus.VALIDATED)

        version.validated_by = validated_by
        version.validated_at = timezone.now()
        version.validation_notes = validation_notes
        version.save(update_fields=[
            'status', 'validated_by', 'validated_at', 'validation_notes', 'updated_at',
        ])

        item = version.item
        if item.status == ItemStatus.UNDER_REVIEW:
            ItemStateMachine.transition(item, ItemStatus.VALIDATED)
            item.save(update_fields=['status', 'updated_at'])

        _audit(
            actor=validated_by,
            action=AuditAction.VALIDATE,
            entity_type='ResultVersion',
            entity_id=version.id,
            diff={'before': {'status': ResultStatus.SUBMITTED},
                  'after': {'status': ResultStatus.VALIDATED,
                             'validated_by': str(validated_by.id)}},
            request=request,
        )

        ResultVersionService._refresh_review_status(
            item.analysis_request, actor=validated_by, request=request,
        )

        return version

    @staticmethod
    @transaction.atomic
    def reject(
        version: ResultVersion,
        rejection_notes: str,
        rejected_by: StaffUser,
        request,
    ) -> ResultVersion:
        """
        Transition SUBMITTED → REJECTED.
        The rejected version stays as a historical record. The item
        transitions back to RESULT_ENTERED so the technician can
        create a new version.
        """
        if not version.is_current:
            raise ValidationError('Only the current version can be rejected.')

        ar = version.item.analysis_request
        if ar.status == RequestStatus.VALIDATED:
            raise ValidationError(
                'The request has been finalized. No further review '
                'modifications are allowed.'
            )

        VersionStateMachine.transition(version, ResultStatus.REJECTED)

        version.rejected_by = rejected_by
        version.rejected_at = timezone.now()
        version.rejection_notes = rejection_notes
        version.save(update_fields=[
            'status', 'rejected_by', 'rejected_at', 'rejection_notes', 'updated_at',
        ])

        item = version.item
        if item.status == ItemStatus.UNDER_REVIEW:
            ItemStateMachine.transition(item, ItemStatus.RESULT_ENTERED)
            item.save(update_fields=['status', 'updated_at'])

        _audit(
            actor=rejected_by,
            action=AuditAction.UPDATE,
            entity_type='ResultVersion',
            entity_id=version.id,
            diff={'before': {'status': ResultStatus.SUBMITTED},
                  'after': {'status': ResultStatus.REJECTED,
                             'rejection_notes': rejection_notes}},
            request=request,
        )

        ResultVersionService._refresh_review_status(
            item.analysis_request, actor=rejected_by, request=request,
        )

        return version

    @staticmethod
    @transaction.atomic
    def publish(
        version: ResultVersion,
        published_by: StaffUser,
        request,
    ) -> ResultVersion:
        """
        Transition VALIDATED → PUBLISHED.
        This transition is IRREVERSIBLE.
        """
        if not version.is_current:
            raise ValidationError('Only the current version can be published.')

        VersionStateMachine.transition(version, ResultStatus.PUBLISHED)

        version.published_by = published_by
        version.published_at = timezone.now()
        version.save(update_fields=[
            'status', 'published_by', 'published_at', 'updated_at',
        ])

        _audit(
            actor=published_by,
            action=AuditAction.PUBLISH,
            entity_type='ResultVersion',
            entity_id=version.id,
            diff={'before': {'status': ResultStatus.VALIDATED},
                  'after': {'status': ResultStatus.PUBLISHED,
                             'published_by': str(published_by.id),
                             'published_at': str(version.published_at)}},
            request=request,
        )

        return version

    @staticmethod
    def _refresh_review_status(
        analysis_request,
        actor: StaffUser,
        request,
    ) -> None:
        """
        Derive the request-level status from the aggregate state of all
        items. This is the **single place** where the review-phase
        lifecycle lives.

        Operationally rejected items (execution_mode REJECTED at
        confirmation) are excluded — they are permanently done.

        Precedence (first match wins):

            1. All active VALIDATED           → READY_FOR_RELEASE
               (all items individually validated; biologist must
               explicitly finalize before request becomes VALIDATED)
            2. Any UNDER_REVIEW               → AWAITING_REVIEW
            3. No UNDER_REVIEW, some
               RESULT_ENTERED                 → RETEST_REQUIRED
            4. Otherwise                      → IN_ANALYSIS

        Note: VALIDATED (the request-level status) is only reachable
        via the explicit ``finalize_validation`` action, never through
        this automatic derivation.
        """
        analysis_request.refresh_from_db(fields=['status'])

        eligible = {
            RequestStatus.IN_ANALYSIS,
            RequestStatus.AWAITING_REVIEW,
            RequestStatus.RETEST_REQUIRED,
            RequestStatus.READY_FOR_RELEASE,
        }
        if analysis_request.status not in eligible:
            return

        items = list(analysis_request.items.all())
        active = [i for i in items if i.status != ItemStatus.REJECTED]
        if not active:
            return

        def _transition_to(target: str) -> None:
            if analysis_request.status == target:
                return
            prev = analysis_request.status
            RequestStateMachine.transition(analysis_request, target)
            analysis_request.save(update_fields=['status', 'updated_at'])
            _audit(
                actor=actor,
                action=AuditAction.UPDATE,
                entity_type='AnalysisRequest',
                entity_id=analysis_request.id,
                diff={
                    'before': {'status': prev},
                    'after': {'status': target, 'reason': 'review_progress'},
                },
                request=request,
            )

        statuses = {i.status for i in active}

        # 1. Every active item validated → ready for biologist to finalize
        if statuses == {ItemStatus.VALIDATED}:
            _transition_to(RequestStatus.READY_FOR_RELEASE)
            return

        # 2. At least one item still under review → biologist work remains
        if ItemStatus.UNDER_REVIEW in statuses:
            _transition_to(RequestStatus.AWAITING_REVIEW)
            return

        # 3. No items under review, but some need re-entry → retest cycle
        if ItemStatus.RESULT_ENTERED in statuses:
            _transition_to(RequestStatus.RETEST_REQUIRED)
            return

        # 4. Analysis phase (results being entered, not yet submitted)
        _transition_to(RequestStatus.IN_ANALYSIS)


# ---------------------------------------------------------------------------
# ResultFileService
# ---------------------------------------------------------------------------

class ResultFileService:

    @staticmethod
    @transaction.atomic
    def upload(
        result: ResultVersion,
        file,
        uploaded_by: StaffUser,
        request,
    ) -> ResultFile:
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
        result = result_file.result
        if result.status != ResultStatus.DRAFT:
            raise ValidationError(
                f'Files can only be deleted from DRAFT results '
                f'(current status: {result.status}).'
            )

        file_key = result_file.file_key
        file_id = result_file.id
        original_filename = result_file.original_filename

        ResultFile.objects.filter(id=result_file.id).delete()

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
