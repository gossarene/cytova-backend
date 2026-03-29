"""
Cytova — Analysis Request Serializers
"""
from rest_framework import serializers

from apps.patients.models import Patient
from apps.catalog.models import ExamDefinition
from apps.partners.models import PartnerOrganization
from .models import (
    AnalysisRequest, AnalysisRequestItem, ExamTraceability,
    RequestStatus, ItemStatus, ExecutionMode, SourceType, BillingMode, PriceSource,
)


# ---------------------------------------------------------------------------
# Traceability
# ---------------------------------------------------------------------------

class ExamTraceabilitySerializer(serializers.ModelSerializer):
    sample_received_by = serializers.SerializerMethodField()
    performed_by = serializers.SerializerMethodField()

    class Meta:
        model = ExamTraceability
        fields = [
            'sample_received_at', 'sample_received_by',
            'analysis_completed_at', 'performed_by',
        ]

    def _staff_brief(self, obj):
        if obj is None:
            return None
        return {'id': str(obj.id), 'email': obj.email}

    def get_sample_received_by(self, obj):
        return self._staff_brief(obj.sample_received_by)

    def get_performed_by(self, obj):
        return self._staff_brief(obj.performed_by)


# ---------------------------------------------------------------------------
# AnalysisRequestItem — read
# ---------------------------------------------------------------------------

class AnalysisRequestItemBriefSerializer(serializers.ModelSerializer):
    """Compact representation embedded in AnalysisRequestDetailSerializer."""
    exam_code = serializers.CharField(source='exam_definition.code', read_only=True)
    exam_name = serializers.CharField(source='exam_definition.name', read_only=True)
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=4, coerce_to_string=True, read_only=True,
    )
    billed_price = serializers.DecimalField(
        max_digits=12, decimal_places=4, coerce_to_string=True, read_only=True,
    )

    class Meta:
        model = AnalysisRequestItem
        fields = [
            'id', 'exam_definition_id', 'exam_code', 'exam_name',
            'status', 'execution_mode', 'rejection_reason',
            'external_partner_name', 'notes',
            'unit_price', 'billed_price', 'price_source',
            'created_at',
        ]


class AnalysisRequestItemSerializer(serializers.ModelSerializer):
    """Full item representation including traceability."""
    exam_code = serializers.CharField(source='exam_definition.code', read_only=True)
    exam_name = serializers.CharField(source='exam_definition.name', read_only=True)
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=4, coerce_to_string=True, read_only=True,
    )
    billed_price = serializers.DecimalField(
        max_digits=12, decimal_places=4, coerce_to_string=True, read_only=True,
    )
    traceability = ExamTraceabilitySerializer(read_only=True)

    class Meta:
        model = AnalysisRequestItem
        fields = [
            'id', 'analysis_request_id',
            'exam_definition_id', 'exam_code', 'exam_name',
            'status', 'execution_mode', 'rejection_reason',
            'external_partner_name', 'notes',
            'unit_price', 'billed_price', 'price_source', 'pricing_rule_id',
            'traceability',
            'created_at', 'updated_at',
        ]


# ---------------------------------------------------------------------------
# AnalysisRequest — read
# ---------------------------------------------------------------------------

class AnalysisRequestListSerializer(serializers.ModelSerializer):
    patient_name = serializers.SerializerMethodField()
    items_count = serializers.SerializerMethodField()
    created_by_email = serializers.CharField(
        source='created_by.email', read_only=True, default=None,
    )
    partner_organization_name = serializers.CharField(
        source='partner_organization.name', read_only=True, default=None,
    )

    class Meta:
        model = AnalysisRequest
        fields = [
            'id', 'request_number', 'patient_id', 'patient_name',
            'status', 'source_type', 'billing_mode',
            'partner_organization_id', 'partner_organization_name',
            'items_count', 'created_by_email', 'created_at',
        ]

    def get_patient_name(self, obj):
        if obj.patient_id:
            try:
                return obj.patient.full_name
            except Exception:
                return None
        return None

    def get_items_count(self, obj):
        # Relies on prefetch_related('items') on the queryset for efficiency
        return obj.items.count()


