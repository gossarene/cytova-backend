"""
Cytova — Patient ID generator.

The Cytova Patient ID is the public-facing handle a patient quotes when
talking to a lab — over the phone, on a paper form, or to a
receptionist. Two design constraints drive the format:

  - **Unambiguous when dictated**: avoid characters that look or sound
    similar (``0``/``O``, ``1``/``I``).
  - **Short enough to read aloud once**: a single ``CV-XXXX-XXXX``
    block of 10 characters is easy to spell out without losing place.

Format: ``CV-XXXX-XXXX``
Alphabet: 32-character Crockford-style base32 minus 0, 1, I, O.
Search space: 32**8 ≈ 1.1 trillion candidates.
"""
from __future__ import annotations

import secrets

from django.db import IntegrityError, transaction


# 32 characters — 0/1/I/O removed because they're easy to confuse with
# letters/digits when spoken or hand-written. The remaining set is what
# Crockford-base32 allows; we don't actually base-encode anything, but
# the alphabet inherits the same readability guarantees.
ALPHABET = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'

PREFIX = 'CV'
GROUP_LEN = 4
MAX_ATTEMPTS = 10


def _random_block(length: int = GROUP_LEN) -> str:
    """One uppercase alphanumeric block, sampled with cryptographic RNG."""
    return ''.join(secrets.choice(ALPHABET) for _ in range(length))


def _candidate() -> str:
    """Build a ``CV-XXXX-XXXX`` candidate without checking uniqueness."""
    return f'{PREFIX}-{_random_block()}-{_random_block()}'


def generate_cytova_patient_id() -> str:
    """
    Return a guaranteed-unique Cytova Patient ID.

    Implementation
    --------------
    Generates a candidate, then probes the ``patient_portal_profile``
    table to confirm it isn't already taken. Loops up to
    ``MAX_ATTEMPTS`` times before raising ``RuntimeError`` — at the
    32**8 search space, a collision after 10 attempts implies the table
    is either full or the RNG is broken; in either case, surfacing a
    loud error is the right behaviour.

    The collision check is a cheap indexed point query
    (``cytova_patient_id`` is unique-indexed). The DB-level unique
    constraint is the last line of defence: if a parallel writer races
    us between probe and insert, the ``IntegrityError`` from the actual
    INSERT in the service layer triggers the caller's retry path.
    Imported lazily to keep the module importable from migrations.
    """
    from .models import PatientProfile

    for _ in range(MAX_ATTEMPTS):
        candidate = _candidate()
        if not PatientProfile.objects.filter(cytova_patient_id=candidate).exists():
            return candidate
    raise RuntimeError(
        f'Could not generate a unique Cytova Patient ID after '
        f'{MAX_ATTEMPTS} attempts.'
    )


@transaction.atomic
def generate_with_db_retry(create_profile_fn, *, attempts: int = MAX_ATTEMPTS):
    """
    Optional helper for callers that want race-safe insertion.

    Wraps a profile-creation callable that takes the candidate ID,
    catches an ``IntegrityError`` on the unique constraint, and retries
    a fresh ID. Used by the signup service when racing parallel
    registrations would otherwise produce a 500. The application-level
    probe in ``generate_cytova_patient_id`` handles the common case;
    this helper covers the narrow window between probe and insert.
    """
    last_error: IntegrityError | None = None
    for _ in range(attempts):
        candidate = generate_cytova_patient_id()
        try:
            with transaction.atomic():
                return create_profile_fn(candidate)
        except IntegrityError as e:
            last_error = e
            continue
    raise RuntimeError(
        f'Could not assign a unique Cytova Patient ID after '
        f'{attempts} race-safe attempts.'
    ) from last_error
