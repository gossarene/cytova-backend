"""
Cytova — Tenants Serializers (Platform Admin API)
"""
from rest_framework import serializers
from .models import Tenant, Domain, Plan, Subscription, SubscriptionStatus


class DomainSerializer(serializers.ModelSerializer):
    class Meta:
        model = Domain
        fields = ['domain', 'is_primary']


class SubscriptionBriefSerializer(serializers.ModelSerializer):
    """Compact subscription info embedded in tenant detail."""
    plan_code = serializers.CharField(source='plan.code', read_only=True)
    plan_name = serializers.CharField(source='plan.name', read_only=True)
    trial_days_remaining = serializers.IntegerField(read_only=True)
    is_usable = serializers.BooleanField(read_only=True)

    class Meta:
        model = Subscription
        fields = [
            'id', 'plan_code', 'plan_name', 'status',
            'started_at', 'trial_end_date', 'current_period_end',
            'trial_days_remaining', 'is_usable',
        ]


class TenantListSerializer(serializers.ModelSerializer):
    subscription_status = serializers.SerializerMethodField()
    subscription_plan = serializers.SerializerMethodField()

    class Meta:
        model = Tenant
        fields = [
            'id', 'name', 'subdomain', 'plan', 'is_active',
            'subscription_status', 'subscription_plan',
            'created_at',
        ]

    def get_subscription_status(self, obj):
        sub = obj.active_subscription
        return sub.status if sub else None

    def get_subscription_plan(self, obj):
        sub = obj.active_subscription
        return sub.plan.code if sub else None


class TenantDetailSerializer(serializers.ModelSerializer):
    domains = DomainSerializer(many=True, read_only=True)
    active_subscription = serializers.SerializerMethodField()

    class Meta:
        model = Tenant
        fields = [
            'id', 'name', 'subdomain', 'schema_name', 'plan',
            'is_active', 'created_at', 'activated_at', 'suspended_at',
            'domains', 'active_subscription',
        ]

    def get_active_subscription(self, obj):
        sub = obj.active_subscription
        if sub is None:
            # Fall back to latest subscription of any status
            sub = (
                obj.subscriptions
                .select_related('plan')
                .order_by('-created_at')
                .first()
            )
        if sub is None:
            return None
        return SubscriptionBriefSerializer(sub).data


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
