"""
Cytova — Catalog Serializers
"""
from rest_framework import serializers
from .models import ExamCategory, ExamDefinition, LabExamSettings, PricingRule, PricingType, SampleType


# ---------------------------------------------------------------------------
# Exam Category
# ---------------------------------------------------------------------------

class ExamCategoryListSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExamCategory
        fields = ['id', 'name', 'description', 'display_order', 'is_active', 'created_at']


class ExamCategoryDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExamCategory
        fields = ['id', 'name', 'description', 'display_order', 'is_active', 'created_at', 'updated_at']


class ExamCategoryCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    display_order = serializers.IntegerField(required=False, default=0)

    def validate_name(self, value):
        if ExamCategory.objects.filter(name=value).exists():
            raise serializers.ValidationError('A category with this name already exists.')
        return value


class ExamCategoryUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    display_order = serializers.IntegerField(required=False)

    def validate_name(self, value):
        instance = self.context.get('instance')
        qs = ExamCategory.objects.filter(name=value)
        if instance:
            qs = qs.exclude(pk=instance.pk)
        if qs.exists():
            raise serializers.ValidationError('A category with this name already exists.')
        return value


# ---------------------------------------------------------------------------
# Lab Exam Settings
# ---------------------------------------------------------------------------

class LabExamSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = LabExamSettings
        fields = [
            'id', 'reference_range', 'turnaround_hours_override',
            'is_enabled', 'internal_notes', 'updated_at',
        ]


class LabExamSettingsWriteSerializer(serializers.Serializer):
    reference_range = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    turnaround_hours_override = serializers.IntegerField(
        required=False, allow_null=True, min_value=1,
    )
    is_enabled = serializers.BooleanField(default=True)
    internal_notes = serializers.CharField(required=False, allow_blank=True, default='')


# ---------------------------------------------------------------------------
# Exam Definition
# ---------------------------------------------------------------------------

class ExamDefinitionListSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=4, coerce_to_string=True)
    is_enabled = serializers.SerializerMethodField()

    class Meta:
        model = ExamDefinition
        fields = [
            'id', 'code', 'name', 'category_id', 'category_name',
            'sample_type', 'turnaround_hours', 'unit_price', 'is_active',
            'is_enabled', 'created_at',
        ]

    def get_is_enabled(self, obj):
        try:
            return obj.lab_settings.is_enabled
        except LabExamSettings.DoesNotExist:
            return True  # Default: enabled until explicitly disabled


class ExamDefinitionDetailSerializer(serializers.ModelSerializer):
    category = ExamCategoryListSerializer(read_only=True)
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=4, coerce_to_string=True)
    lab_settings = LabExamSettingsSerializer(read_only=True)

    class Meta:
        model = ExamDefinition
        fields = [
            'id', 'code', 'name', 'category', 'sample_type',
            'turnaround_hours', 'description', 'unit_price', 'is_active',
            'lab_settings', 'created_at', 'updated_at',
        ]


class ExamDefinitionCreateSerializer(serializers.Serializer):
    category_id = serializers.UUIDField()
    code = serializers.CharField(max_length=50)
    name = serializers.CharField(max_length=255)
    sample_type = serializers.ChoiceField(choices=SampleType.choices)
    turnaround_hours = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=0, default=0,
    )

    def validate_category_id(self, value):
        if not ExamCategory.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError('Category not found or inactive.')
        return value

    def validate_code(self, value):
        code = value.upper()
        if ExamDefinition.objects.filter(code=code).exists():
            raise serializers.ValidationError('An exam with this code already exists.')
        return code


class ExamDefinitionUpdateSerializer(serializers.Serializer):
    """
    code is intentionally excluded: it is immutable once referenced by an
    exam item (data model constraint). Excluding it from the update path
    prevents accidental mutation.
    """
    category_id = serializers.UUIDField(required=False)
    name = serializers.CharField(max_length=255, required=False)
    sample_type = serializers.ChoiceField(choices=SampleType.choices, required=False)
    turnaround_hours = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    description = serializers.CharField(required=False, allow_blank=True)
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=0, required=False,
    )

    def validate_category_id(self, value):
        if not ExamCategory.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError('Category not found or inactive.')
        return value


