"""
Phase B — Cytova patient-identity link/unlink endpoints.

HTTP-level coverage for the receptionist's "Add Cytova identity"
workflow. The link service shares its identity-verification surface
with Notify-Cytova (same ``verify_patient_identity`` call site), so
the matching rules — and the single non-distinguishing failure
surface — stay aligned across the two entrypoints.

What's pinned here
------------------
- Successful link flips ``Patient.has_cytova_identity`` True and
  populates the snapshot + ``verified_at/by``; writes a single
  PATIENT_CYTOVA_IDENTITY_LINKED audit row.
- Identity mismatch surfaces a generic 400 + writes a brute-force-
  observability audit row with ONLY the truncated attempted
  Cytova ID. Patient PII never appears in any response or audit
  payload.
- Re-linking on an already-linked patient refuses with 409
  ALREADY_LINKED — the operator must unlink first to keep audit
  lineage continuous.
- Unlink clears the snapshot, stamps unlinked_at/by, preserves
  ``verified_at/by`` (historical truth survives), writes a
  PATIENT_CYTOVA_IDENTITY_UNLINKED audit row with the previous
  snapshot in the diff.
- Unlink on an unlinked patient is a no-op (no audit, no error)
  so the UI can fire-and-forget.
- Non-staff and unauthenticated callers are rejected.
"""
from __future__ import annotations

from datetime import date

import pytest
from django.core.cache import cache
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.test import APIClient

from apps.audit.models import AuditAction, AuditLog
from apps.patient_portal.services import register_patient_account
from apps.patients.models import Patient


LINK_URL = '/api/v1/patients/{pk}/link-cytova-identity/'
UNLINK_URL = '/api/v1/patients/{pk}/unlink-cytova-identity/'


# ---------------------------------------------------------------------------
# Subscription + cache fixtures (mirror sibling lab-side suites)
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


# ---------------------------------------------------------------------------
# Domain fixtures
# ---------------------------------------------------------------------------

_DOC_SEQ = 0


def _make_patient(*, lab_admin, **overrides) -> Patient:
    """Tiny factory — mirrors the helper in test_cytova_identity_link.py
    but lives here so the API tests are self-contained."""
    global _DOC_SEQ
    _DOC_SEQ += 1
    defaults: dict = {
        'document_type': 'NATIONAL_ID_CARD',
        'document_number': f'NID-LINK-{_DOC_SEQ:04d}',
        'first_name': 'Ada',
        'last_name': 'Lovelace',
        'date_of_birth': date(1990, 5, 17),
        'gender': 'FEMALE',
        'created_by': lab_admin,
    }
    defaults.update(overrides)
    return Patient.objects.create(**defaults)


@pytest.fixture()
def portal_account():
    """Real PatientAccount + PatientProfile in the public schema. The
    helper auto-generates a Cytova Patient ID (``CV-XXXX-XXXX``) we
    then quote back in the link payload."""
    return register_patient_account(
        email='link-test@portal.test',
        password='Strong-Pass-1234!',
        first_name='Ada', last_name='Lovelace',
        date_of_birth=date(1990, 5, 17), accept_terms=True,
    )


@pytest.fixture()
def api_client():
    return APIClient(HTTP_HOST='testlab.localhost')


@pytest.fixture()
def admin_client(api_client, lab_admin):
    api_client.force_authenticate(user=lab_admin)
    return api_client


def _link_payload(portal_account, *, override: dict | None = None) -> dict:
    profile = portal_account.profile
    body = {
        'cytova_patient_id': profile.cytova_patient_id,
        'first_name': profile.first_name,
        'last_name': profile.last_name,
        'date_of_birth': profile.date_of_birth.isoformat(),
    }
    if override:
        body.update(override)
    return body


