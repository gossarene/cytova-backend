"""DRF input validator for the exams-by-partner endpoints."""
from __future__ import annotations

from rest_framework import serializers

from .services import (
    DEFAULT_PERFORMED_REQUEST_STATUSES, EXAM_PROGRESS_CHOICES,
    EXAM_PROGRESS_PERFORMED, ExamsByPartnerFilters,
)


class ExamsByPartnerFiltersSerializer(serializers.Serializer):
    """Filter payload for both preview + export endpoints.

    Empty optional lists are accepted and treated as "no constraint
    on that axis". The serializer normalises everything to tuples
    before handing off to the service so the dataclass stays
    immutable.

    Request-status default policy
    -----------------------------
    The legacy default for ``request_statuses`` is the "performed"
    set (``VALIDATED`` / ``COMPLETED`` / ``RESULT_ISSUED``). That's
    correct when ``exam_progress_status`` is PERFORMED (the default)
    but actively wrong for IN_PROGRESS / REJECTED / ALL — insisting
    on VALIDATED parents would zero out items still in analysis.

    Resolution: when ``request_statuses`` is NOT supplied by the
    caller AND ``exam_progress_status`` is non-PERFORMED, the
    request-status filter is left empty (no constraint). Explicit
    callers who pass ``request_statuses=[...]`` always win.
    """
    period_start = serializers.DateField()
    period_end = serializers.DateField()
    partner_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, default=list,
    )
    exam_family_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, default=list,
    )
    exam_definition_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, default=list,
    )
    request_statuses = serializers.ListField(
        child=serializers.CharField(max_length=40),
        required=False,
        default=list,
    )
    item_statuses = serializers.ListField(
        child=serializers.CharField(max_length=40),
        required=False,
        default=list,
    )
    exam_progress_status = serializers.ChoiceField(
        choices=EXAM_PROGRESS_CHOICES,
        required=False,
        default=EXAM_PROGRESS_PERFORMED,
    )
    include_direct = serializers.BooleanField(required=False, default=True)
    include_amount = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        if attrs['period_end'] < attrs['period_start']:
            raise serializers.ValidationError({
                'period_end': 'Period end must be on or after period start.',
            })
        return attrs

    def to_filters(self) -> ExamsByPartnerFilters:
        data = self.validated_data
        # Detect whether the caller actually supplied request_statuses.
        # ``initial_data`` reflects the raw POST body — if the key is
        # absent we apply the PERFORMED-aware default; otherwise we
        # honour the caller's explicit choice (including an empty
        # list, which means "no parent-status constraint").
        request_statuses_supplied = (
            'request_statuses' in (self.initial_data or {})
        )
        progress = data['exam_progress_status']
        if request_statuses_supplied:
            request_statuses = tuple(data['request_statuses'])
        elif progress == EXAM_PROGRESS_PERFORMED:
            request_statuses = DEFAULT_PERFORMED_REQUEST_STATUSES
        else:
            # Non-PERFORMED group with no explicit caller value —
            # drop the parent-status constraint so the item-level
            # filter is the authority.
            request_statuses = ()

        return ExamsByPartnerFilters(
            period_start=data['period_start'],
            period_end=data['period_end'],
            partner_ids=tuple(str(p) for p in data['partner_ids']),
            exam_family_ids=tuple(str(p) for p in data['exam_family_ids']),
            exam_definition_ids=tuple(str(p) for p in data['exam_definition_ids']),
            request_statuses=request_statuses,
            item_statuses=tuple(data['item_statuses']),
            exam_progress_status=progress,
            include_direct=data['include_direct'],
            include_amount=data['include_amount'],
        )
