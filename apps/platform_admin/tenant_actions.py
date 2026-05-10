"""
Service layer for the platform-admin tenant action surface.

Suspend / reactivate flow through the legacy ``TenantService`` so we
have one canonical implementation of those state mutations — the
platform-admin layer adds authn / audit on top of that path.

Trial extension and plan change live here because they don't have
an existing helper that fits cleanly:

  - ``extend_trial`` adds ``days`` to the latest TRIAL subscription's
    ``trial_end_date``. Reversible via a negative-days call (not
    exposed) and the prior value is recoverable from the audit
    ``before`` snapshot.

  - ``change_plan`` closes the current TRIAL / ACTIVE subscription
    by marking it EXPIRED and inserts a new ACTIVE subscription on
    the requested plan. Reversible: ``SubscriptionService.activate``
    accepts EXPIRED → ACTIVE so a misclick can be undone by
    activating the prior row and cancelling the new one. The audit
    row records both subscription ids so that recovery doesn't rely
    on guessing.

All public functions are wrapped in ``transaction.atomic`` so a
mid-flight DB error rolls everything back rather than leaving the
tenant with half a state change.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.tenants.models import (
    Subscription, SubscriptionPlan, SubscriptionStatus, Tenant,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class NoActiveTrialError(ValidationError):
    """Raised by ``extend_trial`` when the tenant has no TRIAL subscription.

    Subclass of DRF's ``ValidationError`` so the existing exception
    handler maps it to a 400 with the standard envelope.
    """

    def __init__(self):
        super().__init__({
            'detail': 'Tenant has no TRIAL subscription to extend.',
            'code': 'NO_ACTIVE_TRIAL',
        })


# ---------------------------------------------------------------------------
# Result carriers — keep the audit path independent of the ORM
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExtendTrialResult:
    subscription: Subscription
    before_trial_end: object  # datetime — typed loose to allow mocks
    after_trial_end: object


@dataclass(frozen=True)
class ChangePlanResult:
    new_subscription: Subscription
    previous_subscription: Subscription | None
    previous_plan_code: str | None
    new_plan_code: str


# ---------------------------------------------------------------------------
# extend_trial
# ---------------------------------------------------------------------------

@transaction.atomic
def extend_trial(tenant: Tenant, *, days: int) -> ExtendTrialResult:
    """Extend the tenant's latest TRIAL subscription by ``days``.

    "Latest" is by ``created_at`` desc — chosen instead of any
    "active" filter because the platform-admin surface is the
    authority for trial state and we want the operator to see the
    same row in the list view and act on it here. Locking it with
    ``select_for_update`` serialises concurrent extension calls
    against each other and against any background trial-expiry
    job that might race us at the row level.

    Validation of ``days`` (positive, capped) lives in
    ``ExtendTrialSerializer`` — by the time we get here the value
    is trusted.
    """
    subscription = (
        Subscription.objects
        .select_for_update()
        .filter(tenant=tenant, status=SubscriptionStatus.TRIAL)
        .order_by('-created_at')
        .first()
    )
    if subscription is None:
        raise NoActiveTrialError()

    before_trial_end = subscription.trial_end_date
    # Anchor extensions to "now" when the trial has already lapsed
    # so the new trial_end_date isn't still in the past — that would
    # make the extension an audit-only no-op from the tenant's
    # perspective. Anchor to the existing end otherwise so a
    # mid-trial extension truly adds days.
    base = subscription.trial_end_date or timezone.now()
    if base < timezone.now():
        base = timezone.now()
    subscription.trial_end_date = base + timedelta(days=days)
    subscription.save(update_fields=['trial_end_date', 'updated_at'])

    return ExtendTrialResult(
        subscription=subscription,
        before_trial_end=before_trial_end,
        after_trial_end=subscription.trial_end_date,
    )


# ---------------------------------------------------------------------------
# change_plan
# ---------------------------------------------------------------------------

@transaction.atomic
def change_plan(tenant: Tenant, *, new_plan: SubscriptionPlan) -> ChangePlanResult:
    """Switch the tenant onto ``new_plan`` by inserting a new ACTIVE
    Subscription row.

    Behaviour:
      1. The current TRIAL / ACTIVE subscription (if any) is closed
         by marking it ``EXPIRED``. EXPIRED is reversible via
         ``SubscriptionService.activate``, so a misclick is recoverable.
         CANCELLED would have been terminal — we deliberately don't
         use it here.
      2. A new ``Subscription`` row is created with status=ACTIVE on
         ``new_plan``. Idempotent guard: if the only existing usable
         subscription is already on ``new_plan``, we skip the close +
         insert and return the existing row so that an operator
         double-clicking the action doesn't accumulate parallel rows.

    Concurrency:
      ``select_for_update`` on the prior subscription plus the
      enclosing ``transaction.atomic`` keep concurrent calls from
      racing into the multi-active-row state.
    """
    if not new_plan.is_active:
        # Defence in depth — the serializer's queryset already filters
        # ``is_active=True``, but a plan can be deactivated between
        # validation and use. We refuse rather than silently routing
        # the tenant onto a frozen plan.
        raise ValidationError({
            'plan_id': 'Plan is not active.',
            'code': 'PLAN_INACTIVE',
        })

    previous = (
        Subscription.objects
        .select_for_update()
        .filter(
            tenant=tenant,
            status__in=(SubscriptionStatus.TRIAL, SubscriptionStatus.ACTIVE),
        )
        .order_by('-created_at')
        .first()
    )

    if previous is not None and previous.plan_id == new_plan.id:
        # Already on the requested plan — no-op for the data layer,
        # but the caller still gets an audit row from the view so
        # the operator action is visible.
        return ChangePlanResult(
            new_subscription=previous,
            previous_subscription=None,
            previous_plan_code=None,
            new_plan_code=new_plan.code,
        )

    previous_plan_code = previous.plan.code if previous else None

    if previous is not None:
        # EXPIRED is reversible (EXPIRED → ACTIVE permitted by
        # ``SubscriptionService.activate``) and matches "the prior
        # plan period has ended" semantically more accurately than
        # SUSPENDED would.
        previous.status = SubscriptionStatus.EXPIRED
        previous.trial_end_date = None
        previous.save(update_fields=[
            'status', 'trial_end_date', 'updated_at',
        ])

    now = timezone.now()
    new_subscription = Subscription.objects.create(
        tenant=tenant,
        plan=new_plan,
        status=SubscriptionStatus.ACTIVE,
        started_at=now,
        activated_at=now,
    )

    return ChangePlanResult(
        new_subscription=new_subscription,
        previous_subscription=previous,
        previous_plan_code=previous_plan_code,
        new_plan_code=new_plan.code,
    )
