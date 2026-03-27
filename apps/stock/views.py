"""
Cytova — Stock Views

StockCategoryViewSet     — list, create, retrieve, partial_update, deactivate
StockItemViewSet         — list, create, retrieve, partial_update, deactivate
StockLotViewSet          — list, retrieve, create  (nested under /items/<item_pk>/lots/)
StockMovementViewSet     — list, retrieve, create  (nested under /lots/<lot_pk>/movements/)
StockMovementReportViewSet — read-only flat list for reporting
"""
import logging
from decimal import Decimal

from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from common.permissions import IsAnyStaff, IsLabAdmin, IsTechnicianOrAbove
from .filters import StockCategoryFilter, StockItemFilter, StockLotFilter, StockMovementFilter
from .models import StockCategory, StockItem, StockLot, StockMovement
from .serializers import (
    StockCategoryCreateSerializer,
    StockCategoryDetailSerializer,
    StockCategoryListSerializer,
    StockCategoryUpdateSerializer,
    StockItemCreateSerializer,
    StockItemDetailSerializer,
    StockItemListSerializer,
    StockItemUpdateSerializer,
    StockLotCreateSerializer,
    StockLotSerializer,
    StockMovementCreateSerializer,
    StockMovementSerializer,
)
from .services import StockCategoryService, StockItemService, StockLotService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# StockCategoryViewSet
# ---------------------------------------------------------------------------

class StockCategoryViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    queryset = StockCategory.objects.all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = StockCategoryFilter
    search_fields = ['name']
    ordering_fields = ['display_order', 'name', 'created_at']
    ordering = ['display_order', 'name']

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAnyStaff()]
        return [IsLabAdmin()]

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return StockCategoryDetailSerializer
        if self.action == 'create':
            return StockCategoryCreateSerializer
        if self.action == 'partial_update':
            return StockCategoryUpdateSerializer
        return StockCategoryListSerializer

    def create(self, request, *args, **kwargs):
        serializer = StockCategoryCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        category = StockCategoryService.create(
            validated_data=serializer.validated_data,
            created_by=request.user,
            request=request,
        )
        return Response(
            StockCategoryDetailSerializer(category).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        category = self.get_object()
        serializer = StockCategoryUpdateSerializer(
            data=request.data,
            context={'instance': category},
        )
        serializer.is_valid(raise_exception=True)
        if not serializer.validated_data:
            return Response(StockCategoryDetailSerializer(category).data)
        category = StockCategoryService.update(
            category=category,
            validated_data=serializer.validated_data,
            updated_by=request.user,
            request=request,
        )
        return Response(StockCategoryDetailSerializer(category).data)

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, pk=None):
        category = self.get_object()
        category = StockCategoryService.deactivate(
            category=category,
            deactivated_by=request.user,
            request=request,
        )
        return Response(StockCategoryDetailSerializer(category).data)


# ---------------------------------------------------------------------------
# StockItemViewSet
# ---------------------------------------------------------------------------

class StockItemViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = StockItemFilter
    search_fields = ['code', 'name', 'main_supplier_name']
    ordering_fields = ['code', 'name', 'minimum_threshold', 'created_at']
    ordering = ['name']

    def get_queryset(self):
        return StockItem.objects.select_related('category').annotate(
            current_quantity=Coalesce(
                Sum(
                    'lots__current_quantity',
                    filter=Q(lots__is_exhausted=False),
                ),
                Value(Decimal('0')),
                output_field=DecimalField(),
            )
        )

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAnyStaff()]
        return [IsLabAdmin()]

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return StockItemDetailSerializer
        if self.action == 'create':
            return StockItemCreateSerializer
        if self.action == 'partial_update':
            return StockItemUpdateSerializer
        return StockItemListSerializer

    def create(self, request, *args, **kwargs):
        serializer = StockItemCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = StockItemService.create(
            validated_data=serializer.validated_data,
            created_by=request.user,
            request=request,
        )
        item = self.get_queryset().get(id=item.id)
        return Response(
            StockItemDetailSerializer(item).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        item = self.get_object()
        serializer = StockItemUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not serializer.validated_data:
            return Response(StockItemDetailSerializer(item).data)
        item = StockItemService.update(
            item=item,
            validated_data=serializer.validated_data,
            updated_by=request.user,
            request=request,
        )
        item = self.get_queryset().get(id=item.id)
        return Response(StockItemDetailSerializer(item).data)

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, pk=None):
        item = self.get_object()
        item = StockItemService.deactivate(
            item=item,
            deactivated_by=request.user,
            request=request,
        )
        item = self.get_queryset().get(id=item.id)
        return Response(StockItemDetailSerializer(item).data)


