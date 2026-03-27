"""
Cytova — Result Serializers

Security note: ResultFileSerializer deliberately omits file_key.
Raw storage paths are never returned to API clients.
"""
from django.conf import settings
from rest_framework import serializers

from apps.catalog.models import ExamDefinition
from .models import ExamResult, ResultFile, ResultStatus


# ---------------------------------------------------------------------------
# ResultFile
# ---------------------------------------------------------------------------

class ResultFileSerializer(serializers.ModelSerializer):
    """
    Public representation of a result file.
    file_key is intentionally absent — access via the download endpoint.
    """
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
# ExamResult — read
# ---------------------------------------------------------------------------

class ExamResultListSerializer(serializers.ModelSerializer):
    exam_code = serializers.CharField(
        source='item.exam_definition.code', read_only=True,
    )
    exam_name = serializers.CharField(
        source='item.exam_definition.name', read_only=True,
    )
    request_number = serializers.CharField(
        source='item.analysis_request.request_number', read_only=True,
    )
    files_count = serializers.SerializerMethodField()

    class Meta:
        model = ExamResult
        fields = [
            'id', 'item_id', 'exam_code', 'exam_name', 'request_number',
            'status', 'is_abnormal', 'result_value', 'result_unit',
            'validated_at', 'published_at',
            'files_count', 'created_at',
        ]

    def get_files_count(self, obj):
        return obj.files.count()


class ExamResultDetailSerializer(serializers.ModelSerializer):
    exam_code = serializers.CharField(
        source='item.exam_definition.code', read_only=True,
    )
    exam_name = serializers.CharField(
        source='item.exam_definition.name', read_only=True,
    )
    request_number = serializers.CharField(
        source='item.analysis_request.request_number', read_only=True,
    )
    created_by_email = serializers.CharField(
        source='created_by.email', read_only=True, default=None,
    )
    validated_by_email = serializers.CharField(
        source='validated_by.email', read_only=True, default=None,
    )
    published_by_email = serializers.CharField(
        source='published_by.email', read_only=True, default=None,
    )
    files = ResultFileSerializer(many=True, read_only=True)

    class Meta:
        model = ExamResult
        fields = [
            'id', 'item_id', 'exam_code', 'exam_name', 'request_number',
            'status',
            'result_value', 'result_unit', 'reference_range',
            'is_abnormal', 'comments', 'internal_notes',
            'validation_notes',
            'validated_by_email', 'validated_at',
            'published_by_email', 'published_at',
            'created_by_email',
            'files',
            'created_at', 'updated_at',
        ]


# ---------------------------------------------------------------------------
# ExamResult — write
# ---------------------------------------------------------------------------

class ExamResultCreateSerializer(serializers.Serializer):
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

    def validate_item_id(self, value):
        from apps.requests.models import AnalysisRequestItem, ItemStatus
        try:
            item = AnalysisRequestItem.objects.select_related(
                'analysis_request'
            ).get(pk=value)
        except AnalysisRequestItem.DoesNotExist:
            raise serializers.ValidationError('Analysis request item not found.')

        if item.status not in {ItemStatus.IN_PROGRESS, ItemStatus.COMPLETED}:
            raise serializers.ValidationError(
                'A result can only be created for an item that is '
                'IN_PROGRESS or COMPLETED.'
            )

        if hasattr(item, 'result'):
            raise serializers.ValidationError(
                'A result already exists for this item.'
            )

        return value


class ExamResultUpdateSerializer(serializers.Serializer):
    """All fields are optional — only provided fields are updated."""
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


class ValidationNotesSerializer(serializers.Serializer):
    """Optional notes body for validate and reject-validation actions."""
    validation_notes = serializers.CharField(
        required=False, allow_blank=True, default='',
    )


class RejectValidationSerializer(serializers.Serializer):
    """Rejection always requires an explanatory note."""
    validation_notes = serializers.CharField(min_length=1)


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
    """Shape of the response from the download endpoint."""
    url = serializers.URLField()
    expires_in = serializers.IntegerField()
    filename = serializers.CharField()
