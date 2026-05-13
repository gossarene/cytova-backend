from django.apps import AppConfig


class InternalNotificationsConfig(AppConfig):
    """
    Internal-staff workflow notifications — biologist review,
    technician rejection feedback. Lives in TENANT_APPS so the
    log rows + dedupe keys are scoped to the active tenant
    schema (no cross-tenant notification join is possible).
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.internal_notifications'
    label = 'internal_notifications'
