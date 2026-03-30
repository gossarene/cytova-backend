"""
Cytova — Result Views

ExamResultViewSet
    list, retrieve, create, partial_update
    submit, validate, reject_validation, publish

ResultFileViewSet  (nested under results)
    list, upload (POST), download (GET signed URL), delete (DELETE)

Security constraints enforced here and in services:
    - file_key is NEVER included in any response
    - PUBLISHED results reject all mutations
    - Signed URLs are generated only on explicit download requests
    - Upload and delete are blocked on PUBLISHED results
"""
import logging

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.parsers import MultiPartParser, JSONParser
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from common.permissions import (
    IsAnyStaff,
    IsBiologistOrAbove,
    IsTechnicianOrAbove,
)
from .filters import ExamResultFilter
from .models import ExamResult, ResultFile
from .serializers import (
    ExamResultCreateSerializer,
    ExamResultDetailSerializer,
    ExamResultListSerializer,
    ExamResultUpdateSerializer,
    RejectValidationSerializer,
    ResultFileSerializer,
    ResultFileUploadSerializer,
    SignedDownloadURLSerializer,
    ValidationNotesSerializer,
)
from .services import ResultFileService, ResultService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_result_or_404(pk) -> ExamResult:
    try:
        return (
            ExamResult.objects
            .select_related(
                'item__exam_definition',
                'item__analysis_request',
                'created_by', 'validated_by', 'published_by',
            )
            .prefetch_related('files__uploaded_by')
            .get(pk=pk)
        )
    except ExamResult.DoesNotExist:
        raise NotFound('Exam result not found.')


def _get_file_or_404(result_pk, pk) -> ResultFile:
    try:
        return ResultFile.objects.select_related('result', 'uploaded_by').get(
            pk=pk, result_id=result_pk,
        )
    except ResultFile.DoesNotExist:
        raise NotFound('Result file not found.')


# ---------------------------------------------------------------------------
# ExamResultViewSet
# ---------------------------------------------------------------------------

class ExamResultViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = ExamResultFilter
    search_fields = [
        'item__exam_definition__code',
        'item__exam_definition__name',
        'item__analysis_request__request_number',
    ]
    ordering_fields = ['created_at', 'status', 'published_at']
    ordering = ['-created_at']

    def get_queryset(self):
        from django.db.models import Prefetch
        from apps.results.models import ResultFile

        # Prefetch files WITH uploaded_by to avoid N+1 when serializer
        # accesses file.uploaded_by.email in ResultFileSerializer.
        files_qs = ResultFile.objects.select_related('uploaded_by')
        return (
            ExamResult.objects
            .select_related(
                'item__exam_definition',
                'item__analysis_request',
                'created_by', 'validated_by', 'published_by',
            )
            .prefetch_related(Prefetch('files', queryset=files_qs))
        )

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAnyStaff()]
        if self.action in ('validate', 'reject_validation', 'publish'):
            return [IsBiologistOrAbove()]
        # create, partial_update, submit
        return [IsTechnicianOrAbove()]

    def get_serializer_class(self):
        if self.action == 'list':
            return ExamResultListSerializer
        return ExamResultDetailSerializer

    def create(self, request, *args, **kwargs):
        serializer = ExamResultCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = ResultService.create(
            validated_data=serializer.validated_data,
            created_by=request.user,
            request=request,
        )
        result = _get_result_or_404(result.id)
        return Response(
            ExamResultDetailSerializer(result).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        result = _get_result_or_404(kwargs['pk'])
        serializer = ExamResultUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = ResultService.update(
            result=result,
            validated_data=serializer.validated_data,
            updated_by=request.user,
            request=request,
        )
        result = _get_result_or_404(result.id)
        return Response(ExamResultDetailSerializer(result).data)

    @action(detail=True, methods=['post'], url_path='submit')
    def submit(self, request, pk=None):
        result = _get_result_or_404(pk)
        result = ResultService.submit(
            result=result,
            submitted_by=request.user,
            request=request,
        )
        return Response(ExamResultDetailSerializer(result).data)

    @action(detail=True, methods=['post'], url_path='validate')
    def validate(self, request, pk=None):
        result = _get_result_or_404(pk)
        serializer = ValidationNotesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = ResultService.validate(
            result=result,
            validation_notes=serializer.validated_data.get('validation_notes', ''),
            validated_by=request.user,
            request=request,
        )
        return Response(ExamResultDetailSerializer(result).data)

    @action(detail=True, methods=['post'], url_path='reject-validation')
    def reject_validation(self, request, pk=None):
        result = _get_result_or_404(pk)
        serializer = RejectValidationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = ResultService.reject_validation(
            result=result,
            validation_notes=serializer.validated_data['validation_notes'],
            rejected_by=request.user,
            request=request,
        )
        return Response(ExamResultDetailSerializer(result).data)

    @action(detail=True, methods=['post'], url_path='publish')
    def publish(self, request, pk=None):
        result = _get_result_or_404(pk)
        result = ResultService.publish(
            result=result,
            published_by=request.user,
            request=request,
        )
        return Response(ExamResultDetailSerializer(result).data)


# ---------------------------------------------------------------------------
# ResultFileViewSet  (nested: /results/{result_pk}/files/)
# ---------------------------------------------------------------------------

class ResultFileViewSet(GenericViewSet):
    """
    Manages files attached to an ExamResult.

    Security:
    - file_key is NEVER included in any response
    - All file downloads go through a signed URL generated on demand
    - Upload and delete are rejected for PUBLISHED results
    - Download (signed URL) is available to all authenticated staff
    """
    parser_classes = [MultiPartParser, JSONParser]

    def get_permissions(self):
        if self.action in ('list', 'download'):
            return [IsAnyStaff()]
        if self.action == 'delete':
            return [IsTechnicianOrAbove()]
        # upload
        return [IsTechnicianOrAbove()]

    def _get_parent(self, result_pk):
        try:
            return ExamResult.objects.get(pk=result_pk)
        except ExamResult.DoesNotExist:
            raise NotFound('Exam result not found.')

    def list(self, request, result_pk=None, *args, **kwargs):
        self._get_parent(result_pk)
        files = (
            ResultFile.objects
            .filter(result_id=result_pk)
            .select_related('uploaded_by')
            .order_by('created_at')
        )
        serializer = ResultFileSerializer(files, many=True)
        return Response(serializer.data)

    def upload(self, request, result_pk=None, *args, **kwargs):
        result = self._get_parent(result_pk)
        serializer = ResultFileUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        result_file = ResultFileService.upload(
            result=result,
            file=serializer.validated_data['file'],
            uploaded_by=request.user,
            request=request,
        )
        return Response(
            ResultFileSerializer(result_file).data,
            status=status.HTTP_201_CREATED,
        )

    def download(self, request, result_pk=None, pk=None, *args, **kwargs):
        """
        Generate and return a time-limited signed URL for the requested file.
        The file_key is consumed internally; the URL is all the client receives.
        """
        self._get_parent(result_pk)  # 404 if result not found
        result_file = _get_file_or_404(result_pk, pk)

        url_data = ResultFileService.get_download_url(result_file)
        serializer = SignedDownloadURLSerializer(url_data)
        return Response(serializer.data)

    def delete(self, request, result_pk=None, pk=None, *args, **kwargs):
        result = self._get_parent(result_pk)
        result_file = _get_file_or_404(result_pk, pk)
        ResultFileService.delete(
            result_file=result_file,
            deleted_by=request.user,
            request=request,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)
