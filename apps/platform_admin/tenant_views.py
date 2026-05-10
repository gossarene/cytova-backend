"""
Platform-admin tenant listing API.

Endpoints (mounted on the public-schema URL conf only):

  GET /api/v1/platform-admin/tenants/         — list all laboratories
  GET /api/v1/platform-admin/tenants/{id}/    — single laboratory metadata

Read-only by design. The platform-admin tenant write surface stays
on the legacy ``/api/v1/platform/tenants/`` mount (``apps.tenants``) —
this view set deliberately exposes ``list`` + ``retrieve`` only so a
future role split (e.g. read-only auditor accounts) lands cleanly.

Schema isolation
----------------
All sourced fields live in the public schema:
  - ``Tenant`` / ``Domain`` (django-tenants registry)
  - ``Subscription`` (lifecycle metadata)

Tenant-schema tables are never queried. The platform-admin surface
sees lab *metadata*, not lab data — the schema isolation contract
that protects PHI is preserved at the routing layer.
"""
from __future__ import annotations

import django_filters
from django.db.models import OuterRef, Subquery
from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from apps.tenants.models import Subscription, SubscriptionStatus, Tenant
from apps.tenants.services import TenantService

from .audit import log_platform_admin_action
from .authentication import PlatformAdminJWTAuthentication
from .models import PlatformAuditAction
from .permissions import IsPlatformAdmin
from .serializers import (
    ChangePlanSerializer, ExtendTrialSerializer, PlatformTenantSerializer,
)
from .tenant_actions import change_plan, extend_trial


class PlatformTenantFilter(django_filters.FilterSet):
    """Filter set for the tenant listing endpoint.

    ``subscription_status`` is matched against the *latest*
    subscription for each tenant. A tenant whose trial expired and
    was renewed to ACTIVE therefore matches ``status=ACTIVE`` only —
    not ``status=TRIAL`` (which would describe historical state).
    Done with a correlated subquery so it composes with ``is_active``
    and the search filter without producing duplicate rows.
    """
    is_active = django_filters.BooleanFilter()
    subscription_status = django_filters.ChoiceFilter(
        choices=SubscriptionStatus.choices,
        method='filter_subscription_status',
    )

    class Meta:
        model = Tenant
        fields = ['is_active']

    def filter_subscription_status(self, queryset, name, value):
        latest_status = (
            Subscription.objects
            .filter(tenant_id=OuterRef('pk'))
            .order_by('-created_at')
            .values('status')[:1]
        )
        return queryset.annotate(
            _latest_subscription_status=Subquery(latest_status),
        ).filter(_latest_subscription_status=value)


class PlatformTenantViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    """Read-only tenant browsing for platform admins.

    Audit:
      - ``PLATFORM_TENANT_LIST_VIEWED`` after every successful list
        call. ``metadata.query_params`` captures the filter slice so
        a SIEM can later reconstruct what the admin saw.
      - ``PLATFORM_TENANT_DETAIL_VIEWED`` after every successful
        retrieve call, with the viewed tenant's id in ``entity_id``.

    Both audits fire only on the success path — a 4xx (e.g. invalid
    filter) propagates from ``super().list`` before the audit row is
    written, so the audit log never claims an admin viewed something
    they were actually denied.

    Performance:
      ``prefetch_related`` pulls ``domains`` and
      ``subscriptions__plan`` in two extra queries regardless of page
      size, so the serializer's ``SerializerMethodField``s avoid
      N+1 patterns.
    """
    authentication_classes = [PlatformAdminJWTAuthentication]
    permission_classes = [IsPlatformAdmin]
    serializer_class = PlatformTenantSerializer
    filterset_class = PlatformTenantFilter
    search_fields = ['name', 'subdomain']
    ordering_fields = ['created_at', 'name', 'subdomain']
    ordering = ['-created_at']

    def get_queryset(self):
        return (
            Tenant.objects
            .all()
            .prefetch_related('domains', 'subscriptions__plan')
        )

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        log_platform_admin_action(
            request=request,
            action=PlatformAuditAction.PLATFORM_TENANT_LIST_VIEWED,
            actor=request.user,
            entity_type='Tenant',
            metadata={'query_params': dict(request.query_params.lists())},
        )
        return response

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        log_platform_admin_action(
            request=request,
            action=PlatformAuditAction.PLATFORM_TENANT_DETAIL_VIEWED,
            actor=request.user,
            entity_type='Tenant',
            entity_id=instance.id,
            metadata={},
        )
        return Response(serializer.data)

    # ------------------------------------------------------------------
    # Lifecycle actions
    #
    # All four are POST detail actions returning the refreshed tenant
    # in the same shape as ``retrieve``. Audits run AFTER the state
    # mutation succeeds — a service-layer ValidationError propagates
    # to a 400 from DRF's exception handler before the audit row is
    # written, so the audit log never claims a change that didn't
    # happen.
    #
    # Authorisation: gated by ``IsPlatformAdmin`` (any active platform
    # admin). The ``required_platform_roles`` hook in
    # ``HasPlatformRole`` is the seam to tighten suspend/reactivate
    # to SUPER_ADMIN later without touching the action handlers.
    # ------------------------------------------------------------------

    def _refresh(self, tenant: Tenant) -> Tenant:
        """Re-fetch with prefetches so the response payload reflects
        the post-mutation domain + subscription set without an N+1."""
        return (
            Tenant.objects
            .prefetch_related('domains', 'subscriptions__plan')
            .get(pk=tenant.pk)
        )

    @action(detail=True, methods=['post'], url_path='suspend')
    def suspend(self, request, pk=None):
        tenant = self.get_object()
        before_active = tenant.is_active
        tenant = TenantService.suspend_tenant(tenant)

        log_platform_admin_action(
            request=request,
            action=PlatformAuditAction.PLATFORM_TENANT_SUSPENDED,
            actor=request.user,
            entity_type='Tenant',
            entity_id=tenant.id,
            metadata={
                'subdomain': tenant.subdomain,
                'before': {'is_active': before_active},
                'after': {'is_active': tenant.is_active},
            },
        )
        return Response(self.get_serializer(self._refresh(tenant)).data)

    @action(detail=True, methods=['post'], url_path='reactivate')
    def reactivate(self, request, pk=None):
        tenant = self.get_object()
        before_active = tenant.is_active
        tenant = TenantService.activate_tenant(tenant)

        log_platform_admin_action(
            request=request,
            action=PlatformAuditAction.PLATFORM_TENANT_REACTIVATED,
            actor=request.user,
            entity_type='Tenant',
            entity_id=tenant.id,
            metadata={
                'subdomain': tenant.subdomain,
                'before': {'is_active': before_active},
                'after': {'is_active': tenant.is_active},
            },
        )
        return Response(self.get_serializer(self._refresh(tenant)).data)

    @action(detail=True, methods=['post'], url_path='extend-trial')
    def extend_trial(self, request, pk=None):
        tenant = self.get_object()
        serializer = ExtendTrialSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        days = serializer.validated_data['days']

        result = extend_trial(tenant, days=days)

        log_platform_admin_action(
            request=request,
            action=PlatformAuditAction.PLATFORM_TENANT_TRIAL_EXTENDED,
            actor=request.user,
            entity_type='Tenant',
            entity_id=tenant.id,
            metadata={
                'subdomain': tenant.subdomain,
                'days': days,
                'subscription_id': str(result.subscription.id),
                'before': {'trial_end_date': result.before_trial_end},
                'after': {'trial_end_date': result.after_trial_end},
            },
        )
        return Response(self.get_serializer(self._refresh(tenant)).data)

    @action(detail=True, methods=['post'], url_path='change-plan')
    def change_plan(self, request, pk=None):
        tenant = self.get_object()
        serializer = ChangePlanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_plan = serializer.validated_data['plan_id']

        result = change_plan(tenant, new_plan=new_plan)

        log_platform_admin_action(
            request=request,
            action=PlatformAuditAction.PLATFORM_TENANT_PLAN_CHANGED,
            actor=request.user,
            entity_type='Tenant',
            entity_id=tenant.id,
            metadata={
                'subdomain': tenant.subdomain,
                'previous_plan_code': result.previous_plan_code,
                'new_plan_code': result.new_plan_code,
                'previous_subscription_id': (
                    str(result.previous_subscription.id)
                    if result.previous_subscription else None
                ),
                'new_subscription_id': str(result.new_subscription.id),
            },
        )
        return Response(self.get_serializer(self._refresh(tenant)).data)
