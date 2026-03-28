"""
Cytova — Subscription Serializers (Platform Admin API)
"""
from decimal import Decimal

from rest_framework import serializers

from .models import Subscription, SubscriptionPlan, SubscriptionStatus


# ---------------------------------------------------------------------------
# SubscriptionPlan
# ---------------------------------------------------------------------------

class SubscriptionPlanListSerializer(serializers.ModelSerializer):
    class Meta:
        model = SubscriptionPlan
        fields = [
            'id', 'code', 'name', 'description',
            'monthly_price', 'yearly_price', 'trial_days',
            'display_order', 'is_active', 'created_at',
        ]


class SubscriptionPlanDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = SubscriptionPlan
        fields = [
            'id', 'code', 'name', 'description',
            'monthly_price', 'yearly_price', 'trial_days',
            'features', 'display_order', 'is_active',
            'created_at', 'updated_at',
        ]


class SubscriptionPlanCreateSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=30)
    name = serializers.CharField(max_length=100)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    monthly_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, allow_null=True,
        min_value=Decimal('0'),
    )
    yearly_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, allow_null=True,
        min_value=Decimal('0'),
    )
    trial_days = serializers.IntegerField(required=False, default=14, min_value=0)
    features = serializers.JSONField(required=False, default=dict)
    display_order = serializers.IntegerField(required=False, default=0)

    def validate_code(self, value):
        code = value.upper()
        if SubscriptionPlan.objects.filter(code=code).exists():
            raise serializers.ValidationError(
                'A plan with this code already exists.'
            )
        return code


class SubscriptionPlanUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    monthly_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, allow_null=True,
        min_value=Decimal('0'),
    )
    yearly_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, allow_null=True,
        min_value=Decimal('0'),
    )
    trial_days = serializers.IntegerField(required=False, min_value=0)
    features = serializers.JSONField(required=False)
    display_order = serializers.IntegerField(required=False)


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------

class SubscriptionListSerializer(serializers.ModelSerializer):
    tenant_name = serializers.CharField(source='tenant.name', read_only=True)
    tenant_subdomain = serializers.CharField(source='tenant.subdomain', read_only=True)
    plan_code = serializers.CharField(source='plan.code', read_only=True)
    plan_name = serializers.CharField(source='plan.name', read_only=True)
    trial_days_remaining = serializers.IntegerField(read_only=True)

    class Meta:
        model = Subscription
        fields = [
            'id', 'tenant_id', 'tenant_name', 'tenant_subdomain',
            'plan_id', 'plan_code', 'plan_name',
            'status', 'started_at', 'trial_end_date',
            'current_period_end', 'trial_days_remaining',
            'created_at',
        ]


class SubscriptionDetailSerializer(serializers.ModelSerializer):
    tenant_name = serializers.CharField(source='tenant.name', read_only=True)
    tenant_subdomain = serializers.CharField(source='tenant.subdomain', read_only=True)
    plan = SubscriptionPlanListSerializer(read_only=True)
    trial_days_remaining = serializers.IntegerField(read_only=True)
    is_usable = serializers.BooleanField(read_only=True)

    class Meta:
        model = Subscription
        fields = [
            'id', 'tenant_id', 'tenant_name', 'tenant_subdomain',
            'plan', 'status',
            'started_at', 'trial_end_date', 'current_period_end',
            'activated_at', 'suspended_at',
            'cancelled_at', 'cancelled_by',
            'trial_days_remaining', 'is_usable',
            'notes', 'created_at', 'updated_at',
        ]


class SubscriptionActivateSerializer(serializers.Serializer):
    period_months = serializers.IntegerField(default=1, min_value=1, max_value=36)
    notes = serializers.CharField(required=False, allow_blank=True, default='')


class SubscriptionSuspendSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default='')


class SubscriptionCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default='')


class SubscriptionChangePlanSerializer(serializers.Serializer):
    plan_id = serializers.UUIDField()

    def validate_plan_id(self, value):
        if not SubscriptionPlan.objects.filter(id=value, is_active=True).exists():
            raise serializers.ValidationError('Plan not found or inactive.')
        return value
