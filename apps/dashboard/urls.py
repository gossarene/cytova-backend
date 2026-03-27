from django.urls import path

from .views import (
    DashboardAlertsView,
    DashboardOverviewView,
    DashboardPartnersView,
    DashboardPatientsView,
    DashboardProcurementView,
    DashboardRequestsView,
    DashboardResultsView,
    DashboardStockView,
)

urlpatterns = [
    path('overview/',    DashboardOverviewView.as_view(),    name='dashboard-overview'),
    path('patients/',    DashboardPatientsView.as_view(),    name='dashboard-patients'),
    path('requests/',    DashboardRequestsView.as_view(),    name='dashboard-requests'),
    path('partners/',    DashboardPartnersView.as_view(),    name='dashboard-partners'),
    path('results/',     DashboardResultsView.as_view(),     name='dashboard-results'),
    path('stock/',       DashboardStockView.as_view(),       name='dashboard-stock'),
    path('alerts/',      DashboardAlertsView.as_view(),      name='dashboard-alerts'),
    path('procurement/', DashboardProcurementView.as_view(), name='dashboard-procurement'),
]
