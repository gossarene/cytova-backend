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
            'is_active', 'created_at',
        ]


class PartnerOrganizationDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = PartnerOrganization
        fields = [
            'id', 'code', 'name', 'organization_type',
            'contact_person', 'phone', 'email', 'address',
            'default_billing_mode', 'payment_terms_days', 'billing_notes',
            'notes', 'is_active', 'created_at', 'updated_at',
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
    billing_notes = serializers.CharField(required=False, allow_blank=True, default='')
    notes = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_code(self, value):
        code = value.upper()
        if PartnerOrganization.objects.filter(code=code).exists():
            raise serializers.ValidationError(
                'A partner organization with this code already exists.'
            )
        return code


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
