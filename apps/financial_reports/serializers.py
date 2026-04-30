from rest_framework import serializers

from .services import SOURCE_ALL, SOURCE_DIRECT, SOURCE_PARTNER


class FinancialReportFiltersSerializer(serializers.Serializer):
    """Input validator for the preview + export endpoints. Same payload
    shape across both — the export endpoint just runs the preview builder
    and pipes the result through the PDF renderer."""
    period_start = serializers.DateField()
    period_end = serializers.DateField()
    source_type = serializers.ChoiceField(
        choices=[SOURCE_ALL, SOURCE_DIRECT, SOURCE_PARTNER],
    )
    partner_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        default=list,
    )

    def validate(self, attrs):
        if attrs['period_end'] < attrs['period_start']:
            raise serializers.ValidationError({
                'period_end': 'Period end must be on or after period start.',
            })
        # partner_ids is only meaningful when source_type=PARTNER. Drop
        # spurious values silently rather than rejecting — the frontend may
        # leave the array populated when the user toggles between source
        # types, and the backend should not punish UI bookkeeping.
        if attrs['source_type'] != SOURCE_PARTNER:
            attrs['partner_ids'] = []
        return attrs
