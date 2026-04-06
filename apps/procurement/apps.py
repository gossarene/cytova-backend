from django.apps import AppConfig


class ProcurementConfig(AppConfig):
    """
    Thin routing app for the procurement domain.

    Models, services, serializers, and filters live in apps.suppliers.
    This app only provides URL routing under /api/v1/procurement/ to give
    the frontend a coherent procurement namespace.
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.procurement'
    label = 'procurement'
