from django.urls import path

from .views import FinancialReportExportView, FinancialReportPreviewView


urlpatterns = [
    path('preview/', FinancialReportPreviewView.as_view(), name='financial-report-preview'),
    path('export/',  FinancialReportExportView.as_view(),  name='financial-report-export'),
]
