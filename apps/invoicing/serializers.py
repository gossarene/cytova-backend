"""Cytova — Invoice Serializers"""
from rest_framework import serializers

from apps.partners.models import PartnerOrganization
from .models import Invoice, InvoiceLine


class InvoiceLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceLine
        fields = [
            'id',
            'request_number_snapshot', 'public_reference_snapshot',
            'patient_display_name',
            'exam_code_snapshot', 'exam_name_snapshot',
            'performed_date',
            'unit_price_snapshot', 'billed_price_snapshot',
            'line_amount',
        ]


class InvoiceListSerializer(serializers.ModelSerializer):
    partner_name = serializers.CharField(source='partner.name', read_only=True)
    partner_code = serializers.CharField(source='partner.code', read_only=True)
    line_count = serializers.SerializerMethodField()
    has_pdf = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = [
            'id', 'invoice_number', 'status',
            'partner_id', 'partner_name', 'partner_code',
            'period_start', 'period_end',
            'gross_total', 'discount_rate_snapshot',
            'discount_amount', 'subtotal_after_discount',
            'vat_rate_snapshot', 'vat_amount', 'net_total',
            'line_count', 'has_pdf',
            'generated_at', 'confirmed_at',
        ]

    def get_line_count(self, obj):
        return obj.lines.count()

    def get_has_pdf(self, obj):
        return bool(obj.pdf_file_key)


class InvoiceDetailSerializer(serializers.ModelSerializer):
    partner_name = serializers.CharField(source='partner.name', read_only=True)
    partner_code = serializers.CharField(source='partner.code', read_only=True)
    generated_by_email = serializers.CharField(
        source='generated_by.email', read_only=True, default=None,
    )
    confirmed_by_email = serializers.CharField(
        source='confirmed_by.email', read_only=True, default=None,
    )
    lines = InvoiceLineSerializer(many=True, read_only=True)
    has_pdf = serializers.SerializerMethodField()
    pdf_url = serializers.SerializerMethodField()
    has_statement = serializers.SerializerMethodField()
    statement_url = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = [
            'id', 'invoice_number', 'status',
            'partner_id', 'partner_name', 'partner_code',
            'period_start', 'period_end', 'currency',
            'gross_total', 'discount_rate_snapshot',
            'discount_amount', 'subtotal_after_discount',
            'vat_rate_snapshot', 'vat_amount', 'net_total',
            'generated_by_email', 'generated_at',
            'confirmed_by_email', 'confirmed_at',
            'cancelled_at', 'notes',
            'has_pdf', 'pdf_url',
            'has_statement', 'statement_url',
            'lines',
            'created_at',
        ]

    def get_has_pdf(self, obj):
        return bool(obj.pdf_file_key)

    def get_has_statement(self, obj):
        return bool(obj.statement_file_key)

    def get_statement_url(self, obj):
        if obj.statement_file_key:
            return f'/invoicing/{obj.id}/download-statement/'
        return None

    def get_pdf_url(self, obj):
        if obj.pdf_file_key:
            return f'/invoicing/{obj.id}/download/'
        return None


class InvoicePreviewRequestSerializer(serializers.Serializer):
    partner_id = serializers.UUIDField()
    period_start = serializers.DateField()
    period_end = serializers.DateField()

    def validate_partner_id(self, value):
        try:
            PartnerOrganization.objects.get(id=value, is_active=True)
        except PartnerOrganization.DoesNotExist:
            raise serializers.ValidationError('Partner not found or inactive.')
        return value

    def validate(self, attrs):
        if attrs['period_start'] > attrs['period_end']:
            raise serializers.ValidationError(
                {'period_end': 'End date must be on or after start date.'}
            )
        return attrs


class InvoiceGenerateRequestSerializer(InvoicePreviewRequestSerializer):
    notes = serializers.CharField(required=False, default='', allow_blank=True)
