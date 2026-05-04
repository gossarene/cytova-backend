"""
Flexible-identity rollout — tests for the spec §6 acceptance cases.

Three surfaces are pinned together so the rules stay consistent
across the create / update / link entry points:

  1. **Identity** — UNKNOWN-with-blank-number auto-generates an
     ``AUTO-PT-…`` placeholder and stamps the audit flag; real
     types still demand a number; transitions between UNKNOWN and
     real types update the flag correctly.
  2. **Date of birth** — null is accepted only when the operator
     explicitly flips ``date_of_birth_unknown``; the DOB column
     was made nullable but a forgotten field cannot silently
     land null. Existing patients with a DOB stay valid (the
     migration was a constraint relaxation only).
  3. **Cytova link** — the link flow refuses when the patient has
     no DOB on file, with a distinct ``DATE_OF_BIRTH_REQUIRED``
     error so the lab UI can surface the exact recovery path.

The auto-generator's collision retry is also exercised: 50
back-to-back creates must produce 50 distinct identifiers without
hitting the unique constraint at insert time.
"""
from __future__ import annotations

from datetime import date

import pytest
from django.utils import timezone
from rest_framework import serializers as drf_serializers

from apps.patients.models import DocumentType, Patient
from apps.patients.serializers import (
    PatientCreateSerializer,
    PatientIdentityUpdateSerializer,
    PatientUpdateSerializer,
)
from apps.patients.services import (
    DateOfBirthRequired, PatientService, _generate_unknown_identity_number,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DOC_SEQ = 0


def _normal_payload(**overrides):
    """Standard create payload — sane defaults that exercise none
    of the new flexible-identity branches. Tests override only the
    fields they care about."""
    global _DOC_SEQ
    _DOC_SEQ += 1
    base = {
        'document_type': DocumentType.NATIONAL_ID_CARD,
        'document_number': f'NID-FLEX-{_DOC_SEQ:04d}',
        'first_name': 'Ada',
        'last_name': 'Lovelace',
        'date_of_birth': date(1990, 5, 17),
        'gender': 'FEMALE',
    }
    base.update(overrides)
    return base


def _validated(payload):
    """Run the create payload through the serializer the view uses
    so cross-field validation fires exactly the way it does in
    production. Returns the validated dict ready for the service."""
    ser = PatientCreateSerializer(data=payload)
    ser.is_valid(raise_exception=True)
    return dict(ser.validated_data)


# ---------------------------------------------------------------------------
# 1. Identity — UNKNOWN type behaviour
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestUnknownIdentityCreate:

    def test_unknown_type_with_blank_number_auto_generates(
        self, lab_admin, make_request,
    ):
        """Spec §6 case A. Operator picks UNKNOWN and leaves the
        number blank → service auto-generates an ``AUTO-PT-…``
        placeholder and flips the flag."""
        payload = _normal_payload(
            document_type=DocumentType.UNKNOWN,
            document_number='',
        )
        validated = _validated(payload)
        patient = PatientService.create_patient(
            validated, lab_admin, make_request(lab_admin),
        )
        assert patient.document_type == DocumentType.UNKNOWN
        assert patient.identity_number_auto_generated is True
        assert patient.document_number.startswith('AUTO-PT-')
        # Format: AUTO-PT-YYYYMMDD-XXXXXX → 23 chars total.
        assert len(patient.document_number) == 23

    def test_unknown_type_with_supplied_number_keeps_it_verbatim(
        self, lab_admin, make_request,
    ):
        """An operator who supplies a value alongside UNKNOWN is
        vouching for it — the service must NOT overwrite their
        input. Flag stays False because the number is operator-
        supplied, not generated."""
        payload = _normal_payload(
            document_type=DocumentType.UNKNOWN,
            document_number='OPERATOR-SUPPLIED-123',
        )
        validated = _validated(payload)
        patient = PatientService.create_patient(
            validated, lab_admin, make_request(lab_admin),
        )
        assert patient.document_number == 'OPERATOR-SUPPLIED-123'
        assert patient.identity_number_auto_generated is False

    def test_real_type_without_number_is_rejected_at_serializer(
        self, lab_admin, make_request,
    ):
        """Spec §6 case B. Real document type WITHOUT number → the
        cross-field validator surfaces a field-level error before
        the service ever runs. Critical: a 400 here means the
        service can never accidentally auto-generate against a
        real type."""
        payload = _normal_payload(
            document_type=DocumentType.PASSPORT,
            document_number='',
        )
        ser = PatientCreateSerializer(data=payload)
        assert not ser.is_valid()
        assert 'document_number' in ser.errors

    def test_50_back_to_back_unknown_creates_yield_50_distinct_numbers(
        self, lab_admin, make_request,
    ):
        """Auto-generator collision-resistance check. The suffix
        space is 32^6 ≈ 1B values per day; 50 back-to-back creates
        must yield 50 distinct identifiers (the per-call retry
        loop is the safety net for an astronomical collision, not
        the primary mechanism)."""
        ids = set()
        for _ in range(50):
            payload = _normal_payload(
                document_type=DocumentType.UNKNOWN,
                document_number='',
            )
            patient = PatientService.create_patient(
                _validated(payload), lab_admin, make_request(lab_admin),
            )
            ids.add(patient.document_number)
        assert len(ids) == 50

    def test_generator_helper_format(self):
        """Pure-helper sanity: the generator produces the spec
        format ``AUTO-PT-YYYYMMDD-XXXXXX`` with the configured
        date. Pinned so a future refactor that reshapes the
        identifier surfaces in CI."""
        out = _generate_unknown_identity_number(date(2026, 5, 4))
        assert out.startswith('AUTO-PT-20260504-')
        assert len(out) == 23
        # Suffix uses the confusable-free alphabet (no O, 0, I, 1, L).
        suffix = out.rsplit('-', 1)[-1]
        assert all(c not in 'OIL01' for c in suffix)


# ---------------------------------------------------------------------------
# 2. Identity — UNKNOWN ↔ real type transitions on update
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestIdentityTransitions:

    def _create_unknown(self, lab_admin, make_request):
        payload = _normal_payload(
            document_type=DocumentType.UNKNOWN,
            document_number='',
        )
        return PatientService.create_patient(
            _validated(payload), lab_admin, make_request(lab_admin),
        )

    def test_unknown_to_passport_without_number_rejected(
        self, lab_admin, make_request,
    ):
        """Spec §6: changing UNKNOWN → PASSPORT requires a number.
        The serializer surfaces a field error; the service is
        never called with the bad transition."""
        patient = self._create_unknown(lab_admin, make_request)
        ser = PatientIdentityUpdateSerializer(
            data={'document_type': DocumentType.PASSPORT},
            partial=True,
            context={'patient': patient},
        )
        assert not ser.is_valid()
        assert 'document_number' in ser.errors

    def test_unknown_to_passport_with_number_flips_flag_to_false(
        self, lab_admin, make_request,
    ):
        """Spec §6: changing UNKNOWN → PASSPORT WITH a real number
        succeeds AND clears ``identity_number_auto_generated``.
        The auto-generated placeholder is replaced by the real
        number; the flag flips so the UI stops rendering it as a
        placeholder."""
        patient = self._create_unknown(lab_admin, make_request)
        assert patient.identity_number_auto_generated is True

        ser = PatientIdentityUpdateSerializer(
            data={
                'document_type': DocumentType.PASSPORT,
                'document_number': 'PA-9999',
            },
            partial=True,
            context={'patient': patient},
        )
        assert ser.is_valid(), ser.errors

        updated = PatientService.update_patient(
            patient, dict(ser.validated_data),
            lab_admin, make_request(lab_admin),
        )
        assert updated.document_type == DocumentType.PASSPORT
        assert updated.document_number == 'PA-9999'
        assert updated.identity_number_auto_generated is False

    def test_real_type_to_unknown_with_blank_number_auto_generates(
        self, lab_admin, make_request,
    ):
        """Reverse transition: PASSPORT → UNKNOWN with the operator
        clearing the number → service auto-generates a fresh
        placeholder and flips the flag back to True. The previous
        number is gone (operator's intent is to mark "no document
        on file")."""
        # Start with a real-type patient.
        payload = _normal_payload(
            document_type=DocumentType.PASSPORT,
            document_number='PA-1234',
        )
        patient = PatientService.create_patient(
            _validated(payload), lab_admin, make_request(lab_admin),
        )
        assert patient.identity_number_auto_generated is False

        ser = PatientIdentityUpdateSerializer(
            data={
                'document_type': DocumentType.UNKNOWN,
                'document_number': '',
            },
            partial=True,
            context={'patient': patient},
        )
        assert ser.is_valid(), ser.errors
        updated = PatientService.update_patient(
            patient, dict(ser.validated_data),
            lab_admin, make_request(lab_admin),
        )
        assert updated.document_type == DocumentType.UNKNOWN
        assert updated.identity_number_auto_generated is True
        assert updated.document_number.startswith('AUTO-PT-')


# ---------------------------------------------------------------------------
# 3. Date of birth — Cases C/D
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestDateOfBirth:

    def test_dob_unknown_true_allows_null_dob_on_create(
        self, lab_admin, make_request,
    ):
        """Spec §6 case C. Operator flips the unknown flag → DOB
        may be null. Both stored cleanly: dob=None,
        date_of_birth_unknown=True."""
        payload = _normal_payload(
            date_of_birth=None,
            date_of_birth_unknown=True,
        )
        validated = _validated(payload)
        patient = PatientService.create_patient(
            validated, lab_admin, make_request(lab_admin),
        )
        assert patient.date_of_birth is None
        assert patient.date_of_birth_unknown is True

    def test_dob_unknown_false_requires_dob_on_create(self):
        """Spec §6 case D. The default flag stays False; a missing
        DOB must be rejected at the serializer with a field-level
        error. A forgotten date-picker can NEVER silently land
        null — that's the load-bearing safety property of the
        nullable-DOB rollout."""
        payload = _normal_payload(date_of_birth=None)  # flag defaults False
        ser = PatientCreateSerializer(data=payload)
        assert not ser.is_valid()
        assert 'date_of_birth' in ser.errors

    def test_dob_unknown_with_supplied_date_clears_the_date(
        self, lab_admin, make_request,
    ):
        """Consistency rule: if the operator says DOB is unknown
        AND supplies a date, the date wins... no — the FLAG wins.
        Flipping ``date_of_birth_unknown=True`` means "we don't
        know"; carrying a stale date alongside that would let
        downstream code make decisions on data the operator just
        marked as unknown."""
        payload = _normal_payload(
            date_of_birth=date(1990, 1, 1),
            date_of_birth_unknown=True,
        )
        validated = _validated(payload)
        # Validated data already has DOB cleared by the serializer
        # consistency rule.
        assert validated['date_of_birth'] is None
        patient = PatientService.create_patient(
            validated, lab_admin, make_request(lab_admin),
        )
        assert patient.date_of_birth is None
        assert patient.date_of_birth_unknown is True

    def test_existing_patient_with_dob_remains_valid(
        self, lab_admin, make_request,
    ):
        """Migration safety: a pre-rollout patient with a non-null
        DOB stays valid. The constraint relaxation is purely
        additive — no data loss, no validation regression."""
        payload = _normal_payload(date_of_birth=date(1985, 3, 14))
        patient = PatientService.create_patient(
            _validated(payload), lab_admin, make_request(lab_admin),
        )
        assert patient.date_of_birth == date(1985, 3, 14)
        assert patient.date_of_birth_unknown is False

    def test_partial_update_flipping_only_unknown_flag_clears_dob(
        self, lab_admin, make_request,
    ):
        """Realistic operator workflow: patient was created with a
        DOB (placeholder guess), operator later confirms it was
        unknown, flips ONLY the flag in a partial update. The
        validator reads the patient's current ``date_of_birth``
        via context and clears it as part of the consistency
        rule."""
        payload = _normal_payload(date_of_birth=date(1990, 1, 1))
        patient = PatientService.create_patient(
            _validated(payload), lab_admin, make_request(lab_admin),
        )

        ser = PatientUpdateSerializer(
            data={'date_of_birth_unknown': True},
            partial=True,
            context={'patient': patient},
        )
        assert ser.is_valid(), ser.errors
        # Serializer's consistency rule runs even on partial updates.
        validated = dict(ser.validated_data)
        # The DOB was on the patient, now the validator should
        # clear it because the flag flipped.
        assert validated.get('date_of_birth') is None
        assert validated.get('date_of_birth_unknown') is True

    def test_partial_update_clearing_dob_without_flag_rejected(
        self, lab_admin, make_request,
    ):
        """The mirror case: operator clears DOB without flipping
        the unknown flag → rejected. This is the safety net
        against a forgotten field — the only way to land null
        is via the explicit flag."""
        payload = _normal_payload(date_of_birth=date(1990, 1, 1))
        patient = PatientService.create_patient(
            _validated(payload), lab_admin, make_request(lab_admin),
        )

        ser = PatientUpdateSerializer(
            data={'date_of_birth': None},  # no flag flip
            partial=True,
            context={'patient': patient},
        )
        assert not ser.is_valid()
        assert 'date_of_birth' in ser.errors


# ---------------------------------------------------------------------------
# 4. Cytova link guard — refuse when DOB is unknown
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestCytovaLinkGuard:
    """The guard fires BEFORE any cross-schema (public) lookup, so
    these tests can run inside the standard transactional rollback
    fixture — no need for ``transaction=True`` and no need for a
    real ``PatientAccount`` in the public schema. The happy-path
    link (DOB present + matching global account) is already covered
    by ``test_cytova_identity_link_api.py``; here we pin only the
    new failure mode introduced by the flexible-identity rollout."""

    def test_link_refuses_when_dob_unknown(self, lab_admin, make_request):
        """Spec §5: a patient with no DOB on file CANNOT be linked
        to a Cytova account because the global identity-verification
        service requires an exact DOB match. The guard fails
        closed BEFORE burning an identity-verification attempt
        that would 100% fail anyway, with a distinct error code so
        the UI can surface the recovery path ("update the
        patient's DOB first")."""
        # Patient created with DOB unknown — the only way to land
        # ``date_of_birth=None`` per the rollout's safety rules.
        payload = _normal_payload(
            date_of_birth=None,
            date_of_birth_unknown=True,
        )
        patient = PatientService.create_patient(
            _validated(payload), lab_admin, make_request(lab_admin),
        )

        # No global PatientAccount needed — the pre-check fires
        # first and never reaches the patient_portal lookup.
        with pytest.raises(DateOfBirthRequired) as exc:
            PatientService.link_cytova_identity(
                patient=patient,
                cytova_patient_id='CV-1234-5678',
                first_name='Ada',
                last_name='Lovelace',
                date_of_birth=date(1990, 5, 17),
                actor=lab_admin,
                request=make_request(lab_admin),
            )
        # Code matches the spec exactly so the lab UI can branch
        # on a stable string.
        assert exc.value.code == 'DATE_OF_BIRTH_REQUIRED'
        assert 'Date of birth is required' in exc.value.message

    def test_link_with_dob_present_passes_the_guard(
        self, lab_admin, make_request,
    ):
        """The guard only blocks DOB-unknown patients. A patient
        with a DOB on file proceeds past the guard into the normal
        identity-verification path. We assert "guard cleared" by
        observing that the failure (if any) is NOT a
        ``DateOfBirthRequired`` — any other ``CytovaLinkError``
        (e.g. ``IdentityVerificationFailed`` from no-such-global-
        account) is fine for this test, because the guard fired
        and the flow advanced. Avoids creating a real
        PatientAccount in the public schema, which would force
        ``transaction=True`` and trip a pre-existing FK-truncate
        infrastructure issue in the test database."""
        from apps.patients.services import CytovaLinkError

        payload = _normal_payload(date_of_birth=date(1990, 5, 17))
        patient = PatientService.create_patient(
            _validated(payload), lab_admin, make_request(lab_admin),
        )

        with pytest.raises(CytovaLinkError) as exc:
            PatientService.link_cytova_identity(
                patient=patient,
                cytova_patient_id='CV-NONE-XXXX',
                first_name='Ada',
                last_name='Lovelace',
                date_of_birth=date(1990, 5, 17),
                actor=lab_admin,
                request=make_request(lab_admin),
            )
        # Guard cleared: the error is from the verification path,
        # NOT the DOB pre-check.
        assert not isinstance(exc.value, DateOfBirthRequired)
