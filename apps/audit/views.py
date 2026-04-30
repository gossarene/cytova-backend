"""
Cytova — Audit Log read-only HTTP surface.

A single endpoint:

    GET /api/v1/audit/   — list audit entries (newest first, paginated)

Default behaviour
-----------------
- Filtered to the **current calendar month** when no explicit date range
  is given. This keeps the page snappy on tenants with multi-year audit
  history; users widen the range explicitly.
- Pagination via PageNumberPagination so the response carries
  ``{count, next, previous, results}`` + ``page_size`` selector.

Filters
-------
  ?from=YYYY-MM-DD       inclusive lower bound on timestamp
  ?to=YYYY-MM-DD         inclusive upper bound on timestamp (date end-of-day)
  ?action=CREATE         match the AuditAction enum
  ?entity_type=...       case-insensitive contains
  ?actor_id=<uuid>       the staff user who performed the action
  ?search=...            cross-field free-text search; matches actor
                         first/last/full name + email, action,
                         entity_type, entity_id, actor_id and IP.
  ?page=2&page_size=50   pagination

Permission gate: ``audit.view`` (LAB_ADMIN + VIEWER_AUDITOR by default).
The Audit Log model is append-only and inherits the tenant schema from
``CytovaTenantMiddleware`` — no extra isolation logic needed in this view.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Optional

from django.db.models import CharField, Q, QuerySet, Value
from django.db.models.functions import Concat
from django.utils import timezone
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.users.models import StaffUser
from common.permissions import RequiresPermission

from .models import AuditLog
from .serializers import AuditLogSerializer


_AuditView = RequiresPermission('audit.view')


class AuditLogPagination(PageNumberPagination):
    """Page-number pagination tuned for the Audit Log UI: client-selectable
    page size up to 100 (the spec's largest option). Backend cap stays
    conservative — bigger payloads slow the diff-rendering UI."""
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 100


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        return None


def _start_of_month_today() -> tuple[date, date]:
    today = timezone.now().date()
    return today.replace(day=1), today


class AuditLogListView(APIView):
    permission_classes = [_AuditView]
    pagination_class = AuditLogPagination

    def get(self, request):
        qs: QuerySet = AuditLog.objects.all()
        params = request.query_params

        # ---- Date range — default to the current month ----------------
        # Empty params → start_of_month..today. Explicit empty string
        # acts the same as "unset" so the frontend can submit '' to clear.
        from_date = _parse_date(params.get('from'))
        to_date = _parse_date(params.get('to'))
        if from_date is None and to_date is None:
            from_date, to_date = _start_of_month_today()
        if from_date is not None:
            qs = qs.filter(timestamp__gte=datetime.combine(from_date, time.min))
        if to_date is not None:
            # Inclusive on the upper bound — end of the to_date day.
            qs = qs.filter(timestamp__lt=datetime.combine(to_date + timedelta(days=1), time.min))

        # ---- Discrete filters ----------------------------------------
        action = params.get('action')
        if action:
            qs = qs.filter(action=action)

        entity_type = params.get('entity_type')
        if entity_type:
            qs = qs.filter(entity_type__icontains=entity_type)

        actor_id = params.get('actor_id')
        if actor_id:
            qs = qs.filter(actor_id=actor_id)

        ip = params.get('ip_address')
        if ip:
            qs = qs.filter(ip_address=ip)

        # ---- Cross-field search --------------------------------------
        # One query parameter (?search=...) covers every column the
        # Audit Log UI exposes: action, entity type, entity id, IP, the
        # snapshotted actor email and — via a StaffUser lookup — the
        # actor's first/last/full name. Keeps the page to a single
        # search box; see ``AuditLogPage.tsx``.
        search = (params.get('search') or '').strip()
        if search:
            search_q = (
                Q(actor_email__icontains=search)
                | Q(action__icontains=search)
                | Q(entity_type__icontains=search)
                | Q(ip_address__icontains=search)
            )

            # Resolve the search term against StaffUser identity fields so
            # "René" / "GOSSA" / "rené gossa" / "admin@golab" all match the
            # rows whose ``actor_id`` points at that user, even though the
            # audit row itself only stores email + UUID.
            actor_match_ids = list(
                StaffUser.objects.annotate(
                    _full_name=Concat(
                        'first_name', Value(' '), 'last_name',
                        output_field=CharField(),
                    ),
                ).filter(
                    Q(first_name__icontains=search)
                    | Q(last_name__icontains=search)
                    | Q(_full_name__icontains=search)
                    | Q(email__icontains=search)
                ).values_list('id', flat=True)[:500]
            )
            if actor_match_ids:
                search_q |= Q(actor_id__in=actor_match_ids)

            # If the term looks like a UUID prefix, also match the id
            # columns directly. ``icontains`` over UUIDField casts to
            # text on Postgres — fine, just gated to avoid scanning the
            # UUID columns for every plain-text query.
            if len(search) >= 4 and all(
                c in '0123456789abcdefABCDEF-' for c in search
            ):
                search_q |= Q(entity_id__icontains=search) | Q(actor_id__icontains=search)

            qs = qs.filter(search_q)

        rows_qs = qs.order_by('-timestamp')

        # ---- Paginate -----------------------------------------------
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(rows_qs, request, view=self)

        # Pre-fetch the unique actors referenced on this page so the
        # serializer can render live display names in O(1) per row.
        actor_ids = {r.actor_id for r in page if r.actor_id}
        actors_by_id = {
            u.id: u
            for u in StaffUser.objects.filter(id__in=actor_ids).only(
                'id', 'email', 'title', 'first_name', 'last_name',
            )
        } if actor_ids else {}

        ser = AuditLogSerializer(
            page, many=True,
            context={'request': request, 'actor_index': actors_by_id},
        )
        return paginator.get_paginated_response(ser.data)
