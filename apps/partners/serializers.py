"""
Cytova — Partner Organization Serializers
"""
from rest_framework import serializers

from .models import BillingMode, OrganizationType, PartnerOrganization


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
