"""
Tests for subscription lifecycle — create trial, activate, suspend,
reactivate, cancel, change plan, batch expire.
"""
import pytest
from datetime import timedelta

from django.utils import timezone

from apps.tenants.models import (
    Domain,
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
    Tenant,
)
from apps.tenants.subscription_service import SubscriptionPlanService, SubscriptionService
from rest_framework.exceptions import ValidationError


@pytest.fixture(autouse=True)
def _in_tenant_schema():
    yield


@pytest.fixture()
def starter_plan():
    return SubscriptionPlan.objects.create(
        code='STARTER', name='Starter',
        is_trial=True, trial_duration_days=14,
        monthly_price='29.00', yearly_price='290.00',
    )


@pytest.fixture()
def pro_plan():
    return SubscriptionPlan.objects.create(
        code='PRO', name='Pro',
        is_trial=False, trial_duration_days=None,
        monthly_price='99.00', yearly_price='990.00',
    )


@pytest.fixture()
def tenant_with_trial(starter_plan):
    tenant = Tenant(name='Lifecycle Lab', subdomain='lifecycle-lab', schema_name='schema_lifecycle_lab')
    tenant.save()
    Domain.objects.create(domain='lifecycle-lab.localhost', tenant=tenant, is_primary=True)
    sub = SubscriptionService.create_trial(tenant, starter_plan)
    return tenant, sub


# ---------------------------------------------------------------------------
# Trial creation
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestTrialCreation:

    def test_create_trial(self, starter_plan):
        tenant = Tenant(name='Trial Lab', subdomain='trial-lab', schema_name='schema_trial_lab')
        tenant.save()
        Domain.objects.create(domain='trial-lab.localhost', tenant=tenant, is_primary=True)

        sub = SubscriptionService.create_trial(tenant, starter_plan)

        assert sub.status == SubscriptionStatus.TRIAL
        assert sub.plan == starter_plan
        assert sub.trial_end_date is not None
        assert sub.trial_days_remaining is not None
        assert sub.trial_days_remaining <= 14
        assert sub.is_usable is True
        assert sub.activated_at is None

    def test_trial_duration_matches_plan(self, tenant_with_trial, starter_plan):
        _, sub = tenant_with_trial
        expected_end = sub.started_at + timedelta(days=starter_plan.trial_duration_days)
        delta = abs((sub.trial_end_date - expected_end).total_seconds())
        assert delta < 2  # within 2 seconds

    def test_duplicate_trial_prevented(self, tenant_with_trial, starter_plan):
        tenant, _ = tenant_with_trial
        with pytest.raises(ValidationError, match='already has an active'):
            SubscriptionService.create_trial(tenant, starter_plan)


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestActivation:

    def test_activate_trial(self, tenant_with_trial):
        _, sub = tenant_with_trial
        activated = SubscriptionService.activate(sub, period_months=1)

        assert activated.status == SubscriptionStatus.ACTIVE
        assert activated.activated_at is not None
        assert activated.trial_end_date is None
        assert activated.current_period_end is not None
        assert activated.is_usable is True

    def test_activate_expired(self, tenant_with_trial):
        _, sub = tenant_with_trial
        # Force to expired
        Subscription.objects.filter(pk=sub.pk).update(status=SubscriptionStatus.EXPIRED)
        sub.refresh_from_db()

        activated = SubscriptionService.activate(sub, period_months=12, notes='Annual payment')

        assert activated.status == SubscriptionStatus.ACTIVE
        assert activated.notes == 'Annual payment'

    def test_activate_already_active_rejected(self, tenant_with_trial):
        _, sub = tenant_with_trial
        SubscriptionService.activate(sub)
        sub.refresh_from_db()

        with pytest.raises(ValidationError, match='Cannot activate'):
            SubscriptionService.activate(sub)

    def test_activate_cancelled_rejected(self, tenant_with_trial):
        _, sub = tenant_with_trial
        SubscriptionService.cancel(sub)
        sub.refresh_from_db()

        with pytest.raises(ValidationError, match='Cannot activate'):
            SubscriptionService.activate(sub)

    def test_period_end_respects_months(self, tenant_with_trial):
        _, sub = tenant_with_trial
        before = timezone.now()
        activated = SubscriptionService.activate(sub, period_months=6)
        expected_end = before + timedelta(days=180)
        delta = abs((activated.current_period_end - expected_end).total_seconds())
        assert delta < 5


# ---------------------------------------------------------------------------
# Suspension
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestSuspension:

    def test_suspend_active(self, tenant_with_trial):
        _, sub = tenant_with_trial
        SubscriptionService.activate(sub)
        sub.refresh_from_db()

        suspended = SubscriptionService.suspend(sub, reason='Non-payment')

        assert suspended.status == SubscriptionStatus.SUSPENDED
        assert suspended.suspended_at is not None
        assert suspended.notes == 'Non-payment'
        assert suspended.is_usable is False

    def test_suspend_trial_rejected(self, tenant_with_trial):
        _, sub = tenant_with_trial
        with pytest.raises(ValidationError, match='Only ACTIVE'):
            SubscriptionService.suspend(sub)

    def test_suspend_suspended_rejected(self, tenant_with_trial):
        _, sub = tenant_with_trial
        SubscriptionService.activate(sub)
        sub.refresh_from_db()
        SubscriptionService.suspend(sub)
        sub.refresh_from_db()

        with pytest.raises(ValidationError, match='Only ACTIVE'):
            SubscriptionService.suspend(sub)


