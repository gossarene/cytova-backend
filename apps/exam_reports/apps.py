from django.apps import AppConfig


class ExamReportsConfig(AppConfig):
    """
    Operational exam-statistics surface — pivot reports on
    ``AnalysisRequestItem`` aggregated by partner / exam family /
    exam. No models, no Invoice creation, no period locking.
    Reads only the existing tenant tables through the active
    schema set by ``CytovaTenantMiddleware``.
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.exam_reports'
    label = 'exam_reports'
