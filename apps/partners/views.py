"""
Cytova — Partner Organization Views

PartnerOrganizationViewSet — list, create, retrieve, partial_update, deactivate
"""
import logging

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status
from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from common.permissions import IsAnyStaff, IsLabAdmin
from .filters import PartnerOrganizationFilter
from .models import PartnerOrganization
from .serializers import (
    PartnerOrganizationCreateSerializer,
    PartnerOrganizationDetailSerializer,
    PartnerOrganizationListSerializer,
    PartnerOrganizationUpdateSerializer,
)
from .services import PartnerOrganizationService

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