class AnalysisRequestDetailSerializer(serializers.ModelSerializer):
    items = AnalysisRequestItemBriefSerializer(many=True, read_only=True)
    confirmed_by_email = serializers.CharField(
        source='confirmed_by.email', read_only=True, default=None,
    )
    cancelled_by_email = serializers.CharField(
        source='cancelled_by.email', read_only=True, default=None,
    )
    created_by_email = serializers.CharField(
        source='created_by.email', read_only=True, default=None,
    )
    partner_organization_name = serializers.CharField(
        source='partner_organization.name', read_only=True, default=None,
    )
    partner_organization_code = serializers.CharField(
        source='partner_organization.code', read_only=True, default=None,
    )

    class Meta:
        model = AnalysisRequest
        fields = [
            'id', 'request_number', 'patient_id',
            'status', 'notes',
            'source_type', 'billing_mode',
            'partner_organization_id', 'partner_organization_name',
            'partner_organization_code', 'external_reference', 'source_notes',
            'confirmed_at', 'confirmed_by_email',
            'cancelled_at', 'cancelled_by_email',
            'created_by_email',
            'items',
            'created_at', 'updated_at',
        ]


# ---------------------------------------------------------------------------
# AnalysisRequestItem — write
# ---------------------------------------------------------------------------

class AnalysisRequestItemCreateSerializer(serializers.Serializer):
    exam_definition_id = serializers.UUIDField()
    execution_mode = serializers.ChoiceField(
        choices=ExecutionMode.choices,
        default=ExecutionMode.INTERNAL,
    )
    rejection_reason = serializers.CharField(
        required=False, allow_blank=True, default='',
    )
    external_partner_name = serializers.CharField(
        required=False, allow_blank=True, default='',
    )
    notes = serializers.CharField(required=False, allow_blank=True, default='')
    billed_price = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=0,
        required=False, allow_null=True, default=None,
        help_text='Manual billed price override. Leave null to use auto-resolved price.',
    )

    def validate_exam_definition_id(self, value):
        if not ExamDefinition.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError(
                'Exam definition not found or inactive.'
            )
        return value


class AnalysisRequestItemUpdateSerializer(serializers.Serializer):
    """Operational metadata + billed_price can be updated while request is DRAFT."""
    execution_mode = serializers.ChoiceField(
        choices=ExecutionMode.choices, required=False,
    )
    rejection_reason = serializers.CharField(
        required=False, allow_blank=True,
    )
    external_partner_name = serializers.CharField(
        required=False, allow_blank=True,
    )
    notes = serializers.CharField(required=False, allow_blank=True)
    billed_price = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=0,
        required=False, allow_null=True,
        help_text='Manual billed price override. Set null to re-resolve from rules.',
    )


class ItemRejectSerializer(serializers.Serializer):
    rejection_reason = serializers.CharField(min_length=1)


# ---------------------------------------------------------------------------
# AnalysisRequest — write
# ---------------------------------------------------------------------------

