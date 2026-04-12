"""
Cytova — Request Pricing Resolver

Central, single-source-of-truth pricing resolution for the 3-step analysis
request creation workflow.

Business rules (authoritative — the Step 3 recap, the final create path,
and any future surface that needs resolved pricing all call this module):

    source_type = DIRECT_PATIENT
        billed_price = ExamDefinition.unit_price

    source_type = PARTNER_ORGANIZATION
        if an active PartnerExamPrice exists for (partner, exam_definition):
            billed_price = PartnerExamPrice.agreed_price
        else:
            billed_price = ExamDefinition.unit_price

``unit_price`` is always snapshotted from the current reference regardless
of source type — it is the "catalog value at this moment in time".

Historical integrity
--------------------
This resolver only reads. It never writes. When the final create path
persists request items, those rows snapshot the resolved ``unit_price`` and
``billed_price`` into their own columns — so later edits to
``ExamDefinition.unit_price`` or ``PartnerExamPrice.agreed_price`` cannot
retroactively mutate any already-created request. The guarantee lives in
the request data model; this module's only contribution is to make sure
the snapshot is correct at the moment of persistence.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional
from uuid import UUID

from apps.catalog.models import ExamDefinition
from apps.partners.models import PartnerExamPrice, PartnerOrganization

from .models import PriceSource, SourceType


@dataclass(frozen=True)
class ResolvedItemPrice:
    """One resolved pricing line — keyed by exam_definition_id."""
    exam_definition_id: UUID
    exam_code: str
    exam_name: str
    unit_price: Decimal
    billed_price: Decimal
    price_source: str  # one of PriceSource.*


class RequestPricingResolver:
    """
    Stateless resolver. Every callable on this class takes explicit inputs
    and returns explicit outputs — nothing is stored on ``self``, so the
    resolver is trivial to test and safe to call from any thread or
    transaction context.
    """

    @staticmethod
    def resolve(
        source_type: str,
        partner: Optional[PartnerOrganization],
        exams: Iterable[ExamDefinition],
    ) -> list[ResolvedItemPrice]:
        """
        Resolve pricing for every exam in ``exams`` against the given source.

        ``exams`` is an iterable of already-loaded ``ExamDefinition`` rows —
        the resolver does NOT query by id, so the caller controls the query
        batching. This keeps the resolver transparent (no hidden SELECTs)
        and lets the caller do ``select_related`` or other optimisations
        upstream if needed.

        PartnerExamPrice lookups are done as a single bulk query indexed
        by exam_definition_id, avoiding an N+1 when a request has many
        exams.
        """
        exam_list = list(exams)
        if not exam_list:
            return []

        # Bulk-fetch active agreed prices for (partner, exam) pairs in a
        # single query. If the source is DIRECT_PATIENT this is skipped —
        # agreed prices are not consulted at all for direct flows.
        agreed_by_exam: dict[UUID, Decimal] = {}
        if source_type == SourceType.PARTNER_ORGANIZATION and partner is not None:
            exam_ids = [e.id for e in exam_list]
            rows = PartnerExamPrice.objects.filter(
                partner=partner,
                exam_definition_id__in=exam_ids,
                is_active=True,
            ).values_list('exam_definition_id', 'agreed_price')
            agreed_by_exam = dict(rows)

        resolved: list[ResolvedItemPrice] = []
        for exam in exam_list:
            unit_price = exam.unit_price
            agreed = agreed_by_exam.get(exam.id)
            if agreed is not None:
                billed = agreed
                source = PriceSource.PARTNER_AGREED_PRICE
            else:
                billed = unit_price
                source = PriceSource.DEFAULT_PRICE

            resolved.append(ResolvedItemPrice(
                exam_definition_id=exam.id,
                exam_code=exam.code,
                exam_name=exam.name,
                unit_price=unit_price,
                billed_price=billed,
                price_source=source,
            ))

        return resolved

    @staticmethod
    def resolve_for_ids(
        source_type: str,
        partner: Optional[PartnerOrganization],
        exam_ids: Iterable[UUID],
    ) -> list[ResolvedItemPrice]:
        """
        Convenience wrapper that resolves by exam_definition_id — used by
        the preview endpoint where the client supplies raw ids. Preserves
        the order of ``exam_ids`` in the output so the Step 3 recap can
        render the list in the same order the user assembled it.
        """
        ids_list = list(exam_ids)
        if not ids_list:
            return []
        exams = {
            e.id: e for e in ExamDefinition.objects.filter(id__in=ids_list, is_active=True)
        }
        ordered = [exams[i] for i in ids_list if i in exams]
        return RequestPricingResolver.resolve(source_type, partner, ordered)
