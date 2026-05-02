"""
Phase D — Notify Cytova reuses the verified link + lab-setting gate.

After Phase A–C the local ``Patient`` carries a verified Cytova link
snapshot once the receptionist runs the link flow. Phase D teaches
``notify_cytova`` to reuse that link: an empty identity payload now
flows through the linked path (``_resolve_linked_profile`` re-checks
the global account is still active) instead of forcing the operator
to retype the Cytova ID + name + DOB on every share.

The pre-Phase-D behaviour is preserved as the "explicit-payload"
back-compat path. Both paths converge on the same snapshot/audit
flow; the link choice is made in the service layer.

What's pinned here
------------------
- Linked patient + empty body         → 200 + correct snapshot
  (account_id matches the link).
- Linked patient + explicit body      → 200 (back-compat: the
  explicit-payload path still works exactly as it did before).
- Unlinked patient + empty body       → 400 MISSING_IDENTITY (clear
  recovery hint for the frontend).
- Linked patient + deactivated global account → 400
  IDENTITY_VERIFICATION_FAILED (same generic message as a fresh
  mismatch — no info leak about WHY) + audit row carries the
  ``LINKED_ACCOUNT_INACTIVE`` marker so an audit reader can tell
  it apart.
- ``notification_enable_cytova=False`` → 400 CYTOVA_CHANNEL_DISABLED
  (pre-checked before serializer validation so the operator gets a
  clear code instead of a generic missing-field message).
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from django.core.cache import cache
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.test import APIClient

from apps.audit.models import AuditAction, AuditLog
from apps.catalog.models import (
    ExamCategory, ExamDefinition, ExamFamily, ExamTechnique,
    ResultStructure, SampleType,
)
from apps.lab_settings.models import LabSettings
from apps.patient_portal.models import (
    PatientAccount, PatientSharedResult,
)
from apps.patient_portal.services import register_patient_account
from apps.patients.models import Patient
from apps.patients.services import PatientService
from apps.requests.models import RequestStatus, SourceType
from apps.requests.report_service import RequestReportService
from apps.requests.services import (
    AnalysisRequestItemService, AnalysisRequestService,
)
from apps.results.services import ResultVersionService


NOTIFY_URL = '/api/v1/requests/{ar_id}/notify-cytova/'


# ---------------------------------------------------------------------------
# Subscription + cache fixtures (mirror the share-lifecycle suite)
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
                tenant=tenant,
                status=SubscriptionStatus.TRIAL,
                defaults={'plan': plan},
            )
    yield


@pytest.fixture(autouse=True)
def _reset_throttle_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _stub_email_service(monkeypatch):
    """Notify-Cytova always tries to email the patient. Stub the
    provider so tests don't depend on SMTP. We don't care about the
    email outcome here — Phase 1's suite covers that surface; this
    suite focuses on the linked-path resolution."""
    from common.email.providers.base import EmailResult
    from apps.requests import notify_cytova_service as svc

    class _FakeService:
        def send_patient_shared_result_email(self, **_):
            return EmailResult(ok=True)

    monkeypatch.setattr(svc, 'get_email_service', lambda: _FakeService())


# ---------------------------------------------------------------------------
# Lab fixtures: a finalized request with a generated v1 report
# ---------------------------------------------------------------------------

_DOC_SEQ = 0


@pytest.fixture()
def lab_patient(lab_admin):
    global _DOC_SEQ
    _DOC_SEQ += 1
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number=f'NID-LP-{_DOC_SEQ:04d}',
        first_name='Linked', last_name='Patient',
        date_of_birth=date(1990, 5, 17), gender='FEMALE',
        created_by=lab_admin,
    )


@pytest.fixture()
def exam_def():
    cat = ExamCategory.objects.create(name='Labs', display_order=1)
    fam = ExamFamily.objects.create(name='Hematology', display_order=1)
    tech = ExamTechnique.objects.create(name='Spectrophotometry')
    return ExamDefinition.objects.create(
        category=cat, family=fam, technique=tech,
        code='GLU', name='Glucose', sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.SINGLE_VALUE,
        unit='mg/dL', reference_range='70-100',
    )


def _build_finalized_request(
    *, lab_patient, exam_def, lab_admin, technician, biologist, make_request,
):
    """Walk a request to VALIDATED + report v1 — same helper pattern
    as Phase 1's notify-cytova test. Duplicated here so the file is
    self-contained."""
    ar = AnalysisRequestService.create(
        validated_data={
            'patient_id': lab_patient.id,
            'source_type': SourceType.DIRECT_PATIENT,
            'items': [{'exam_definition_id': exam_def.id}],
        },
        created_by=lab_admin, request=make_request(lab_admin),
        confirm_after=True,
    )
    req_tech = make_request(technician)
    req_bio = make_request(biologist)
    for item in ar.items.all():
        AnalysisRequestItemService.mark_collected(
            item=item, collected_by=technician, request=req_tech,
        )
    for item in ar.items.select_related('exam_definition').all():
        v = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req_tech,
            result_value='85',
            values=[{'value': '85', 'is_abnormal': False}],
            comments='Fasting confirmed.',
        )
        ResultVersionService.submit(version=v, submitted_by=technician, request=req_tech)
        v = item.result_versions.get(is_current=True)
        ResultVersionService.validate(
            version=v, validation_notes='OK',
            validated_by=biologist, request=req_bio,
        )
    ar.refresh_from_db()
    AnalysisRequestService.finalize_validation(
        analysis_request=ar, finalized_by=biologist, request=req_bio,
    )
    ar.refresh_from_db()
    RequestReportService.generate_or_get(
        analysis_request=ar, generated_by=biologist,
        request=make_request(biologist),
    )
    ar.refresh_from_db()
    return ar


# ---------------------------------------------------------------------------
# Patient portal account + helper to apply the link
# ---------------------------------------------------------------------------

@pytest.fixture()
def portal_account():
    return register_patient_account(
        email='linked-path@portal.test',
        password='Strong-Pass-1234!',
        first_name='Linked', last_name='Patient',
        date_of_birth=date(1990, 5, 17), accept_terms=True,
    )


def _link(patient, portal_account, *, by_user, request) -> Patient:
    """Apply the link snapshot via the Phase B service. Each test that
    needs a linked patient calls this once at setup so we exercise
    the same path the receptionist would trigger from the UI."""
    profile = portal_account.profile
    return PatientService.link_cytova_identity(
        patient=patient,
        cytova_patient_id=profile.cytova_patient_id,
        first_name=profile.first_name,
        last_name=profile.last_name,
        date_of_birth=profile.date_of_birth,
        actor=by_user,
        request=request,
    )


# ---------------------------------------------------------------------------
# HTTP fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def api_client():
    return APIClient(HTTP_HOST='testlab.localhost')


@pytest.fixture()
def admin_client(api_client, lab_admin):
    api_client.force_authenticate(user=lab_admin)
    return api_client


def _explicit_payload(portal_account) -> dict:
    profile = portal_account.profile
    return {
        'cytova_patient_id': profile.cytova_patient_id,
        'first_name': profile.first_name,
        'last_name': profile.last_name,
        'date_of_birth': profile.date_of_birth.isoformat(),
    }


# ---------------------------------------------------------------------------
# 1. Linked patient + empty body — the new default UX
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestLinkedPathEmptyBody:

    def test_share_succeeds_without_identity_payload(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _build_finalized_request(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician,
            biologist=biologist, make_request=make_request,
        )
        # Link first — same path the Phase E UI will trigger.
        _link(lab_patient, portal_account,
              by_user=lab_admin, request=make_request(lab_admin))

        # Empty body — the operator never re-types identity.
        resp = admin_client.post(
            NOTIFY_URL.format(ar_id=ar.id), data={}, format='json',
        )
        assert resp.status_code == 200, resp.content

        body = resp.json()['data']
        shared = PatientSharedResult.objects.get(pk=body['shared_result_id'])
        # The snapshot row points at the same global account the link
        # snapshot pointed at — proves the linked path resolved
        # correctly rather than silently using a stale handle.
        assert shared.patient_account_id == portal_account.id


# ---------------------------------------------------------------------------
# 2. Back-compat: linked patient + explicit body still works
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestExplicitPayloadStillWorks:

    def test_explicit_payload_uses_back_compat_path(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        """Explicit identity always wins — the service runs the
        original ``verify_patient_identity`` flow regardless of link
        state. Pre-Phase-D callers don't need to change anything."""
        ar = _build_finalized_request(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician,
            biologist=biologist, make_request=make_request,
        )
        # No link applied — the explicit-payload path doesn't depend
        # on it. This is exactly the pre-Phase-D contract.
        resp = admin_client.post(
            NOTIFY_URL.format(ar_id=ar.id),
            data=_explicit_payload(portal_account),
            format='json',
        )
        assert resp.status_code == 200, resp.content


