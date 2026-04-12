"""
Cytova — Result Views

ResultVersionViewSet
    list, retrieve, create, partial_update
    submit, validate, reject, publish

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
from .filters import ResultVersionFilter
from .models import ResultVersion, ResultFile
from .serializers import (
    ResultVersionCreateSerializer,
    ResultVersionDetailSerializer,
    ResultVersionListSerializer,
    ResultVersionUpdateSerializer,
    RejectValidationSerializer,
    ResultFileSerializer,
    ResultFileUploadSerializer,
    SignedDownloadURLSerializer,
    ValidationNotesSerializer,
)
from .services import ResultFileService, ResultVersionService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_version_or_404(pk) -> ResultVersion:
    try:
        return (
            ResultVersion.objects
            .select_related(
                'item__exam_definition',
                'item__analysis_request',
                'entered_by', 'submitted_by',
                'validated_by', 'rejected_by', 'published_by',
            )
            .prefetch_related('files__uploaded_by')
            .get(pk=pk)
        )
    except ResultVersion.DoesNotExist:
        raise NotFound('Result version not found.')


def _get_file_or_404(result_pk, pk) -> ResultFile:
    try:
        return ResultFile.objects.select_related('result', 'uploaded_by').get(
            pk=pk, result_id=result_pk,
        )
    except ResultFile.DoesNotExist:
        raise NotFound('Result file not found.')


# ---------------------------------------------------------------------------
# ResultVersionViewSet
# ---------------------------------------------------------------------------

class ResultVersionViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = ResultVersionFilter
    search_fields = [
        'item__exam_definition__code',
        'item__exam_definition__name',
        'item__analysis_request__request_number',
    ]
    ordering_fields = ['created_at', 'status', 'version_number', 'published_at']
    ordering = ['-created_at']

    def get_queryset(self):
        from django.db.models import Prefetch

        files_qs = ResultFile.objects.select_related('uploaded_by')
        return (
            ResultVersion.objects
            .select_related(
                'item__exam_definition',
                'item__analysis_request',
                'entered_by', 'submitted_by',
                'validated_by', 'rejected_by', 'published_by',
            )
            .prefetch_related(Prefetch('files', queryset=files_qs))
        )

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAnyStaff()]
        if self.action in ('validate', 'reject', 'publish'):
            return [IsBiologistOrAbove()]
        # create, partial_update, submit
        return [IsTechnicianOrAbove()]

    def get_serializer_class(self):
        if self.action == 'list':
            return ResultVersionListSerializer
        return ResultVersionDetailSerializer

    def create(self, request, *args, **kwargs):
        serializer = ResultVersionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        from apps.requests.models import AnalysisRequestItem
        item = (
            AnalysisRequestItem.objects
            .select_related('analysis_request', 'exam_definition')
            .get(pk=data['item_id'])
        )

        version = ResultVersionService.create_draft(
            item=item,
            entered_by=request.user,
            request=request,
            result_value=data.get('result_value', ''),
            result_unit=data.get('result_unit', ''),
            reference_range=data.get('reference_range', ''),
            is_abnormal=data.get('is_abnormal', False),
            comments=data.get('comments', ''),
            internal_notes=data.get('internal_notes', ''),
            notes=data.get('notes', ''),
        )
        version = _get_version_or_404(version.id)
        return Response(
            ResultVersionDetailSerializer(version).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        version = _get_version_or_404(kwargs['pk'])
        serializer = ResultVersionUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        version = ResultVersionService.update_draft(
            version=version,
            validated_data=serializer.validated_data,
            updated_by=request.user,
            request=request,
        )
        version = _get_version_or_404(version.id)
        return Response(ResultVersionDetailSerializer(version).data)

    @action(detail=True, methods=['post'], url_path='submit')
    def submit(self, request, pk=None):
        version = _get_version_or_404(pk)
        version = ResultVersionService.submit(
            version=version,
            submitted_by=request.user,
            request=request,
        )
        return Response(ResultVersionDetailSerializer(version).data)

    @action(detail=True, methods=['post'], url_path='validate')
    def validate(self, request, pk=None):
        version = _get_version_or_404(pk)
        serializer = ValidationNotesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        version = ResultVersionService.validate(
            version=version,
            validation_notes=serializer.validated_data.get('validation_notes', ''),
            validated_by=request.user,
            request=request,
        )
        return Response(ResultVersionDetailSerializer(version).data)

    @action(detail=True, methods=['post'], url_path='reject')
    def reject(self, request, pk=None):
        version = _get_version_or_404(pk)
        serializer = RejectValidationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        version = ResultVersionService.reject(
            version=version,
            rejection_notes=serializer.validated_data['rejection_notes'],
            rejected_by=request.user,
            request=request,
        )
        return Response(ResultVersionDetailSerializer(version).data)

    @action(detail=True, methods=['post'], url_path='publish')
    def publish(self, request, pk=None):
        version = _get_version_or_404(pk)
        version = ResultVersionService.publish(
            version=version,
            published_by=request.user,
            request=request,
        )
        return Response(ResultVersionDetailSerializer(version).data)


# ---------------------------------------------------------------------------
# ResultFileViewSet  (nested: /results/{result_pk}/files/)
# ---------------------------------------------------------------------------

class ResultFileViewSet(GenericViewSet):
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
            return ResultVersion.objects.get(pk=result_pk)
        except ResultVersion.DoesNotExist:
            raise NotFound('Result version not found.')

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
        self._get_parent(result_pk)
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