# ---------------------------------------------------------------------------
# Pricing Rule
# ---------------------------------------------------------------------------

class PricingRuleSerializer(serializers.ModelSerializer):
    exam_definition_id = serializers.UUIDField(source='exam_definition.id', read_only=True)
    exam_code = serializers.CharField(source='exam_definition.code', read_only=True)
    exam_name = serializers.CharField(source='exam_definition.name', read_only=True)
    value = serializers.DecimalField(max_digits=12, decimal_places=4, coerce_to_string=True)
    partner_organization_name = serializers.CharField(
        source='partner_organization.name', read_only=True, default=None,
    )
    created_by = serializers.SerializerMethodField()

    class Meta:
        model = PricingRule
        fields = [
            'id', 'exam_definition_id', 'exam_code', 'exam_name',
            'partner_organization_id', 'partner_organization_name',
            'source_type', 'pricing_type', 'value', 'priority',
            'is_active', 'start_date', 'end_date', 'notes',
            'created_by', 'created_at', 'updated_at',
        ]

    def get_created_by(self, obj):
        if obj.created_by_id:
            return {'id': str(obj.created_by_id), 'email': obj.created_by.email if obj.created_by else None}
        return None


class PricingRuleCreateSerializer(serializers.Serializer):
    exam_definition_id = serializers.UUIDField()
    partner_organization_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    source_type = serializers.CharField(max_length=25, required=False, allow_blank=True, default='')
    pricing_type = serializers.ChoiceField(
        choices=PricingType.choices, default=PricingType.FIXED_PRICE,
    )
    value = serializers.DecimalField(max_digits=12, decimal_places=4, min_value=0)
    priority = serializers.IntegerField(required=False, default=0)
    is_active = serializers.BooleanField(required=False, default=True)
    start_date = serializers.DateField(required=False, allow_null=True, default=None)
    end_date = serializers.DateField(required=False, allow_null=True, default=None)
    notes = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_exam_definition_id(self, value):
        from .models import ExamDefinition
        if not ExamDefinition.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError('Exam definition not found or inactive.')
        return value

    def validate_partner_organization_id(self, value):
        if value is not None:
            from apps.partners.models import PartnerOrganization
            if not PartnerOrganization.objects.filter(id=value, is_active=True).exists():
                raise serializers.ValidationError('Partner organization not found or inactive.')
        return value

    def validate_source_type(self, value):
        if value and value not in ('DIRECT_PATIENT', 'PARTNER_ORGANIZATION'):
            raise serializers.ValidationError(
                'Must be empty, DIRECT_PATIENT, or PARTNER_ORGANIZATION.'
            )
        return value

    def validate(self, attrs):
        start = attrs.get('start_date')
        end = attrs.get('end_date')
        if start and end and end < start:
            raise serializers.ValidationError(
                {'end_date': 'end_date must be on or after start_date.'}
            )

        pricing_type = attrs.get('pricing_type', PricingType.FIXED_PRICE)
        value = attrs.get('value')
        if pricing_type == PricingType.PERCENTAGE_DISCOUNT and value is not None:
            from decimal import Decimal
            if value > Decimal('100'):
                raise serializers.ValidationError(
                    {'value': 'Percentage discount cannot exceed 100.'}
                )

        return attrs


class PricingRuleUpdateSerializer(serializers.Serializer):
    """Updatable fields on an existing pricing rule."""
    pricing_type = serializers.ChoiceField(choices=PricingType.choices, required=False)
    value = serializers.DecimalField(max_digits=12, decimal_places=4, min_value=0, required=False)
    priority = serializers.IntegerField(required=False)
    is_active = serializers.BooleanField(required=False)
    start_date = serializers.DateField(required=False, allow_null=True)
    end_date = serializers.DateField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        start = attrs.get('start_date')
        end = attrs.get('end_date')
        if start and end and end < start:
            raise serializers.ValidationError(
                {'end_date': 'end_date must be on or after start_date.'}
            )

        pricing_type = attrs.get('pricing_type')
        value = attrs.get('value')
        if pricing_type == PricingType.PERCENTAGE_DISCOUNT and value is not None:
            from decimal import Decimal
            if value > Decimal('100'):
                raise serializers.ValidationError(
                    {'value': 'Percentage discount cannot exceed 100.'}
                )

        return attrs
