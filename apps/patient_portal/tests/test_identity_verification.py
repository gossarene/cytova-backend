"""
Tests for ``apps.patient_portal.lookup.verify_patient_identity`` and the
narrower ``find_patient_by_cytova_id`` helper.

The verification function is the single PII-protecting gate between
the lab tenant and the patient portal — these tests assert the
contract that only an *exact* match returns a non-None result, and
that no error message reveals which field failed (the public callable
returns ``None`` for every failure mode).
"""
from __future__ import annotations

from datetime import date

import pytest

from apps.patient_portal.lookup import (
    find_patient_by_cytova_id, verify_patient_identity,
)
from apps.patient_portal.models import PatientAccount
from apps.patient_portal.services import register_patient_account


def _make_patient(*, email='id-verify@portal.test') -> PatientAccount:
    return register_patient_account(
        email=email,
        password='Strong-Pass-1234!',
        first_name='René',
        last_name='GOSSA',
        date_of_birth=date(1985, 3, 12),
        accept_terms=True,
    )


@pytest.mark.django_db(transaction=True)
class TestFindByCytovaId:

    def test_unknown_id_returns_none(self):
        assert find_patient_by_cytova_id('CV-XXXX-XXXX') is None
        # Empty / garbage inputs never raise.
        assert find_patient_by_cytova_id('') is None
        assert find_patient_by_cytova_id(None) is None  # type: ignore[arg-type]

    def test_known_id_returns_profile(self):
        account = _make_patient()
        cytova_id = account.profile.cytova_patient_id
        profile = find_patient_by_cytova_id(cytova_id)
        assert profile is not None
        assert profile.account_id == account.id

    def test_normalises_lowercase_and_missing_hyphens(self):
        account = _make_patient()
        canonical = account.profile.cytova_patient_id
        # CV-XXXX-XXXX → cvxxxxxxxx (lower, no hyphens). Receptionists
        # transcribing IDs over the phone routinely drop both.
        compact = canonical.lower().replace('-', '')
        profile = find_patient_by_cytova_id(compact)
        assert profile is not None
        assert profile.account_id == account.id


@pytest.mark.django_db(transaction=True)
class TestVerifyPatientIdentity:

    def test_full_match_returns_profile(self):
        account = _make_patient()
        cytova_id = account.profile.cytova_patient_id
        profile = verify_patient_identity(
            cytova_id=cytova_id,
            first_name='René',
            last_name='GOSSA',
            date_of_birth=date(1985, 3, 12),
        )
        assert profile is not None
        assert profile.account_id == account.id

    def test_case_insensitive_and_whitespace_tolerant(self):
        account = _make_patient()
        cytova_id = account.profile.cytova_patient_id
        profile = verify_patient_identity(
            cytova_id=cytova_id,
            first_name='  rené ',
            last_name='gossa',
            date_of_birth=date(1985, 3, 12),
        )
        assert profile is not None

    def test_unknown_cytova_id_returns_none(self):
        assert verify_patient_identity(
            cytova_id='CV-AAAA-BBBB',
            first_name='René', last_name='GOSSA',
            date_of_birth=date(1985, 3, 12),
        ) is None

    def test_first_name_mismatch_returns_none(self):
        account = _make_patient()
        assert verify_patient_identity(
            cytova_id=account.profile.cytova_patient_id,
            first_name='Marie',  # wrong
            last_name='GOSSA',
            date_of_birth=date(1985, 3, 12),
        ) is None

    def test_last_name_mismatch_returns_none(self):
        account = _make_patient()
        assert verify_patient_identity(
            cytova_id=account.profile.cytova_patient_id,
            first_name='René',
            last_name='Smith',  # wrong
            date_of_birth=date(1985, 3, 12),
        ) is None

    def test_dob_mismatch_returns_none(self):
        account = _make_patient()
        assert verify_patient_identity(
            cytova_id=account.profile.cytova_patient_id,
            first_name='René', last_name='GOSSA',
            date_of_birth=date(1985, 3, 13),  # wrong by one day
        ) is None

    def test_inactive_account_blocks_match_even_when_identity_is_correct(self):
        account = _make_patient()
        account.is_active = False
        account.save(update_fields=['is_active'])
        assert verify_patient_identity(
            cytova_id=account.profile.cytova_patient_id,
            first_name='René', last_name='GOSSA',
            date_of_birth=date(1985, 3, 12),
        ) is None

    def test_no_signal_about_which_field_failed(self):
        """The public contract is binary: profile-or-None. The function
        never raises a typed exception, never sets an attribute, never
        leaks any information about why a match failed. Asserting on
        the public surface here keeps the contract honest if anyone
        adds detail later."""
        account = _make_patient()
        cytova_id = account.profile.cytova_patient_id
        for bad in (
            {'first_name': 'wrong'},
            {'last_name': 'wrong'},
            {'date_of_birth': date(1990, 1, 1)},
        ):
            kwargs = {
                'cytova_id': cytova_id,
                'first_name': 'René',
                'last_name': 'GOSSA',
                'date_of_birth': date(1985, 3, 12),
                **bad,
            }
            assert verify_patient_identity(**kwargs) is None
