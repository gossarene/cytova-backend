from django.urls import path
from .views import (
    LabelDefaultsView, LabelPresetListView, LabLogoView, LabSettingsView,
)

urlpatterns = [
    path('', LabSettingsView.as_view(), name='lab-settings'),
    path('logo/', LabLogoView.as_view(), name='lab-settings-logo'),
    path('label-defaults/', LabelDefaultsView.as_view(), name='lab-settings-label-defaults'),
    path('label-presets/', LabelPresetListView.as_view(), name='lab-settings-label-presets'),
]
