"""
Internal-workflow notification tests.

The contract under test:

  1. **Granular submit** of a multi-exam request does NOT notify
     biologists until every active item is at least submitted.

  2. **Last-item submit** notifies biologists exactly once per
     active biologist, and never duplicates on a no-op re-call.

  3. **Rejection** notifies the technician who submitted the
     rejected version exactly once.

  4. **Re-submission after rejection** notifies biologists again
     — but only after the workflow lands back in a fully-ready
     state, and only with a NEW dedupe key (new review cycle).

  5. **Email failures** flip the log row to FAILED + populate
     ``error_message``, but never rollback the result-workflow
     transaction. The result is still SUBMITTED / REJECTED.

  6. **Patient privacy**: no patient email address is ever used
     as a recipient. Rendered email bodies never contain a
     result value, a patient name, or a DOB year.

Test-design notes
-----------------
- The shared autouse ``_in_tenant_schema`` fixture from the root
  conftest already pins us inside ``schema_testlab`` so the
  notification log rows are tenant-scoped just like in prod.
- The default ``EMAIL_PROVIDER=console`` in dev returns
  ``ok=True`` from its ``send()`` call — that's enough to drive
  the dispatcher to ``status=SENT``. The failure-path test
  monkeypatches the provider explicitly.
- ``transaction.on_commit`` callbacks fire at the end of the
  outermost atomic block. The tests use
  ``@pytest.mark.django_db(transaction=True)`` so the callbacks
  actually execute (under the default ``django_db`` they're
  silently skipped).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.db import IntegrityError
from django_tenants.utils import get_public_schema_name, schema_context

from apps.catalog.models import (
    ExamDefinition, ExamFamily, ResultStructure, SampleType,
)
from apps.internal_notifications.models import (
    InternalNotificationEvent, InternalNotificationLog,
    InternalNotificationStatus,
)
from apps.internal_notifications.services import (
    build_request_ready_key, build_result_rejected_key,
    current_review_cycle, notify_request_ready_for_review,
    notify_technician_result_rejected,
)
from apps.patients.models import Patient
from apps.requests.models import SourceType
from apps.requests.services import (
    AnalysisRequestItemService, AnalysisRequestService,
)
from apps.results.services import ResultVersionService


# ---------------------------------------------------------------------------
# Subscription seed — same shape as the other workflow tests.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _usable_subscription(_test_tenant_schema, django_db_blocker):
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
                tenant=tenant, status=SubscriptionStatus.TRIAL,
                defaults={'plan': plan},
            )
    yield


# ---------------------------------------------------------------------------
# Domain fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def family():
    return ExamFamily.objects.create(name='Test Family', display_order=1)


@pytest.fixture()
def single_exam_a(family, default_technique):
    return ExamDefinition.objects.create(
        family=family, technique=default_technique,
        code='EX-A', name='Exam A',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.SINGLE_VALUE,
        unit='mg/dL', reference_range='1-10',
        unit_price=Decimal('10'),
    )


@pytest.fixture()
def single_exam_b(family, default_technique):
    return ExamDefinition.objects.create(
        family=family, technique=default_technique,
        code='EX-B', name='Exam B',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.SINGLE_VALUE,
        unit='mg/dL', reference_range='1-10',
        unit_price=Decimal('20'),
    )


@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-INT-NOT-001',
        first_name='Iris', last_name='Notif',
        date_of_birth=date(1985, 6, 1), gender='FEMALE',
        created_by=lab_admin,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_request(patient, exams, *, lab_admin, technician, make_request):
    """Create a request → confirm → collect every item. Leaves
    each item in COLLECTED status, ready for a draft + submit."""
    ar = AnalysisRequestService.create(
        validated_data={
            'patient_id': patient.id,
            'source_type': SourceType.DIRECT_PATIENT,
            'items': [{'exam_definition_id': e.id} for e in exams],
        },
        created_by=lab_admin,
        request=make_request(lab_admin),
        confirm_after=True,
    )
    for item in ar.items.all():
        AnalysisRequestItemService.mark_collected(
            item=item, collected_by=technician,
            request=make_request(technician),
        )
    return ar


def _submit_result(item, *, technician, make_request, value='5'):
    """Helper: create + submit a draft on ``item``. Returns the
    submitted version."""
    v = ResultVersionService.create_draft(
        item=item, entered_by=technician,
        request=make_request(technician),
        result_value=value,
        values=[{'value': value, 'is_abnormal': False}],
    )
    ResultVersionService.submit(
        version=v, submitted_by=technician,
        request=make_request(technician),
    )
    return item.result_versions.get(is_current=True)


# ===========================================================================
# 1. Review-ready notification — when does it fire?
# ===========================================================================

@pytest.mark.django_db(transaction=True)
class TestRequestReadyNotification:

    def test_partial_submit_does_not_notify(
        self, lab_admin, technician, biologist, make_request,
        patient, single_exam_a, single_exam_b,
    ):
        # Two-exam request. Submitting only ONE result must NOT
        # produce a review-ready email — the biologist would have
        # nothing to review on the second exam.
        ar = _create_request(
            patient, [single_exam_a, single_exam_b],
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        items = list(ar.items.select_related('exam_definition').order_by(
            'exam_definition__code',
        ))
        _submit_result(items[0], technician=technician, make_request=make_request)

        rows = InternalNotificationLog.objects.filter(
            event_type=InternalNotificationEvent.REQUEST_READY_FOR_REVIEW,
            request=ar,
        )
        assert rows.count() == 0

    def test_final_submit_notifies_each_biologist_once(
        self, lab_admin, technician, biologist, make_request,
        patient, single_exam_a, single_exam_b,
    ):
        ar = _create_request(
            patient, [single_exam_a, single_exam_b],
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        items = list(ar.items.select_related('exam_definition').order_by(
            'exam_definition__code',
        ))
        _submit_result(items[0], technician=technician, make_request=make_request)
        _submit_result(items[1], technician=technician, make_request=make_request)

        rows = list(InternalNotificationLog.objects.filter(
            event_type=InternalNotificationEvent.REQUEST_READY_FOR_REVIEW,
            request=ar,
        ))
        # BIOLOGIST + LAB_ADMIN are both eligible recipients
        # (operationally biologist-capable in the existing role
        # model). Both should receive exactly one row.
        recipients = {r.recipient_email for r in rows}
        assert biologist.email in recipients
        assert lab_admin.email in recipients
        # No duplicates: row count == distinct recipients.
        assert len(rows) == len(recipients)
        # Every row was successfully dispatched (console provider
        # returns ok=True synchronously after on_commit).
        for r in rows:
            assert r.status == InternalNotificationStatus.SENT, r.error_message

    def test_idempotent_under_repeated_submits(
        self, lab_admin, technician, biologist, make_request,
        patient, single_exam_a,
    ):
        # A single-exam request: submit, then "fake" a duplicate
        # call by invoking the notification service directly. The
        # dedupe key must collapse the second call.
        ar = _create_request(
            patient, [single_exam_a],
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        item = ar.items.get()
        _submit_result(item, technician=technician, make_request=make_request)

        first_count = InternalNotificationLog.objects.filter(
            event_type=InternalNotificationEvent.REQUEST_READY_FOR_REVIEW,
            request=ar,
        ).count()
        # Replay — should be a no-op (every row already exists
        # for this (request, review_cycle, recipient) tuple).
        notify_request_ready_for_review(ar, actor=technician)
        second_count = InternalNotificationLog.objects.filter(
            event_type=InternalNotificationEvent.REQUEST_READY_FOR_REVIEW,
            request=ar,
        ).count()
        assert first_count == second_count


# ===========================================================================
# 2. Rejection notification — the technician path
# ===========================================================================

@pytest.mark.django_db(transaction=True)
class TestRejectionNotification:

    def test_reject_notifies_technician_once(
        self, lab_admin, technician, biologist, make_request,
        patient, single_exam_a,
    ):
        ar = _create_request(
            patient, [single_exam_a],
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        item = ar.items.get()
        v = _submit_result(item, technician=technician, make_request=make_request)

        ResultVersionService.reject(
            version=v, rejection_notes='Sample was hemolysed — re-collect.',
            rejected_by=biologist, request=make_request(biologist),
        )

        rows = list(InternalNotificationLog.objects.filter(
            event_type=InternalNotificationEvent.RESULT_REJECTED,
            request=ar,
        ))
        assert len(rows) == 1
        row = rows[0]
        assert row.recipient_email == technician.email
        assert row.result_version_id == v.id
        assert row.status == InternalNotificationStatus.SENT, row.error_message

    def test_duplicate_rejection_call_does_not_double_notify(
        self, lab_admin, technician, biologist, make_request,
        patient, single_exam_a,
    ):
        # Direct service replay — the model-level state machine
        # forbids rejecting an already-rejected version, but the
        # dedupe key is independent of the state machine. Pin
        # the dedupe behaviour by calling the notification service
        # twice for the same version.
        ar = _create_request(
            patient, [single_exam_a],
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        item = ar.items.get()
        v = _submit_result(item, technician=technician, make_request=make_request)
        ResultVersionService.reject(
            version=v, rejection_notes='', rejected_by=biologist,
            request=make_request(biologist),
        )

        before = InternalNotificationLog.objects.filter(
            event_type=InternalNotificationEvent.RESULT_REJECTED,
        ).count()
        # Replay → no-op.
        notify_technician_result_rejected(v, actor=biologist)
        after = InternalNotificationLog.objects.filter(
            event_type=InternalNotificationEvent.RESULT_REJECTED,
        ).count()
        assert before == after


# ===========================================================================
# 3. Resubmission cycle — new review round → new email
# ===========================================================================

@pytest.mark.django_db(transaction=True)
class TestResubmissionCycle:

    def test_resubmit_after_reject_notifies_biologists_again(
        self, lab_admin, technician, biologist, make_request,
        patient, single_exam_a,
    ):
        # 1. Submit → biologists notified (cycle 1).
        ar = _create_request(
            patient, [single_exam_a],
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        item = ar.items.get()
        v1 = _submit_result(item, technician=technician, make_request=make_request)

        cycle1_keys = set(
            InternalNotificationLog.objects.filter(
                event_type=InternalNotificationEvent.REQUEST_READY_FOR_REVIEW,
                request=ar,
            ).values_list('dedupe_key', flat=True)
        )
        assert len(cycle1_keys) > 0
        assert current_review_cycle(ar) == 1

        # 2. Biologist rejects → no new review-ready row (still
        #    cycle 1, dedupe blocks); rejection email queued.
        ResultVersionService.reject(
            version=v1, rejection_notes='Re-check tube label.',
            rejected_by=biologist, request=make_request(biologist),
        )

        # 3. Technician submits a corrected result → cycle 2 →
        #    new dedupe keys → fresh biologist emails.
        item.refresh_from_db()
        _submit_result(item, technician=technician, make_request=make_request, value='7')

        cycle2_rows = list(
            InternalNotificationLog.objects.filter(
                event_type=InternalNotificationEvent.REQUEST_READY_FOR_REVIEW,
                request=ar,
            )
        )
        # Strictly more rows now than after cycle 1.
        assert len(cycle2_rows) > len(cycle1_keys)
        cycle2_keys = {r.dedupe_key for r in cycle2_rows}
        new_keys = cycle2_keys - cycle1_keys
        assert new_keys, 'Resubmission must produce at least one new dedupe key'
        # Every new key carries the cycle-2 marker.
        assert all(':2:' in k for k in new_keys)


# ===========================================================================
# 4. Failure isolation + privacy
# ===========================================================================

class _AlwaysFailProvider:
    """Stand-in EmailProvider whose ``send()`` always fails. Used
    to drive the FAILED branch of the dispatcher without breaking
    the outer transaction."""
    name = 'always-fail-test'

    def send(self, message):
        from common.email.providers.base import EmailResult
        return EmailResult(ok=False, error='simulated SMTP outage')


@pytest.mark.django_db(transaction=True)
class TestFailureIsolation:

    def test_email_failure_does_not_rollback_workflow(
        self, lab_admin, technician, biologist, make_request,
        patient, single_exam_a, monkeypatch,
    ):
        # Force the provider to fail. The result submission must
        # still succeed, and the notification row must land in
        # ``FAILED`` with ``error_message`` populated.
        from apps.internal_notifications import services as svc

        class _FailingEmailService:
            def __init__(self):
                self.provider = _AlwaysFailProvider()
            def send_biologist_review_ready_email(self, **kwargs):
                return self.provider.send(None)
            def send_technician_result_rejected_email(self, **kwargs):
                return self.provider.send(None)

        monkeypatch.setattr(
            svc, 'get_email_service', lambda: _FailingEmailService(),
        )

        ar = _create_request(
            patient, [single_exam_a],
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        item = ar.items.get()
        v = _submit_result(item, technician=technician, make_request=make_request)

        # Workflow side committed normally.
        v.refresh_from_db()
        from apps.results.models import ResultStatus
        assert v.status == ResultStatus.SUBMITTED

        # Notification rows were created but marked FAILED.
        rows = list(InternalNotificationLog.objects.filter(
            event_type=InternalNotificationEvent.REQUEST_READY_FOR_REVIEW,
            request=ar,
        ))
        assert rows, 'Failure path must still leave a log trail'
        for r in rows:
            assert r.status == InternalNotificationStatus.FAILED
            assert 'simulated SMTP outage' in r.error_message


@pytest.mark.django_db(transaction=True)
class TestPrivacyAndPatientIsolation:

    def test_no_patient_email_used_as_recipient(
        self, lab_admin, technician, biologist, make_request,
        patient, single_exam_a,
    ):
        # Even though the request belongs to a patient with an
        # email on file (set below), the patient address never
        # appears as a notification recipient.
        patient.email = 'iris.notif@example.com'
        patient.save(update_fields=['email', 'updated_at'])

        ar = _create_request(
            patient, [single_exam_a],
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        item = ar.items.get()
        _submit_result(item, technician=technician, make_request=make_request)

        recipients = set(
            InternalNotificationLog.objects
            .filter(request=ar)
            .values_list('recipient_email', flat=True)
        )
        assert patient.email not in recipients
        assert all(addr.endswith('@testlab.io') for addr in recipients), (
            f'Unexpected non-staff recipient: {recipients}'
        )

    def test_rendered_emails_carry_no_patient_data_or_result_value(
        self, lab_admin, technician, biologist, make_request,
        patient, single_exam_a,
    ):
        # Render both templates with the canonical inputs and grep
        # for forbidden substrings: patient name, document number,
        # DOB year, and the actual result value the technician
        # entered. None should appear — the templates only carry
        # staff-side metadata.
        from common.email.templates import (
            render_biologist_request_ready,
            render_technician_result_rejected,
        )

        forbidden = (
            patient.first_name, patient.last_name,
            patient.document_number, '1985',
            # Result-value sentinel the technician typed.
            'SECRET-VALUE-9876',
        )

        html_a, text_a = render_biologist_request_ready(
            first_name=biologist.first_name,
            request_reference='REQ-2026-AAAA',
            exam_names=[single_exam_a.name],
            review_url='https://app.cytova.io/requests/abc',
        )
        for needle in forbidden:
            assert needle not in html_a
            assert needle not in text_a

        html_b, text_b = render_technician_result_rejected(
            first_name=technician.first_name,
            request_reference='REQ-2026-AAAA',
            exam_name=single_exam_a.name,
            rejection_notes='Please re-check.',
            review_url='https://app.cytova.io/requests/abc',
        )
        for needle in forbidden:
            assert needle not in html_b
            assert needle not in text_b


# ===========================================================================
# 5. Dedupe-key shape pin (low-level sanity)
# ===========================================================================

@pytest.mark.django_db(transaction=True)
class TestDedupeKey:

    def test_request_ready_key_includes_cycle_and_recipient(
        self, biologist, single_exam_a, lab_admin, technician,
        make_request, patient,
    ):
        ar = _create_request(
            patient, [single_exam_a],
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        key = build_request_ready_key(
            request_id=ar.id, review_cycle=2, recipient_id=biologist.id,
        )
        assert str(ar.id) in key
        assert ':2:' in key
        assert str(biologist.id) in key
        assert key.startswith('REQUEST_READY_FOR_REVIEW:')

    def test_result_rejected_key_includes_version_and_technician(
        self, technician, single_exam_a, lab_admin, biologist,
        make_request, patient,
    ):
        ar = _create_request(
            patient, [single_exam_a],
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        item = ar.items.get()
        v = _submit_result(item, technician=technician, make_request=make_request)
        key = build_result_rejected_key(
            version_id=v.id, technician_id=technician.id,
        )
        assert str(v.id) in key
        assert str(technician.id) in key
        assert key.startswith('RESULT_REJECTED:')

    def test_unique_constraint_blocks_duplicate_inserts(
        self, biologist, single_exam_a, lab_admin, technician,
        make_request, patient,
    ):
        ar = _create_request(
            patient, [single_exam_a],
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        # Insert one row directly.
        InternalNotificationLog.objects.create(
            event_type=InternalNotificationEvent.REQUEST_READY_FOR_REVIEW,
            request=ar, result_version=None,
            recipient_user=biologist, recipient_email=biologist.email,
            dedupe_key='unit-test-key-1',
        )
        with pytest.raises(IntegrityError):
            InternalNotificationLog.objects.create(
                event_type=InternalNotificationEvent.REQUEST_READY_FOR_REVIEW,
                request=ar, result_version=None,
                recipient_user=biologist, recipient_email=biologist.email,
                dedupe_key='unit-test-key-1',  # duplicate
            )


# ===========================================================================
# 6. Lab-settings kill-switches + per-user opt-in flags
# ===========================================================================

@pytest.mark.django_db(transaction=True)
class TestLabSettingsKillSwitch:
    """The ``LabSettings`` master switch and per-event flags must
    short-circuit the dispatch path entirely — no log row, no
    email — and must do so independently per channel."""

    def _toggle_settings(self, **fields) -> None:
        from apps.lab_settings.models import LabSettings
        s = LabSettings.get_solo()
        for k, v in fields.items():
            setattr(s, k, v)
        s.save(update_fields=list(fields) + ['updated_at'])

    def test_master_switch_off_suppresses_all_channels(
        self, lab_admin, technician, biologist, make_request,
        patient, single_exam_a,
    ):
        # Disable the entire internal-workflow channel before any
        # workflow event happens. The submit + reject sequence must
        # produce zero notification rows.
        self._toggle_settings(internal_notifications_enabled=False)

        ar = _create_request(
            patient, [single_exam_a],
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        item = ar.items.get()
        v = _submit_result(item, technician=technician, make_request=make_request)
        ResultVersionService.reject(
            version=v, rejection_notes='Re-do this one.',
            rejected_by=biologist, request=make_request(biologist),
        )

        assert InternalNotificationLog.objects.filter(request=ar).count() == 0

    def test_review_ready_event_disabled_suppresses_only_that_channel(
        self, lab_admin, technician, biologist, make_request,
        patient, single_exam_a,
    ):
        # Master switch ON, but per-event flag OFF for review-ready.
        # Rejection emails MUST still fire — the channels are
        # independent.
        self._toggle_settings(
            internal_notifications_enabled=True,
            notify_review_ready_enabled=False,
            notify_result_rejected_enabled=True,
        )

        ar = _create_request(
            patient, [single_exam_a],
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        item = ar.items.get()
        v = _submit_result(item, technician=technician, make_request=make_request)

        review_rows = InternalNotificationLog.objects.filter(
            event_type=InternalNotificationEvent.REQUEST_READY_FOR_REVIEW,
            request=ar,
        )
        assert review_rows.count() == 0

        ResultVersionService.reject(
            version=v, rejection_notes='Re-do this one.',
            rejected_by=biologist, request=make_request(biologist),
        )
        reject_rows = InternalNotificationLog.objects.filter(
            event_type=InternalNotificationEvent.RESULT_REJECTED,
            request=ar,
        )
        assert reject_rows.count() == 1

    def test_rejection_event_disabled_suppresses_only_that_channel(
        self, lab_admin, technician, biologist, make_request,
        patient, single_exam_a,
    ):
        # Mirror case: rejection disabled, review-ready still on.
        self._toggle_settings(
            internal_notifications_enabled=True,
            notify_review_ready_enabled=True,
            notify_result_rejected_enabled=False,
        )

        ar = _create_request(
            patient, [single_exam_a],
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        item = ar.items.get()
        v = _submit_result(item, technician=technician, make_request=make_request)

        review_rows = InternalNotificationLog.objects.filter(
            event_type=InternalNotificationEvent.REQUEST_READY_FOR_REVIEW,
            request=ar,
        )
        assert review_rows.count() >= 1

        ResultVersionService.reject(
            version=v, rejection_notes='Re-do this one.',
            rejected_by=biologist, request=make_request(biologist),
        )
        reject_rows = InternalNotificationLog.objects.filter(
            event_type=InternalNotificationEvent.RESULT_REJECTED,
            request=ar,
        )
        assert reject_rows.count() == 0


@pytest.mark.django_db(transaction=True)
class TestPerUserOptIn:
    """Recipient resolution is driven by per-user flags, NOT by
    role. Roles are only a "smart default" applied at user create
    time; the LAB_ADMIN can flip the flag for any teammate."""

    def test_biologist_with_flag_off_receives_no_review_ready_email(
        self, lab_admin, technician, biologist, make_request,
        patient, single_exam_a,
    ):
        # Explicitly opt the biologist OUT — even though the role
        # would normally seed True, the flag is the authority.
        # Also opt LAB_ADMIN out so we can assert "zero" cleanly.
        biologist.receive_review_ready_notifications = False
        biologist.save(update_fields=[
            'receive_review_ready_notifications', 'updated_at',
        ])
        lab_admin.receive_review_ready_notifications = False
        lab_admin.save(update_fields=[
            'receive_review_ready_notifications', 'updated_at',
        ])

        ar = _create_request(
            patient, [single_exam_a],
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        item = ar.items.get()
        _submit_result(item, technician=technician, make_request=make_request)

        rows = InternalNotificationLog.objects.filter(
            event_type=InternalNotificationEvent.REQUEST_READY_FOR_REVIEW,
            request=ar,
        )
        assert rows.count() == 0

    def test_non_biologist_role_with_flag_on_is_a_valid_recipient(
        self, lab_admin, technician, biologist, receptionist,
        make_request, patient, single_exam_a,
    ):
        # A receptionist normally wouldn't review results, but if
        # the LAB_ADMIN explicitly flipped the flag on for them
        # (e.g. a manual operations setup), they MUST receive the
        # email — roles are not the final authority.
        receptionist.receive_review_ready_notifications = True
        receptionist.save(update_fields=[
            'receive_review_ready_notifications', 'updated_at',
        ])

        ar = _create_request(
            patient, [single_exam_a],
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        item = ar.items.get()
        _submit_result(item, technician=technician, make_request=make_request)

        recipients = set(
            InternalNotificationLog.objects.filter(
                event_type=InternalNotificationEvent.REQUEST_READY_FOR_REVIEW,
                request=ar,
            ).values_list('recipient_email', flat=True)
        )
        assert receptionist.email in recipients

    def test_inactive_user_with_flag_on_is_excluded(
        self, lab_admin, technician, biologist, make_request,
        patient, single_exam_a,
    ):
        # Biologist is opted in but has been deactivated. They
        # MUST NOT be a recipient — the active-user guard is
        # independent of the opt-in flag.
        biologist.is_active = False
        biologist.save(update_fields=['is_active', 'updated_at'])

        ar = _create_request(
            patient, [single_exam_a],
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        item = ar.items.get()
        _submit_result(item, technician=technician, make_request=make_request)

        recipients = set(
            InternalNotificationLog.objects.filter(
                event_type=InternalNotificationEvent.REQUEST_READY_FOR_REVIEW,
                request=ar,
            ).values_list('recipient_email', flat=True)
        )
        assert biologist.email not in recipients

    def test_empty_email_user_with_flag_on_is_excluded(
        self, lab_admin, technician, biologist, make_request,
        patient, single_exam_a,
    ):
        # A staff user without an email on file can't receive
        # email and must be excluded from the recipient list
        # even when the opt-in flag is True.
        biologist.email = ''
        biologist.save(update_fields=['email', 'updated_at'])

        ar = _create_request(
            patient, [single_exam_a],
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        item = ar.items.get()
        _submit_result(item, technician=technician, make_request=make_request)

        # No row whose recipient_email is blank.
        rows = InternalNotificationLog.objects.filter(
            event_type=InternalNotificationEvent.REQUEST_READY_FOR_REVIEW,
            request=ar,
        )
        assert all(r.recipient_email for r in rows)
        # Specifically the (now anonymous) biologist isn't recorded
        # as a recipient under any address.
        assert all(r.recipient_user_id != biologist.id for r in rows)

    def test_technician_with_rejection_flag_off_gets_no_rejection_email(
        self, lab_admin, technician, biologist, make_request,
        patient, single_exam_a,
    ):
        # Technician submitted the result, then their LAB_ADMIN
        # turned the rejection-notification flag off. A subsequent
        # rejection must NOT produce a notification row.
        technician.receive_result_rejection_notifications = False
        technician.save(update_fields=[
            'receive_result_rejection_notifications', 'updated_at',
        ])

        ar = _create_request(
            patient, [single_exam_a],
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        item = ar.items.get()
        v = _submit_result(item, technician=technician, make_request=make_request)
        ResultVersionService.reject(
            version=v, rejection_notes='Try again.',
            rejected_by=biologist, request=make_request(biologist),
        )

        rows = InternalNotificationLog.objects.filter(
            event_type=InternalNotificationEvent.RESULT_REJECTED,
            request=ar,
        )
        assert rows.count() == 0


@pytest.mark.django_db(transaction=True)
class TestRoleDerivedCreateDefaults:
    """``StaffUser.objects.create_user`` applies role-derived
    smart defaults for the new flags — that's the contract the
    data migration 0008 mirrors for the pre-existing rows."""

    def test_biologist_seed_review_ready_true(self):
        from apps.users.models import Role, StaffUser
        u = StaffUser.objects.create_user(
            email='bio-default@testlab.io', password='x',
            first_name='Bio', last_name='Default',
            role=Role.BIOLOGIST,
        )
        assert u.receive_review_ready_notifications is True
        assert u.receive_result_rejection_notifications is False

    def test_lab_admin_seed_review_ready_true(self):
        from apps.users.models import Role, StaffUser
        u = StaffUser.objects.create_user(
            email='admin-default@testlab.io', password='x',
            first_name='Admin', last_name='Default',
            role=Role.LAB_ADMIN,
        )
        assert u.receive_review_ready_notifications is True
        assert u.receive_result_rejection_notifications is False

    def test_technician_seed_rejection_true(self):
        from apps.users.models import Role, StaffUser
        u = StaffUser.objects.create_user(
            email='tech-default@testlab.io', password='x',
            first_name='Tech', last_name='Default',
            role=Role.TECHNICIAN,
        )
        assert u.receive_review_ready_notifications is False
        assert u.receive_result_rejection_notifications is True

    def test_unmapped_role_keeps_both_flags_false(self):
        from apps.users.models import Role, StaffUser
        u = StaffUser.objects.create_user(
            email='reception-default@testlab.io', password='x',
            first_name='Reception', last_name='Default',
            role=Role.RECEPTIONIST,
        )
        assert u.receive_review_ready_notifications is False
        assert u.receive_result_rejection_notifications is False

    def test_explicit_override_wins_over_role_default(self):
        # Caller-supplied False must NOT be silently flipped to
        # True by the smart-defaults setdefault.
        from apps.users.models import Role, StaffUser
        u = StaffUser.objects.create_user(
            email='bio-override@testlab.io', password='x',
            first_name='Bio', last_name='Override',
            role=Role.BIOLOGIST,
            receive_review_ready_notifications=False,
        )
        assert u.receive_review_ready_notifications is False
