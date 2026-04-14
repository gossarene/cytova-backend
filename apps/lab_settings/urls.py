from django.urls import path
from .views import LabSettingsView, LabLogoView

urlpatterns = [
    path('', LabSettingsView.as_view(), name='lab-settings'),
    path('logo/', LabLogoView.as_view(), name='lab-settings-logo'),
]
