"""
Cytova — Catalog Serializers
"""
from rest_framework import serializers
from .models import (
    ExamCategory, ExamFamily, ExamSubFamily, TubeType, ExamTechnique,
    ExamDefinition, ExamParameter, LabExamSettings, PricingRule,
    PricingType, ResultStructure, SampleType,
)


# ---------------------------------------------------------------------------
# Reference model serializers
# ---------------------------------------------------------------------------

class ExamFamilyListSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExamFamily
        fields = ['id', 'name', 'description', 'display_order', 'is_active', 'created_at']


class ExamFamilyDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExamFamily
        fields = ['id', 'name', 'description', 'display_order', 'is_active', 'created_at', 'updated_at']


class ExamFamilyCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    display_order = serializers.IntegerField(required=False, default=0)

    def validate_name(self, value):
        if ExamFamily.objects.filter(name=value).exists():
            raise serializers.ValidationError('A family with this name already exists.')
        return value


class ExamFamilyUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    display_order = serializers.IntegerField(required=False)

    def validate_name(self, value):
        instance = self.context.get('instance')
        qs = ExamFamily.objects.filter(name=value)
        if instance:
            qs = qs.exclude(pk=instance.pk)
        if qs.exists():
            raise serializers.ValidationError('A family with this name already exists.')
        return value


class ExamSubFamilyListSerializer(serializers.ModelSerializer):
    """Compact list/dropdown serializer for sub-families."""
    family_name = serializers.CharField(source='family.name', read_only=True)

    class Meta:
        model = ExamSubFamily
        fields = ['id', 'family_id', 'family_name', 'name', 'is_active', 'created_at']


# Backwards-compatible alias: historical code (e.g. ExamDefinitionDetailSerializer)
# imports ExamSubFamilySerializer for nested representation.
ExamSubFamilySerializer = ExamSubFamilyListSerializer


class ExamSubFamilyDetailSerializer(serializers.ModelSerializer):
    family_name = serializers.CharField(source='family.name', read_only=True)

    class Meta:
        model = ExamSubFamily
        fields = [
            'id', 'family_id', 'family_name', 'name',
            'is_active', 'created_at', 'updated_at',
        ]


class ExamSubFamilyCreateSerializer(serializers.Serializer):
    family_id = serializers.UUIDField()
    name = serializers.CharField(max_length=150)

    def validate_family_id(self, value):
        if not ExamFamily.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError('Family not found or inactive.')
        return value

    def validate(self, attrs):
        if ExamSubFamily.objects.filter(family_id=attrs['family_id'], name=attrs['name']).exists():
            raise serializers.ValidationError({
                'name': 'A sub-family with this name already exists in this family.'
            })
        return attrs


class ExamSubFamilyUpdateSerializer(serializers.Serializer):
    """
    Partial update. family_id is intentionally immutable — moving a sub-family
    between families would break exam references silently. Clients who need to
    reassign must deactivate + recreate.
    """
    name = serializers.CharField(max_length=150, required=False)

    def validate_name(self, value):
        instance = self.context.get('instance')
        if not instance:
            return value
        qs = ExamSubFamily.objects.filter(
            family_id=instance.family_id, name=value,
        ).exclude(pk=instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                'A sub-family with this name already exists in this family.'
            )
        return value


class TubeTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = TubeType
        fields = ['id', 'name', 'description', 'is_active', 'created_at']


# Naming parity with other reference entities.
TubeTypeListSerializer = TubeTypeSerializer


class TubeTypeCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    description = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_name(self, value):
        if TubeType.objects.filter(name=value).exists():
            raise serializers.ValidationError('A tube type with this name already exists.')
        return value


class TubeTypeUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100, required=False)
    description = serializers.CharField(required=False, allow_blank=True)

    def validate_name(self, value):
        instance = self.context.get('instance')
        qs = TubeType.objects.filter(name=value)
        if instance:
            qs = qs.exclude(pk=instance.pk)
        if qs.exists():
            raise serializers.ValidationError('A tube type with this name already exists.')
        return value


class ExamTechniqueSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExamTechnique
        fields = ['id', 'name', 'description', 'is_active', 'created_at']


# Naming parity with other reference entities.
ExamTechniqueListSerializer = ExamTechniqueSerializer


class ExamTechniqueCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
    description = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_name(self, value):
        if ExamTechnique.objects.filter(name=value).exists():
            raise serializers.ValidationError('A technique with this name already exists.')
        return value


class ExamTechniqueUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150, required=False)
    description = serializers.CharField(required=False, allow_blank=True)

    def validate_name(self, value):
        instance = self.context.get('instance')
        qs = ExamTechnique.objects.filter(name=value)
        if instance:
            qs = qs.exclude(pk=instance.pk)
        if qs.exists():
            raise serializers.ValidationError('A technique with this name already exists.')
        return value


class SampleTypeSerializer(serializers.Serializer):
    """
    Read-only serializer for sample types.

    Sample types are a fixed clinical taxonomy enforced at the model level via
    ``SampleType.choices``. They are exposed as a structured reference list so
    the frontend can populate dropdowns consistently, but they are intentionally
    not writable — adding/removing a value requires a migration + code review
    (it affects exam definitions, traceability, and reporting).
    """
    value = serializers.CharField()
    label = serializers.CharField()


