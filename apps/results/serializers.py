"""
Cytova — Result Serializers

Security note: ResultFileSerializer deliberately omits file_key.
Raw storage paths are never returned to API clients.
"""
from django.conf import settings
from rest_framework import serializers

from apps.catalog.models import ExamDefinition
from .models import ResultVersion, ResultValue, ResultFile, ResultStatus


# ---------------------------------------------------------------------------
# ResultFile
# ---------------------------------------------------------------------------

class ResultFileSerializer(serializers.ModelSerializer):
    uploaded_by_email = serializers.CharField(
        source='uploaded_by.email', read_only=True, default=None,
    )
    file_size_kb = serializers.SerializerMethodField()

    class Meta:
        model = ResultFile
        fields = [
            'id', 'original_filename', 'mime_type',
            'file_size', 'file_size_kb',
            'uploaded_by_email', 'created_at',
        ]

    def get_file_size_kb(self, obj):
        return round(obj.file_size / 1024, 1)


# ---------------------------------------------------------------------------
# ResultValue
# ---------------------------------------------------------------------------

class ResultValueSerializer(serializers.ModelSerializer):
    parameter_id = serializers.UUIDField(read_only=True, default=None)

    class Meta:
        model = ResultValue
        fields = [
            'id', 'parameter_id',
            'name_snapshot', 'value', 'unit_snapshot',
            'reference_range_snapshot', 'is_abnormal',
            'display_order',
        ]


class ResultValueInputSerializer(serializers.Serializer):
    """Input for a single result value row (used in create/update)."""
    parameter_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    value = serializers.CharField(allow_blank=True, default='')
    is_abnormal = serializers.BooleanField(default=False)


# ---------------------------------------------------------------------------
# ResultVersion — read
# ---------------------------------------------------------------------------

class ResultVersionListSerializer(serializers.ModelSerializer):
    exam_code = serializers.CharField(
        source='item.exam_definition.code', read_only=True,
    )
    exam_name = serializers.CharField(
        source='item.exam_definition.name', read_only=True,
    )
    request_number = serializers.CharField(
        source='item.analysis_request.request_number', read_only=True,
    )
    entered_by_email = serializers.CharField(
        source='entered_by.email', read_only=True, default=None,
    )
    files_count = serializers.SerializerMethodField()

    class Meta:
        model = ResultVersion
        fields = [
            'id', 'item_id', 'exam_code', 'exam_name', 'request_number',
            'version_number', 'is_current', 'status',
            'is_abnormal', 'result_value', 'result_unit',
            'entered_by_email', 'entered_at',
            'submitted_at', 'validated_at', 'published_at',
            'files_count', 'created_at',
        ]

    def get_files_count(self, obj):
        return obj.files.count()


class ResultVersionDetailSerializer(serializers.ModelSerializer):
    exam_code = serializers.CharField(
        source='item.exam_definition.code', read_only=True,
    )
    exam_name = serializers.CharField(
        source='item.exam_definition.name', read_only=True,
    )
    request_number = serializers.CharField(
        source='item.analysis_request.request_number', read_only=True,
    )
    entered_by_email = serializers.CharField(
        source='entered_by.email', read_only=True, default=None,
    )
    entered_by_display = serializers.SerializerMethodField()
    submitted_by_email = serializers.CharField(
        source='submitted_by.email', read_only=True, default=None,
    )
    submitted_by_display = serializers.SerializerMethodField()
    validated_by_email = serializers.CharField(
        source='validated_by.email', read_only=True, default=None,
    )
    # Validator is a medical/signature context — surface the title-prefixed
    # name so the UI can render "Dr René GOSSA" without recomposing.
    validated_by_display = serializers.SerializerMethodField()
    rejected_by_email = serializers.CharField(
        source='rejected_by.email', read_only=True, default=None,
    )
    rejected_by_display = serializers.SerializerMethodField()
    published_by_email = serializers.CharField(
        source='published_by.email', read_only=True, default=None,
    )
    published_by_display = serializers.SerializerMethodField()
    files = ResultFileSerializer(many=True, read_only=True)
    values = ResultValueSerializer(many=True, read_only=True)

    class Meta:
        model = ResultVersion
        fields = [
            'id', 'item_id', 'exam_code', 'exam_name', 'request_number',
            'version_number', 'is_current', 'status',
            'result_value', 'result_unit', 'reference_range',
            'is_abnormal', 'comments', 'internal_notes',
            'notes',
            'entered_by_email', 'entered_by_display', 'entered_at',
            'submitted_by_email', 'submitted_by_display', 'submitted_at',
            'validation_notes',
            'validated_by_email', 'validated_by_display', 'validated_at',
            'rejection_notes',
            'rejected_by_email', 'rejected_by_display', 'rejected_at',
            'published_by_email', 'published_by_display', 'published_at',
            'files', 'values',
            'created_at', 'updated_at',
        ]

    @staticmethod
    def _user_display(user):
        return user.display_name if user is not None else None

    def get_entered_by_display(self, obj):
        return self._user_display(obj.entered_by)

    def get_submitted_by_display(self, obj):
        return self._user_display(obj.submitted_by)

    def get_validated_by_display(self, obj):
        # Medical signature context — title-prefixed name.
        return obj.validated_by.professional_display_name if obj.validated_by else None

    def get_rejected_by_display(self, obj):
        return self._user_display(obj.rejected_by)

    def get_published_by_display(self, obj):
        return self._user_display(obj.published_by)