# ---------------------------------------------------------------------------
# StockLotViewSet  (nested under /stock/items/<item_pk>/lots/)
# ---------------------------------------------------------------------------

class StockLotViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = StockLotFilter
    ordering_fields = ['received_at', 'lot_number', 'expiry_date', 'current_quantity']
    ordering = ['-received_at']

    def _get_item(self):
        return get_object_or_404(StockItem, pk=self.kwargs['item_pk'])

    def get_queryset(self):
        return (
            StockLot.objects
            .filter(item_id=self.kwargs['item_pk'])
            .select_related('item')
        )

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAnyStaff()]
        return [IsTechnicianOrAbove()]

    def get_serializer_class(self):
        if self.action == 'create':
            return StockLotCreateSerializer
        return StockLotSerializer

    def create(self, request, *args, **kwargs):
        item = self._get_item()
        serializer = StockLotCreateSerializer(
            data=request.data,
            context={'item_id': item.id},
        )
        serializer.is_valid(raise_exception=True)
        lot = StockLotService.create(
            item=item,
            validated_data=serializer.validated_data,
            created_by=request.user,
            request=request,
        )
        lot = StockLot.objects.select_related('item').get(id=lot.id)
        return Response(StockLotSerializer(lot).data, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# StockMovementViewSet  (nested under /stock/lots/<lot_pk>/movements/)
# ---------------------------------------------------------------------------

class StockMovementViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = StockMovementFilter
    ordering_fields = ['performed_at', 'movement_type']
    ordering = ['-performed_at']

    def _get_lot(self):
        return get_object_or_404(StockLot, pk=self.kwargs['lot_pk'])

    def get_queryset(self):
        return (
            StockMovement.objects
            .filter(lot_id=self.kwargs['lot_pk'])
            .select_related('performed_by')
        )

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAnyStaff()]
        return [IsTechnicianOrAbove()]

    def get_serializer_class(self):
        if self.action == 'create':
            return StockMovementCreateSerializer
        return StockMovementSerializer

    def create(self, request, *args, **kwargs):
        lot = self._get_lot()
        serializer = StockMovementCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        vd = serializer.validated_data
        try:
            movement = StockLotService.record_movement(
                lot=lot,
                movement_type=vd['movement_type'],
                quantity=vd['quantity'],
                reason=vd.get('reason', ''),
                reference=vd.get('reference', ''),
                reference_type=vd.get('reference_type', ''),
                performed_by=request.user,
                request=request,
            )
        except ValueError as exc:
            raise ValidationError({'quantity': str(exc)})

        movement = StockMovement.objects.select_related('performed_by').get(id=movement.id)
        return Response(StockMovementSerializer(movement).data, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# StockMovementReportViewSet  — flat read-only list across all lots
# ---------------------------------------------------------------------------

class StockMovementReportViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    """
    Read-only flat view of all stock movements, for reporting and audit purposes.
    Supports filtering by lot, movement_type, and date range.
    """
    serializer_class = StockMovementSerializer
    permission_classes = [IsAnyStaff]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = StockMovementFilter
    ordering_fields = ['performed_at', 'movement_type']
    ordering = ['-performed_at']

    def get_queryset(self):
        return StockMovement.objects.select_related('lot__item', 'performed_by').all()
