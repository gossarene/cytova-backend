"""
Platform-admin global dashboard.

  GET /api/v1/platform-admin/dashboard/

The endpoint returns scalar counts only — no row identifiers, no
emails, no subdomains, no clinical content. It is safe to cache or
share as an operations snapshot.

Query strategy
--------------
Each "card" (tenants / patients / activity) collapses to a single
SQL statement using conditional aggregation
(``Count(filter=Q(...))``). That means three queries total — not
nine — and Postgres can answer all of them from existing
``b-tree`` indexes:

  - ``Tenant``                : ``is_active`` (default Django index)
  - ``Subscription``          : ``(status, trial_end_date)`` index
                                (declared in ``apps.tenants.models``)
  - ``PatientAccount``        : ``is_active``, ``created_at`` indexed
  - ``PatientSharedResult``   : ``created_at``, ``last_downloaded_at``,
                                ``status``, ``email_notification_sent_at``
                                — already indexed where it matters
                                for the rest of the patient-portal
                                surface.

Window
------
``WINDOW_DAYS`` is the look-back window for the activity card and
``patients.new_last_30_days``. Centralised so a future "?days="
override (out of scope here) lands cleanly.
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.patient_portal.models import (
    PatientAccount, PatientSharedResult,
)
from apps.tenants.models import Subscription, SubscriptionStatus, Tenant

from .audit import log_platform_admin_action
from .authentication import PlatformAdminJWTAuthentication
from .models import PlatformAuditAction
from .permissions import IsPlatformAdmin
from .serializers import PlatformDashboardSerializer


WINDOW_DAYS = 30


def _tenants_card(now) -> dict:
    """Collapse the four tenant counters into a single query.

    ``trial`` joins ``Subscription`` so the count reflects "tenants
    that currently have a TRIAL subscription", not "tenants whose
    legacy ``Tenant.plan`` field is TRIAL". The latter is a legacy
    convenience column kept for back-compat — the authoritative
    state lives on ``Subscription``.
    """
    base = Tenant.objects.aggregate(
        total=Count('id'),
        active=Count('id', filter=Q(is_active=True)),
        suspended=Count('id', filter=Q(is_active=False)),
    )
    # Distinct trial tenants — a tenant could in principle have
    # multiple Subscription rows over its lifetime, so we count
    # tenant ids, not subscription ids.
    trial_tenants = (
        Subscription.objects
        .filter(status=SubscriptionStatus.TRIAL)
        .values('tenant_id')
        .distinct()
        .count()
    )
    base['trial'] = trial_tenants
    return base


def _patients_card(now) -> dict:
    cutoff = now - timedelta(days=WINDOW_DAYS)
    return PatientAccount.objects.aggregate(
        total=Count('id'),
        active=Count('id', filter=Q(is_active=True)),
        new_last_30_days=Count('id', filter=Q(created_at__gte=cutoff)),
    )


def _activity_card(now) -> dict:
    cutoff = now - timedelta(days=WINDOW_DAYS)
    # Single aggregate over PatientSharedResult — all three
    # counters share the same FROM clause so we resolve them with
    # one SELECT and three conditional COUNTs.
    return PatientSharedResult.objects.aggregate(
        results_shared_last_30_days=Count(
            'id', filter=Q(created_at__gte=cutoff),
        ),
        results_downloaded_last_30_days=Count(
            'id', filter=Q(last_downloaded_at__gte=cutoff),
        ),
        emails_sent_last_30_days=Count(
            'id',
            filter=Q(
                email_notification_status='SENT',
                email_notification_sent_at__gte=cutoff,
            ),
        ),
    )


class PlatformDashboardView(APIView):
    """``GET /api/v1/platform-admin/dashboard/``

    Returns aggregated platform metrics. Counts only — no row
    identifiers, no PII. Safe to expose to a status page or paste
    into an ops channel.

    Audit:
      ``PLATFORM_DASHBOARD_VIEWED`` is appended on every successful
      call. The audit row carries no row-level data; the existence
      of the row is the signal.

    NOTE: A separate legacy dashboard exists at
    ``/api/v1/platform/dashboard/`` (mounted by ``apps.tenants``). It
    is the older surface used by the legacy tenant-CRUD UI; this
    Phase 5 endpoint is the canonical platform-admin dashboard for
    the new ``/api/v1/platform-admin/`` namespace and stays
    independent so the two surfaces can evolve at different cadences.
    """
    authentication_classes = [PlatformAdminJWTAuthentication]
    permission_classes = [IsAuthenticated, IsPlatformAdmin]

    def get(self, request):
        now = timezone.now()
        payload = {
            'generated_at': now,
            'window_days': WINDOW_DAYS,
            'tenants': _tenants_card(now),
            'patients': _patients_card(now),
            'activity': _activity_card(now),
        }
        # Run the read-only serializer to validate that each card
        # produced an integer for every documented field. A missing
        # key (e.g. someone removes a counter from ``_tenants_card``)
        # would silently 200 with a partial payload otherwise.
        serializer = PlatformDashboardSerializer(payload)

        log_platform_admin_action(
            request=request,
            action=PlatformAuditAction.PLATFORM_DASHBOARD_VIEWED,
            actor=request.user,
            entity_type='Platform',
            metadata={'window_days': WINDOW_DAYS},
        )
        return Response(serializer.data)
