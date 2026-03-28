"""
Cytova — Seed Subscription Plans

Idempotent management command that creates the default subscription plans.
Safe to run multiple times — existing plans are updated, not duplicated.

Usage:
    python manage.py seed_plans
    python manage.py seed_plans --verbosity 2
"""
from django.core.management.base import BaseCommand

from apps.tenants.models import SubscriptionPlan


# Plan definitions: code is the unique key for idempotent upsert.
DEFAULT_PLANS = [
    {
        'code': 'TRIAL',
        'name': 'Free Trial',
        'description': (
            'Full-featured trial for new laboratories. '
            'No credit card required.'
        ),
        'is_trial': True,
        'is_public': False,
        'trial_duration_days': 14,
        'monthly_price': None,
        'yearly_price': None,
        'features': {
            'max_users': 5,
            'max_patients': 100,
            'max_exams': 500,
        },
        'display_order': 0,
    },
    {
        'code': 'STARTER',
        'name': 'Starter',
        'description': 'For small laboratories getting started with Cytova.',
        'is_trial': False,
        'is_public': True,
        'trial_duration_days': None,
        'monthly_price': '49.00',
        'yearly_price': '490.00',
        'features': {
            'max_users': 10,
            'max_patients': 1000,
        },
        'display_order': 1,
    },
    {
        'code': 'PRO',
        'name': 'Professional',
        'description': 'For growing laboratories that need advanced features.',
        'is_trial': False,
        'is_public': True,
        'trial_duration_days': None,
        'monthly_price': '149.00',
        'yearly_price': '1490.00',
        'features': {
            'max_users': 50,
            'max_patients': None,  # unlimited
        },
        'display_order': 2,
    },
    {
        'code': 'ENTERPRISE',
        'name': 'Enterprise',
        'description': 'For large laboratories with custom requirements.',
        'is_trial': False,
        'is_public': True,
        'trial_duration_days': None,
        'monthly_price': None,  # custom pricing
        'yearly_price': None,
        'features': {
            'max_users': None,  # unlimited
            'max_patients': None,
        },
        'display_order': 3,
    },
]


class Command(BaseCommand):
    help = 'Seed default subscription plans (idempotent).'

    def handle(self, *args, **options):
        verbosity = options.get('verbosity', 1)

        for plan_data in DEFAULT_PLANS:
            code = plan_data['code']
            defaults = {k: v for k, v in plan_data.items() if k != 'code'}

            plan, created = SubscriptionPlan.objects.update_or_create(
                code=code,
                defaults=defaults,
            )

            if verbosity >= 1:
                action = 'Created' if created else 'Updated'
                trial_info = ''
                if plan.is_trial:
                    trial_info = f' (trial: {plan.trial_duration_days} days)'
                self.stdout.write(
                    f'  {action}: {plan.code} — {plan.name}{trial_info}'
                )

        if verbosity >= 1:
            self.stdout.write(self.style.SUCCESS(
                f'\nDone. {len(DEFAULT_PLANS)} plans seeded.'
            ))