# ---------------------------------------------------------------------------
# 1. Link success
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestLinkSuccess:

    def test_link_populates_snapshot_and_returns_200(
        self, admin_client, lab_admin, portal_account,
    ):
        local = _make_patient(lab_admin=lab_admin)
        resp = admin_client.post(
            LINK_URL.format(pk=local.id),
            data=_link_payload(portal_account),
            format='json',
        )
        assert resp.status_code == 200, resp.content

        local.refresh_from_db()
        # Snapshot is populated on both halves of the link.
        assert local.cytova_patient_id == portal_account.profile.cytova_patient_id
        assert local.cytova_patient_account_id == portal_account.id
        # Verified metadata reflects the actor + timestamp.
        assert local.cytova_identity_verified_at is not None
        assert local.cytova_identity_verified_by_id == lab_admin.id
        # And the convenience flag flips.
        assert local.has_cytova_identity is True

    def test_link_writes_audit_row_with_safe_metadata_only(
        self, admin_client, lab_admin, portal_account,
    ):
        local = _make_patient(lab_admin=lab_admin)
        admin_client.post(
            LINK_URL.format(pk=local.id),
            data=_link_payload(portal_account),
            format='json',
        )

        rows = list(AuditLog.objects.filter(
            entity_type='Patient', entity_id=local.id,
            action=AuditAction.PATIENT_CYTOVA_IDENTITY_LINKED,
        ))
        assert len(rows) == 1
        diff_after = rows[0].diff['after']
        # IDs already known to both sides are safe.
        assert diff_after['cytova_patient_id'] == portal_account.profile.cytova_patient_id
        assert diff_after['cytova_patient_account_id'] == str(portal_account.id)
        # Patient PII is NEVER written into the audit metadata —
        # the global PatientAccount already holds it; we don't keep
        # a tenant-side copy in the audit log.
        flat = repr(diff_after)
        assert 'Ada' not in flat
        assert 'Lovelace' not in flat
        assert 'link-test@portal.test' not in flat

    def test_link_response_does_not_leak_global_patient_data(
        self, admin_client, lab_admin, portal_account,
    ):
        """The link response is the patient detail payload — and that
        payload must NOT carry the global PatientAccount's email or
        any global profile field. (Phase C exposes safe link-status
        fields; Phase B's response contract is "doesn't leak".)"""
        local = _make_patient(lab_admin=lab_admin)
        resp = admin_client.post(
            LINK_URL.format(pk=local.id),
            data=_link_payload(portal_account),
            format='json',
        )
        body = resp.content.decode()
        assert 'link-test@portal.test' not in body


