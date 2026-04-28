from django.urls import path

from .views import (
    DashboardAlertsView,
    DashboardAnalyticsView,
    DashboardCockpitView,
    DashboardOverviewView,
    DashboardPartnersView,
    DashboardPatientsView,
    DashboardProcurementView,
    DashboardRequestsView,
    DashboardResultsView,
    DashboardSetupProgressView,
    DashboardStockView,
)

urlpatterns = [
    path('cockpit/',         DashboardCockpitView.as_view(),         name='dashboard-cockpit'),
    path('analytics/',       DashboardAnalyticsView.as_view(),       name='dashboard-analytics'),
    path('setup-progress/',  DashboardSetupProgressView.as_view(),   name='dashboard-setup-progress'),
    path('overview/',    DashboardOverviewView.as_view(),    name='dashboard-overview'),
    path('patients/',    DashboardPatientsView.as_view(),    name='dashboard-patients'),
    path('requests/',    DashboardRequestsView.as_view(),    name='dashboard-requests'),
    path('partners/',    DashboardPartnersView.as_view(),    name='dashboard-partners'),
    path('results/',     DashboardResultsView.as_view(),     name='dashboard-results'),
    path('stock/',       DashboardStockView.as_view(),       name='dashboard-stock'),
    path('alerts/',      DashboardAlertsView.as_view(),      name='dashboard-alerts'),
    path('procurement/', DashboardProcurementView.as_view(), name='dashboard-procurement'),
]
