"""
Cytova — Partner Organization Serializers
"""
from decimal import Decimal

from rest_framework import serializers

from apps.catalog.models import ExamDefinition
from .models import BillingMode, OrganizationType, PartnerExamPrice, PartnerOrganization


class PartnerOrganizationListSerializer(serializers.ModelSerializer):
    class Meta:
        model = PartnerOrganization
        fields = [
            'id', 'code', 'name', 'organization_type',
            'contact_person', 'phone', 'email',
            'invoice_discount_rate',
            'is_active', 'created_at',
        ]


class PartnerOrganizationDetailSerializer(serializers.ModelSerializer):
    # Surfaced as a URL — the model field resolves through the configured
    # storage (FileSystemStorage in dev, S3 in prod). DRF's
    # ImageField.to_representation builds an absolute URL when a request
    # is present in the serializer context, which is the case here
    # because the viewset always passes ``context={'request': request}``.
    report_header_logo = serializers.ImageField(read_only=True)

    class Meta:
        model = PartnerOrganization
        fields = [
            'id', 'code', 'name', 'organization_type',
            'contact_person', 'phone', 'email', 'address',
            'default_billing_mode', 'payment_terms_days',
            'invoice_discount_rate', 'billing_notes',
            'notes', 'is_active', 'created_at', 'updated_at',
            # Optional report branding (per-partner)
            'custom_report_branding_enabled',
            'report_header_name', 'report_header_subtitle',
            'report_header_address', 'report_header_phone',
            'report_header_email', 'report_header_logo',
            'report_footer_text',
        ]


class PartnerOrganizationCreateSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=50)
    name = serializers.CharField(max_length=255)
    organization_type = serializers.ChoiceField(choices=OrganizationType.choices)
    contact_person = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default='',
    )
    phone = serializers.CharField(
        max_length=50, required=False, allow_blank=True, default='',
    )
    email = serializers.EmailField(required=False, allow_blank=True, default='')
    address = serializers.CharField(required=False, allow_blank=True, default='')
    default_billing_mode = serializers.ChoiceField(
        choices=BillingMode.choices, required=False, allow_null=True, default=None,
    )
    payment_terms_days = serializers.IntegerField(
        required=False, allow_null=True, default=None, min_value=0,
    )
    invoice_discount_rate = serializers.DecimalField(
        max_digits=5, decimal_places=2,
        required=False, allow_null=True, default=None,
    )
    billing_notes = serializers.CharField(required=False, allow_blank=True, default='')
    notes = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_code(self, value):
        code = value.upper()
        if PartnerOrganization.objects.filter(code=code).exists():
            raise serializers.ValidationError(
                'A partner organization with this code already exists.'
            )
        return code


# Allowed image content types for partner-supplied report logos. Kept
# narrow on purpose: the result PDF renderer feeds the file straight to
# reportlab's ``ImageReader`` (PIL under the hood), which only handles
# raster formats. SVG would require an additional rasterisation step
# and a strict policy check against XML payload risks — out of scope
# for this opt-in branding feature.
LOGO_ALLOWED_CONTENT_TYPES = ('image/png', 'image/jpeg')
LOGO_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB hard cap.


class PartnerBrandingUpdateSerializer(serializers.Serializer):
    """
    Multipart-friendly partial update for partner-specific report branding.

    Field semantics
    ---------------
    - ``custom_report_branding_enabled`` toggles the override; the lab
      branding is always restored as fallback when this is False or any
      individual branding field is empty.
    - ``report_header_logo`` is an actual uploaded file. Validated for
      content type + size here; the model's ``ImageField`` then enforces
      that the bytes can be opened by Pillow.
    - ``clear_logo`` is a write-only flag that lets the UI delete an
      existing logo without uploading a replacement. Useful for the
      "Remove logo" button in the branding drawer.
    """
    custom_report_branding_enabled = serializers.BooleanField(required=False)
    report_header_name = serializers.CharField(
        max_length=255, required=False, allow_blank=True,
    )
    report_header_subtitle = serializers.CharField(
        max_length=255, required=False, allow_blank=True,
    )
    report_header_address = serializers.CharField(required=False, allow_blank=True)
    report_header_phone = serializers.CharField(
        max_length=50, required=False, allow_blank=True,
    )
    report_header_email = serializers.EmailField(required=False, allow_blank=True)
    report_header_logo = serializers.ImageField(required=False, allow_null=True)
    report_footer_text = serializers.CharField(required=False, allow_blank=True)
    clear_logo = serializers.BooleanField(required=False, write_only=True)

    def validate_report_header_logo(self, value):
        if value is None:
            return value
        if value.size > LOGO_MAX_BYTES:
            raise serializers.ValidationError(
                f'Logo file is too large ({value.size // 1024} KB). '
                f'Maximum allowed: {LOGO_MAX_BYTES // 1024} KB.'
            )
        # ``content_type`` is the browser-supplied MIME — fast first check.
        # The serializer's underlying ImageField already calls Pillow to
        # confirm the bytes parse as a real image, so a spoofed MIME with
        # garbage payload still fails downstream.
        ctype = (value.content_type or '').lower()
        if ctype not in LOGO_ALLOWED_CONTENT_TYPES:
            raise serializers.ValidationError(
                'Unsupported logo format. Allowed: PNG or JPEG.'
            )
        return value

    def validate(self, attrs):
        # ``clear_logo`` and ``report_header_logo`` are mutually exclusive
        # — uploading a new logo and asking to clear the existing one in
        # the same request is contradictory and almost always a UI bug.
        if attrs.get('clear_logo') and attrs.get('report_header_logo') is not None:
            raise serializers.ValidationError({
                'clear_logo': (
                    'Cannot clear and upload a new logo in the same request.'
                ),
            })
        return attrs


class PartnerOrganizationUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255, required=False)
    organization_type = serializers.ChoiceField(
        choices=OrganizationType.choices, required=False,
    )
    contact_person = serializers.CharField(
        max_length=255, required=False, allow_blank=True,
    )
    phone = serializers.CharField(
        max_length=50, required=False, allow_blank=True,
    )
    email = serializers.EmailField(required=False, allow_blank=True)
    address = serializers.CharField(required=False, allow_blank=True)
    default_billing_mode = serializers.ChoiceField(
        choices=BillingMode.choices, required=False, allow_null=True,
    )
    payment_terms_days = serializers.IntegerField(
        required=False, allow_null=True, min_value=0,
    )
    invoice_discount_rate = serializers.DecimalField(
        max_digits=5, decimal_places=2,
        required=False, allow_null=True,
    )
    billing_notes = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)


# ---------------------------------------------------------------------------
# Partner Exam Price
# ---------------------------------------------------------------------------

class PartnerExamPriceListSerializer(serializers.ModelSerializer):
    """
    Compact list/detail shape surfaced by the partner-scoped endpoints.

    Includes the denormalised exam identity (``exam_code``, ``exam_name``)
    and the reference ``unit_price`` so a frontend table can render an
    "agreed vs reference" comparison in a single request with no extra
    lookups. Kept read-only; creation/update go through the dedicated
    create/update serializers below so payload shape stays intentional.
    """
    exam_code = serializers.CharField(source='exam_definition.code', read_only=True)
    exam_name = serializers.CharField(source='exam_definition.name', read_only=True)
    reference_unit_price = serializers.DecimalField(
        source='exam_definition.unit_price',
        max_digits=12, decimal_places=4, coerce_to_string=True, read_only=True,
    )
    agreed_price = serializers.DecimalField(
        max_digits=12, decimal_places=4, coerce_to_string=True,
    )
    partner_id = serializers.UUIDField(read_only=True)
    partner_code = serializers.CharField(source='partner.code', read_only=True)
    partner_name = serializers.CharField(source='partner.name', read_only=True)

    class Meta:
        model = PartnerExamPrice
        fields = [
            'id',
            'partner_id', 'partner_code', 'partner_name',
            'exam_definition_id', 'exam_code', 'exam_name',
            'reference_unit_price', 'agreed_price',
            'notes', 'is_active', 'created_at', 'updated_at',
        ]


class PartnerExamPriceCreateSerializer(serializers.Serializer):
    """
    Write-path serializer for creating a new agreed price.

    Partner is implicit from the URL (nested route) — the view injects it
    into the service layer. Only the exam and the price are client-supplied.
    """
    exam_definition_id = serializers.UUIDField()
    agreed_price = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=Decimal('0'),
    )
    notes = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_exam_definition_id(self, value):
        if not ExamDefinition.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError('Exam definition not found or inactive.')
        return value

    def validate(self, attrs):
        # Duplicate-active guard at the serializer level — gives a clean
        # 400 with a field-scoped error before the DB unique constraint
        # would otherwise raise an IntegrityError. The partner is passed
        # in via context from the view.
        partner = self.context.get('partner')
        if partner is None:
            return attrs
        exists = PartnerExamPrice.objects.filter(
            partner=partner,
            exam_definition_id=attrs['exam_definition_id'],
            is_active=True,
        ).exists()
        if exists:
            raise serializers.ValidationError({
                'exam_definition_id': (
                    'An active agreed price already exists for this '
                    'partner and exam. Deactivate the existing one first.'
                ),
            })
        return attrs


class PartnerExamPriceUpdateSerializer(serializers.Serializer):
    """
    Partial-update serializer.

    ``partner`` and ``exam_definition`` are intentionally NOT editable:
    changing either would effectively be "this is a different agreement",
    which the lab should model as deactivate + create rather than a silent
    reparent. Only the negotiated value and notes can move.
    """
    agreed_price = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=Decimal('0'), required=False,
    )
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        # Explicit rejection of any attempt to move the (partner, exam)
        # pair — mirrors the ExamDefinition.code immutability rule from
        # an earlier step and gives an auditable 400 instead of a silent
        # strip.
        for immutable in ('partner_id', 'exam_definition_id'):
            if immutable in self.initial_data:
                raise serializers.ValidationError({
                    immutable: (
                        'This field is immutable on an existing agreed '
                        'price. Deactivate and create a new row instead.'
                    ),
                })
        return attrs
