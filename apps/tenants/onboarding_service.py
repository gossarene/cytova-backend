"""
Cytova — Laboratory Onboarding Service

Handles the complete self-service signup flow:
1. Create Tenant + Domain in the public schema
2. Resolve the default trial plan and create a Subscription
3. Switch to the new tenant schema
4. Create the initial LAB_ADMIN StaffUser
5. Seed default bootstrap data
6. Write audit logs (tenant + subscription + admin user)

Note on atomicity: tenant.save() triggers DDL (CREATE SCHEMA) which
auto-commits in PostgreSQL and cannot be rolled back. The post-DDL
operations (domain, subscription, user, seed) run in a regular
transaction context within the new schema. If any of those fail, the
tenant+schema exist but are empty — a cleanup task can garbage-collect
tenants with no admin user.
"""
import logging

from django.conf import settings
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.tenants.models import Tenant, Domain, Plan

logger = logging.getLogger(__name__)


class OnboardingResult:
    """Value object returned by OnboardingService.signup()."""
    __slots__ = ('tenant', 'domain', 'admin_user', 'subscription')

    def __init__(self, tenant, domain, admin_user, subscription):
        self.tenant = tenant
        self.domain = domain
        self.admin_user = admin_user
        self.subscription = subscription


class OnboardingService:

    @staticmethod
    def signup(validated_data: dict) -> OnboardingResult:
        """
        Full self-service signup flow. See module docstring for details.
        """
        from .subscription_service import SubscriptionService

        slug = validated_data['slug']
        schema_name = f'schema_{slug}'
        laboratory_name = validated_data['laboratory_name']

        # ---- Step 1: Resolve trial plan BEFORE creating anything ----
        # Fail fast if no trial plan is configured.
        trial_plan = SubscriptionService.get_default_trial_plan()

        # ---- Step 2: Create Tenant + Domain in public schema ----
        # The 4-digit numeric code is allocated automatically by
        # ``Tenant.save()`` (see TenantCodeAllocator).
        tenant = Tenant(
            schema_name=schema_name,
            name=laboratory_name,
            subdomain=slug,
            plan=Plan.STARTER,
            is_active=True,
            activated_at=timezone.now(),
        )
        tenant.save()  # DDL: CREATE SCHEMA + run migrations

        primary_domain_name = f'{slug}.{settings.CYTOVA_DOMAIN}'
        Domain.objects.create(
            domain=primary_domain_name,
            tenant=tenant,
            is_primary=True,
        )

        # ---- Step 3: Create trial subscription ----
        subscription = SubscriptionService.create_trial(
            tenant=tenant,
            plan=trial_plan,
        )

        # ---- Step 4: Create admin user inside the tenant schema ----
        with schema_context(schema_name):
            from apps.users.models import StaffUser, Role

            admin_user = StaffUser.objects.create_user(
                email=validated_data['admin_email'],
                password=validated_data['admin_password'],
                first_name=validated_data['admin_first_name'],
                last_name=validated_data['admin_last_name'],
                role=Role.LAB_ADMIN,
                is_staff=True,
                is_superuser=True,
            )

            # ---- Step 5: Seed bootstrap data ----
            OnboardingService._seed_defaults()

            # ---- Step 6: Audit logs ----
            OnboardingService._audit_onboarding(
                tenant, admin_user, subscription,
            )

        logger.info(
            'Laboratory onboarded: name=%s slug=%s admin=%s plan=%s trial_days=%d',
            laboratory_name, slug, admin_user.email,
            trial_plan.code, trial_plan.trial_duration_days,
        )

        return OnboardingResult(
            tenant=tenant,
            domain=primary_domain_name,
            admin_user=admin_user,
            subscription=subscription,
        )

    @staticmethod
    def _seed_defaults():
        """
        Create default data in the newly provisioned tenant schema.
        Called inside schema_context of the new tenant.
        """
        from apps.catalog.models import ExamCategory, ExamFamily

        default_families = [
            ('Hematology', 1),
            ('Biochemistry', 2),
            ('Microbiology', 3),
            ('Immunology', 4),
            ('Parasitology', 5),
        ]
        for name, order in default_families:
            ExamFamily.objects.get_or_create(
                name=name,
                defaults={'display_order': order},
            )
            # Legacy category kept for backward compatibility
            ExamCategory.objects.get_or_create(
                name=name,
                defaults={'display_order': order},
            )

        from apps.stock.models import StockCategory

        default_stock_categories = [
            ('Reagents', 1),
            ('Consumables', 2),
            ('Equipment', 3),
        ]
        for name, order in default_stock_categories:
            StockCategory.objects.get_or_create(
                name=name,
                defaults={'display_order': order},
            )

    @staticmethod
    def _audit_onboarding(tenant, admin_user, subscription):
        """
        Write audit log entries in the new tenant schema.
        Called inside schema_context of the new tenant.
        """
        from apps.audit.models import AuditLog, AuditAction, ActorType

        AuditLog.objects.create(
            actor_type=ActorType.SYSTEM,
            actor_id=admin_user.id,
            actor_email=admin_user.email,
            action=AuditAction.CREATE,
            entity_type='TenantOnboarding',
            entity_id=tenant.id,
            diff={
                'tenant_name': tenant.name,
                'subdomain': tenant.subdomain,
                'admin_email': admin_user.email,
                'plan_code': subscription.plan.code,
                'subscription_status': subscription.status,
                'trial_end_date': str(subscription.trial_end_date),
                'trial_duration_days': subscription.plan.trial_duration_days,
            },
        )