# ---------------------------------------------------------------------------
# 2. Link failure — generic error, no PII leakage, audit observable
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestLinkFailure:

    def test_wrong_name_returns_generic_error(
        self, admin_client, lab_admin, portal_account,
    ):
        local = _make_patient(lab_admin=lab_admin)
        resp = admin_client.post(
            LINK_URL.format(pk=local.id),
            data=_link_payload(portal_account, override={'first_name': 'Wrong'}),
            format='json',
        )
        assert resp.status_code == 400, resp.content
        body = resp.json()
        # Single non-distinguishing failure — never says which field
        # failed. Same code Notify-Cytova uses, so the UI handles
        # both surfaces with one branch.
        assert body['errors'][0]['code'] == 'IDENTITY_VERIFICATION_FAILED'
        assert body['data'] is None

        # Patient stays unlinked.
        local.refresh_from_db()
        assert local.has_cytova_identity is False
        assert local.cytova_patient_id == ''

    def test_wrong_dob_returns_generic_error(
        self, admin_client, lab_admin, portal_account,
    ):
        local = _make_patient(lab_admin=lab_admin)
        resp = admin_client.post(
            LINK_URL.format(pk=local.id),
            data=_link_payload(portal_account, override={
                'date_of_birth': '1900-01-01',
            }),
            format='json',
        )
        assert resp.status_code == 400, resp.content
        assert resp.json()['errors'][0]['code'] == 'IDENTITY_VERIFICATION_FAILED'

    def test_unknown_cytova_id_returns_generic_error(
        self, admin_client, lab_admin, portal_account,
    ):
        local = _make_patient(lab_admin=lab_admin)
        resp = admin_client.post(
            LINK_URL.format(pk=local.id),
            data=_link_payload(portal_account, override={
                'cytova_patient_id': 'CV-AAAA-ZZZZ',  # not in DB
            }),
            format='json',
        )
        assert resp.status_code == 400, resp.content
        assert resp.json()['errors'][0]['code'] == 'IDENTITY_VERIFICATION_FAILED'

    def test_failed_attempt_writes_audit_with_only_truncated_id(
        self, admin_client, lab_admin, portal_account,
    ):
        """Brute-force-detection audit: every failed verification
        leaves a trail. The trail carries only the (already-public)
        attempted Cytova ID — never the wrong-name or wrong-DOB the
        attacker might be probing."""
        local = _make_patient(lab_admin=lab_admin)
        admin_client.post(
            LINK_URL.format(pk=local.id),
            data=_link_payload(portal_account, override={
                'first_name': 'Probe-Name-Should-Not-Audit',
            }),
            format='json',
        )

        rows = list(AuditLog.objects.filter(
            entity_type='Patient', entity_id=local.id,
            action=AuditAction.UPDATE,
        ))
        assert len(rows) == 1
        diff_after = rows[0].diff['after']
        assert diff_after['cytova_link_outcome'] == 'IDENTITY_MISMATCH'
        # The attempted Cytova ID IS recorded — it's already public.
        assert diff_after['cytova_patient_id_attempted'] == portal_account.profile.cytova_patient_id
        # But the wrong-name / wrong-DOB the attacker probed must
        # NOT appear in the audit row anywhere.
        flat = repr(rows[0].diff)
        assert 'Probe-Name-Should-Not-Audit' not in flat


# ---------------------------------------------------------------------------
# 3. ALREADY_LINKED — relinking refused; operator must unlink first
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestRelinkRefused:

    def test_relinking_already_linked_patient_returns_409(
        self, admin_client, lab_admin, portal_account,
    ):
        local = _make_patient(lab_admin=lab_admin)
        # First link succeeds.
        first = admin_client.post(
            LINK_URL.format(pk=local.id),
            data=_link_payload(portal_account), format='json',
        )
        assert first.status_code == 200

        # Second attempt — even with the SAME identity payload —
        # refuses with 409 so the audit lineage stays clean.
        second = admin_client.post(
            LINK_URL.format(pk=local.id),
            data=_link_payload(portal_account), format='json',
        )
        assert second.status_code == 409, second.content
        assert second.json()['errors'][0]['code'] == 'ALREADY_LINKED'

    def test_relink_after_unlink_succeeds(
        self, admin_client, lab_admin, portal_account,
    ):
        """The operator's recovery path: unlink, then re-link to the
        same (or a different) global identity. Audit log keeps the
        full link → unlink → link history."""
        local = _make_patient(lab_admin=lab_admin)
        admin_client.post(
            LINK_URL.format(pk=local.id),
            data=_link_payload(portal_account), format='json',
        )
        admin_client.post(UNLINK_URL.format(pk=local.id))
        relink = admin_client.post(
            LINK_URL.format(pk=local.id),
            data=_link_payload(portal_account), format='json',
        )
        assert relink.status_code == 200, relink.content

        # All three audit rows survive.
        actions = list(
            AuditLog.objects
            .filter(entity_type='Patient', entity_id=local.id)
            .order_by('timestamp')
            .values_list('action', flat=True)
        )
        assert AuditAction.PATIENT_CYTOVA_IDENTITY_LINKED in actions
        assert AuditAction.PATIENT_CYTOVA_IDENTITY_UNLINKED in actions
        # And the count of LINKED rows is two — the original + the
        # relink. Neither overwrote the other.
        link_count = sum(
            1 for a in actions if a == AuditAction.PATIENT_CYTOVA_IDENTITY_LINKED
        )
        assert link_count == 2


