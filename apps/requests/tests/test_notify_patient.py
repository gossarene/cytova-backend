"""
Tests for the patient result-ready email notification flow.

Covers:
  - active access token reused if one exists
  - new access token created when none exists
  - email dispatched via the EmailService abstraction (provider-agnostic)
  - missing patient email → distinct PATIENT_EMAIL_MISSING error
  - disabled email channel → distinct EMAIL_CHANNEL_DISABLED error
  - response includes secure_link / expires_at / channel statuses
  - rendered email never contains medical data (result values, request
    numbers, exam codes/names, etc.)

Reuses the autouse `_in_tenant_schema` fixture from the root conftest so
all writes (LabSettings, ResultAccessToken, AuditLog) hit the test tenant
schema. Builds a finalized request with a generated PDF report via the
existing helper from test_patient_access.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.test import override_settings
from django_tenants.utils import get_public_schema_name, schema_context

from apps.audit.models import AuditLog
from apps.catalog.models import (
    ExamCategory, ExamDefinition, ExamFamily, ResultStructure, SampleType,
)
from apps.lab_settings.models import LabSettings
from apps.patients.models import Patient
from apps.requests.models import ResultAccessToken, SourceType
from apps.requests.notification_service import (
    EMAIL_CHANNEL,
    EmailChannelDisabled,
    PatientEmailMissing,
    RequestNotificationService,
)
from apps.requests.patient_access import ResultAccessService
from apps.requests.report_service import RequestReportService
from apps.requests.services import (
    AnalysisRequestItemService, AnalysisRequestService,
)
from apps.results.services import ResultVersionService
from common.email import EmailService
from common.email.providers.base import EmailMessage, EmailResult


pytestmark = pytest.mark.no_auto_labels


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _usable_subscription(_test_tenant_schema, django_db_blocker):
    """Same pattern as test_patient_access — every test needs a usable
    trial subscription so SubscriptionEnforcementMiddleware doesn't gate
    the request."""
    from apps.tenants.models import (
        Subscription, SubscriptionPlan, SubscriptionStatus, Tenant,
    )
    with django_db_blocker.unblock():
        with schema_context(get_public_schema_name()):
            plan, _ = SubscriptionPlan.objects.get_or_create(
                code='TEST_TRIAL',
                defaults={
                    'name': 'Test Trial', 'is_trial': True,
                    'trial_duration_days': 30, 'is_public': False,
                },
            )
            tenant = Tenant.objects.get(schema_name=_test_tenant_schema)
            Subscription.objects.get_or_create(
                tenant=tenant,
                status=SubscriptionStatus.TRIAL,
                defaults={'plan': plan},
            )
    yield


@pytest.fixture(autouse=True)
def _disable_pdf_protection():
    """Skip PDF password protection so the test PDF write is fast."""
    lab = LabSettings.get_solo()
    lab.result_pdf_password_enabled = False
    lab.notification_enable_email = True  # default-on for these tests
    lab.lab_name = 'Test Lab'
    lab.save(update_fields=[
        'result_pdf_password_enabled',
        'notification_enable_email',
        'lab_name',
        'updated_at',
    ])


@pytest.fixture()
def patient_with_email(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD', document_number='NID-NOTIF-001',
        first_name='Alice', last_name='Patient',
        date_of_birth=date(1990, 1, 1), gender='FEMALE',
        phone='12345678',
        email='alice.patient@example.com',
        created_by=lab_admin,
    )


@pytest.fixture()
def patient_without_email(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD', document_number='NID-NOTIF-002',
        first_name='Bob', last_name='NoEmail',
        date_of_birth=date(1985, 5, 15), gender='MALE',
        phone='87654321',
        email='',
        created_by=lab_admin,
    )


@pytest.fixture()
def exam(default_technique):
    cat = ExamCategory.objects.create(name='Cat', display_order=1)
    fam = ExamFamily.objects.create(name='Fam', display_order=1)
    return ExamDefinition.objects.create(
        category=cat, family=fam, technique=default_technique,
        code='NTF', name='NotifyTest',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.SINGLE_VALUE,
        unit='u', reference_range='0-100',
        unit_price=Decimal('10'),
    )


@pytest.fixture
def captured_emails(monkeypatch):
    """Patch the email service factory so emails are captured (not sent)."""
    captured: list[EmailMessage] = []

    class _StubProvider:
        name = 'stub'

        def send(self, message: EmailMessage) -> EmailResult:
            captured.append(message)
            return EmailResult(ok=True)

    monkeypatch.setattr(
        'apps.requests.notification_service.get_email_service',
        lambda: EmailService(provider=_StubProvider()),
    )
    return captured


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _finalize_with_report(patient, lab_admin, technician, biologist, make_request, exam):
    """Same workflow as test_patient_access — produce a request with a
    generated PDF so the access token can be created."""
    from apps.requests.label_service import RequestLabelService
    ar = AnalysisRequestService.create(
        validated_data={
            'patient_id': patient.id,
            'source_type': SourceType.DIRECT_PATIENT,
            'items': [{'exam_definition_id': exam.id}],
        },
        created_by=lab_admin,
        request=make_request(lab_admin),
        confirm_after=True,
    )
    RequestLabelService.generate_or_get(ar, lab_admin, make_request(lab_admin))
    req_t = make_request(technician)
    req_b = make_request(biologist)
    for item in ar.items.all():
        AnalysisRequestItemService.mark_collected(item=item, collected_by=technician, request=req_t)
    for item in ar.items.select_related('exam_definition').all():
        item.refresh_from_db()
        v = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req_t,
            result_value='42', values=[{'value': '42', 'is_abnormal': False}], comments='',
        )
        ResultVersionService.submit(version=v, submitted_by=technician, request=req_t)
        v = item.result_versions.get(is_current=True)
        ResultVersionService.validate(version=v, validation_notes='OK', validated_by=biologist, request=req_b)
    ar.refresh_from_db()
    AnalysisRequestService.finalize_validation(analysis_request=ar, finalized_by=biologist, request=req_b)
    ar.refresh_from_db()
    RequestReportService.generate_or_get(ar, biologist, make_request(biologist))
    return ar


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------

class TestSecureLinkLifecycle:

    def test_creates_new_access_token_when_none_exists(
        self, patient_with_email, exam, lab_admin, technician, biologist,
        make_request, captured_emails,
    ):
        ar = _finalize_with_report(
            patient_with_email, lab_admin, technician, biologist, make_request, exam,
        )
        assert ResultAccessToken.objects.filter(analysis_request=ar).count() == 0

        outcome = RequestNotificationService.notify_patient(ar, make_request(lab_admin))

        # Exactly one token exists, and it's the one referenced by the link.
        tokens = list(ResultAccessToken.objects.filter(analysis_request=ar))
        assert len(tokens) == 1
        assert tokens[0].token in outcome.secure_link
        assert outcome.expires_at == tokens[0].expires_at.isoformat()

    def test_reuses_active_token_if_one_exists(
        self, patient_with_email, exam, lab_admin, technician, biologist,
        make_request, captured_emails,
    ):
        ar = _finalize_with_report(
            patient_with_email, lab_admin, technician, biologist, make_request, exam,
        )
        existing = ResultAccessService.create_token(ar)
        assert ResultAccessToken.objects.filter(analysis_request=ar, is_active=True).count() == 1

        outcome = RequestNotificationService.notify_patient(ar, make_request(lab_admin))

        # No new token created — count unchanged, and the link uses the
        # pre-existing token string.
        assert ResultAccessToken.objects.filter(analysis_request=ar).count() == 1
        assert existing.token in outcome.secure_link


# ---------------------------------------------------------------------------
# Email dispatch
# ---------------------------------------------------------------------------

class TestEmailDispatch:

    def test_email_sent_via_email_service(
        self, patient_with_email, exam, lab_admin, technician, biologist,
        make_request, captured_emails,
    ):
        ar = _finalize_with_report(
            patient_with_email, lab_admin, technician, biologist, make_request, exam,
        )
        outcome = RequestNotificationService.notify_patient(ar, make_request(lab_admin))

        assert len(captured_emails) == 1
        msg = captured_emails[0]
        assert msg.to_email == patient_with_email.email
        assert msg.subject == 'Your lab result is ready'
        # Secure link is in the body — but no medical data.
        assert outcome.secure_link in msg.text
        assert outcome.secure_link in msg.html

        # Response shape per spec
        assert outcome.channels_attempted == [EMAIL_CHANNEL]
        assert outcome.channels_succeeded == [EMAIL_CHANNEL]
        assert outcome.channels_failed == []

    def test_no_medical_data_in_rendered_email(
        self, patient_with_email, exam, lab_admin, technician, biologist,
        make_request, captured_emails,
    ):
        """The patient's request_number, the value '42', the exam code 'NTF',
        the exam name 'NotifyTest', and any clinical notes must NOT leak into
        the email body. This is the central confidentiality contract for the
        notification template."""
        ar = _finalize_with_report(
            patient_with_email, lab_admin, technician, biologist, make_request, exam,
        )
        RequestNotificationService.notify_patient(ar, make_request(lab_admin))
        msg = captured_emails[0]
        joined = (msg.text + ' ' + msg.html).lower()

        forbidden = [
            ar.request_number.lower(),     # request identifier
            ' 42 ',                        # the literal result value (with spaces to avoid false hits in port numbers etc.)
            'ntf',                         # exam code
            'notifytest',                  # exam name
            'reference range',             # clinical vocabulary
            'abnormal',
            'diagnosis',
            'positive', 'negative',        # likely lab outcome words
        ]
        leaks = [token for token in forbidden if token in joined]
        assert not leaks, f'medical data leaked into email body: {leaks}'

    def test_audit_log_written_on_success(
        self, patient_with_email, exam, lab_admin, technician, biologist,
        make_request, captured_emails,
    ):
        ar = _finalize_with_report(
            patient_with_email, lab_admin, technician, biologist, make_request, exam,
        )
        before = AuditLog.objects.filter(entity_type='PatientResultNotification').count()
        RequestNotificationService.notify_patient(ar, make_request(lab_admin))
        after = AuditLog.objects.filter(entity_type='PatientResultNotification').count()
        assert after == before + 1

        log = AuditLog.objects.filter(
            entity_type='PatientResultNotification',
        ).order_by('-timestamp').first()
        assert log.diff['channel'] == EMAIL_CHANNEL
        assert log.diff['status'] == 'SENT'
        # Diff carries provider name + structured fields only — no link, no email body.
        assert 'secure_link' not in log.diff
        assert 'token' not in log.diff


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------

class TestValidationErrors:

    def test_missing_patient_email_raises(
        self, patient_without_email, exam, lab_admin, technician, biologist,
        make_request, captured_emails,
    ):
        ar = _finalize_with_report(
            patient_without_email, lab_admin, technician, biologist, make_request, exam,
        )
        with pytest.raises(PatientEmailMissing):
            RequestNotificationService.notify_patient(ar, make_request(lab_admin))
        # No email sent.
        assert captured_emails == []

    def test_disabled_email_channel_raises(
        self, patient_with_email, exam, lab_admin, technician, biologist,
        make_request, captured_emails,
    ):
        lab = LabSettings.get_solo()
        lab.notification_enable_email = False
        lab.save(update_fields=['notification_enable_email', 'updated_at'])

        ar = _finalize_with_report(
            patient_with_email, lab_admin, technician, biologist, make_request, exam,
        )
        with pytest.raises(EmailChannelDisabled):
            RequestNotificationService.notify_patient(ar, make_request(lab_admin))
        assert captured_emails == []


# ---------------------------------------------------------------------------
# Provider failure handling
# ---------------------------------------------------------------------------

class TestProviderFailure:

    def test_provider_failure_records_channel_failed(
        self, patient_with_email, exam, lab_admin, technician, biologist,
        make_request, monkeypatch,
    ):
        class _FailingProvider:
            name = 'failing-stub'

            def send(self, message):
                return EmailResult(ok=False, error='http_400')

        monkeypatch.setattr(
            'apps.requests.notification_service.get_email_service',
            lambda: EmailService(provider=_FailingProvider()),
        )

        ar = _finalize_with_report(
            patient_with_email, lab_admin, technician, biologist, make_request, exam,
        )
        outcome = RequestNotificationService.notify_patient(ar, make_request(lab_admin))

        assert outcome.channels_attempted == [EMAIL_CHANNEL]
        assert outcome.channels_succeeded == []
        assert len(outcome.channels_failed) == 1
        failed = outcome.channels_failed[0]
        assert failed.channel == EMAIL_CHANNEL
        assert failed.status == 'FAILED'
        assert failed.provider == 'failing-stub'
        assert failed.error == 'http_400'

        # Audit row should reflect the failure.
        log = AuditLog.objects.filter(
            entity_type='PatientResultNotification',
        ).order_by('-timestamp').first()
        assert log.diff['status'] == 'FAILED'
        assert log.diff['error'] == 'http_400'


# ---------------------------------------------------------------------------
# Tenant-aware reset link
# ---------------------------------------------------------------------------

class TestTenantAwareLink:

    @override_settings(DEBUG=True, CYTOVA_DEV_FRONTEND_PORT=3000)
    def test_secure_link_uses_request_host_in_dev(
        self, patient_with_email, exam, lab_admin, technician, biologist,
        make_request, captured_emails,
    ):
        ar = _finalize_with_report(
            patient_with_email, lab_admin, technician, biologist, make_request, exam,
        )

        # make_request from the project's conftest yields an HttpRequest-ish
        # object. Override its host so we can assert the link follows the
        # incoming tenant subdomain.
        req = make_request(lab_admin)
        req.META['HTTP_HOST'] = 'veno-lab.cytova.io:8000'

        outcome = RequestNotificationService.notify_patient(ar, req)
        assert 'http://veno-lab.cytova.io:3000/results/access/' in outcome.secure_link
        assert ':8000' not in outcome.secure_link


# ---------------------------------------------------------------------------
# View-level permission gating
#
# The notify_patient action is more restricted than the access-token actions
# it shares a card with — patient-facing communication is reserved for
# receptionists and lab admins. These tests lock in that contract through
# the actual ViewSet permission resolver so future refactors that touch
# ``get_permissions`` can't silently widen the gate back to IsAnyStaff.
# ---------------------------------------------------------------------------

class TestNotifyPatientPermission:

    def _viewset(self, action_name: str, request):
        from apps.requests.views import AnalysisRequestViewSet
        view = AnalysisRequestViewSet()
        view.action = action_name
        view.request = request
        return view

    def _has_perm(self, user, make_request) -> bool:
        view = self._viewset('notify_patient', make_request(user))
        permissions = view.get_permissions()
        return all(p.has_permission(view.request, view) for p in permissions)

    def test_lab_admin_allowed(self, lab_admin, make_request):
        assert self._has_perm(lab_admin, make_request) is True

    def test_receptionist_allowed(self, receptionist, make_request):
        assert self._has_perm(receptionist, make_request) is True

    def test_biologist_blocked(self, biologist, make_request):
        # Biologists validate results but don't handle patient comms.
        assert self._has_perm(biologist, make_request) is False

    def test_technician_blocked(self, technician, make_request):
        assert self._has_perm(technician, make_request) is False
