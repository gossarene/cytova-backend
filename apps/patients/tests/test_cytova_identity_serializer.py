"""
Phase C — PatientDetailSerializer exposure of the Cytova-link
snapshot.

What's pinned here
------------------
- An unlinked patient surfaces ``has_cytova_identity=False`` and the
  rest of the link fields as null/empty — no special branch needed
  in the UI.
- A linked patient surfaces all the safe fields, and
  ``cytova_identity_verified_by_display`` resolves to the linker's
  display name via the same ``user.display_name`` convention the
  requests serializer uses.
- The internal ``cytova_patient_account_id`` snapshot is NEVER
  serialised — it's a backend cross-schema reference, not part of
  the patient's UI contract.
- No global ``PatientAccount`` data ever appears in the response —
  the link is a *snapshot*, not a join. The lab tenant must never
  carry a serialised copy of the global patient's email / name / DOB.
- After the linking staff user is removed (SET_NULL on the FK),
  ``cytova_identity_verified_at`` survives but
  ``cytova_identity_verified_by_display`` falls back to ``None``
  cleanly — no AttributeError when the UI reads it.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from django.utils import timezone

from apps.patients.models import DocumentType, Patient
from apps.patients.serializers import PatientDetailSerializer


_DOC_SEQ = 0


def _make_patient(*, lab_admin, **overrides) -> Patient:
    global _DOC_SEQ
    _DOC_SEQ += 1
    defaults: dict = {
        'document_type': DocumentType.NATIONAL_ID_CARD,
        'document_number': f'NID-SER-{_DOC_SEQ:04d}',
        'first_name': 'Ada',
        'last_name': 'Lovelace',
        'date_of_birth': date(1990, 5, 17),
        'gender': 'FEMALE',
        'created_by': lab_admin,
    }
    defaults.update(overrides)
    return Patient.objects.create(**defaults)


# ---------------------------------------------------------------------------
# Unlinked patient — every link field reads as the default, no leak surface
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestUnlinkedPatientPayload:

    def test_link_fields_present_with_default_values(self, lab_admin):
        local = _make_patient(lab_admin=lab_admin)
        data = PatientDetailSerializer(local).data

        # Every link field is present so the UI can read it without
        # branching on its existence — defaults reflect the unlinked
        # state.
        assert data['has_cytova_identity'] is False
        assert data['cytova_patient_id'] == ''
        assert data['cytova_identity_verified_at'] is None
        assert data['cytova_identity_verified_by_display'] is None
        assert data['cytova_identity_unlinked_at'] is None

    def test_internal_account_id_snapshot_never_appears(self, lab_admin):
        """``cytova_patient_account_id`` is the backend's cross-schema
        reference — useful for re-verification at notify time, but
        never part of the lab UI contract. The serializer must not
        leak it under either the snake_case field name or as a
        camelCase variant."""
        local = _make_patient(lab_admin=lab_admin)
        data = PatientDetailSerializer(local).data
        assert 'cytova_patient_account_id' not in data
        assert 'cytovaPatientAccountId' not in data


# ---------------------------------------------------------------------------
# Linked patient — safe fields populated; account_id still hidden
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestLinkedPatientPayload:

    def _link(self, patient: Patient, *, by_user) -> None:
        """Apply a link snapshot directly on the model. We don't need
        the link service for this test — the serializer reads the
        model fields, and what we're pinning is the read-side
        contract."""
        patient.cytova_patient_id = 'CV-SER-0001'
        patient.cytova_patient_account_id = uuid.uuid4()
        patient.cytova_identity_verified_at = timezone.now()
        patient.cytova_identity_verified_by = by_user
        patient.save(update_fields=[
            'cytova_patient_id', 'cytova_patient_account_id',
            'cytova_identity_verified_at', 'cytova_identity_verified_by',
            'updated_at',
        ])

    def test_link_fields_populated(self, lab_admin):
        local = _make_patient(lab_admin=lab_admin)
        self._link(local, by_user=lab_admin)
        local.refresh_from_db()

        data = PatientDetailSerializer(local).data
        assert data['has_cytova_identity'] is True
        assert data['cytova_patient_id'] == 'CV-SER-0001'
        assert data['cytova_identity_verified_at'] is not None
        # ``verified_by_display`` resolves through ``user.display_name``
        # — same convention the requests serializer uses, so the
        # frontend renders both lab- and patient-side actor names
        # identically.
        assert data['cytova_identity_verified_by_display'] == lab_admin.display_name

    def test_account_id_snapshot_never_serialised_after_link(self, lab_admin):
        """Spike a known sentinel into the snapshot UUID and confirm
        it doesn't appear anywhere in the serialised payload. Catches
        a regression where a future field-set widening might
        re-introduce the cross-schema id by accident."""
        local = _make_patient(lab_admin=lab_admin)
        sentinel = uuid.UUID('11111111-aaaa-bbbb-cccc-222222222222')
        local.cytova_patient_id = 'CV-SER-0002'
        local.cytova_patient_account_id = sentinel
        local.cytova_identity_verified_at = timezone.now()
        local.cytova_identity_verified_by = lab_admin
        local.save()

        data = PatientDetailSerializer(local).data
        assert 'cytova_patient_account_id' not in data
        # And the sentinel string doesn't surface anywhere — even
        # under a future field with a different name.
        assert str(sentinel) not in repr(data)


# ---------------------------------------------------------------------------
# Privacy: no global PatientAccount data ever leaks
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestNoGlobalPatientDataLeak:

    def test_global_account_email_never_surfaces(self, lab_admin):
        """The link is a snapshot — the lab tenant carries the Cytova
        ID, the account UUID, and verified-at metadata. It does NOT
        carry a copy of the global patient's email. Confirm by
        spiking a recognisable local email AND verifying that the
        serialiser exposes ONLY the local field, not a global lookup.

        This test deliberately doesn't materialise a real
        PatientAccount — the rule under test is structural: the
        serializer never joins to the public schema, regardless of
        what happens to live there."""
        local = _make_patient(
            lab_admin=lab_admin,
            email='LOCAL-ONLY@lab.test',  # the LOCAL patient email
        )
        local.cytova_patient_id = 'CV-SER-0003'
        local.cytova_patient_account_id = uuid.uuid4()
        local.cytova_identity_verified_at = timezone.now()
        local.cytova_identity_verified_by = lab_admin
        local.save()

        data = PatientDetailSerializer(local).data
        # Local email is exposed under the existing local-patient
        # contract. That's expected.
        assert data['email'] == 'LOCAL-ONLY@lab.test'
        # No "global_email" / "patient_account_email" / etc. surface
        # under any name. The flat repr catches accidental nesting.
        flat = repr(data)
        assert 'global_email' not in flat
        assert 'patient_account_email' not in flat
        assert 'cytova_email' not in flat


# ---------------------------------------------------------------------------
# SET_NULL graceful fallback — the staff user can disappear without breaking
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestVerifiedByDisplayWithDeletedUser:

    def test_verified_by_display_is_none_after_staff_removed(
        self, lab_admin, technician,
    ):
        """The FK ``cytova_identity_verified_by`` is SET_NULL by
        design (off-boarding shouldn't break a previously-verified
        link). After the staff user is removed:

          - ``verified_at`` survives (the historical truth: this
            patient *was* verified at time T),
          - ``verified_by_display`` resolves to ``None`` cleanly so
            the UI just shows the timestamp without an actor name.

        Catches the regression where a missing FK would AttributeError
        at serialisation time."""
        local = _make_patient(lab_admin=lab_admin)
        local.cytova_patient_id = 'CV-SER-0004'
        local.cytova_patient_account_id = uuid.uuid4()
        local.cytova_identity_verified_at = timezone.now()
        local.cytova_identity_verified_by = technician
        local.save()

        # Off-board the technician — StaffUser is hard-deletable
        # (unlike Patient).
        technician.delete()
        local.refresh_from_db()

        data = PatientDetailSerializer(local).data
        # Link itself preserved.
        assert data['has_cytova_identity'] is True
        assert data['cytova_identity_verified_at'] is not None
        # Display resolves cleanly to None — no AttributeError.
        assert data['cytova_identity_verified_by_display'] is None
