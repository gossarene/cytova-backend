"""
Phase A — Patient model fields for the lab → Cytova identity link.

Pure model-layer coverage. The link/unlink endpoints, serializer
exposure, and Notify-Cytova reuse are deliberately out of scope for
Phase A — only the field shape, defaults, partial-unique constraint,
and the ``has_cytova_identity`` helper are exercised here.

Why a partial unique
--------------------
``cytova_patient_id`` is the patient-facing global Cytova ID
(CV-XXXX-XXXX). Two local patients pointing at the same global ID
would almost certainly indicate an operator error or stale record,
so the model carries a unique constraint — but only over non-empty
values, since every unlinked row carries the empty-string default.
A plain unique index would refuse to keep more than one unlinked
row in the table.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.patients.models import DocumentType, Patient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DOC_SEQ = 0


def _make_patient(*, lab_admin, **overrides) -> Patient:
    """Tiny factory — keeps each test focused on the field behaviour
    rather than re-stating six required Patient columns. Increments a
    document number so the existing
    ``unique(document_type, document_number)`` constraint never
    collides between tests."""
    global _DOC_SEQ
    _DOC_SEQ += 1
    defaults: dict = {
        'document_type': DocumentType.NATIONAL_ID_CARD,
        'document_number': f'NID-CYT-{_DOC_SEQ:04d}',
        'first_name': 'Ada',
        'last_name': 'Lovelace',
        'date_of_birth': date(1990, 5, 17),
        'gender': 'FEMALE',
        'created_by': lab_admin,
    }
    defaults.update(overrides)
    return Patient.objects.create(**defaults)


# ---------------------------------------------------------------------------
# Field defaults — every Cytova field is unlinked-by-default
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestCytovaFieldDefaults:

    def test_freshly_created_patient_has_no_cytova_link(self, lab_admin):
        p = _make_patient(lab_admin=lab_admin)

        # Both halves of the snapshot are unset on a fresh row.
        assert p.cytova_patient_id == ''
        assert p.cytova_patient_account_id is None
        # Verified / unlinked metadata starts cleared.
        assert p.cytova_identity_verified_at is None
        assert p.cytova_identity_verified_by is None
        assert p.cytova_identity_unlinked_at is None
        assert p.cytova_identity_unlinked_by is None
        # Convenience helper reflects the unlinked state — the UI and
        # the Notify-Cytova service rely on this single flag instead
        # of repeating the two-field check at every call site.
        assert p.has_cytova_identity is False

    def test_partial_link_does_not_count_as_linked(self, lab_admin):
        """Defensive check: if a future bug populates only one half of
        the snapshot, ``has_cytova_identity`` must still read False so
        the rest of the system can't be tricked into acting on a
        half-state row."""
        p = _make_patient(lab_admin=lab_admin)
        p.cytova_patient_id = 'CV-AAAA-BBBB'  # ID set, account_id missing
        p.save(update_fields=['cytova_patient_id'])
        assert p.has_cytova_identity is False

        # And the inverse: account_id set, ID missing.
        p2 = _make_patient(lab_admin=lab_admin)
        p2.cytova_patient_account_id = uuid.uuid4()
        p2.save(update_fields=['cytova_patient_account_id'])
        assert p2.has_cytova_identity is False


# ---------------------------------------------------------------------------
# Linking — ``has_cytova_identity`` flips True only when both halves are set
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestCytovaLinkSnapshot:

    def test_full_link_flips_helper_true(self, lab_admin):
        p = _make_patient(lab_admin=lab_admin)
        p.cytova_patient_id = 'CV-1234-5678'
        p.cytova_patient_account_id = uuid.uuid4()
        p.cytova_identity_verified_at = timezone.now()
        p.cytova_identity_verified_by = lab_admin
        p.save(update_fields=[
            'cytova_patient_id', 'cytova_patient_account_id',
            'cytova_identity_verified_at', 'cytova_identity_verified_by',
        ])

        p.refresh_from_db()
        assert p.has_cytova_identity is True
        # Verified-by is a SET_NULL FK, not a hard reference — nullable
        # for the case where the staff user is later deactivated.
        assert p.cytova_identity_verified_by_id == lab_admin.id


# ---------------------------------------------------------------------------
# Partial unique constraint — only non-empty Cytova IDs compete
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestCytovaIdPartialUnique:

    def test_two_unlinked_patients_can_coexist(self, lab_admin):
        """Default (empty-string) Cytova IDs do not collide. A plain
        unique index would reject the second row; the partial
        constraint's NOT-empty filter is precisely what makes
        unlinked-by-default rows free to multiply."""
        _make_patient(lab_admin=lab_admin)
        _make_patient(lab_admin=lab_admin)
        # Sanity: the table really does contain two unlinked rows.
        assert Patient.objects.filter(cytova_patient_id='').count() >= 2

    def test_two_patients_cannot_share_a_non_empty_cytova_id(self, lab_admin):
        """Two local patients both pointing at the same global Cytova
        ID is the operator error the constraint exists to prevent."""
        cytova_id = 'CV-DUPE-0001'
        _make_patient(lab_admin=lab_admin, cytova_patient_id=cytova_id)

        with pytest.raises(IntegrityError):
            _make_patient(lab_admin=lab_admin, cytova_patient_id=cytova_id)

    def test_relinking_after_unlink_works(self, lab_admin):
        """Once a patient is unlinked (cytova_patient_id back to empty),
        the same global Cytova ID can be linked to a different local
        patient. Critical for receptionist workflows where a
        mis-linked record gets corrected by unlinking and pointing
        the right local patient at the global ID."""
        cytova_id = 'CV-MOVE-0001'
        first = _make_patient(lab_admin=lab_admin, cytova_patient_id=cytova_id)
        # Unlink — partial constraint frees the value.
        first.cytova_patient_id = ''
        first.save(update_fields=['cytova_patient_id'])

        # Linking another patient to the same global ID now succeeds.
        second = _make_patient(lab_admin=lab_admin, cytova_patient_id=cytova_id)
        assert second.cytova_patient_id == cytova_id


# ---------------------------------------------------------------------------
# Cross-schema reference rule — account_id is a UUID snapshot, not a FK
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestCytovaAccountIdIsSnapshot:

    def test_arbitrary_uuid_is_accepted(self, lab_admin):
        """``cytova_patient_account_id`` is intentionally NOT a foreign
        key — patient_portal tables live in the public schema and
        cross-schema FKs aren't supported under django-tenants. This
        test pins the rule by writing a UUID that doesn't correspond
        to any existing PatientAccount row; the model must accept it
        without raising. Validity is re-checked at use time
        (Phase D), not enforced by the schema here."""
        random_uuid = uuid.uuid4()
        p = _make_patient(
            lab_admin=lab_admin,
            cytova_patient_account_id=random_uuid,
        )
        p.refresh_from_db()
        assert p.cytova_patient_account_id == random_uuid


# ---------------------------------------------------------------------------
# SET_NULL on staff user — preserves the audit trail when staff are removed
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestVerifiedByOnStaffDelete:

    def test_verified_by_clears_to_null_when_staff_user_is_deleted(
        self, lab_admin, technician,
    ):
        """The link audit (``cytova_identity_verified_at``) must
        survive deactivation/removal of the staff user who performed
        it. SET_NULL on the FK keeps the timestamp + the rest of the
        row intact while clearing the now-orphaned reference."""
        p = _make_patient(
            lab_admin=lab_admin,
            cytova_patient_id='CV-AUD-0001',
            cytova_patient_account_id=uuid.uuid4(),
            cytova_identity_verified_at=timezone.now(),
            cytova_identity_verified_by=technician,
        )
        # StaffUser is hard-deletable in this codebase (unlike Patient,
        # which blocks delete). Removing the technician simulates
        # off-boarding.
        technician.delete()
        p.refresh_from_db()

        # Identity itself is preserved — the lab still knows the
        # patient is linked and when the link was verified.
        assert p.cytova_patient_id == 'CV-AUD-0001'
        assert p.cytova_identity_verified_at is not None
        # Only the staff-user reference is cleared.
        assert p.cytova_identity_verified_by_id is None
        # And ``has_cytova_identity`` still reads True — the verified
        # link doesn't depend on the staff user still existing.
        assert p.has_cytova_identity is True
