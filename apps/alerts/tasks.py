"""
Cytova — Inventory Alert Celery Tasks

Periodic scan:
    scan_inventory_alerts_all_tenants
        Iterates every active tenant and runs the full alert scan.
        Designed for Celery Beat scheduling (e.g. every 15 minutes).

On-demand scan:
    scan_inventory_alerts_tenant
        Runs the scan for a single tenant. Accepts tenant_schema as argument
        (per CLAUDE.md: "Celery tasks must receive tenant_schema as an argument").
"""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(
    name='alerts.scan_all_tenants',
    ignore_result=True,
    soft_time_limit=20 * 60,
    time_limit=25 * 60,
)
def scan_inventory_alerts_all_tenants():
    """
    Periodic: runs the full alert scan for every active tenant.

    Schedule via Celery Beat (django_celery_beat.DatabaseScheduler):
        IntervalSchedule → every 15 minutes
        PeriodicTask → task='alerts.scan_all_tenants'
    """
    from django_tenants.utils import schema_context
    from apps.tenants.models import Tenant
    from .services import InventoryAlertScanService

    tenants = Tenant.objects.filter(is_active=True).values_list(
        'schema_name', flat=True,
    )

    for schema_name in tenants:
        try:
            with schema_context(schema_name):
                summary = InventoryAlertScanService.run_full_scan()
                logger.info(
                    'Alert scan for schema=%s: %s', schema_name, summary,
                )
        except Exception:
            logger.exception(
                'Alert scan failed for schema=%s', schema_name,
            )


@shared_task(
    name='alerts.scan_tenant',
    ignore_result=True,
)
def scan_inventory_alerts_tenant(tenant_schema: str):
    """
    On-demand: runs the full alert scan for a single tenant.
    Pass the tenant's schema_name as argument.
    """
    from django_tenants.utils import schema_context
    from .services import InventoryAlertScanService

    with schema_context(tenant_schema):
        summary = InventoryAlertScanService.run_full_scan()
        logger.info('Alert scan for schema=%s: %s', tenant_schema, summary)
        return summary