# ---------------------------------------------------------------------------
# Reactivation
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestReactivation:

    def test_reactivate_suspended(self, tenant_with_trial):
        _, sub = tenant_with_trial
        SubscriptionService.activate(sub)
        sub.refresh_from_db()
        SubscriptionService.suspend(sub)
        sub.refresh_from_db()

        reactivated = SubscriptionService.reactivate(sub)

        assert reactivated.status == SubscriptionStatus.ACTIVE
        assert reactivated.suspended_at is None
        assert reactivated.current_period_end is not None
        assert reactivated.is_usable is True

    def test_reactivate_active_rejected(self, tenant_with_trial):
        _, sub = tenant_with_trial
        SubscriptionService.activate(sub)
        sub.refresh_from_db()

        with pytest.raises(ValidationError, match='Only SUSPENDED'):
            SubscriptionService.reactivate(sub)


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestCancellation:

    def test_cancel_trial(self, tenant_with_trial):
        _, sub = tenant_with_trial
        cancelled = SubscriptionService.cancel(sub, cancelled_by='admin', reason='Changed mind')

        assert cancelled.status == SubscriptionStatus.CANCELLED
        assert cancelled.cancelled_at is not None
        assert cancelled.cancelled_by == 'admin'
        assert cancelled.is_usable is False

    def test_cancel_active(self, tenant_with_trial):
        _, sub = tenant_with_trial
        SubscriptionService.activate(sub)
        sub.refresh_from_db()

        cancelled = SubscriptionService.cancel(sub, cancelled_by='platform')
        assert cancelled.status == SubscriptionStatus.CANCELLED

    def test_cancel_is_terminal(self, tenant_with_trial):
        _, sub = tenant_with_trial
        SubscriptionService.cancel(sub)
        sub.refresh_from_db()

        with pytest.raises(ValidationError, match='already cancelled'):
            SubscriptionService.cancel(sub)


# ---------------------------------------------------------------------------
# Plan change
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestPlanChange:

    def test_change_plan_on_trial(self, tenant_with_trial, pro_plan):
        _, sub = tenant_with_trial
        changed = SubscriptionService.change_plan(sub, pro_plan)

        assert changed.plan == pro_plan
        assert changed.status == SubscriptionStatus.TRIAL  # unchanged

    def test_change_plan_on_active(self, tenant_with_trial, pro_plan):
        _, sub = tenant_with_trial
        SubscriptionService.activate(sub)
        sub.refresh_from_db()

        changed = SubscriptionService.change_plan(sub, pro_plan)
        assert changed.plan == pro_plan

    def test_change_plan_on_suspended_rejected(self, tenant_with_trial, pro_plan):
        _, sub = tenant_with_trial
        SubscriptionService.activate(sub)
        sub.refresh_from_db()
        SubscriptionService.suspend(sub)
        sub.refresh_from_db()

        with pytest.raises(ValidationError, match='TRIAL or ACTIVE'):
            SubscriptionService.change_plan(sub, pro_plan)

    def test_change_plan_on_cancelled_rejected(self, tenant_with_trial, pro_plan):
        _, sub = tenant_with_trial
        SubscriptionService.cancel(sub)
        sub.refresh_from_db()

        with pytest.raises(ValidationError, match='TRIAL or ACTIVE'):
            SubscriptionService.change_plan(sub, pro_plan)


# ---------------------------------------------------------------------------
# Batch expiration
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestBatchExpiration:

    def test_expire_trials_past_end_date(self, starter_plan):
        tenant = Tenant(name='Expired Trial', subdomain='exp-trial', schema_name='schema_exp_trial')
        tenant.save()
        Domain.objects.create(domain='exp-trial.localhost', tenant=tenant, is_primary=True)

        sub = SubscriptionService.create_trial(tenant, starter_plan)
        # Backdate trial_end_date to yesterday
        Subscription.objects.filter(pk=sub.pk).update(
            trial_end_date=timezone.now() - timedelta(days=1),
        )

        count = SubscriptionService.expire_trials()
        assert count == 1

        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.EXPIRED

    def test_expire_trials_skips_active(self, tenant_with_trial):
        _, sub = tenant_with_trial
        # Still within trial period — should not expire
        count = SubscriptionService.expire_trials()
        assert count == 0
        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.TRIAL

    def test_expire_active_past_period_end(self, tenant_with_trial):
        _, sub = tenant_with_trial
        SubscriptionService.activate(sub, period_months=1)
        sub.refresh_from_db()

        # Backdate period end to yesterday
        Subscription.objects.filter(pk=sub.pk).update(
            current_period_end=timezone.now() - timedelta(days=1),
        )

        count = SubscriptionService.expire_active()
        assert count == 1

        sub.refresh_from_db()
        assert sub.status == SubscriptionStatus.EXPIRED
