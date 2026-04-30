from django.apps import AppConfig


class FinancialReportsConfig(AppConfig):
    """
    Read-only financial-simulation surface — no models, no Invoice creation,
    no period locking. Sits next to ``apps.invoicing`` but never writes to
    its tables. Sees only the existing AnalysisRequest / AnalysisRequestItem
    / PartnerOrganization data through the active tenant schema.
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.financial_reports'
    label = 'financial_reports'
