"""
Cytova — Subscription Celery Tasks

Periodic tasks for subscription lifecycle management.
These run in the public schema (subscriptions are platform-level).
"""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(
    name='subscriptions.expire_trials',
    ignore_result=True,
)
def expire_trial_subscriptions():
    """
    Periodic: expire all TRIAL subscriptions past their trial_end_date.
    Schedule via Celery Beat (e.g. every hour).
    """
    from .subscription_service import SubscriptionService

    count = SubscriptionService.expire_trials()
    logger.info('Trial expiration task complete: %d expired', count)
    return count


@shared_task(
    name='subscriptions.expire_active',
    ignore_result=True,
)
def expire_active_subscriptions():
    """
    Periodic: expire all ACTIVE subscriptions past their current_period_end.
    Schedule via Celery Beat (e.g. daily).
    """
    from .subscription_service import SubscriptionService

    count = SubscriptionService.expire_active()
    logger.info('Active expiration task complete: %d expired', count)
    return count
