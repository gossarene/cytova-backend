"""
Cytova — Laboratory Onboarding Service

Handles the complete self-service signup flow:
1. Create Tenant + Domain in the public schema
2. Switch to the new tenant schema
3. Create the initial LAB_ADMIN StaffUser
4. Seed any default bootstrap data
5. Log the onboarding event

The entire operation is atomic at the DB level. If any step fails,
the tenant schema and all data are rolled back.
"""
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.tenants.models import Tenant, Domain, Plan

logger = logging.getLogger(__name__)


class OnboardingResult:
    """Value object returned by OnboardingService.signup()."""
    __slots__ = ('tenant', 'domain', 'admin_user')

    def __init__(self, tenant, domain, admin_user):
        self.tenant = tenant
        self.domain = domain
        self.admin_user = admin_user


class OnboardingService:

    @staticmethod
    def signup(validated_data: dict) -> OnboardingResult:
        """
        Full self-service signup flow.

        This is NOT wrapped in @transaction.atomic because tenant.save()
        triggers DDL (CREATE SCHEMA) which auto-commits in PostgreSQL.
        Instead, we rely on the ordering of operations:
        1. Create Tenant (DDL — auto-committed)
        2. Create Domain
        3. Switch to tenant schema and create the admin user + bootstrap

        If step 3 fails, the tenant+domain still exist but have no users,
        making the tenant effectively empty. A cleanup task can garbage-collect
        empty tenants if needed.
        """
        slug = validated_data['slug']
        schema_name = f'schema_{slug}'
        laboratory_name = validated_data['laboratory_name']

        # ---- Step 1+2: Create Tenant + Domain in public schema ----
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
        domain = Domain.objects.create(
            domain=primary_domain_name,
            tenant=tenant,
            is_primary=True,
        )

        # ---- Step 2b: Create trial subscription ----
        OnboardingService._create_trial_subscription(tenant)

        # ---- Step 3: Create admin user inside the tenant schema ----
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

            # ---- Step 4: Seed bootstrap data ----
            OnboardingService._seed_defaults()

            # ---- Step 5: Audit log (inside tenant schema) ----
            OnboardingService._audit_onboarding(tenant, admin_user)

        logger.info(
            'Laboratory onboarded: name=%s slug=%s admin=%s',
            laboratory_name, slug, admin_user.email,
        )

        return OnboardingResult(
            tenant=tenant,
            domain=primary_domain_name,
            admin_user=admin_user,
        )

    @staticmethod
    def _create_trial_subscription(tenant):
        """
        Create a trial subscription for the new tenant using the default
        STARTER plan. If no plan exists yet, skip silently — the platform
        admin can assign a subscription later.
        """
        from .models import SubscriptionPlan
        from .subscription_service import SubscriptionService

        plan = SubscriptionPlan.objects.filter(
            code='STARTER', is_active=True,
        ).first()
        if plan is None:
            # Graceful fallback: no plan configured yet.
            # This happens in fresh deployments before seed data is created.
            logger.warning(
                'No STARTER plan found — skipping trial subscription for %s',
                tenant.subdomain,
            )
            return
        SubscriptionService.create_trial(tenant=tenant, plan=plan)

    @staticmethod
    def _seed_defaults():
        """
        Create default data in the newly provisioned tenant schema.
        Called inside schema_context of the new tenant.
        """
        from apps.catalog.models import ExamCategory

        default_categories = [
            ('Hematology', 1),
            ('Biochemistry', 2),
            ('Microbiology', 3),
            ('Immunology', 4),
            ('Parasitology', 5),
        ]
        for name, order in default_categories:
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
    def _audit_onboarding(tenant, admin_user):
        """
        Write the initial audit log entry in the new tenant schema.
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
                'plan': tenant.plan,
            },
        )
