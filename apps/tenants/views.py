"""
Cytova — Tenants Views (Platform Admin API)

Served on admin.cytova.io/api/v1/platform/
All endpoints require PlatformAdminJWTAuthentication + IsPlatformAdmin.

TenantViewSet:
    GET    /platform/tenants/              — list all tenants
    POST   /platform/tenants/              — provision new tenant
    GET    /platform/tenants/{id}/         — tenant detail (with subscription)
    PATCH  /platform/tenants/{id}/         — update name/plan
    POST   /platform/tenants/{id}/suspend/ — suspend tenant
    POST   /platform/tenants/{id}/activate/— reactivate tenant

PlatformAdminLoginView:
    POST   /platform/auth/login/           — issue platform admin access token

PlatformDashboardView:
    GET    /platform/dashboard/            — platform-level statistics
"""
from django.db.models import Count, F, Q
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet
import django_filters

from .authentication import PlatformAdminJWTAuthentication
from .models import Tenant, Plan, PlatformAdmin, Subscription, SubscriptionStatus
from .permissions import IsPlatformAdmin
from .platform_audit import PlatformAction, log_platform_action
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
        return Tenant.objects.prefetch_related('domains', 'subscriptions__plan').all()

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

        log_platform_action(
            request=request,
            action=PlatformAction.CREATE,
            entity_type='Tenant',
            entity_id=tenant.id,
            diff={'after': {
                'name': tenant.name,
                'subdomain': tenant.subdomain,
                'plan': tenant.plan,
            }},
        )

        return Response(
            TenantDetailSerializer(tenant).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, pk=None):
        tenant = self.get_object()
        serializer = TenantUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        before = {k: getattr(tenant, k) for k in serializer.validated_data}
        tenant = TenantService.update_tenant(tenant, dict(serializer.validated_data))
        after = {k: getattr(tenant, k) for k in serializer.validated_data}

        log_platform_action(
            request=request,
            action=PlatformAction.UPDATE,
            entity_type='Tenant',
            entity_id=tenant.id,
            diff={'before': before, 'after': after},
        )

        return Response(TenantDetailSerializer(tenant).data)

    @action(detail=True, methods=['post'])
    def suspend(self, request, pk=None):
        tenant = self.get_object()
        tenant = TenantService.suspend_tenant(tenant)

        log_platform_action(
            request=request,
            action=PlatformAction.SUSPEND,
            entity_type='Tenant',
            entity_id=tenant.id,
            diff={'after': {'is_active': False}},
        )

        return Response(TenantDetailSerializer(tenant).data)

    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        tenant = self.get_object()
        tenant = TenantService.activate_tenant(tenant)

        log_platform_action(
            request=request,
            action=PlatformAction.ACTIVATE,
            entity_type='Tenant',
            entity_id=tenant.id,
            diff={'after': {'is_active': True}},
        )

        return Response(TenantDetailSerializer(tenant).data)


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


class PlatformDashboardView(APIView):
    """
    GET /platform/dashboard/

    Platform-level overview statistics for the admin panel.
    """
    authentication_classes = [PlatformAdminJWTAuthentication]
    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        # Tenant counts
        total_tenants = Tenant.objects.count()
        active_tenants = Tenant.objects.filter(is_active=True).count()

        # Subscription status breakdown
        sub_by_status = dict(
            Subscription.objects
            .values('status')
            .annotate(count=Count('id'))
            .values_list('status', 'count')
        )

        # Tenants with no subscription at all
        tenants_with_sub = Subscription.objects.values('tenant_id').distinct().count()
        tenants_no_sub = total_tenants - tenants_with_sub

        # Plan distribution (active/trial subscriptions only)
        plan_distribution = dict(
            Subscription.objects
            .filter(status__in=[SubscriptionStatus.TRIAL, SubscriptionStatus.ACTIVE])
            .values(plan_code=F('plan__code'))
            .annotate(count=Count('id'))
            .values_list('plan_code', 'count')
        )

        # Trials expiring soon
        from datetime import timedelta
        from django.conf import settings as django_settings
        from django.utils import timezone

        now = timezone.now()
        warning_days = getattr(django_settings, 'PLATFORM_TRIAL_WARNING_DAYS', 7)
        trials_expiring_soon = Subscription.objects.filter(
            status=SubscriptionStatus.TRIAL,
            trial_end_date__lte=now + timedelta(days=warning_days),
            trial_end_date__gt=now,
        ).count()

        return Response({
            'data': {
                'tenants': {
                    'total': total_tenants,
                    'active': active_tenants,
                    'suspended': total_tenants - active_tenants,
                },
                'subscriptions': {
                    'by_status': sub_by_status,
                    'no_subscription': tenants_no_sub,
                    'trials_expiring_soon': trials_expiring_soon,
                },
                'plan_distribution': plan_distribution,
            },
            'meta': None,
            'errors': [],
        })
