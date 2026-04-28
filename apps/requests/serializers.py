"""
Cytova — Analysis Request Serializers
"""
from rest_framework import serializers

from apps.patients.models import Patient
from apps.catalog.models import ExamDefinition
from apps.partners.models import PartnerOrganization
from .models import (
    AnalysisRequest, AnalysisRequestItem, ExamTraceability,
    RequestLabel, RequestLabelBatch,
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

    collected_by_email = serializers.CharField(
        source='collected_by.email', read_only=True, default=None,
    )

    class Meta:
        model = AnalysisRequestItem
        fields = [
            'id', 'exam_definition_id', 'exam_code', 'exam_name',
            'status', 'execution_mode', 'rejection_reason',
            'external_partner_name', 'notes',
            'unit_price', 'billed_price', 'price_source',
            'collected_at', 'collected_by_email', 'collection_notes',
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
    collected_by_email = serializers.CharField(
        source='collected_by.email', read_only=True, default=None,
    )

    class Meta:
        model = AnalysisRequestItem
        fields = [
            'id', 'analysis_request_id',
            'exam_definition_id', 'exam_code', 'exam_name',
            'status', 'execution_mode', 'rejection_reason',
            'external_partner_name', 'notes',
            'unit_price', 'billed_price', 'price_source', 'pricing_rule_id',
            'collected_at', 'collected_by_email', 'collection_notes',
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
            'id', 'request_number', 'public_reference',
            'patient_id', 'patient_name',
            'status', 'closure_status',
            'source_type', 'billing_mode',
            'partner_organization_id', 'partner_organization_name',
            'items_count', 'created_by_email', 'created_at',
            # Surfaced on the list so a row badge can show the notification
            # state without paging into the detail view.
            'notified_by_email_at', 'notification_count',
            'last_patient_notification_channel',
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
    # Patient email surfaced so the UI can pre-validate "Notify by email"
    # actions without a second round-trip. The notify-patient endpoint
    # still validates server-side and returns PATIENT_EMAIL_MISSING if
    # the field is blank — this is purely a UX hint.
    patient_email = serializers.CharField(
        source='patient.email', read_only=True, default='',
    )
    partner_organization_name = serializers.CharField(
        source='partner_organization.name', read_only=True, default=None,
    )
    partner_organization_code = serializers.CharField(
        source='partner_organization.code', read_only=True, default=None,
    )
    # Report availability is surfaced directly on the detail payload so the
    # UI can decide between "Generate" / "Regenerate" / "Download" without a
    # second round-trip and without losing state on reload.
    has_report = serializers.SerializerMethodField()
    current_report = serializers.SerializerMethodField()

    # Aggregated patient summary for the detail header card. Same fields
    # the patient list/detail already exposes — never bypasses the patient
    # serializer's permission rules (the detail view is staff-authenticated
    # and tenant-isolated; staff can already read these from /patients/).
    patient_summary = serializers.SerializerMethodField()

    # Notification tracking — surfaced so the UI renders the "Patient
    # notified by email" badge + warns before re-notify.
    notified_by_email_by_email = serializers.CharField(
        source='notified_by_email_by.email', read_only=True, default=None,
    )
    delivered_by_email = serializers.CharField(
        source='delivered_by.email', read_only=True, default=None,
    )
    archived_by_email = serializers.CharField(
        source='archived_by.email', read_only=True, default=None,
    )

    class Meta:
        model = AnalysisRequest
        fields = [
            'id', 'request_number', 'public_reference', 'patient_id',
            'patient_email', 'patient_summary',
            'status', 'closure_status', 'notes',
            'source_type', 'billing_mode',
            'partner_organization_id', 'partner_organization_name',
            'partner_organization_code', 'external_reference', 'source_notes',
            'confirmed_at', 'confirmed_by_email',
            'cancelled_at', 'cancelled_by_email',
            'created_by_email',
            'items',
            'has_report', 'current_report',
            # Notification + lifecycle stamps
            'notified_by_email_at', 'notified_by_email_by_email',
            'notification_count', 'last_patient_notification_channel',
            'delivered_at', 'delivered_by_email',
            'archived_at', 'archived_by_email',
            'created_at', 'updated_at',
        ]

    def get_patient_summary(self, obj):
        p = obj.patient
        if p is None:
            return None
        # Only expose what the patient list/detail already exposes to staff.
        # No medical data, no insurance details — staff hit /patients/{id}/
        # for the full record and the modal's "View patient details" link.
        return {
            'id': str(p.id),
            'full_name': f'{p.first_name} {p.last_name}'.strip(),
            'first_name': p.first_name,
            'last_name': p.last_name,
            'document_number': p.document_number,
            'phone': p.phone or '',
            'email': p.email or '',
        }

    def _get_current_report(self, obj):
        # Use the queryset filter directly rather than obj.reports.all()
        # so prefetching strategies stay compatible — is_current is indexed.
        return (
            obj.reports
            .filter(is_current=True)
            .select_related('generated_by')
            .first()
        )

    def get_has_report(self, obj) -> bool:
        return self._get_current_report(obj) is not None

    def get_current_report(self, obj):
        report = self._get_current_report(obj)
        if report is None:
            return None
        return {
            'id': str(report.id),
            'version_number': report.version_number,
            'generated_at': report.generated_at.isoformat(),
            'generated_by_email': (
                report.generated_by.email if report.generated_by else None
            ),
            'pdf_url': f'/requests/{obj.id}/report/download/',
            'downloadable': bool(report.pdf_file_key),
        }


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


class ItemMarkCollectedSerializer(serializers.Serializer):
    """Input for the ``mark-collected`` action. Notes are optional."""
    collection_notes = serializers.CharField(
        required=False, allow_blank=True, default='',
        max_length=2000,
    )


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

    # Lifecycle flag — when true, the request is created AND confirmed in
    # a single atomic transaction (DRAFT → CONFIRMED via the existing
    # state machine). Used by the 3-step creation wizard whose final
    # button semantically means "commit this request". Default false so
    # legacy clients that create drafts for later editing keep working.
    confirm = serializers.BooleanField(required=False, default=False)

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

        # Confirm-on-create requires at least one item. Catching this at
        # the serializer layer gives a field-scoped 400 ("items: ...")
        # instead of a downstream state-machine error from ``confirm``.
        if attrs.get('confirm') and not attrs.get('items'):
            raise serializers.ValidationError({
                'items': (
                    'At least one exam item is required when confirm=true.'
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


# ---------------------------------------------------------------------------
# Pricing Preview — Step 3 recap
# ---------------------------------------------------------------------------

class PricingPreviewRequestSerializer(serializers.Serializer):
    """
    Input for the preview endpoint. Accepts only what the resolver needs —
    ``source_type``, optional ``partner_organization_id``, and the list of
    exam ids the user has selected so far. Deliberately DOES NOT accept a
    patient id: pricing does not depend on the patient, so requiring one
    at preview time would be an artificial coupling.
    """
    source_type = serializers.ChoiceField(choices=SourceType.choices)
    partner_organization_id = serializers.UUIDField(
        required=False, allow_null=True, default=None,
    )
    exam_definition_ids = serializers.ListField(
        child=serializers.UUIDField(),
        allow_empty=False,
    )

    def validate_partner_organization_id(self, value):
        if value is not None:
            if not PartnerOrganization.objects.filter(id=value, is_active=True).exists():
                raise serializers.ValidationError(
                    'Partner organization not found or inactive.'
                )
        return value

    def validate_exam_definition_ids(self, value):
        # Reject duplicates up-front so the resolver does not have to
        # defend against them and the returned list stays 1:1 with input.
        if len(value) != len({str(v) for v in value}):
            raise serializers.ValidationError('Duplicate exam ids are not allowed.')
        return value

    def validate(self, attrs):
        source_type = attrs['source_type']
        partner_id = attrs.get('partner_organization_id')

        if source_type == SourceType.PARTNER_ORGANIZATION and partner_id is None:
            raise serializers.ValidationError({
                'partner_organization_id': (
                    'Required when source_type is PARTNER_ORGANIZATION.'
                ),
            })
        if source_type == SourceType.DIRECT_PATIENT and partner_id is not None:
            raise serializers.ValidationError({
                'partner_organization_id': (
                    'Must be null when source_type is DIRECT_PATIENT.'
                ),
            })
        return attrs


class ResolvedItemPriceSerializer(serializers.Serializer):
    """
    Output shape for one resolved line — mirrors the
    ``ResolvedItemPrice`` dataclass used by ``RequestPricingResolver``.
    """
    exam_definition_id = serializers.UUIDField()
    exam_code = serializers.CharField()
    exam_name = serializers.CharField()
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=4, coerce_to_string=True,
    )
    billed_price = serializers.DecimalField(
        max_digits=12, decimal_places=4, coerce_to_string=True,
    )
    price_source = serializers.ChoiceField(choices=PriceSource.choices)


# ---------------------------------------------------------------------------
# Request Labels
# ---------------------------------------------------------------------------

class RequestLabelSerializer(serializers.ModelSerializer):
    class Meta:
        model = RequestLabel
        fields = ['id', 'barcode_value', 'label_index', 'family_name']


class RequestLabelBatchSerializer(serializers.ModelSerializer):
    """
    Serializer for a label batch, including the ordered list of labels
    and the API path of the protected download endpoint.

    ``pdf_url`` is the **API-relative path** of the protected backend
    download endpoint — NOT a raw ``/media/...`` path and NOT a public
    pre-signed URL. Every download goes through an authenticated,
    tenant-scoped backend action that streams the PDF. This guarantees
    that a leaked URL alone cannot expose a sensitive lab document.

    The shape is ``/requests/<uuid>/labels/download/`` (deliberately
    without the ``/api/v1`` prefix) so the frontend axios client can
    consume it directly: ``api.get(pdf_url, { responseType: 'blob' })``
    will prepend its own base URL and automatically include the JWT
    Authorization header.
    """
    labels = RequestLabelSerializer(many=True, read_only=True)
    pdf_url = serializers.SerializerMethodField()
    generated_by_email = serializers.CharField(
        source='generated_by.email', read_only=True, default=None,
    )

    class Meta:
        model = RequestLabelBatch
        fields = [
            'id',
            'analysis_request_id',
            'label_count',
            'family_count',
            'generated_at',
            'generated_by_email',
            'pdf_url',
            'labels',
        ]

    def get_pdf_url(self, obj):
        if not obj.pdf_file_key:
            return None
        return f'/requests/{obj.analysis_request_id}/labels/download/'
