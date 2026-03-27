"""
Cytova — Tenants Views (Platform Admin API)

Served on admin.cytova.io/api/v1/platform/
All endpoints require PlatformAdminJWTAuthentication + IsPlatformAdmin.

TenantViewSet:
    GET    /platform/tenants/              — list all tenants
    POST   /platform/tenants/              — provision new tenant
    GET    /platform/tenants/{id}/         — tenant detail
    PATCH  /platform/tenants/{id}/         — update name/plan
    POST   /platform/tenants/{id}/suspend/ — suspend tenant
    POST   /platform/tenants/{id}/activate/— reactivate tenant

PlatformAdminLoginView:
    POST   /platform/auth/login/           — issue platform admin access token
"""
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet
import django_filters

from .authentication import PlatformAdminJWTAuthentication
from .models import Tenant, Plan, PlatformAdmin
from .permissions import IsPlatformAdmin
from .serializers import (
    TenantListSerializer,
    TenantDetailSerializer,
    TenantCreateSerializer,
    TenantUpdateSerializer,
    PlatformAdminLoginSerializer,
)
from .services import TenantService
from .tokens import PlatformAdminAccessToken


class TenantFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter()
    plan = django_filters.ChoiceFilter(choices=Plan.choices)

    class Meta:
        model = Tenant
        fields = ['is_active', 'plan']


class TenantViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    authentication_classes = [PlatformAdminJWTAuthentication]
    permission_classes = [IsPlatformAdmin]
    filterset_class = TenantFilter
    search_fields = ['name', 'subdomain']
    ordering_fields = ['name', 'subdomain', 'created_at', 'plan']

    def get_queryset(self):
        return Tenant.objects.prefetch_related('domains').all()

    def get_serializer_class(self):
        if self.action == 'list':
            return TenantListSerializer
        if self.action == 'create':
            return TenantCreateSerializer
        if self.action == 'partial_update':
            return TenantUpdateSerializer
        return TenantDetailSerializer

    def create(self, request):
        serializer = TenantCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tenant = TenantService.provision_tenant(dict(serializer.validated_data))
        return Response(
            TenantDetailSerializer(tenant, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, pk=None):
        tenant = self.get_object()
        serializer = TenantUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        tenant = TenantService.update_tenant(tenant, dict(serializer.validated_data))
        return Response(TenantDetailSerializer(tenant, context={'request': request}).data)

    @action(detail=True, methods=['post'])
    def suspend(self, request, pk=None):
        tenant = self.get_object()
        tenant = TenantService.suspend_tenant(tenant)
        return Response(TenantDetailSerializer(tenant, context={'request': request}).data)

    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        tenant = self.get_object()
        tenant = TenantService.activate_tenant(tenant)
        return Response(TenantDetailSerializer(tenant, context={'request': request}).data)


class PlatformAdminLoginView(APIView):
    """
    POST /platform/auth/login/

    Authenticate a platform admin with email + password.
    Returns a long-lived access token with user_type='PLATFORM_ADMIN'.
    No refresh token — platform admins re-authenticate when the token expires.
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        serializer = PlatformAdminLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data['email']
        password = serializer.validated_data['password']

        try:
            admin = PlatformAdmin.objects.get(email=email, is_active=True)
        except PlatformAdmin.DoesNotExist:
            return Response(
                {
                    'data': None,
                    'meta': None,
                    'errors': [{
                        'code': 'AUTHENTICATION_FAILED',
                        'message': 'Invalid credentials.',
                        'field': None,
                        'detail': {},
                    }],
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        if not admin.check_password(password):
            return Response(
                {
                    'data': None,
                    'meta': None,
                    'errors': [{
                        'code': 'AUTHENTICATION_FAILED',
                        'message': 'Invalid credentials.',
                        'field': None,
                        'detail': {},
                    }],
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        access = PlatformAdminAccessToken.for_user(admin)

        return Response({
            'data': {
                'access_token': str(access),
                'token_type': 'Bearer',
                'expires_in': int(access.lifetime.total_seconds()),
                'admin': {
                    'id': str(admin.id),
                    'email': admin.email,
                },
            },
            'meta': None,
            'errors': [],
        })