# ---------------------------------------------------------------------------
# 4. Unlink — clears snapshot, preserves verified_at history
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestUnlink:

    def test_unlink_clears_snapshot_and_stamps_unlinked(
        self, admin_client, lab_admin, portal_account,
    ):
        local = _make_patient(lab_admin=lab_admin)
        admin_client.post(
            LINK_URL.format(pk=local.id),
            data=_link_payload(portal_account), format='json',
        )

        resp = admin_client.post(UNLINK_URL.format(pk=local.id))
        assert resp.status_code == 200, resp.content

        local.refresh_from_db()
        # Live link is cleared.
        assert local.cytova_patient_id == ''
        assert local.cytova_patient_account_id is None
        assert local.has_cytova_identity is False
        # Unlinked-at stamp records when the link became inactive.
        assert local.cytova_identity_unlinked_at is not None
        assert local.cytova_identity_unlinked_by_id == lab_admin.id
        # ``verified_at`` / ``verified_by`` are NOT cleared — the
        # historical truth (this patient WAS verified at time T by
        # user U) survives the unlink. The combination of unlinked_at
        # + verified_at lets an audit reader see "verified at T0,
        # unlinked at T1 > T0".
        assert local.cytova_identity_verified_at is not None
        assert local.cytova_identity_verified_by_id == lab_admin.id

    def test_unlink_writes_audit_with_previous_snapshot(
        self, admin_client, lab_admin, portal_account,
    ):
        local = _make_patient(lab_admin=lab_admin)
        admin_client.post(
            LINK_URL.format(pk=local.id),
            data=_link_payload(portal_account), format='json',
        )
        admin_client.post(UNLINK_URL.format(pk=local.id))

        rows = list(AuditLog.objects.filter(
            entity_type='Patient', entity_id=local.id,
            action=AuditAction.PATIENT_CYTOVA_IDENTITY_UNLINKED,
        ))
        assert len(rows) == 1
        # Diff captures the *previous* snapshot — so even after the
        # live row is cleared, the audit chain remains complete.
        diff_before = rows[0].diff['before']
        assert diff_before['cytova_patient_id'] == portal_account.profile.cytova_patient_id
        assert diff_before['cytova_patient_account_id'] == str(portal_account.id)

    def test_unlink_on_unlinked_patient_is_idempotent(
        self, admin_client, lab_admin,
    ):
        """No link to clear → no audit row, no error. UI can
        fire-and-forget without tracking local state."""
        local = _make_patient(lab_admin=lab_admin)
        resp = admin_client.post(UNLINK_URL.format(pk=local.id))
        assert resp.status_code == 200, resp.content
        assert AuditLog.objects.filter(
            entity_type='Patient', entity_id=local.id,
            action=AuditAction.PATIENT_CYTOVA_IDENTITY_UNLINKED,
        ).count() == 0


# ---------------------------------------------------------------------------
# 5. Permission gate
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestPermissionGate:

    def test_unauthenticated_link_blocked(
        self, api_client, lab_admin, portal_account,
    ):
        local = _make_patient(lab_admin=lab_admin)
        resp = api_client.post(
            LINK_URL.format(pk=local.id),
            data=_link_payload(portal_account), format='json',
        )
        assert resp.status_code in (401, 403), resp.content

    def test_unauthenticated_unlink_blocked(
        self, api_client, lab_admin,
    ):
        local = _make_patient(lab_admin=lab_admin)
        resp = api_client.post(UNLINK_URL.format(pk=local.id))
        assert resp.status_code in (401, 403), resp.content

    def test_technician_cannot_link(
        self, api_client, lab_admin, technician, portal_account,
    ):
        """Same gate as Notify-Cytova / notify-by-email: receptionist
        + lab admin only. Technicians (and viewers / inventory) are
        explicitly NOT allowed to link patient identities."""
        api_client.force_authenticate(user=technician)
        local = _make_patient(lab_admin=lab_admin)
        resp = api_client.post(
            LINK_URL.format(pk=local.id),
            data=_link_payload(portal_account), format='json',
        )
        assert resp.status_code == 403, resp.content
