from django.urls import path

from .views import ExamsByPartnerExportView, ExamsByPartnerPreviewView


urlpatterns = [
    path(
        'exams-by-partner/preview/',
        ExamsByPartnerPreviewView.as_view(),
        name='exam-reports-exams-by-partner-preview',
    ),
    path(
        'exams-by-partner/export/',
        ExamsByPartnerExportView.as_view(),
        name='exam-reports-exams-by-partner-export',
    ),
]
