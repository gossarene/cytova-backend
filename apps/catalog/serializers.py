"""
Cytova — Catalog Serializers
"""
from django.utils import timezone
from rest_framework import serializers
from .models import ExamCategory, ExamDefinition, LabExamSettings, PricingRule, SampleType


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
    is_enabled = serializers.SerializerMethodField()

    class Meta:
        model = ExamDefinition
        fields = [
            'id', 'code', 'name', 'category_id', 'category_name',
            'sample_type', 'turnaround_hours', 'is_active', 'is_enabled',
            'created_at',
        ]

    def get_is_enabled(self, obj):
        try:
            return obj.lab_settings.is_enabled
        except LabExamSettings.DoesNotExist:
            return True  # Default: enabled until explicitly disabled


class ExamDefinitionDetailSerializer(serializers.ModelSerializer):
    category = ExamCategoryListSerializer(read_only=True)
    lab_settings = LabExamSettingsSerializer(read_only=True)

    class Meta:
        model = ExamDefinition
        fields = [
            'id', 'code', 'name', 'category', 'sample_type',
            'turnaround_hours', 'description', 'is_active',
            'lab_settings', 'created_at', 'updated_at',
        ]


class ExamDefinitionCreateSerializer(serializers.Serializer):
    category_id = serializers.UUIDField()
    code = serializers.CharField(max_length=50)
    name = serializers.CharField(max_length=255)
    sample_type = serializers.ChoiceField(choices=SampleType.choices)
    turnaround_hours = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    description = serializers.CharField(required=False, allow_blank=True, default='')

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

    def validate_category_id(self, value):
        if not ExamCategory.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError('Category not found or inactive.')
        return value


# ---------------------------------------------------------------------------
# Pricing Rule
# ---------------------------------------------------------------------------

class PricingRuleSerializer(serializers.ModelSerializer):
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=4, coerce_to_string=True)
    billed_price = serializers.DecimalField(max_digits=12, decimal_places=4, coerce_to_string=True)
    created_by = serializers.SerializerMethodField()

    class Meta:
        model = PricingRule
        fields = [
            'id', 'unit_price', 'billed_price',
            'effective_from', 'effective_to', 'insurance_code',
            'created_by', 'created_at',
        ]

    def get_created_by(self, obj):
        if obj.created_by_id:
            return {'id': str(obj.created_by_id), 'email': obj.created_by.email if obj.created_by else None}
        return None


class PricingRuleCreateSerializer(serializers.Serializer):
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=4, min_value=0)
    billed_price = serializers.DecimalField(max_digits=12, decimal_places=4, min_value=0)
    effective_from = serializers.DateField()
    effective_to = serializers.DateField(required=False, allow_null=True)
    insurance_code = serializers.CharField(max_length=50, required=False, allow_blank=True, default='')

    def validate(self, attrs):
        effective_from = attrs['effective_from']
        effective_to = attrs.get('effective_to')
        if effective_to is not None and effective_to <= effective_from:
            raise serializers.ValidationError(
                {'effective_to': 'effective_to must be after effective_from.'}
            )
        return attrs
