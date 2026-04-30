"""
Cytova — Financial Reports composer.

Builds a stateless financial-simulation payload (summary + per-request rows
+ chart series) from real ``AnalysisRequest`` / ``AnalysisRequestItem`` data.
Nothing is persisted: no Invoice records, no invoice numbers, no period
locking — the user can re-run the same period any number of times and the
underlying invoicing flow is untouched.

Pricing source of truth
-----------------------
Per-row gross uses each ``AnalysisRequestItem.billed_price`` — a value
snapshotted on the item at request creation, NOT a live read from
``ExamDefinition.unit_price``. This means that re-pricing the catalog
later cannot retroactively change a historical financial report. Partner
discounts apply ``PartnerOrganization.invoice_discount_rate`` AT QUERY
TIME, mirroring the invoicing service. The combination is acceptable for
a simulation surface; a fully snapshotted "FinancialReportSnapshot" model
would be the next step for immutable saved reports.

TODO: introduce FinancialReportSnapshot later for immutable saved reports.

Performance
-----------
- The base request queryset is fetched once with ``select_related`` for
  patient + partner_organization (used in row construction) and
  ``prefetch_related`` for items + their exam_definition (used both for
  per-row drill-down and for the rows' aggregated gross/exam_count, so
  we avoid the round-trip required by the previous annotated approach).
- Item-level chart aggregations (top_exams_*) run as a single GROUP BY
  query at the DB layer — no Python loops over items there.
- Source / partner / partner-time-comparison series are derived from the
  in-memory rows (no extra queries).

Tenant isolation is implicit: every query runs in the active tenant schema
set by ``CytovaTenantMiddleware``.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Optional

from django.db.models import Count, DecimalField, F, Prefetch, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncDate


# Source-type tokens are exposed to the frontend filter and stored as-is in
# the response so the UI can render labels.
SOURCE_ALL = 'ALL'
SOURCE_DIRECT = 'DIRECT_PATIENT'
SOURCE_PARTNER = 'PARTNER'

_QUANT = Decimal('0.01')


def _q(value: Decimal) -> Decimal:
    """2-dp rounded half-up — matches the invoicing service convention."""
    return value.quantize(_QUANT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class FinancialReportFilters:
    period_start: date
    period_end: date
    source_type: str
    partner_ids: tuple[str, ...] = ()


def build_financial_report(filters: FinancialReportFilters) -> dict[str, Any]:
    """Return ``{summary, rows, charts, filters_applied}`` for the given
    filters. Designed to be cheap on small/medium periods — ~1 GROUP-BY
    query per chart series, all reading the same indexed columns
    (``confirmed_at``, ``status``, ``source_type``, ``partner_organization``).
    """
    from apps.requests.models import (
        AnalysisRequest,
        AnalysisRequestItem,
        ItemStatus,
        RequestStatus,
        SourceType,
    )

    # ---- base queryset --------------------------------------------------
    # Match invoicing's billing safety: only VALIDATED + COMPLETED workflow
    # states count as billable. CANCELLED is excluded; DRAFT/CONFIRMED/etc.
    # are excluded because there is nothing to bill yet.
    requests_qs = AnalysisRequest.objects.filter(
        status__in=[RequestStatus.VALIDATED, RequestStatus.COMPLETED],
        confirmed_at__date__gte=filters.period_start,
        confirmed_at__date__lte=filters.period_end,
    )

    if filters.source_type == SOURCE_DIRECT:
        requests_qs = requests_qs.filter(source_type=SourceType.DIRECT_PATIENT)
    elif filters.source_type == SOURCE_PARTNER:
        requests_qs = requests_qs.filter(source_type=SourceType.PARTNER_ORGANIZATION)
        if filters.partner_ids:
            requests_qs = requests_qs.filter(
                partner_organization_id__in=filters.partner_ids,
            )

    # ---- per-request rows ----------------------------------------------
    # Single query batch:
    #   1 SELECT for the requests + their patient + partner (select_related)
    #   1 SELECT for all items belonging to those requests, with their
    #     exam_definition pre-joined (prefetch_related + Prefetch).
    # All in-request aggregation (gross, exam_count, exam drill-down)
    # happens in Python from the prefetched data — no per-row queries.
    items_prefetch = Prefetch(
        'items',
        queryset=AnalysisRequestItem.objects
            .exclude(execution_mode='REJECTED')
            .select_related('exam_definition')
            .order_by('exam_definition__code'),
        to_attr='priced_items',
    )
    request_rows_raw = list(
        requests_qs
        .select_related('patient', 'partner_organization')
        .prefetch_related(items_prefetch)
        .order_by('-confirmed_at', 'request_number')
    )

    rows: list[dict[str, Any]] = []
    summary_gross = Decimal('0')
    summary_discount = Decimal('0')
    summary_net = Decimal('0')
    summary_request_count = 0
    summary_exam_count = 0

    for ar in request_rows_raw:
        priced_items = list(getattr(ar, 'priced_items', []) or [])
        gross = sum(
            ((it.billed_price or Decimal('0')) for it in priced_items),
            Decimal('0'),
        )
        discount_rate = (
            ar.partner_organization.invoice_discount_rate
            if ar.partner_organization_id and ar.partner_organization
            else Decimal('0')
        ) or Decimal('0')
        discount = _q(gross * discount_rate / Decimal('100'))
        net = _q(gross - discount)
        gross_q = _q(gross)

        # Drill-down: group items by exam definition so the table can
        # expand into one sub-row per distinct exam (with quantity).
        per_exam: dict[Any, dict[str, Any]] = {}
        for it in priced_items:
            ed = it.exam_definition
            key = ed.id if ed else None
            slot = per_exam.setdefault(key, {
                'code': ed.code if ed else '',
                'name': ed.name if ed else '',
                'quantity': 0,
                'unit_price': it.unit_price or Decimal('0'),
                'gross': Decimal('0'),
            })
            slot['quantity'] += 1
            slot['gross'] += it.billed_price or Decimal('0')
        exams_out: list[dict[str, Any]] = []
        for slot in per_exam.values():
            exam_gross_q = _q(slot['gross'])
            exam_discount_q = _q(slot['gross'] * discount_rate / Decimal('100'))
            exam_net_q = _q(exam_gross_q - exam_discount_q)
            exams_out.append({
                'code':            slot['code'],
                'name':            slot['name'],
                'quantity':        slot['quantity'],
                'unit_price':      str(_q(slot['unit_price'])),
                'gross_amount':    str(exam_gross_q),
                'discount_amount': str(exam_discount_q),
                'net_amount':      str(exam_net_q),
            })

        patient = ar.patient
        rows.append({
            'request_id': str(ar.id),
            'reference': ar.public_reference or ar.request_number,
            'date': (ar.confirmed_at.date().isoformat() if ar.confirmed_at else None),
            'patient_name': f'{patient.last_name}, {patient.first_name}' if patient else '',
            'source_type': ar.source_type,
            'partner_id': (
                str(ar.partner_organization_id) if ar.partner_organization_id else None
            ),
            'partner_name': (
                ar.partner_organization.name if ar.partner_organization_id and ar.partner_organization else ''
            ),
            'exam_count': len(priced_items),
            'gross_amount': str(gross_q),
            'discount_amount': str(discount),
            'net_amount': str(net),
            'exams': exams_out,
        })
        summary_gross += gross_q
        summary_discount += discount
        summary_net += net
        summary_request_count += 1
        summary_exam_count += len(priced_items)

    summary = {
        'request_count': summary_request_count,
        'exam_count': summary_exam_count,
        'gross_total': str(_q(summary_gross)),
        'discount_total': str(_q(summary_discount)),
        'net_total': str(_q(summary_net)),
    }

    charts = _build_charts(filters, requests_qs, rows, request_rows_raw)

    return {
        'summary': summary,
        'rows': rows,
        'charts': charts,
        'filters_applied': {
            'period_start': filters.period_start.isoformat(),
            'period_end': filters.period_end.isoformat(),
            'source_type': filters.source_type,
            'partner_ids': list(filters.partner_ids),
        },
    }


# ---------------------------------------------------------------------------
# Chart series — built from the same filtered queryset as the rows so the
# charts and the table tell the same story.
# ---------------------------------------------------------------------------

def _build_charts(
    filters: FinancialReportFilters,
    requests_qs,
    rows: list[dict[str, Any]],
    request_rows_raw: list,
) -> dict[str, Any]:
    from apps.requests.models import (
        AnalysisRequest,
        AnalysisRequestItem,
        SourceType,
    )

    # 1. Source distribution — DIRECT_PATIENT vs PARTNER, by net revenue.
    # Seed sum() with Decimal('0') so the empty case stays a Decimal —
    # bare sum() of an empty generator yields ``int 0`` and breaks
    # subsequent .quantize() calls.
    direct_net = sum(
        (Decimal(r['net_amount'])
         for r in rows if r['source_type'] == SourceType.DIRECT_PATIENT),
        Decimal('0'),
    )
    partner_net = sum(
        (Decimal(r['net_amount'])
         for r in rows if r['source_type'] == SourceType.PARTNER_ORGANIZATION),
        Decimal('0'),
    )
    source_distribution = [
        {'source': SourceType.DIRECT_PATIENT, 'value': str(_q(direct_net))},
        {'source': SourceType.PARTNER_ORGANIZATION, 'value': str(_q(partner_net))},
    ]

    # 2. Time evolution — daily revenue + request count.
    by_day_revenue: dict[str, Decimal] = defaultdict(lambda: Decimal('0'))
    by_day_requests: dict[str, int] = defaultdict(int)
    for r in rows:
        d = r['date']
        if d is None:
            continue
        by_day_revenue[d] += Decimal(r['net_amount'])
        by_day_requests[d] += 1
    time_evolution = [
        {
            'date': d,
            'revenue': str(_q(by_day_revenue[d])),
            'requests': by_day_requests[d],
        }
        for d in sorted(by_day_revenue.keys())
    ]

    # 3. Top exams — by revenue and by volume. One GROUP BY query at the
    # item level, filtered by the same parent-request criteria.
    exam_qs = AnalysisRequestItem.objects.filter(
        analysis_request__in=requests_qs,
    ).exclude(execution_mode='REJECTED')
    exam_aggregate = list(
        exam_qs
        .values(
            code=F('exam_definition__code'),
            name=F('exam_definition__name'),
        )
        .annotate(
            revenue=Coalesce(
                Sum('billed_price'),
                Value(Decimal('0')),
                output_field=DecimalField(max_digits=14, decimal_places=4),
            ),
            volume=Count('id'),
        )
    )
    top_exams_by_revenue = [
        {'code': r['code'], 'name': r['name'], 'value': str(_q(r['revenue']))}
        for r in sorted(exam_aggregate, key=lambda x: x['revenue'], reverse=True)[:10]
    ]
    top_exams_by_volume = [
        {'code': r['code'], 'name': r['name'], 'value': r['volume']}
        for r in sorted(exam_aggregate, key=lambda x: x['volume'], reverse=True)[:10]
    ]

    # 4. Top partners — only when the filter could legitimately surface
    # multiple partners. Spec rules:
    #   - source = ALL                       → show
    #   - source = PARTNER, no ids           → show (all partners)
    #   - source = PARTNER, multiple ids     → show
    #   - source = DIRECT or single partner  → hide (empty list)
    partner_count = len(filters.partner_ids)
    show_top_partners = (
        filters.source_type == SOURCE_ALL
        or (filters.source_type == SOURCE_PARTNER and partner_count != 1)
    )
    top_partners_by_revenue: list[dict[str, Any]] = []
    if show_top_partners:
        per_partner: dict[Optional[str], dict[str, Any]] = {}
        for r in rows:
            if r['source_type'] != SourceType.PARTNER_ORGANIZATION:
                continue
            key = r['partner_id']
            slot = per_partner.setdefault(
                key,
                {'partner_id': key, 'name': r['partner_name'], 'value': Decimal('0')},
            )
            slot['value'] += Decimal(r['net_amount'])
        ranked = sorted(
            per_partner.values(),
            key=lambda x: x['value'], reverse=True,
        )[:10]
        top_partners_by_revenue = [
            {**s, 'value': str(_q(s['value']))} for s in ranked
        ]

    # 5. Partner time comparison — only with 2+ partners selected (or 2+
    # implied because the source is ALL / unfiltered PARTNER and the
    # ranked list has 2+ entries).
    partner_time_comparison: list[dict[str, Any]] = []
    if show_top_partners and len(top_partners_by_revenue) >= 2 and (
        partner_count >= 2 or partner_count == 0 or filters.source_type == SOURCE_ALL
    ):
        # Build daily series per partner for the partners that made the
        # top-N list. Capped at 5 series so the chart stays readable.
        ranked_ids = [s['partner_id'] for s in top_partners_by_revenue[:5]]
        daily: dict[str, dict[str, Decimal]] = defaultdict(
            lambda: defaultdict(lambda: Decimal('0'))
        )
        # daily[partner_id][day_iso] = revenue
        for r in rows:
            pid = r['partner_id']
            if pid not in ranked_ids or r['date'] is None:
                continue
            daily[pid][r['date']] += Decimal(r['net_amount'])
        all_days = sorted({
            d for series in daily.values() for d in series.keys()
        })
        for s in top_partners_by_revenue[:5]:
            pid = s['partner_id']
            partner_time_comparison.append({
                'partner_id': pid,
                'name': s['name'],
                'series': [
                    {'date': d, 'value': str(_q(daily[pid][d]))}
                    for d in all_days
                ],
            })
        # Suppress comparison if a single partner was explicitly selected
        # — the spec wants this chart only when comparing 2+ partners.
        if partner_count == 1:
            partner_time_comparison = []

    return {
        'source_distribution': source_distribution,
        'time_evolution': time_evolution,
        'top_exams_by_revenue': top_exams_by_revenue,
        'top_exams_by_volume': top_exams_by_volume,
        'top_partners_by_revenue': top_partners_by_revenue,
        'partner_time_comparison': partner_time_comparison,
    }
