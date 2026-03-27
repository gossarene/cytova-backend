"""
Cytova — Analysis Request Views

AnalysisRequestViewSet
    list, retrieve, create, partial_update, confirm, cancel

AnalysisRequestItemViewSet  (nested under requests)
    list, retrieve, create (add item), partial_update (update metadata),
    destroy (remove from draft), start, complete, reject
"""
import logging

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from common.permissions import (
    IsAnyStaff,
    IsBiologistOrAbove,
    IsLabAdmin,
    IsReceptionistOrLabAdmin,
    IsTechnicianOrAbove,
)
from .filters import AnalysisRequestFilter, AnalysisRequestItemFilter
from .models import AnalysisRequest, AnalysisRequestItem
from .serializers import (
    AnalysisRequestCreateSerializer,
    AnalysisRequestDetailSerializer,
    AnalysisRequestItemCreateSerializer,
    AnalysisRequestItemSerializer,
    AnalysisRequestItemUpdateSerializer,
    AnalysisRequestListSerializer,
    AnalysisRequestUpdateSerializer,
    ItemRejectSerializer,
)
from .services import AnalysisRequestItemService, AnalysisRequestService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_request_or_404(pk) -> AnalysisRequest:
    try:
        return AnalysisRequest.objects.get(pk=pk)
    except AnalysisRequest.DoesNotExist:
        raise NotFound('Analysis request not found.')


def _get_item_or_404(request_pk, pk) -> AnalysisRequestItem:
    try:
        return (
            AnalysisRequestItem.objects
            .select_related('analysis_request', 'traceability')
            .get(pk=pk, analysis_request_id=request_pk)
        )
    except AnalysisRequestItem.DoesNotExist:
        raise NotFound('Analysis request item not found.')


# ---------------------------------------------------------------------------
# AnalysisRequestViewSet
# ---------------------------------------------------------------------------

class AnalysisRequestViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = AnalysisRequestFilter
    search_fields = ['request_number', 'patient__first_name', 'patient__last_name',
                     'patient__national_id', 'external_reference',
                     'partner_organization__code', 'partner_organization__name']
    ordering_fields = ['created_at', 'status', 'request_number']
    ordering = ['-created_at']

    def get_queryset(self):
        return (
            AnalysisRequest.objects
            .select_related(
                'patient', 'partner_organization',
                'created_by', 'confirmed_by', 'cancelled_by',
            )
            .prefetch_related('items')
        )

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAnyStaff()]
        if self.action == 'cancel':
            return [IsLabAdmin()]
        # create, partial_update, confirm
        return [IsReceptionistOrLabAdmin()]

    def get_serializer_class(self):
        if self.action == 'list':
            return AnalysisRequestListSerializer
        return AnalysisRequestDetailSerializer

    def create(self, request, *args, **kwargs):
        serializer = AnalysisRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ar = AnalysisRequestService.create(
            validated_data=serializer.validated_data,
            created_by=request.user,
            request=request,
        )
        ar = (
            AnalysisRequest.objects
            .select_related(
                'patient', 'partner_organization',
                'created_by', 'confirmed_by', 'cancelled_by',
            )
            .prefetch_related('items')
            .get(id=ar.id)
        )
        return Response(
            AnalysisRequestDetailSerializer(ar).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        ar = _get_request_or_404(kwargs['pk'])
        serializer = AnalysisRequestUpdateSerializer(
            data=request.data,
            context={'instance': ar},
        )
        serializer.is_valid(raise_exception=True)
        ar = AnalysisRequestService.update(
            analysis_request=ar,
            validated_data=serializer.validated_data,
            updated_by=request.user,
            request=request,
        )
        return Response(AnalysisRequestDetailSerializer(ar).data)

    @action(detail=True, methods=['post'], url_path='confirm')
    def confirm(self, request, pk=None):
        ar = _get_request_or_404(pk)
        ar = AnalysisRequestService.confirm(
            analysis_request=ar,
            confirmed_by=request.user,
            request=request,
        )
        ar = (
            AnalysisRequest.objects
            .select_related(
                'patient', 'partner_organization',
                'created_by', 'confirmed_by', 'cancelled_by',
            )
            .prefetch_related('items')
            .get(id=ar.id)
        )
        return Response(AnalysisRequestDetailSerializer(ar).data)

    @action(detail=True, methods=['post'], url_path='cancel')
    def cancel(self, request, pk=None):
        ar = _get_request_or_404(pk)
        ar = AnalysisRequestService.cancel(
            analysis_request=ar,
            cancelled_by=request.user,
            request=request,
        )
        return Response(AnalysisRequestDetailSerializer(ar).data)


# ---------------------------------------------------------------------------
# AnalysisRequestItemViewSet  (nested under /requests/{request_pk}/items/)
# ---------------------------------------------------------------------------

class AnalysisRequestItemViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = AnalysisRequestItemFilter
    ordering_fields = ['created_at', 'status']
    ordering = ['created_at']

    def get_queryset(self):
        request_pk = self.kwargs['request_pk']
        return (
            AnalysisRequestItem.objects
            .filter(analysis_request_id=request_pk)
            .select_related(
                'exam_definition', 'pricing_rule',
                'traceability__sample_received_by',
                'traceability__performed_by',
            )
        )

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAnyStaff()]
        if self.action in ('start', 'complete'):
            return [IsTechnicianOrAbove()]
        if self.action == 'reject':
            return [IsBiologistOrAbove()]
        # create, partial_update, destroy
        return [IsReceptionistOrLabAdmin()]

    def get_serializer_class(self):
        if self.action == 'create':
            return AnalysisRequestItemCreateSerializer
        if self.action == 'partial_update':
            return AnalysisRequestItemUpdateSerializer
        return AnalysisRequestItemSerializer

    def _get_parent(self):
        return _get_request_or_404(self.kwargs['request_pk'])

    def list(self, request, *args, **kwargs):
        self._get_parent()  # 404 if parent request not found
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        self._get_parent()
        return super().retrieve(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        ar = self._get_parent()
        serializer = AnalysisRequestItemCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = AnalysisRequestService.add_item(
            analysis_request=ar,
            validated_data=serializer.validated_data,
            added_by=request.user,
            request=request,
        )
        item = (
            AnalysisRequestItem.objects
            .select_related(
                'exam_definition', 'pricing_rule',
                'traceability__sample_received_by',
                'traceability__performed_by',
            )
            .get(id=item.id)
        )
        return Response(
            AnalysisRequestItemSerializer(item).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        item = _get_item_or_404(self.kwargs['request_pk'], kwargs['pk'])
        serializer = AnalysisRequestItemUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = AnalysisRequestItemService.update(
            item=item,
            validated_data=serializer.validated_data,
            updated_by=request.user,
            request=request,
        )
        return Response(AnalysisRequestItemSerializer(item).data)

    def destroy(self, request, *args, **kwargs):
        ar = self._get_parent()
        item = _get_item_or_404(self.kwargs['request_pk'], kwargs['pk'])
        AnalysisRequestService.remove_item(
            analysis_request=ar,
            item=item,
            removed_by=request.user,
            request=request,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post'], url_path='start')
    def start(self, request, request_pk=None, pk=None):
        item = _get_item_or_404(request_pk, pk)
        item = AnalysisRequestItemService.start(
            item=item,
            started_by=request.user,
            request=request,
        )
        return Response(AnalysisRequestItemSerializer(item).data)

    @action(detail=True, methods=['post'], url_path='complete')
    def complete(self, request, request_pk=None, pk=None):
        item = _get_item_or_404(request_pk, pk)
        item = AnalysisRequestItemService.complete(
            item=item,
            completed_by=request.user,
            request=request,
        )
        return Response(AnalysisRequestItemSerializer(item).data)

    @action(detail=True, methods=['post'], url_path='reject')
    def reject(self, request, request_pk=None, pk=None):
        item = _get_item_or_404(request_pk, pk)
        serializer = ItemRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = AnalysisRequestItemService.reject(
            item=item,
            rejection_reason=serializer.validated_data['rejection_reason'],
            rejected_by=request.user,
            request=request,
        )
        return Response(AnalysisRequestItemSerializer(item).data)
