"""
Cytova — Tenants Serializers (Platform Admin API)
"""
from rest_framework import serializers
from .models import Tenant, Domain, Plan


class DomainSerializer(serializers.ModelSerializer):
    class Meta:
        model = Domain
        fields = ['domain', 'is_primary']


class TenantListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tenant
        fields = ['id', 'name', 'subdomain', 'plan', 'is_active', 'created_at']


class TenantDetailSerializer(serializers.ModelSerializer):
    domains = DomainSerializer(many=True, read_only=True)

    class Meta:
        model = Tenant
        fields = [
            'id', 'name', 'subdomain', 'schema_name', 'plan',
            'is_active', 'created_at', 'activated_at', 'suspended_at',
            'domains',
        ]


class TenantCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    subdomain = serializers.SlugField(max_length=100)
    plan = serializers.ChoiceField(choices=Plan.choices, default=Plan.STARTER)

    def validate_subdomain(self, value):
        if Tenant.objects.filter(subdomain=value).exists():
            raise serializers.ValidationError(
                'A laboratory with this subdomain already exists.'
            )
        return value.lower()


class TenantUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255, required=False)
    plan = serializers.ChoiceField(choices=Plan.choices, required=False)


class PlatformAdminLoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, style={'input_type': 'password'})
