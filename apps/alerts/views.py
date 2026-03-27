"""
Cytova — Inventory Alert Views

InventoryAlertViewSet
    list, retrieve, acknowledge, resolve, bulk_acknowledge, summary
"""
import logging

from django.db.models import Count
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status
from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from common.permissions import IsAnyStaff, IsBiologistOrAbove, IsTechnicianOrAbove
from .filters import InventoryAlertFilter
from .models import OPEN_STATUSES, InventoryAlert
from .serializers import (
    AlertSummarySerializer,
    BulkAcknowledgeSerializer,
    InventoryAlertDetailSerializer,
    InventoryAlertListSerializer,
)
from .services import InventoryAlertService

logger = logging.getLogger(__name__)


class InventoryAlertViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = InventoryAlertFilter
    ordering_fields = ['severity', 'alert_type', 'status', 'created_at']
    ordering = ['-created_at']

    def get_queryset(self):
        return (
            InventoryAlert.objects
            .select_related(
                'stock_item', 'stock_lot',
                'acknowledged_by', 'resolved_by',
            )
        )

    def get_permissions(self):
        if self.action in ('list', 'retrieve', 'summary'):
            return [IsAnyStaff()]
        if self.action in ('acknowledge', 'bulk_acknowledge'):
            return [IsTechnicianOrAbove()]
        if self.action == 'resolve':
            return [IsBiologistOrAbove()]
        return [IsAnyStaff()]

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return InventoryAlertDetailSerializer
        return InventoryAlertListSerializer

    @action(detail=True, methods=['post'], url_path='acknowledge')
    def acknowledge(self, request, pk=None):
        alert = self.get_object()
        alert = InventoryAlertService.acknowledge(
            alert=alert,
            acknowledged_by=request.user,
            request=request,
        )
        return Response(InventoryAlertDetailSerializer(alert).data)

    @action(detail=True, methods=['post'], url_path='resolve')
    def resolve(self, request, pk=None):
        alert = self.get_object()
        alert = InventoryAlertService.resolve(
            alert=alert,
            resolved_by=request.user,
            request=request,
        )
        return Response(InventoryAlertDetailSerializer(alert).data)

    @action(detail=False, methods=['post'], url_path='bulk-acknowledge')
    def bulk_acknowledge(self, request):
        serializer = BulkAcknowledgeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        count = InventoryAlertService.bulk_acknowledge(
            alert_ids=serializer.validated_data['alert_ids'],
            acknowledged_by=request.user,
            request=request,
        )
        return Response({'acknowledged': count})

    @action(detail=False, methods=['get'], url_path='summary')
    def summary(self, request):
        """
        Aggregated counts of open alerts grouped by alert_type and severity.
        Designed for dashboard widgets.
        """
        rows = (
            InventoryAlert.objects
            .filter(status__in=OPEN_STATUSES)
            .values('alert_type', 'severity')
            .annotate(count=Count('id'))
            .order_by('alert_type', 'severity')
        )
        return Response(AlertSummarySerializer(rows, many=True).data)