# ---------------------------------------------------------------------------
# ResultVersion — write
# ---------------------------------------------------------------------------

class ResultVersionCreateSerializer(serializers.Serializer):
    item_id = serializers.UUIDField()
    result_value = serializers.CharField(allow_blank=True, default='')
    result_unit = serializers.CharField(
        max_length=50, required=False, allow_blank=True, default='',
    )
    reference_range = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default='',
    )
    is_abnormal = serializers.BooleanField(default=False)
    comments = serializers.CharField(required=False, allow_blank=True, default='')
    internal_notes = serializers.CharField(
        required=False, allow_blank=True, default='',
    )
    notes = serializers.CharField(required=False, allow_blank=True, default='')
    values = ResultValueInputSerializer(many=True, required=False, default=[])

    def validate_item_id(self, value):
        from apps.requests.models import AnalysisRequestItem, ItemStatus
        try:
            AnalysisRequestItem.objects.get(pk=value)
        except AnalysisRequestItem.DoesNotExist:
            raise serializers.ValidationError('Analysis request item not found.')
        return value


class ResultVersionUpdateSerializer(serializers.Serializer):
    result_value = serializers.CharField(required=False, allow_blank=True)
    result_unit = serializers.CharField(
        max_length=50, required=False, allow_blank=True,
    )
    reference_range = serializers.CharField(
        max_length=100, required=False, allow_blank=True,
    )
    is_abnormal = serializers.BooleanField(required=False)
    comments = serializers.CharField(required=False, allow_blank=True)
    internal_notes = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    values = ResultValueInputSerializer(many=True, required=False)


class ReviewCommentsUpdateSerializer(serializers.Serializer):
    """Update patient-facing comments during biologist review."""
    comments = serializers.CharField(required=False, allow_blank=True)
    validation_notes = serializers.CharField(required=False, allow_blank=True)


class ValidationNotesSerializer(serializers.Serializer):
    validation_notes = serializers.CharField(
        required=False, allow_blank=True, default='',
    )


class RejectValidationSerializer(serializers.Serializer):
    rejection_notes = serializers.CharField(min_length=1)


# ---------------------------------------------------------------------------
# ResultFile — write
# ---------------------------------------------------------------------------

class ResultFileUploadSerializer(serializers.Serializer):
    file = serializers.FileField()

    def validate_file(self, value):
        allowed = getattr(
            settings,
            'RESULT_FILE_ALLOWED_MIME_TYPES',
            list(ResultFile.ALLOWED_MIME_TYPES),
        )
        max_size = getattr(settings, 'RESULT_FILE_MAX_SIZE', 20 * 1024 * 1024)

        content_type = getattr(value, 'content_type', '')
        if content_type not in allowed:
            raise serializers.ValidationError(
                f'Unsupported file type: {content_type}. '
                f'Allowed types: {", ".join(sorted(allowed))}.'
            )

        if value.size > max_size:
            max_mb = max_size / (1024 * 1024)
            raise serializers.ValidationError(
                f'File too large. Maximum size is {max_mb:.0f} MB.'
            )

        return value


# ---------------------------------------------------------------------------
# Signed URL response
# ---------------------------------------------------------------------------

class SignedDownloadURLSerializer(serializers.Serializer):
    url = serializers.URLField()
    expires_in = serializers.IntegerField()
    filename = serializers.CharField()