class AnalysisRequestCreateSerializer(serializers.Serializer):
    patient_id = serializers.UUIDField()
    notes = serializers.CharField(required=False, allow_blank=True, default='')
    items = AnalysisRequestItemCreateSerializer(many=True, required=False, default=list)

    # Source tracking
    source_type = serializers.ChoiceField(
        choices=SourceType.choices, default=SourceType.DIRECT_PATIENT,
    )
    partner_organization_id = serializers.UUIDField(
        required=False, allow_null=True, default=None,
    )
    external_reference = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default='',
    )
    billing_mode = serializers.ChoiceField(
        choices=BillingMode.choices, default=BillingMode.DIRECT_PAYMENT,
    )
    source_notes = serializers.CharField(
        required=False, allow_blank=True, default='',
    )

    def validate_patient_id(self, value):
        if not Patient.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError('Patient not found or inactive.')
        return value

    def validate_partner_organization_id(self, value):
        if value is not None:
            if not PartnerOrganization.objects.filter(id=value, is_active=True).exists():
                raise serializers.ValidationError(
                    'Partner organization not found or inactive.'
                )
        return value

    def validate_items(self, value):
        exam_ids = [item['exam_definition_id'] for item in value]
        if len(exam_ids) != len(set(str(e) for e in exam_ids)):
            raise serializers.ValidationError(
                'Duplicate exam definitions are not allowed in a single request.'
            )
        return value

    def validate(self, attrs):
        source_type = attrs.get('source_type', SourceType.DIRECT_PATIENT)
        partner_id = attrs.get('partner_organization_id')
        billing_mode = attrs.get('billing_mode', BillingMode.DIRECT_PAYMENT)

        if source_type == SourceType.DIRECT_PATIENT:
            if partner_id is not None:
                raise serializers.ValidationError({
                    'partner_organization_id': (
                        'Must be null when source_type is DIRECT_PATIENT.'
                    ),
                })
            if billing_mode == BillingMode.PARTNER_BILLING:
                raise serializers.ValidationError({
                    'billing_mode': (
                        'PARTNER_BILLING is not valid for DIRECT_PATIENT requests.'
                    ),
                })

        if source_type == SourceType.PARTNER_ORGANIZATION:
            if partner_id is None:
                raise serializers.ValidationError({
                    'partner_organization_id': (
                        'Required when source_type is PARTNER_ORGANIZATION.'
                    ),
                })

        return attrs


class AnalysisRequestUpdateSerializer(serializers.Serializer):
    """
    Updatable fields on a DRAFT request.
    Source fields can be changed while DRAFT; item list is managed separately.
    """
    notes = serializers.CharField(required=False, allow_blank=True)
    source_type = serializers.ChoiceField(
        choices=SourceType.choices, required=False,
    )
    partner_organization_id = serializers.UUIDField(
        required=False, allow_null=True,
    )
    external_reference = serializers.CharField(
        max_length=100, required=False, allow_blank=True,
    )
    billing_mode = serializers.ChoiceField(
        choices=BillingMode.choices, required=False,
    )
    source_notes = serializers.CharField(required=False, allow_blank=True)

    def validate_partner_organization_id(self, value):
        if value is not None:
            if not PartnerOrganization.objects.filter(id=value, is_active=True).exists():
                raise serializers.ValidationError(
                    'Partner organization not found or inactive.'
                )
        return value

    def validate(self, attrs):
        # Cross-field validation needs the instance context to fill defaults
        instance = self.context.get('instance')
        source_type = attrs.get(
            'source_type',
            getattr(instance, 'source_type', None),
        )
        partner_id = attrs.get('partner_organization_id', _UNSET)
        billing_mode = attrs.get(
            'billing_mode',
            getattr(instance, 'billing_mode', None),
        )

        # Resolve partner_id: if not in payload, use current instance value
        if partner_id is _UNSET:
            partner_id = getattr(instance, 'partner_organization_id', None) if instance else None
        # If explicitly set to None in the payload, it's None

        if source_type == SourceType.DIRECT_PATIENT:
            if partner_id is not None:
                raise serializers.ValidationError({
                    'partner_organization_id': (
                        'Must be null when source_type is DIRECT_PATIENT.'
                    ),
                })
            if billing_mode == BillingMode.PARTNER_BILLING:
                raise serializers.ValidationError({
                    'billing_mode': (
                        'PARTNER_BILLING is not valid for DIRECT_PATIENT requests.'
                    ),
                })

        if source_type == SourceType.PARTNER_ORGANIZATION:
            if partner_id is None:
                raise serializers.ValidationError({
                    'partner_organization_id': (
                        'Required when source_type is PARTNER_ORGANIZATION.'
                    ),
                })

        return attrs


# Sentinel for distinguishing "not in payload" from explicit null
_UNSET = object()
