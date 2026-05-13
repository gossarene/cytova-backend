"""
Cytova — Exams-by-Partner pivot report composer.

Answers "how many exams did each partner produce for us during
the selected period?" as a pivot:

  rows    = exam family + exam (deterministic order)
  columns = partners (sorted by name)
  cells   = COUNT(AnalysisRequestItem) matching the filters

The primary metric is exam count. Monetary columns are an OPT-IN
add-on (``include_amount=True``) so the report can also surface
revenue / billed totals when an operator asks for it — without
fighting the pivot for primacy.

Implementation notes
--------------------
- One GROUP BY query at the item layer; everything else is
  in-memory pivot assembly.
- Items in ``execution_mode='REJECTED'`` are excluded — a
  rejected request item never "happened" operationally.
- Direct-patient items have ``partner_id=None`` on the parent
  request. The default behaviour groups them under a synthetic
  "Direct (no partner)" column. The filter can opt them out via
  ``include_direct=False`` (the API surfaces a dedicated flag
  rather than overloading ``partner_ids``).
- Multi-tenant isolation is enforced by the active tenant schema
  (the middleware swaps ``search_path`` per request). No explicit
  ``tenant_id`` filter is needed at this layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from django.db.models import Count, DecimalField, F, Sum, Value
from django.db.models.functions import Coalesce


# Synthetic "no partner" column key. Kept outside the UUID space so
# the JSON cell map distinguishes it from a real partner UUID. The
# frontend treats this key like any other column — only the label
# is special.
DIRECT_PARTNER_KEY = '__direct__'
DIRECT_PARTNER_LABEL = 'Direct (no partner)'

# Default operational statuses that count as "exam performed".
# VALIDATED / COMPLETED / RESULT_ISSUED all imply the lab did the
# work. Items with parents still in DRAFT / CONFIRMED / ANALYSIS
# haven't produced a result yet, so they're excluded by default —
# the caller can override via ``request_statuses``.
DEFAULT_PERFORMED_REQUEST_STATUSES = ('VALIDATED', 'COMPLETED', 'RESULT_ISSUED')


# ---------------------------------------------------------------------------
# Exam-level progress groups
# ---------------------------------------------------------------------------
#
# A single ``AnalysisRequest`` can carry items in different
# operational states (e.g. one item validated, another still in
# analysis, a third rejected for sample quality). The pivot answers
# "how many exams are in <state>" — so the grouping MUST happen at
# the item level, not at the parent request level.
#
# Mapping rationale (matches ``apps.requests.models.ItemStatus``):
#
#   PERFORMED    — the lab produced a clinical result for this item.
#                  VALIDATED + COMPLETED both pass the validation
#                  gate. ``ExecutionMode.REJECTED`` is excluded
#                  because a rejected item never produced a result
#                  even when its status field happens to read e.g.
#                  ``VALIDATED`` on a stale draft.
#
#   IN_PROGRESS  — the lab is still working on this item; no
#                  validated result yet. PENDING, COLLECTED,
#                  RESULT_ENTERED, UNDER_REVIEW, and the legacy
#                  IN_PROGRESS state all qualify. Excludes rejected
#                  items (they belong to the REJECTED group).
#
#   REJECTED     — the item was refused operationally. Two signals
#                  collapse into this group: ``ItemStatus.REJECTED``
#                  (workflow rejection) AND ``ExecutionMode.REJECTED``
#                  (line-level rejection on a confirmed request).
#                  Either condition is enough; both can hold on the
#                  same row.
#
#   ALL          — no item-level filter; every group is counted.

EXAM_PROGRESS_ALL = 'ALL'
EXAM_PROGRESS_PERFORMED = 'PERFORMED'
EXAM_PROGRESS_IN_PROGRESS = 'IN_PROGRESS'
EXAM_PROGRESS_REJECTED = 'REJECTED'

EXAM_PROGRESS_CHOICES = (
    EXAM_PROGRESS_ALL,
    EXAM_PROGRESS_PERFORMED,
    EXAM_PROGRESS_IN_PROGRESS,
    EXAM_PROGRESS_REJECTED,
)

# ``ItemStatus`` values per group, named here so callers + tests
# share the same source of truth. A future enum addition needs to
# be explicitly mapped — and the serializer's choice list keeps
# the surface narrow regardless.
_PERFORMED_ITEM_STATUSES = ('VALIDATED', 'COMPLETED')
_IN_PROGRESS_ITEM_STATUSES = (
    'PENDING', 'COLLECTED', 'RESULT_ENTERED', 'UNDER_REVIEW', 'IN_PROGRESS',
)


_QUANT = Decimal('0.01')


def _q(value: Decimal) -> Decimal:
    return value.quantize(_QUANT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class ExamsByPartnerFilters:
    """All filter axes for the pivot.

    Empty tuples mean "no constraint on that axis" — passing
    ``partner_ids=()`` returns every partner that produced an item
    in the period (plus the synthetic Direct column unless
    ``include_direct`` is False).

    ``exam_progress_status`` (one of ``EXAM_PROGRESS_CHOICES``)
    is the primary item-level filter — it groups items by what
    state THEY (not their parent request) are in. A single
    request with both validated and rejected items is counted
    in PERFORMED for the validated lines and in REJECTED for
    the rejected lines.

    ``request_statuses`` is a SEPARATE optional filter on the
    parent request. It defaults to the legacy performed set
    (``DEFAULT_PERFORMED_REQUEST_STATUSES``) so existing callers
    that don't know about ``exam_progress_status`` keep getting
    the prior numbers — but the serializer drops this default
    automatically when the caller picks a non-PERFORMED exam
    progress group, since insisting on VALIDATED parents would
    otherwise zero out the IN_PROGRESS column.

    ``item_statuses`` is a free-form narrow filter on
    ``ItemStatus`` values — used to drill into a single state
    inside a group (e.g. only ``UNDER_REVIEW`` items among the
    IN_PROGRESS set).
    """
    period_start: date
    period_end: date
    partner_ids: tuple[str, ...] = ()
    exam_family_ids: tuple[str, ...] = ()
    exam_definition_ids: tuple[str, ...] = ()
    request_statuses: tuple[str, ...] = DEFAULT_PERFORMED_REQUEST_STATUSES
    item_statuses: tuple[str, ...] = ()
    exam_progress_status: str = EXAM_PROGRESS_PERFORMED
    include_direct: bool = True
    include_amount: bool = False


def build_exams_by_partner_report(
    filters: ExamsByPartnerFilters,
) -> dict[str, Any]:
    """Return the pivot payload.

    Shape::

        {
          'partners': [
            { 'id': '<uuid|__direct__>', 'name': 'SERENA' },
            ...
          ],
          'rows': [
            {
              'exam_family_id':   '<uuid|null>',
              'exam_family_name': 'Hematology',
              'exam_id':          '<uuid>',
              'exam_code':        'NFS',
              'exam_name':        'Numération Formule Sanguine',
              'counts':           { '<partner_id>': 133, ... },
              'amounts':          { '<partner_id>': '12345.67' }  # only if
                                                                  # include_amount
              'total':            133,
              'total_amount':     '12345.67'                       # only if
                                                                   # include_amount
            }
          ],
          'subtotals': {
            '<family_id|null>': {
              'family_name': 'Hematology',
              'counts': {...},
              'total': N,
              'amounts': {...},   # only if include_amount
              'total_amount': '...'
            }
          },
          'grand_total': {
            'counts': {...},
            'total': N,
            'amounts': {...},   # only if include_amount
            'total_amount': '...'
          },
          'filters_applied': { ... }
        }
    """
    from django.db.models import Q
    from apps.requests.models import AnalysisRequestItem

    # ---- Base queryset --------------------------------------------------
    # All filtering is on indexed columns: ``analysis_request__status``,
    # ``analysis_request__confirmed_at``, ``analysis_request__partner_organization``,
    # ``exam_definition``, ``exam_definition__family``, ``execution_mode``,
    # ``status``.
    #
    # NOTE: the unconditional ``.exclude(execution_mode='REJECTED')``
    # that used to live here is GONE — the REJECTED group MUST surface
    # those rows. The exam-progress group filter below is now the
    # authority on whether REJECTED-mode rows are included.
    qs = AnalysisRequestItem.objects.filter(
        analysis_request__confirmed_at__date__gte=filters.period_start,
        analysis_request__confirmed_at__date__lte=filters.period_end,
    )

    # ---- Exam-level progress group -------------------------------------
    # A request can carry items in different states; the count band
    # is driven by EACH item's own status + execution mode, not by
    # the parent request's status.
    progress = filters.exam_progress_status
    if progress == EXAM_PROGRESS_PERFORMED:
        qs = qs.filter(status__in=_PERFORMED_ITEM_STATUSES).exclude(
            execution_mode='REJECTED',
        )
    elif progress == EXAM_PROGRESS_IN_PROGRESS:
        qs = qs.filter(status__in=_IN_PROGRESS_ITEM_STATUSES).exclude(
            execution_mode='REJECTED',
        )
    elif progress == EXAM_PROGRESS_REJECTED:
        # Either signal counts as a rejection — workflow-status
        # rejection OR line-level execution-mode rejection.
        qs = qs.filter(
            Q(status='REJECTED') | Q(execution_mode='REJECTED'),
        )
    # EXAM_PROGRESS_ALL → no item-level filter at all (rejected
    # items included). Callers who still want them excluded can
    # pass ``exam_progress_status=PERFORMED`` or
    # ``item_statuses=('VALIDATED', ...)``.

    if filters.request_statuses:
        qs = qs.filter(analysis_request__status__in=filters.request_statuses)
    if filters.item_statuses:
        qs = qs.filter(status__in=filters.item_statuses)
    if filters.exam_family_ids:
        qs = qs.filter(
            exam_definition__family_id__in=filters.exam_family_ids,
        )
    if filters.exam_definition_ids:
        qs = qs.filter(exam_definition_id__in=filters.exam_definition_ids)

    # Partner filter — when ``partner_ids`` is non-empty the caller
    # explicitly wants those columns only. ``include_direct`` adds
    # the synthetic "no partner" column on top of the explicit list.
    if filters.partner_ids:
        from django.db.models import Q
        partner_filter = Q(
            analysis_request__partner_organization_id__in=filters.partner_ids,
        )
        if filters.include_direct:
            partner_filter |= Q(analysis_request__partner_organization_id__isnull=True)
        qs = qs.filter(partner_filter)
    elif not filters.include_direct:
        # Caller wants all partners EXCEPT the direct column.
        qs = qs.filter(
            analysis_request__partner_organization_id__isnull=False,
        )

    # ---- One GROUP BY query at the (exam, partner) grain ---------------
    aggregate = (
        qs.values(
            family_id=F('exam_definition__family_id'),
            family_name=F('exam_definition__family__name'),
            family_order=F('exam_definition__family__display_order'),
            exam_id=F('exam_definition_id'),
            exam_code=F('exam_definition__code'),
            exam_name=F('exam_definition__name'),
            partner_id=F('analysis_request__partner_organization_id'),
            partner_name=F('analysis_request__partner_organization__name'),
        )
        .annotate(
            count=Count('id'),
            amount=Coalesce(
                Sum('billed_price'),
                Value(Decimal('0')),
                output_field=DecimalField(max_digits=14, decimal_places=4),
            ),
        )
    )

    # ---- In-memory pivot assembly --------------------------------------
    # Columns: partner_id → display name. ``None`` partner id becomes
    # the synthetic DIRECT key.
    partner_labels: dict[str, str] = {}
    rows_by_exam: dict[Any, dict[str, Any]] = {}
    family_meta: dict[Any, dict[str, Any]] = {}

    for r in aggregate:
        pid_raw = r['partner_id']
        if pid_raw is None:
            pid = DIRECT_PARTNER_KEY
            pname = DIRECT_PARTNER_LABEL
        else:
            pid = str(pid_raw)
            pname = r['partner_name'] or '(unnamed partner)'
        partner_labels[pid] = pname

        # Family bucket (None for exams without a family — keep them
        # under an "Uncategorised" slot so they still surface).
        fid = str(r['family_id']) if r['family_id'] else ''
        fname = r['family_name'] or 'Uncategorised'
        family_meta.setdefault(fid, {
            'family_id': fid or None,
            'family_name': fname,
            'family_order': r['family_order'] if r['family_order'] is not None else 9999,
        })

        # Exam row keyed by exam_id.
        exam_key = str(r['exam_id'])
        row = rows_by_exam.setdefault(exam_key, {
            'exam_family_id': fid or None,
            'exam_family_name': fname,
            'family_order': family_meta[fid]['family_order'],
            'exam_id': exam_key,
            'exam_code': r['exam_code'],
            'exam_name': r['exam_name'],
            'counts': {},
            'amounts': {} if filters.include_amount else None,
            'total': 0,
            'total_amount': Decimal('0') if filters.include_amount else None,
        })
        row['counts'][pid] = row['counts'].get(pid, 0) + r['count']
        row['total'] += r['count']
        if filters.include_amount:
            amt = r['amount'] or Decimal('0')
            row['amounts'][pid] = (
                (row['amounts'].get(pid) or Decimal('0')) + amt
            )
            row['total_amount'] = (row['total_amount'] or Decimal('0')) + amt

    # ---- Order partners deterministically: name asc, with DIRECT last
    # so the "Direct" column doesn't push real partners to the right
    # edge of the screen.
    real_partners = sorted(
        (pid for pid in partner_labels if pid != DIRECT_PARTNER_KEY),
        key=lambda pid: partner_labels[pid].lower(),
    )
    ordered_partner_ids: list[str] = list(real_partners)
    if DIRECT_PARTNER_KEY in partner_labels:
        ordered_partner_ids.append(DIRECT_PARTNER_KEY)

    partners = [
        {'id': pid, 'name': partner_labels[pid]}
        for pid in ordered_partner_ids
    ]

    # ---- Order rows: family.display_order, family.name, exam.code.
    rows = sorted(
        rows_by_exam.values(),
        key=lambda r: (
            r['family_order'],
            r['exam_family_name'].lower(),
            (r['exam_code'] or '').lower(),
        ),
    )

    # ---- Subtotals per family + grand total in a single pass.
    subtotals: dict[str, dict[str, Any]] = {}
    grand_counts: dict[str, int] = {}
    grand_amounts: dict[str, Decimal] = {}
    grand_total = 0
    grand_total_amount = Decimal('0')

    for row in rows:
        fid_key = row['exam_family_id'] or ''
        sub = subtotals.setdefault(fid_key, {
            'family_id': row['exam_family_id'],
            'family_name': row['exam_family_name'],
            'counts': {},
            'amounts': {} if filters.include_amount else None,
            'total': 0,
            'total_amount': Decimal('0') if filters.include_amount else None,
        })
        for pid, c in row['counts'].items():
            sub['counts'][pid] = sub['counts'].get(pid, 0) + c
            grand_counts[pid] = grand_counts.get(pid, 0) + c
        sub['total'] += row['total']
        grand_total += row['total']

        if filters.include_amount:
            for pid, amt in (row['amounts'] or {}).items():
                sub['amounts'][pid] = (
                    (sub['amounts'].get(pid) or Decimal('0')) + amt
                )
                grand_amounts[pid] = grand_amounts.get(pid, Decimal('0')) + amt
            sub['total_amount'] = (sub['total_amount'] or Decimal('0')) + (
                row['total_amount'] or Decimal('0')
            )
            grand_total_amount += row['total_amount'] or Decimal('0')

    # ---- Serialise Decimal → string for JSON safety + UX consistency
    # (frontend rendering applies its own locale-aware formatting).
    def _amt_str(d):
        return str(_q(Decimal(d)))

    serialised_rows = []
    for row in rows:
        out = {
            'exam_family_id': row['exam_family_id'],
            'exam_family_name': row['exam_family_name'],
            'exam_id': row['exam_id'],
            'exam_code': row['exam_code'],
            'exam_name': row['exam_name'],
            'counts': dict(row['counts']),
            'total': row['total'],
        }
        if filters.include_amount:
            out['amounts'] = {
                pid: _amt_str(amt) for pid, amt in (row['amounts'] or {}).items()
            }
            out['total_amount'] = _amt_str(row['total_amount'])
        serialised_rows.append(out)

    serialised_subtotals = {}
    for fid_key, sub in subtotals.items():
        s = {
            'family_id': sub['family_id'],
            'family_name': sub['family_name'],
            'counts': dict(sub['counts']),
            'total': sub['total'],
        }
        if filters.include_amount:
            s['amounts'] = {
                pid: _amt_str(amt) for pid, amt in (sub['amounts'] or {}).items()
            }
            s['total_amount'] = _amt_str(sub['total_amount'])
        # The dict key is the family-id-or-empty-string; expose as a
        # JSON-friendly value here (UUID or null).
        serialised_subtotals[fid_key or '__none__'] = s

    grand_total_payload: dict[str, Any] = {
        'counts': grand_counts,
        'total': grand_total,
    }
    if filters.include_amount:
        grand_total_payload['amounts'] = {
            pid: _amt_str(amt) for pid, amt in grand_amounts.items()
        }
        grand_total_payload['total_amount'] = _amt_str(grand_total_amount)

    return {
        'partners': partners,
        'rows': serialised_rows,
        'subtotals': serialised_subtotals,
        'grand_total': grand_total_payload,
        'filters_applied': {
            'period_start': filters.period_start.isoformat(),
            'period_end': filters.period_end.isoformat(),
            'partner_ids': list(filters.partner_ids),
            'exam_family_ids': list(filters.exam_family_ids),
            'exam_definition_ids': list(filters.exam_definition_ids),
            'request_statuses': list(filters.request_statuses),
            'item_statuses': list(filters.item_statuses),
            'exam_progress_status': filters.exam_progress_status,
            'include_direct': filters.include_direct,
            'include_amount': filters.include_amount,
        },
    }
