"""
Cytova — Tenant Service (Platform Admin)

Tenant provisioning, suspension, and reactivation.

Note on schema migrations: creating a Tenant via save() auto-creates the
PostgreSQL schema (auto_create_schema=True). Tenant-app migrations are applied
by running: python manage.py migrate_schemas --schema <schema_name>
This should be triggered as a Celery task after provisioning in production.
"""
import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.tenants.models import Tenant, Domain, Plan

logger = logging.getLogger(__name__)


class TenantService:

    @staticmethod
    def provision_tenant(validated_data: dict) -> Tenant:
        """
        Create a new Tenant + primary Domain in a single transaction.
        django-tenants auto-creates the PostgreSQL schema on tenant.save().
        """
        subdomain = validated_data['subdomain']
        schema_name = f'schema_{subdomain}'

        with transaction.atomic():
            tenant = Tenant(
                schema_name=schema_name,
                name=validated_data['name'],
                subdomain=subdomain,
                plan=validated_data.get('plan', Plan.STARTER),
                is_active=True,
                activated_at=timezone.now(),
            )
            tenant.save()

            primary_domain = f'{subdomain}.{settings.CYTOVA_DOMAIN}'
            Domain.objects.create(
                domain=primary_domain,
                tenant=tenant,
                is_primary=True,
            )

        logger.info(
            'Tenant provisioned: name=%s subdomain=%s schema=%s',
            tenant.name, subdomain, schema_name,
        )

        return tenant

    @staticmethod
    def suspend_tenant(tenant: Tenant) -> Tenant:
        """Mark tenant as inactive. Idempotent."""
        if not tenant.is_active:
            return tenant

        tenant.is_active = False
        tenant.suspended_at = timezone.now()
        tenant.save(update_fields=['is_active', 'suspended_at'])

        logger.info('Tenant suspended: %s', tenant.subdomain)

        return tenant

    @staticmethod
    def activate_tenant(tenant: Tenant) -> Tenant:
        """Reactivate a previously suspended tenant. Idempotent."""
        if tenant.is_active:
            return tenant

        tenant.is_active = True
        tenant.suspended_at = None
        tenant.activated_at = timezone.now()
        tenant.save(update_fields=['is_active', 'suspended_at', 'activated_at'])

        logger.info('Tenant activated: %s', tenant.subdomain)

        return tenant

    @staticmethod
    def update_tenant(tenant: Tenant, validated_data: dict) -> Tenant:
        for field, value in validated_data.items():
            setattr(tenant, field, value)
        tenant.save(update_fields=list(validated_data.keys()))
        return tenant
