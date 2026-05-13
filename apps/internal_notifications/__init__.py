"""
Cytova — Internal-workflow email notifications.

Distinct from the patient-facing email surface in
``apps.requests.notification_service`` (which handles
share-link / Notify-Cytova / patient-result-ready flows). This
app fires notifications BETWEEN staff users only:

  - Biologists when a request is ready for biological validation
  - Technicians when a result they submitted gets rejected
  - Biologists again when a rejected result is corrected and the
    request becomes ready for a new review round

The boundary is enforced by the recipient resolution layer —
neither send path ever reads from ``apps.patients`` or
``apps.patient_portal``, so a refactor can't accidentally surface
patient data on internal staff emails.
"""
default_app_config = 'apps.internal_notifications.apps.InternalNotificationsConfig'