# ---------------------------------------------------------------------------
# Legacy Exam Category (kept for backward compatibility)
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

class ExamParameterSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExamParameter
        fields = [
            'id', 'code', 'name', 'unit', 'reference_range',
            'display_order', 'is_active', 'created_at',
        ]


class ExamParameterWriteSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=50)
    name = serializers.CharField(max_length=255)
    unit = serializers.CharField(max_length=50, required=False, allow_blank=True, default='')
    reference_range = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    display_order = serializers.IntegerField(required=False, default=0)
    is_active = serializers.BooleanField(required=False, default=True)


class ExamParameterUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255, required=False)
    unit = serializers.CharField(max_length=50, required=False, allow_blank=True)
    reference_range = serializers.CharField(max_length=100, required=False, allow_blank=True)
    display_order = serializers.IntegerField(required=False)
    is_active = serializers.BooleanField(required=False)


class ExamDefinitionListSerializer(serializers.ModelSerializer):
    family_name = serializers.CharField(source='family.name', default=None, read_only=True)
    sub_family_name = serializers.CharField(source='sub_family.name', default=None, read_only=True)
    tube_type_name = serializers.CharField(source='tube_type.name', default=None, read_only=True)
    technique_name = serializers.CharField(source='technique.name', default=None, read_only=True)
    category_name = serializers.CharField(source='category.name', default=None, read_only=True)
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=4, coerce_to_string=True)
    is_enabled = serializers.SerializerMethodField()
    parameters_count = serializers.SerializerMethodField()

    class Meta:
        model = ExamDefinition
        fields = [
            'id', 'code', 'name',
            'family_id', 'family_name',
            'sub_family_id', 'sub_family_name',
            'tube_type_id', 'tube_type_name',
            'technique_id', 'technique_name',
            'fasting_required',
            'result_structure', 'unit', 'reference_range',
            'sample_type', 'turnaround_hours', 'unit_price',
            'is_active', 'is_enabled',
            'parameters_count',
            'category_id', 'category_name',
            'created_at',
        ]

    def get_is_enabled(self, obj):
        try:
            return obj.lab_settings.is_enabled
        except LabExamSettings.DoesNotExist:
            return True

    def get_parameters_count(self, obj):
        if obj.result_structure != ResultStructure.MULTI_PARAMETER:
            return 0
        return obj.parameters.filter(is_active=True).count()


class ExamDefinitionDetailSerializer(serializers.ModelSerializer):
    family = ExamFamilyListSerializer(read_only=True)
    sub_family = ExamSubFamilySerializer(read_only=True)
    tube_type = TubeTypeSerializer(read_only=True)
    technique = ExamTechniqueSerializer(read_only=True)
    category = ExamCategoryListSerializer(read_only=True)
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=4, coerce_to_string=True)
    lab_settings = LabExamSettingsSerializer(read_only=True)
    parameters = ExamParameterSerializer(many=True, read_only=True)

    class Meta:
        model = ExamDefinition
        fields = [
            'id', 'code', 'name',
            'family', 'sub_family', 'tube_type', 'technique',
            'fasting_required',
            'result_structure', 'unit', 'reference_range',
            'sample_type', 'turnaround_hours', 'description',
            'unit_price', 'is_active',
            'lab_settings',
            'parameters',
            'category',
            'created_at', 'updated_at',
        ]


class ExamDefinitionCreateSerializer(serializers.Serializer):
    family_id = serializers.UUIDField()
    sub_family_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    tube_type_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    technique_id = serializers.UUIDField()
    fasting_required = serializers.BooleanField(required=False, default=False)
    code = serializers.CharField(max_length=50)
    name = serializers.CharField(max_length=255)
    sample_type = serializers.ChoiceField(choices=SampleType.choices)
    result_structure = serializers.ChoiceField(
        choices=ResultStructure.choices,
        default=ResultStructure.SINGLE_VALUE,
    )
    unit = serializers.CharField(max_length=50, required=False, allow_blank=True, default='')
    reference_range = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    turnaround_hours = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=0, default=0,
    )
    parameters = ExamParameterWriteSerializer(many=True, required=False, default=[])

    def validate_family_id(self, value):
        if not ExamFamily.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError('Family not found or inactive.')
        return value

    def validate_sub_family_id(self, value):
        if value is not None and not ExamSubFamily.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError('Sub-family not found or inactive.')
        return value

    def validate_tube_type_id(self, value):
        if value is not None and not TubeType.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError('Tube type not found or inactive.')
        return value

    def validate_technique_id(self, value):
        if not ExamTechnique.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError('Technique not found or inactive.')
        return value

    def validate_code(self, value):
        code = value.upper()
        if ExamDefinition.objects.filter(code=code).exists():
            raise serializers.ValidationError('An exam with this code already exists.')
        return code

    def validate(self, attrs):
        sub_family_id = attrs.get('sub_family_id')
        if sub_family_id is not None:
            family_id = attrs.get('family_id')
            if not ExamSubFamily.objects.filter(
                id=sub_family_id, family_id=family_id, is_active=True,
            ).exists():
                raise serializers.ValidationError({
                    'sub_family_id': 'Sub-family does not belong to the selected family.',
                })

        structure = attrs.get('result_structure', ResultStructure.SINGLE_VALUE)
        params = attrs.get('parameters', [])

        if structure == ResultStructure.SINGLE_VALUE:
            if not attrs.get('unit', '').strip():
                raise serializers.ValidationError({
                    'unit': 'Unit is required for single-value exams.',
                })

        if structure == ResultStructure.MULTI_PARAMETER:
            if not params:
                raise serializers.ValidationError({
                    'parameters': 'At least one parameter is required for multi-parameter exams.',
                })
            codes = [p['code'] for p in params]
            if len(codes) != len(set(codes)):
                raise serializers.ValidationError({
                    'parameters': 'Parameter codes must be unique within the exam.',
                })

        return attrs


