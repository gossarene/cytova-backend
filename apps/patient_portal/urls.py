from django.urls import path

from .views import (
    PatientLoginView, PatientLogoutView, PatientMeView, PatientRefreshView,
    PatientSharedResultDownloadView, PatientSharedResultHideView,
    PatientSharedResultListView, PatientSharedResultVersionsView,
    PatientSignupView, PatientVerifyEmailView,
)

urlpatterns = [
    path('signup/', PatientSignupView.as_view(), name='patient-portal-signup'),
    path('verify-email/', PatientVerifyEmailView.as_view(), name='patient-portal-verify-email'),
    path('login/', PatientLoginView.as_view(), name='patient-portal-login'),
    path('logout/', PatientLogoutView.as_view(), name='patient-portal-logout'),
    path('refresh/', PatientRefreshView.as_view(), name='patient-portal-refresh'),
    path('me/', PatientMeView.as_view(), name='patient-portal-me'),

    # Shared results — list + per-id hide + per-file download + version history.
    path('results/', PatientSharedResultListView.as_view(),
         name='patient-portal-results'),
    # Static ``files/<token>/download/`` declared BEFORE the dynamic
    # ``<uuid:pk>/`` route so the literal ``files`` segment isn't
    # swallowed by the UUID converter.
    path('results/files/<str:file_token>/download/',
         PatientSharedResultDownloadView.as_view(),
         name='patient-portal-result-download'),
    # Version history. Declared BEFORE the bare ``<uuid:pk>/`` hide
    # route so Django matches the more-specific suffix first.
    path('results/<uuid:pk>/versions/',
         PatientSharedResultVersionsView.as_view(),
         name='patient-portal-result-versions'),
    path('results/<uuid:pk>/', PatientSharedResultHideView.as_view(),
         name='patient-portal-result-hide'),
]
