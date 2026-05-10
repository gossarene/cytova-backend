"""
Platform-admin patient account API.

Endpoints (mounted on the public-schema URL conf only):

  GET  /api/v1/platform-admin/patients/                   list
  GET  /api/v1/platform-admin/patients/{id}/              detail
  POST /api/v1/platform-admin/patients/{id}/deactivate/   flip is_active=False
  POST /api/v1/platform-admin/patients/{id}/reactivate/   flip is_active=True

Scope contract
--------------
The platform-admin patient surface is for account-level support only:

  - It reads / writes ``PatientAccount`` rows in the public schema.
  - It does NOT touch any tenant ``apps.patients.Patient`` row.
  - It does NOT read clinical content. Specifically: no
    ``PatientSharedResult`` source/file payload, no PDFs, no tokens,
    no per-row download history. A scalar ``results_count`` is the
    only thing the surface aggregates from share data — and even
    that is filtered to ``status=ACTIVE`` so revoked / hidden rows
    are not counted (otherwise the metric would leak revocation
    behaviour).

Reversibility
-------------
Deactivate / reactivate are pure boolean toggles on ``is_active``.
The action is fully reversible by the inverse call. We deliberately
do NOT cascade into tokens here — token revocation on lockout is a
separate concern handled by the patient-portal logout-all path.
"""
from __future__ import annotations

import django_filters
from django.db.models import Count, Q
from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from apps.patient_portal.models import (
    PatientAccount, PatientSharedResult, SharedResultStatus,
)

from .audit import log_platform_admin_action
from .authentication import PlatformAdminJWTAuthentication
from .models import PlatformAuditAction
from .permissions import IsPlatformAdmin
from .serializers import PlatformPatientAccountSerializer


class PlatformPatientAccountFilter(django_filters.FilterSet):
    """Filter set for the patient-account listing endpoint.

    ``is_email_verified`` is computed from
    ``email_verified_at IS NOT NULL`` because the model stores the
    timestamp rather than a separate flag — exposing the boolean
    keeps the platform-admin contract stable even if the underlying
    storage is later split into a ``BooleanField`` + a separate
    ``verified_at`` column.
    """
    is_active = django_filters.BooleanFilter()
    is_email_verified = django_filters.BooleanFilter(
        method='filter_is_email_verified',
    )

    class Meta:
        model = PatientAccount
        fields = ['is_active']

    def filter_is_email_verified(self, queryset, name, value):
        if value is True:
            return queryset.filter(email_verified_at__isnull=False)
        if value is False:
            return queryset.filter(email_verified_at__isnull=True)
        return queryset


class PlatformPatientAccountViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    """Read + lifecycle viewset for platform-admin patient management.

    Inherits only ``ListModelMixin`` + ``RetrieveModelMixin`` — no
    ``create``/``update``/``destroy`` will be auto-routed even when
    new mixins land in DRF. Lifecycle changes are explicit POST
    actions so a misclick on the URL bar (DELETE on a list page)
    can never wipe a patient.

    Performance:
      - ``select_related('profile')`` resolves ``cytova_patient_id``
        in the same query as the patient row.
      - ``annotate(results_count=…)`` counts only ``ACTIVE`` shared
        results — revoked rows are excluded so the metric doesn't
        leak revocation behaviour. One aggregate column, no N+1.

    Audit:
      - ``PLATFORM_PATIENT_LIST_VIEWED`` after every successful list
        (with the active query params snapshot).
      - ``PLATFORM_PATIENT_DETAIL_VIEWED`` after every successful
        retrieve, with the patient id in ``entity_id``.
      - ``PLATFORM_PATIENT_DEACTIVATED`` /
        ``PLATFORM_PATIENT_REACTIVATED`` after the toggle, with
        before / after ``is_active`` values in metadata.
      - All audits fire only on the success path. A 401 / 404 / 400
        propagates BEFORE the audit row is written so the log never
        claims a change that didn't happen.
    """
    authentication_classes = [PlatformAdminJWTAuthentication]
    permission_classes = [IsPlatformAdmin]
    serializer_class = PlatformPatientAccountSerializer
    filterset_class = PlatformPatientAccountFilter
    search_fields = ['email']
    ordering_fields = ['created_at', 'email', 'last_login']
    ordering = ['-created_at']

    def get_queryset(self):
        return (
            PatientAccount.objects
            .select_related('profile')
            .annotate(
                results_count=Count(
                    'shared_results',
                    filter=Q(
                        shared_results__status=SharedResultStatus.ACTIVE,
                    ),
                ),
            )
        )

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        log_platform_admin_action(
            request=request,
            action=PlatformAuditAction.PLATFORM_PATIENT_LIST_VIEWED,
            actor=request.user,
            entity_type='PatientAccount',
            metadata={'query_params': dict(request.query_params.lists())},
        )
        return response

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        log_platform_admin_action(
            request=request,
            action=PlatformAuditAction.PLATFORM_PATIENT_DETAIL_VIEWED,
            actor=request.user,
            entity_type='PatientAccount',
            entity_id=instance.id,
            metadata={},
        )
        return Response(serializer.data)

    # ------------------------------------------------------------------
    # Lifecycle actions — toggle is_active.
    #
    # Deliberately separate endpoints (not a single `set_active`
    # taking a boolean) so the audit action code is unambiguous from
    # the URL alone — a SIEM rule can match path + action without
    # having to read the request body.
    # ------------------------------------------------------------------

    def _refresh(self, account: PatientAccount) -> PatientAccount:
        """Re-fetch with the same prefetches the list/retrieve queries
        use, so the response payload includes the same shape."""
        return self.get_queryset().get(pk=account.pk)

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, pk=None):
        account = self.get_object()
        before_active = account.is_active
        if account.is_active:
            account.is_active = False
            account.save(update_fields=['is_active', 'updated_at'])

        log_platform_admin_action(
            request=request,
            action=PlatformAuditAction.PLATFORM_PATIENT_DEACTIVATED,
            actor=request.user,
            entity_type='PatientAccount',
            entity_id=account.id,
            metadata={
                'before': {'is_active': before_active},
                'after': {'is_active': account.is_active},
            },
        )
        return Response(self.get_serializer(self._refresh(account)).data)

    @action(detail=True, methods=['post'], url_path='reactivate')
    def reactivate(self, request, pk=None):
        account = self.get_object()
        before_active = account.is_active
        if not account.is_active:
            account.is_active = True
            account.save(update_fields=['is_active', 'updated_at'])

        log_platform_admin_action(
            request=request,
            action=PlatformAuditAction.PLATFORM_PATIENT_REACTIVATED,
            actor=request.user,
            entity_type='PatientAccount',
            entity_id=account.id,
            metadata={
                'before': {'is_active': before_active},
                'after': {'is_active': account.is_active},
            },
        )
        return Response(self.get_serializer(self._refresh(account)).data)