class ExamDefinitionStructureChangeSerializer(serializers.Serializer):
    """Input for ``POST /exam-definitions/{id}/change-structure/``.

    Allows a lab admin to correct a mis-typed structure on an exam
    definition without breaking existing requests (in-flight items
    keep their snapshotted structure — see
    ``apps.requests.item_structure``).

    Switching to MULTI_PARAMETER REQUIRES a non-empty ``parameters``
    list — the service refuses an empty target since a multi-param
    exam with zero parameters can't be entered. Reusing an existing
    parameter ``code`` reactivates that parameter rather than
    failing on the (exam, code) unique constraint, so an admin
    correcting their own earlier flip-flop doesn't need to chase
    down hidden rows.
    """
    result_structure = serializers.ChoiceField(
        choices=ResultStructure.choices,
    )
    parameters = ExamParameterWriteSerializer(many=True, required=False, default=[])

    def validate(self, attrs):
        structure = attrs['result_structure']
        params = attrs.get('parameters') or []
        if structure == ResultStructure.MULTI_PARAMETER:
            codes = [p.get('code', '').strip() for p in params]
            if len(codes) != len(set(codes)):
                raise serializers.ValidationError({
                    'parameters': 'Parameter codes must be unique within the exam.',
                })
        return attrs


class ExamDefinitionUpdateSerializer(serializers.Serializer):
    """code and result_structure are immutable after creation."""
    family_id = serializers.UUIDField(required=False)
    sub_family_id = serializers.UUIDField(required=False, allow_null=True)
    tube_type_id = serializers.UUIDField(required=False, allow_null=True)
    technique_id = serializers.UUIDField(required=False)
    fasting_required = serializers.BooleanField(required=False)
    unit = serializers.CharField(max_length=50, required=False, allow_blank=True)
    reference_range = serializers.CharField(max_length=100, required=False, allow_blank=True)
    name = serializers.CharField(max_length=255, required=False)
    sample_type = serializers.ChoiceField(choices=SampleType.choices, required=False)
    turnaround_hours = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    description = serializers.CharField(required=False, allow_blank=True)
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=4, min_value=0, required=False,
    )

    def validate_family_id(self, value):
        if not ExamFamily.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError('Family not found or inactive.')
        return value

    def validate_sub_family_id(self, value):
        if value is not None and not ExamSubFamily.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError('Sub-family not found or inactive.')
        return value

    def validate_tube_type_id(self, value):
        if value is not None and not TubeType.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError('Tube type not found or inactive.')
        return value

    def validate_technique_id(self, value):
        if not ExamTechnique.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError('Technique not found or inactive.')
        return value

    def validate(self, attrs):
        if 'code' in self.initial_data:
            raise serializers.ValidationError({
                'code': 'Code is immutable after creation and cannot be changed.',
            })
        if 'result_structure' in self.initial_data:
            raise serializers.ValidationError({
                'result_structure': 'Result structure is immutable after creation.',
            })

        # Coherence rule on the *resulting* (family, sub_family) pair after
        # applying the partial payload to the current instance. The caller
        # must supply context={'instance': exam} for this check to work on
        # partial updates that only touch one side of the pair.
        instance = self.context.get('instance')

        if 'family_id' in attrs:
            resulting_family_id = attrs['family_id']
        else:
            resulting_family_id = getattr(instance, 'family_id', None)

        if 'sub_family_id' in attrs:
            resulting_sub_family_id = attrs['sub_family_id']
        else:
            resulting_sub_family_id = getattr(instance, 'sub_family_id', None)

        if resulting_sub_family_id is not None:
            if not ExamSubFamily.objects.filter(
                id=resulting_sub_family_id,
                family_id=resulting_family_id,
                is_active=True,
            ).exists():
                raise serializers.ValidationError({
                    'sub_family_id': 'Sub-family does not belong to the selected family.',
                })

        if instance and 'unit' in attrs:
            from .models import ResultStructure
            if instance.result_structure == ResultStructure.SINGLE_VALUE:
                if not attrs['unit'].strip():
                    raise serializers.ValidationError({
                        'unit': 'Unit is required for single-value exams.',
                    })

        return attrs


# ---------------------------------------------------------------------------
# Pricing Rule (unchanged)
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
