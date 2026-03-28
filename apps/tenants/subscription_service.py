"""
Cytova — Subscription Service

Manages subscription lifecycle for tenants.

SubscriptionPlanService:  create, update, deactivate
SubscriptionService:      create_trial, activate, suspend, reactivate, cancel, expire_trials
"""
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import (
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
    Tenant,
    TERMINAL_SUBSCRIPTION_STATUSES,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SubscriptionPlanService
# ---------------------------------------------------------------------------

class SubscriptionPlanService:

    @staticmethod
    def create(validated_data: dict) -> SubscriptionPlan:
        code = validated_data.get('code', '').upper()
        validated_data['code'] = code
        plan = SubscriptionPlan(**validated_data)
        plan.save()
        logger.info('Subscription plan created: %s', code)
        return plan

    @staticmethod
    def update(plan: SubscriptionPlan, validated_data: dict) -> SubscriptionPlan:
        for field, value in validated_data.items():
            setattr(plan, field, value)
        plan.save(update_fields=list(validated_data.keys()) + ['updated_at'])
        logger.info('Subscription plan updated: %s', plan.code)
        return plan

    @staticmethod
    def deactivate(plan: SubscriptionPlan) -> SubscriptionPlan:
        if not plan.is_active:
            return plan
        plan.is_active = False
        plan.save(update_fields=['is_active', 'updated_at'])
        logger.info('Subscription plan deactivated: %s', plan.code)
        return plan


# ---------------------------------------------------------------------------
# SubscriptionService
# ---------------------------------------------------------------------------

class SubscriptionService:

    @staticmethod
    def get_default_trial_plan() -> SubscriptionPlan:
        """
        Resolve the default trial plan from the database.
        Raises ValidationError if no active trial plan exists.
        """
        plan = SubscriptionPlan.objects.filter(
            is_trial=True,
            is_active=True,
        ).first()
        if plan is None:
            raise ValidationError(
                'No active trial plan found in the database. '
                'Run: python manage.py seed_plans'
            )
        return plan

    @staticmethod
    def create_trial(
        tenant: Tenant,
        plan: SubscriptionPlan | None = None,
    ) -> Subscription:
        """
        Create a TRIAL subscription for a newly onboarded tenant.

        If `plan` is None, the default trial plan is resolved from the DB.
        Trial duration comes from the plan's `trial_duration_days` field.
        """
        if plan is None:
            plan = SubscriptionService.get_default_trial_plan()

        if not plan.trial_duration_days:
            raise ValidationError(
                f'Plan {plan.code} has no trial_duration_days set.'
            )

        existing = Subscription.objects.filter(
            tenant=tenant,
            status__in=[SubscriptionStatus.TRIAL, SubscriptionStatus.ACTIVE],
        ).exists()
        if existing:
            raise ValidationError(
                'Tenant already has an active or trial subscription.'
            )

        now = timezone.now()
        trial_end = now + timedelta(days=plan.trial_duration_days)

        subscription = Subscription.objects.create(
            tenant=tenant,
            plan=plan,
            status=SubscriptionStatus.TRIAL,
            started_at=now,
            trial_end_date=trial_end,
        )

        logger.info(
            'Trial subscription created: tenant=%s plan=%s trial_end=%s duration=%dd',
            tenant.subdomain, plan.code, trial_end.date(), plan.trial_duration_days,
        )

        return subscription

    @staticmethod
    def activate(
        subscription: Subscription,
        period_months: int = 1,
        notes: str = '',
    ) -> Subscription:
        """
        Transition TRIAL or EXPIRED → ACTIVE.
        Sets the billing period end based on period_months.
        In the future, this is called after payment confirmation.
        """
        if subscription.status not in (
            SubscriptionStatus.TRIAL,
            SubscriptionStatus.EXPIRED,
        ):
            raise ValidationError(
                f'Cannot activate a subscription in {subscription.status} status.'
            )

        now = timezone.now()
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.activated_at = now
        subscription.current_period_end = now + timedelta(days=30 * period_months)
        subscription.trial_end_date = None
        if notes:
            subscription.notes = notes
        subscription.save(update_fields=[
            'status', 'activated_at', 'current_period_end',
            'trial_end_date', 'notes', 'updated_at',
        ])

        # Sync tenant.plan with subscription plan code
        SubscriptionService._sync_tenant_plan(subscription)

        logger.info(
            'Subscription activated: tenant=%s plan=%s',
            subscription.tenant.subdomain, subscription.plan.code,
        )

        return subscription

    @staticmethod
    def suspend(
        subscription: Subscription,
        reason: str = '',
    ) -> Subscription:
        """
        Suspend an ACTIVE subscription (e.g. non-payment, admin action).
        The tenant remains in the DB but should be blocked from access.
        """
        if subscription.status != SubscriptionStatus.ACTIVE:
            raise ValidationError(
                f'Only ACTIVE subscriptions can be suspended (current: {subscription.status}).'
            )

        subscription.status = SubscriptionStatus.SUSPENDED
        subscription.suspended_at = timezone.now()
        if reason:
            subscription.notes = reason
        subscription.save(update_fields=[
            'status', 'suspended_at', 'notes', 'updated_at',
        ])

        logger.info(
            'Subscription suspended: tenant=%s reason=%s',
            subscription.tenant.subdomain, reason,
        )

        return subscription

    @staticmethod
    def reactivate(
        subscription: Subscription,
        period_months: int = 1,
    ) -> Subscription:
        """Reactivate a SUSPENDED subscription."""
        if subscription.status != SubscriptionStatus.SUSPENDED:
            raise ValidationError(
                f'Only SUSPENDED subscriptions can be reactivated (current: {subscription.status}).'
            )

        now = timezone.now()
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.suspended_at = None
        subscription.current_period_end = now + timedelta(days=30 * period_months)
        subscription.save(update_fields=[
            'status', 'suspended_at', 'current_period_end', 'updated_at',
        ])

        logger.info(
            'Subscription reactivated: tenant=%s',
            subscription.tenant.subdomain,
        )

        return subscription

    @staticmethod
    def cancel(
        subscription: Subscription,
        cancelled_by: str = 'admin',
        reason: str = '',
    ) -> Subscription:
        """
        Cancel a subscription. Terminal — cannot be undone.
        `cancelled_by` indicates the actor: "admin", "platform", or "system".
        """
        if subscription.status in TERMINAL_SUBSCRIPTION_STATUSES:
            raise ValidationError('Subscription is already cancelled.')

        subscription.status = SubscriptionStatus.CANCELLED
        subscription.cancelled_at = timezone.now()
        subscription.cancelled_by = cancelled_by
        if reason:
            subscription.notes = reason
        subscription.save(update_fields=[
            'status', 'cancelled_at', 'cancelled_by', 'notes', 'updated_at',
        ])

        logger.info(
            'Subscription cancelled: tenant=%s by=%s',
            subscription.tenant.subdomain, cancelled_by,
        )

        return subscription

    @staticmethod
    def change_plan(
        subscription: Subscription,
        new_plan: SubscriptionPlan,
    ) -> Subscription:
        """
        Change the plan on a TRIAL or ACTIVE subscription.
        Does not alter the billing period or status.
        """
        if subscription.status not in (
            SubscriptionStatus.TRIAL,
            SubscriptionStatus.ACTIVE,
        ):
            raise ValidationError(
                f'Plan can only be changed on TRIAL or ACTIVE subscriptions '
                f'(current: {subscription.status}).'
            )

        old_plan_code = subscription.plan.code
        subscription.plan = new_plan
        subscription.save(update_fields=['plan', 'updated_at'])

        SubscriptionService._sync_tenant_plan(subscription)

        logger.info(
            'Subscription plan changed: tenant=%s from=%s to=%s',
            subscription.tenant.subdomain, old_plan_code, new_plan.code,
        )

        return subscription

    @staticmethod
    def expire_trials() -> int:
        """
        Batch job: expire all TRIAL subscriptions past their trial_end_date.
        Designed to be called by a Celery Beat task.
        Returns the number of subscriptions expired.
        """
        now = timezone.now()
        expired_qs = Subscription.objects.filter(
            status=SubscriptionStatus.TRIAL,
            trial_end_date__lte=now,
        )
        count = expired_qs.update(
            status=SubscriptionStatus.EXPIRED,
            updated_at=now,
        )
        if count:
            logger.info('Expired %d trial subscriptions', count)
        return count

    @staticmethod
    def expire_active() -> int:
        """
        Batch job: expire all ACTIVE subscriptions past their current_period_end.
        Returns the number of subscriptions expired.
        """
        now = timezone.now()
        expired_qs = Subscription.objects.filter(
            status=SubscriptionStatus.ACTIVE,
            current_period_end__isnull=False,
            current_period_end__lte=now,
        )
        count = expired_qs.update(
            status=SubscriptionStatus.EXPIRED,
            updated_at=now,
        )
        if count:
            logger.info('Expired %d active subscriptions', count)
        return count

    @staticmethod
    def _sync_tenant_plan(subscription: Subscription) -> None:
        """
        Keep Tenant.plan in sync with the subscription's plan code.
        Best-effort — if the plan code doesn't map to a Plan choice, skip.
        """
        from .models import Plan
        code = subscription.plan.code
        valid_codes = {c.value for c in Plan}
        if code in valid_codes:
            Tenant.objects.filter(pk=subscription.tenant_id).update(plan=code)
