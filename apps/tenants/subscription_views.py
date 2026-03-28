"""
Cytova — Subscription Views (Platform Admin API)

SubscriptionPlanViewSet   — CRUD for plan definitions
SubscriptionViewSet       — lifecycle management per tenant subscription
"""
import logging

import django_filters
from rest_framework import filters, status
from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from .authentication import PlatformAdminJWTAuthentication
from .models import Subscription, SubscriptionPlan, SubscriptionStatus
from .permissions import IsPlatformAdmin
from .platform_audit import PlatformAction, log_platform_action
from .subscription_serializers import (
    SubscriptionActivateSerializer,
    SubscriptionCancelSerializer,
    SubscriptionChangePlanSerializer,
    SubscriptionDetailSerializer,
    SubscriptionListSerializer,
    SubscriptionPlanCreateSerializer,
    SubscriptionPlanDetailSerializer,
    SubscriptionPlanListSerializer,
    SubscriptionPlanUpdateSerializer,
    SubscriptionSuspendSerializer,
)
from .subscription_service import SubscriptionPlanService, SubscriptionService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SubscriptionPlanViewSet
# ---------------------------------------------------------------------------

class SubscriptionPlanFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = SubscriptionPlan
        fields = ['is_active']


class SubscriptionPlanViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    authentication_classes = [PlatformAdminJWTAuthentication]
    permission_classes = [IsPlatformAdmin]
    filterset_class = SubscriptionPlanFilter
    search_fields = ['code', 'name']
    ordering_fields = ['display_order', 'name', 'created_at']
    ordering = ['display_order', 'name']

    def get_queryset(self):
        return SubscriptionPlan.objects.all()

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return SubscriptionPlanDetailSerializer
        if self.action == 'create':
            return SubscriptionPlanCreateSerializer
        if self.action == 'partial_update':
            return SubscriptionPlanUpdateSerializer
        return SubscriptionPlanListSerializer

    def create(self, request, *args, **kwargs):
        serializer = SubscriptionPlanCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        plan = SubscriptionPlanService.create(serializer.validated_data)

        log_platform_action(
            request=request,
            action=PlatformAction.CREATE,
            entity_type='SubscriptionPlan',
            entity_id=plan.id,
            diff={'after': {'code': plan.code, 'name': plan.name}},
        )

        return Response(
            SubscriptionPlanDetailSerializer(plan).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        plan = self.get_object()
        serializer = SubscriptionPlanUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not serializer.validated_data:
            return Response(SubscriptionPlanDetailSerializer(plan).data)

        before = {k: getattr(plan, k) for k in serializer.validated_data}
        plan = SubscriptionPlanService.update(plan, serializer.validated_data)
        after = {k: getattr(plan, k) for k in serializer.validated_data}

        log_platform_action(
            request=request,
            action=PlatformAction.UPDATE,
            entity_type='SubscriptionPlan',
            entity_id=plan.id,
            diff={'before': before, 'after': after},
        )

        return Response(SubscriptionPlanDetailSerializer(plan).data)

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, pk=None):
        plan = self.get_object()
        plan = SubscriptionPlanService.deactivate(plan)

        log_platform_action(
            request=request,
            action=PlatformAction.DEACTIVATE,
            entity_type='SubscriptionPlan',
            entity_id=plan.id,
            diff={'after': {'is_active': False}},
        )

        return Response(SubscriptionPlanDetailSerializer(plan).data)


# ---------------------------------------------------------------------------
# SubscriptionViewSet
# ---------------------------------------------------------------------------

class SubscriptionFilter(django_filters.FilterSet):
    tenant_id = django_filters.UUIDFilter(field_name='tenant_id')
    plan_id = django_filters.UUIDFilter(field_name='plan_id')
    status = django_filters.ChoiceFilter(choices=SubscriptionStatus.choices)

    class Meta:
        model = Subscription
        fields = ['tenant_id', 'plan_id', 'status']


class SubscriptionViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    authentication_classes = [PlatformAdminJWTAuthentication]
    permission_classes = [IsPlatformAdmin]
    filterset_class = SubscriptionFilter
    search_fields = ['tenant__name', 'tenant__subdomain']
    ordering_fields = ['created_at', 'status', 'trial_end_date']
    ordering = ['-created_at']

    def get_queryset(self):
        return Subscription.objects.select_related('tenant', 'plan').all()

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return SubscriptionDetailSerializer
        return SubscriptionListSerializer

    @action(detail=True, methods=['post'], url_path='activate')
    def activate(self, request, pk=None):
        subscription = self.get_object()
        serializer = SubscriptionActivateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        old_status = subscription.status
        subscription = SubscriptionService.activate(
            subscription=subscription,
            period_months=serializer.validated_data.get('period_months', 1),
            notes=serializer.validated_data.get('notes', ''),
        )

        log_platform_action(
            request=request,
            action=PlatformAction.ACTIVATE,
            entity_type='Subscription',
            entity_id=subscription.id,
            diff={
                'before': {'status': old_status},
                'after': {'status': subscription.status},
                'tenant': subscription.tenant.subdomain,
            },
        )

        return Response(SubscriptionDetailSerializer(subscription).data)

    @action(detail=True, methods=['post'], url_path='suspend')
    def suspend(self, request, pk=None):
        subscription = self.get_object()
        serializer = SubscriptionSuspendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        subscription = SubscriptionService.suspend(
            subscription=subscription,
            reason=serializer.validated_data.get('reason', ''),
        )

        log_platform_action(
            request=request,
            action=PlatformAction.SUSPEND,
            entity_type='Subscription',
            entity_id=subscription.id,
            diff={
                'after': {'status': SubscriptionStatus.SUSPENDED},
                'tenant': subscription.tenant.subdomain,
            },
        )

        return Response(SubscriptionDetailSerializer(subscription).data)

    @action(detail=True, methods=['post'], url_path='reactivate')
    def reactivate(self, request, pk=None):
        subscription = self.get_object()
        subscription = SubscriptionService.reactivate(subscription=subscription)

        log_platform_action(
            request=request,
            action=PlatformAction.ACTIVATE,
            entity_type='Subscription',
            entity_id=subscription.id,
            diff={
                'after': {'status': SubscriptionStatus.ACTIVE},
                'tenant': subscription.tenant.subdomain,
            },
        )

        return Response(SubscriptionDetailSerializer(subscription).data)

    @action(detail=True, methods=['post'], url_path='cancel')
    def cancel(self, request, pk=None):
        subscription = self.get_object()
        serializer = SubscriptionCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        subscription = SubscriptionService.cancel(
            subscription=subscription,
            cancelled_by='platform',
            reason=serializer.validated_data.get('reason', ''),
        )

        log_platform_action(
            request=request,
            action=PlatformAction.CANCEL,
            entity_type='Subscription',
            entity_id=subscription.id,
            diff={
                'after': {'status': SubscriptionStatus.CANCELLED},
                'tenant': subscription.tenant.subdomain,
            },
        )

        return Response(SubscriptionDetailSerializer(subscription).data)

    @action(detail=True, methods=['post'], url_path='change-plan')
    def change_plan(self, request, pk=None):
        subscription = self.get_object()
        serializer = SubscriptionChangePlanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        old_plan_code = subscription.plan.code
        new_plan = SubscriptionPlan.objects.get(
            id=serializer.validated_data['plan_id'],
        )
        subscription = SubscriptionService.change_plan(
            subscription=subscription,
            new_plan=new_plan,
        )

        log_platform_action(
            request=request,
            action=PlatformAction.PLAN_CHANGE,
            entity_type='Subscription',
            entity_id=subscription.id,
            diff={
                'before': {'plan': old_plan_code},
                'after': {'plan': new_plan.code},
                'tenant': subscription.tenant.subdomain,
            },
        )

        return Response(SubscriptionDetailSerializer(subscription).data)
