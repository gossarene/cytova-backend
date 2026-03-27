"""
Cytova — Suppliers & Procurement Views

SupplierViewSet           — list, create, retrieve, partial_update, deactivate
PurchaseOrderViewSet      — list, create, retrieve, partial_update, confirm, cancel, close
PurchaseOrderItemViewSet  — list, create, retrieve, destroy (DRAFT only)
ReceptionViewSet          — list, create, retrieve (nested under order)
"""
import logging

from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status
from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from common.permissions import IsAnyStaff, IsLabAdmin, IsTechnicianOrAbove
from .filters import PurchaseOrderFilter, ReceptionFilter, SupplierFilter
from .models import (
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseOrderStatus,
    Reception,
    Supplier,
)
from .serializers import (
    PurchaseOrderCreateSerializer,
    PurchaseOrderDetailSerializer,
    PurchaseOrderItemCreateSerializer,
    PurchaseOrderItemSerializer,
    PurchaseOrderListSerializer,
    PurchaseOrderUpdateSerializer,
    ReceptionCreateSerializer,
    ReceptionDetailSerializer,
    ReceptionListSerializer,
    SupplierCreateSerializer,
    SupplierDetailSerializer,
    SupplierListSerializer,
    SupplierUpdateSerializer,
)
from .services import PurchaseOrderService, ReceptionService, SupplierService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SupplierViewSet
# ---------------------------------------------------------------------------

class SupplierViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    queryset = Supplier.objects.all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = SupplierFilter
    search_fields = ['name', 'contact_name', 'email']
    ordering_fields = ['name', 'created_at']
    ordering = ['name']

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAnyStaff()]
        return [IsLabAdmin()]

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return SupplierDetailSerializer
        if self.action == 'create':
            return SupplierCreateSerializer
        if self.action == 'partial_update':
            return SupplierUpdateSerializer
        return SupplierListSerializer

    def create(self, request, *args, **kwargs):
        serializer = SupplierCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        supplier = SupplierService.create(
            validated_data=serializer.validated_data,
            created_by=request.user,
            request=request,
        )
        return Response(
            SupplierDetailSerializer(supplier).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        supplier = self.get_object()
        serializer = SupplierUpdateSerializer(
            data=request.data,
            context={'instance': supplier},
        )
        serializer.is_valid(raise_exception=True)
        if not serializer.validated_data:
            return Response(SupplierDetailSerializer(supplier).data)
        supplier = SupplierService.update(
            supplier=supplier,
            validated_data=serializer.validated_data,
            updated_by=request.user,
            request=request,
        )
        return Response(SupplierDetailSerializer(supplier).data)

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, pk=None):
        supplier = self.get_object()
        supplier = SupplierService.deactivate(
            supplier=supplier,
            deactivated_by=request.user,
            request=request,
        )
        return Response(SupplierDetailSerializer(supplier).data)


# ---------------------------------------------------------------------------
# PurchaseOrderViewSet
# ---------------------------------------------------------------------------

class PurchaseOrderViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = PurchaseOrderFilter
    search_fields = ['order_number', 'supplier__name']
    ordering_fields = ['order_number', 'status', 'expected_delivery_date', 'created_at']
    ordering = ['-created_at']

    def get_queryset(self):
        return (
            PurchaseOrder.objects
            .select_related('supplier', 'confirmed_by', 'cancelled_by', 'closed_by', 'created_by')
            .prefetch_related('items__stock_item')
        )

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAnyStaff()]
        return [IsLabAdmin()]

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return PurchaseOrderDetailSerializer
        if self.action == 'create':
            return PurchaseOrderCreateSerializer
        if self.action == 'partial_update':
            return PurchaseOrderUpdateSerializer
        return PurchaseOrderListSerializer

    def create(self, request, *args, **kwargs):
        serializer = PurchaseOrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = PurchaseOrderService.create(
            validated_data=serializer.validated_data,
            created_by=request.user,
            request=request,
        )
        order = self.get_queryset().get(id=order.id)
        return Response(
            PurchaseOrderDetailSerializer(order).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        order = self.get_object()
        serializer = PurchaseOrderUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not serializer.validated_data:
            return Response(PurchaseOrderDetailSerializer(order).data)
        order = PurchaseOrderService.update(
            order=order,
            validated_data=serializer.validated_data,
            updated_by=request.user,
            request=request,
        )
        order = self.get_queryset().get(id=order.id)
        return Response(PurchaseOrderDetailSerializer(order).data)

    @action(detail=True, methods=['post'], url_path='confirm')
    def confirm(self, request, pk=None):
        order = self.get_object()
        order = PurchaseOrderService.confirm(
            order=order,
            confirmed_by=request.user,
            request=request,
        )
        order = self.get_queryset().get(id=order.id)
        return Response(PurchaseOrderDetailSerializer(order).data)

    @action(detail=True, methods=['post'], url_path='cancel')
    def cancel(self, request, pk=None):
        order = self.get_object()
        order = PurchaseOrderService.cancel(
            order=order,
            cancelled_by=request.user,
            request=request,
        )
        order = self.get_queryset().get(id=order.id)
        return Response(PurchaseOrderDetailSerializer(order).data)

    @action(detail=True, methods=['post'], url_path='close')
    def close(self, request, pk=None):
        order = self.get_object()
        order = PurchaseOrderService.close(
            order=order,
            closed_by=request.user,
            request=request,
        )
        order = self.get_queryset().get(id=order.id)
        return Response(PurchaseOrderDetailSerializer(order).data)


# ---------------------------------------------------------------------------
# PurchaseOrderItemViewSet  (nested under /purchase-orders/<order_pk>/items/)
# ---------------------------------------------------------------------------

class PurchaseOrderItemViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    ordering = ['created_at']

    def _get_order(self):
        return get_object_or_404(PurchaseOrder, pk=self.kwargs['order_pk'])

    def get_queryset(self):
        return (
            PurchaseOrderItem.objects
            .filter(order_id=self.kwargs['order_pk'])
            .select_related('stock_item')
        )

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAnyStaff()]
        return [IsLabAdmin()]

    def get_serializer_class(self):
        if self.action == 'create':
            return PurchaseOrderItemCreateSerializer
        return PurchaseOrderItemSerializer

    def create(self, request, *args, **kwargs):
        order = self._get_order()
        serializer = PurchaseOrderItemCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = PurchaseOrderService.add_item(
            order=order,
            validated_data=serializer.validated_data,
            added_by=request.user,
            request=request,
        )
        item = PurchaseOrderItem.objects.select_related('stock_item').get(id=item.id)
        return Response(
            PurchaseOrderItemSerializer(item).data,
            status=status.HTTP_201_CREATED,
        )

    def destroy(self, request, *args, **kwargs):
        order = self._get_order()
        item = self.get_object()
        PurchaseOrderService.remove_item(
            order=order,
            order_item=item,
            removed_by=request.user,
            request=request,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# ReceptionViewSet  (nested under /purchase-orders/<order_pk>/receptions/)
# ---------------------------------------------------------------------------

class ReceptionViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = ReceptionFilter
    ordering_fields = ['received_at', 'created_at']
    ordering = ['-received_at']

    def _get_order(self):
        return get_object_or_404(
            PurchaseOrder.objects.select_related('supplier'),
            pk=self.kwargs['order_pk'],
        )

    def get_queryset(self):
        return (
            Reception.objects
            .filter(order_id=self.kwargs['order_pk'])
            .select_related('received_by')
            .prefetch_related('items__order_item__stock_item', 'items__stock_lot')
        )

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAnyStaff()]
        return [IsTechnicianOrAbove()]

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ReceptionDetailSerializer
        if self.action == 'create':
            return ReceptionCreateSerializer
        return ReceptionListSerializer

    def create(self, request, *args, **kwargs):
        order = self._get_order()
        serializer = ReceptionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reception = ReceptionService.create(
            order=order,
            validated_data=serializer.validated_data,
            received_by=request.user,
            request=request,
        )
        reception = (
            Reception.objects
            .select_related('received_by')
            .prefetch_related('items__order_item__stock_item', 'items__stock_lot')
            .get(id=reception.id)
        )
        return Response(
            ReceptionDetailSerializer(reception).data,
            status=status.HTTP_201_CREATED,
        )
