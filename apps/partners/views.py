"""
Cytova — Partner Organization Views

PartnerOrganizationViewSet — list, create, retrieve, partial_update, deactivate
PartnerExamPriceViewSet    — nested under partners; CRUD + deactivate/reactivate
"""
import logging

from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status
from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from common.permissions import IsAnyStaff, IsLabAdmin
from .filters import PartnerOrganizationFilter
from .models import PartnerExamPrice, PartnerOrganization
from .serializers import (
    PartnerExamPriceCreateSerializer,
    PartnerExamPriceListSerializer,
    PartnerExamPriceUpdateSerializer,
    PartnerOrganizationCreateSerializer,
    PartnerOrganizationDetailSerializer,
    PartnerOrganizationListSerializer,
    PartnerOrganizationUpdateSerializer,
)
from .services import PartnerExamPriceService, PartnerOrganizationService

logger = logging.getLogger(__name__)


class PartnerOrganizationViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    queryset = PartnerOrganization.objects.all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = PartnerOrganizationFilter
    search_fields = ['code', 'name', 'contact_person', 'email']
    ordering_fields = ['code', 'name', 'organization_type', 'created_at']
    ordering = ['name']

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAnyStaff()]
        return [IsLabAdmin()]

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return PartnerOrganizationDetailSerializer
        if self.action == 'create':
            return PartnerOrganizationCreateSerializer
        if self.action == 'partial_update':
            return PartnerOrganizationUpdateSerializer
        return PartnerOrganizationListSerializer

    def create(self, request, *args, **kwargs):
        serializer = PartnerOrganizationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        partner = PartnerOrganizationService.create(
            validated_data=serializer.validated_data,
            created_by=request.user,
            request=request,
        )
        return Response(
            PartnerOrganizationDetailSerializer(partner).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        partner = self.get_object()
        serializer = PartnerOrganizationUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not serializer.validated_data:
            return Response(PartnerOrganizationDetailSerializer(partner).data)
        partner = PartnerOrganizationService.update(
            partner=partner,
            validated_data=serializer.validated_data,
            updated_by=request.user,
            request=request,
        )
        return Response(PartnerOrganizationDetailSerializer(partner).data)

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, pk=None):
        partner = self.get_object()
        partner = PartnerOrganizationService.deactivate(
            partner=partner,
            deactivated_by=request.user,
            request=request,
        )
        return Response(PartnerOrganizationDetailSerializer(partner).data)


# ---------------------------------------------------------------------------
# PartnerExamPrice
# ---------------------------------------------------------------------------

class PartnerExamPriceViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    """
    Nested under a partner. All actions are scoped to the partner pulled
    from the URL kwargs (``partner_pk``), so cross-partner access is
    structurally impossible through this viewset — any request that
    resolves to a different partner's agreed price returns 404 via
    ``get_object``.

    Read path (list / retrieve) is open to any authenticated staff so the
    UI can display agreed-price info in a partner detail screen without
    elevating the viewer's role. Write actions require ``IsLabAdmin``, in
    line with how the rest of the catalog/partner write surface is gated.
    """
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['exam_definition__code', 'exam_definition__name']
    ordering_fields = ['agreed_price', 'created_at']
    ordering = ['-created_at']

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAnyStaff()]
        return [IsLabAdmin()]

    def get_serializer_class(self):
        if self.action == 'create':
            return PartnerExamPriceCreateSerializer
        if self.action == 'partial_update':
            return PartnerExamPriceUpdateSerializer
        return PartnerExamPriceListSerializer

    # -- Scoping helpers ----------------------------------------------------

    def _get_partner(self) -> PartnerOrganization:
        return get_object_or_404(PartnerOrganization, pk=self.kwargs['partner_pk'])

    def get_queryset(self):
        return PartnerExamPrice.objects.select_related(
            'partner', 'exam_definition',
        ).filter(partner_id=self.kwargs['partner_pk'])

    # -- Actions ------------------------------------------------------------

    def create(self, request, *args, **kwargs):
        partner = self._get_partner()
        serializer = PartnerExamPriceCreateSerializer(
            data=request.data,
            context={'partner': partner},
        )
        serializer.is_valid(raise_exception=True)
        price = PartnerExamPriceService.create(
            partner=partner,
            validated_data=serializer.validated_data,
            created_by=request.user,
            request=request,
        )
        price = PartnerExamPrice.objects.select_related(
            'partner', 'exam_definition',
        ).get(pk=price.pk)
        return Response(
            PartnerExamPriceListSerializer(price).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        price = self.get_object()
        serializer = PartnerExamPriceUpdateSerializer(
            data=request.data,
            context={'instance': price},
        )
        serializer.is_valid(raise_exception=True)
        price = PartnerExamPriceService.update(
            price=price,
            validated_data=serializer.validated_data,
            updated_by=request.user,
            request=request,
        )
        price = PartnerExamPrice.objects.select_related(
            'partner', 'exam_definition',
        ).get(pk=price.pk)
        return Response(PartnerExamPriceListSerializer(price).data)

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, pk=None, **kwargs):
        price = self.get_object()
        price = PartnerExamPriceService.deactivate(
            price=price,
            deactivated_by=request.user,
            request=request,
        )
        return Response(PartnerExamPriceListSerializer(price).data)

    @action(detail=True, methods=['post'], url_path='reactivate')
    def reactivate(self, request, pk=None, **kwargs):
        price = self.get_object()
        price = PartnerExamPriceService.reactivate(
            price=price,
            reactivated_by=request.user,
            request=request,
        )
        return Response(PartnerExamPriceListSerializer(price).data)