# ---------------------------------------------------------------------------
# 3. Unlinked patient + empty body — clear recovery hint
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestUnlinkedRequiresIdentity:

    def test_unlinked_patient_with_empty_body_returns_missing_identity(
        self, admin_client, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _build_finalized_request(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician,
            biologist=biologist, make_request=make_request,
        )
        # No link applied + no identity payload → the safety net
        # fires. The Phase E UI hides this surface, but a caller
        # bypassing the UX still gets a clean recovery hint.
        resp = admin_client.post(
            NOTIFY_URL.format(ar_id=ar.id), data={}, format='json',
        )
        assert resp.status_code == 400, resp.content
        assert resp.json()['errors'][0]['code'] == 'MISSING_IDENTITY'


# ---------------------------------------------------------------------------
# 4. Linked patient with deactivated global account — safe error + audit
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestLinkedAccountInactive:

    def test_deactivated_global_account_surfaces_generic_failure(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        """Critical security invariant: the operator sees the SAME
        generic ``IDENTITY_VERIFICATION_FAILED`` message as a fresh
        interactive mismatch. No info leak about why
        (deactivated-vs-mismatched). The audit row carries the
        ``LINKED_ACCOUNT_INACTIVE`` marker so an investigator can
        tell them apart server-side."""
        ar = _build_finalized_request(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician,
            biologist=biologist, make_request=make_request,
        )
        _link(lab_patient, portal_account,
              by_user=lab_admin, request=make_request(lab_admin))

        # Deactivate the global account — simulates the patient
        # closing their Cytova account or an admin disabling it.
        with schema_context(get_public_schema_name()):
            PatientAccount.objects.filter(id=portal_account.id).update(
                is_active=False,
            )

        resp = admin_client.post(
            NOTIFY_URL.format(ar_id=ar.id), data={}, format='json',
        )
        # Same generic 400 — no leak.
        assert resp.status_code == 400, resp.content
        assert resp.json()['errors'][0]['code'] == 'IDENTITY_VERIFICATION_FAILED'

        # Audit row carries the distinguishing marker so an
        # investigator can tell "linked-but-now-inactive" apart from
        # a fresh interactive mismatch.
        rows = list(AuditLog.objects.filter(
            entity_type='AnalysisRequest', entity_id=ar.id,
            action=AuditAction.UPDATE,
        ))
        assert any(
            r.diff.get('after', {}).get('notify_cytova_outcome')
            == 'LINKED_ACCOUNT_INACTIVE'
            for r in rows
        ), [r.diff for r in rows]


# ---------------------------------------------------------------------------
# 5. Lab setting kill switch
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestLabSettingDisabled:

    def test_disabled_setting_blocks_endpoint_with_clear_code(
        self, admin_client, portal_account, lab_patient, exam_def,
        lab_admin, technician, biologist, make_request,
    ):
        ar = _build_finalized_request(
            lab_patient=lab_patient, exam_def=exam_def,
            lab_admin=lab_admin, technician=technician,
            biologist=biologist, make_request=make_request,
        )
        _link(lab_patient, portal_account,
              by_user=lab_admin, request=make_request(lab_admin))

        # Toggle off. Pre-checked before serializer validation so the
        # operator never gets a misleading "missing fields" error.
        settings = LabSettings.get_solo()
        settings.notification_enable_cytova = False
        settings.save(update_fields=['notification_enable_cytova', 'updated_at'])

        resp = admin_client.post(
            NOTIFY_URL.format(ar_id=ar.id), data={}, format='json',
        )
        assert resp.status_code == 400, resp.content
        assert resp.json()['errors'][0]['code'] == 'CYTOVA_CHANNEL_DISABLED'

        # No share row was created — the kill switch fires before the
        # snapshot path runs.
        assert not PatientSharedResult.objects.filter(
            source_request_id=ar.id,
        ).exists()
