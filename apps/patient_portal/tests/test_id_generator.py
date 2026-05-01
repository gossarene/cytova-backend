"""
Tests for ``apps.patient_portal.id_generator``.

Covers the format guarantees (prefix, blocks, alphabet) and uniqueness
across many generations — the latter doubles as a smoke for the
collision-retry path: a non-zero collision rate during the loop would
manifest as a duplicate slipping through.
"""
from __future__ import annotations

import re

import pytest

from apps.patient_portal.id_generator import (
    ALPHABET, generate_cytova_patient_id,
)
from apps.patient_portal.models import (
    PatientAccount, PatientProfile,
)


# Build the regex from the same alphabet so a future tweak to the
# alphabet (e.g. dropping more confusable chars) automatically updates
# the assertion.
_ID_REGEX = re.compile(rf'^CV-[{ALPHABET}]{{4}}-[{ALPHABET}]{{4}}$')


@pytest.mark.django_db(transaction=True)
class TestPatientIdGenerator:

    def test_format_matches_regex(self):
        for _ in range(50):
            pid = generate_cytova_patient_id()
            assert _ID_REGEX.match(pid), pid

    def test_alphabet_excludes_ambiguous_characters(self):
        # The whole point of the chosen alphabet is to dodge 0/O and
        # 1/I — assert they never appear in any generated ID.
        forbidden = set('01IO')
        for _ in range(100):
            pid = generate_cytova_patient_id()
            assert forbidden.isdisjoint(set(pid)), pid

    def test_unique_across_many_persisted_profiles(self):
        # Persist 200 profiles back-to-back; the in-DB uniqueness probe
        # in the generator means a duplicate would loop until exhaustion
        # and raise. Persisting also exercises the unique constraint at
        # the DB level (defence in depth).
        seen: set[str] = set()
        for i in range(200):
            account = PatientAccount.objects.create_user(
                email=f'unique-{i}@portal.test', password='x' * 12,
            )
            profile = PatientProfile.objects.create(
                account=account,
                cytova_patient_id=generate_cytova_patient_id(),
                first_name='F', last_name='L',
                date_of_birth='1990-01-01',
            )
            assert profile.cytova_patient_id not in seen
            seen.add(profile.cytova_patient_id)
        assert len(seen) == 200
