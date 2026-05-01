"""
Cytova — Patient Portal lookup + identity verification.

Lab tenants call into this module to (a) find a portal account by its
public Cytova Patient ID and (b) verify the patient's claimed identity
(name + DOB) before sharing a result. The functions are deliberately
narrow: callers receive *only* the boolean / minimal handle they
need to proceed — never the patient's name, email, or DOB. That keeps
the cross-domain coupling tight and makes accidental PII leakage
through the lab tenant impossible.

Returned objects, when provided, are model instances. Callers MUST
NOT propagate fields off them into HTTP responses or logs without
explicit policy review — see the ``find_patient_by_cytova_id``
docstring.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from .models import PatientProfile


def _normalize_cytova_id(value: str) -> str:
    """Canonicalise the user-typed Cytova ID. The format is
    ``CV-XXXX-XXXX`` (uppercase, hyphenated), but receptionists
    typing it from a phone or paper form may strip the hyphens or
    lower-case it. We accept both and normalise."""
    if not value:
        return ''
    cleaned = value.strip().upper().replace(' ', '')
    # Re-introduce the canonical hyphens if the caller typed
    # ``CVXXXXXXXX``. Matches the generator's ``CV-XXXX-XXXX`` shape;
    # any other shape (wrong length, wrong prefix) falls through to a
    # lookup miss rather than raising — callers can't tell why.
    if len(cleaned) == 10 and cleaned.startswith('CV'):
        return f'CV-{cleaned[2:6]}-{cleaned[6:10]}'
    return cleaned


def find_patient_by_cytova_id(cytova_id: str) -> Optional[PatientProfile]:
    """Return the ``PatientProfile`` matching the supplied Cytova ID,
    or ``None`` if no patient exists with that ID. Callers MUST treat
    a non-None return as an internal handle only:

    - never echo ``profile.first_name`` / ``last_name`` / ``date_of_birth``
      / ``account.email`` into an HTTP response or audit message;
    - never use the absence vs. presence of a result as a signal in an
      error message (that would leak whether the ID exists);
    - the only safe public uses are: pass the profile to
      ``verify_patient_identity``, or read ``profile.account_id`` to
      seed a snapshot row whose visibility is gated by other checks.
    """
    canonical = _normalize_cytova_id(cytova_id)
    if not canonical:
        return None
    try:
        return PatientProfile.objects.select_related('account').get(
            cytova_patient_id=canonical,
        )
    except PatientProfile.DoesNotExist:
        return None


def _name_matches(claim: str, stored: str) -> bool:
    """Trim + casefold + collapse internal whitespace so user-typed
    "  rené " matches "René" and "GOSSA  " matches "gossa". This is the
    same canonicalisation a receptionist would apply by hand."""
    return ' '.join((claim or '').strip().casefold().split()) == \
           ' '.join((stored or '').strip().casefold().split())


def verify_patient_identity(
    cytova_id: str,
    first_name: str,
    last_name: str,
    date_of_birth: date,
) -> Optional[PatientProfile]:
    """Verify a claimed identity against the portal account.

    All three fields must match for the return to be non-None:

    - ``first_name`` and ``last_name`` are compared case-insensitively
      with surrounding whitespace stripped (no fuzzy matching: typos
      should fail closed);
    - ``date_of_birth`` must match exactly.

    On success returns the ``PatientProfile`` (same caveats as
    ``find_patient_by_cytova_id`` apply — internal handle only).
    On any failure mode (unknown ID, name mismatch, DOB mismatch, or
    inactive account) returns ``None``. The caller MUST surface a
    single non-distinguishing error — telling a lab user *which* field
    failed would expose enough about the patient to abuse the lookup
    as an enumeration oracle (e.g. brute-forcing names against a known
    Cytova ID).
    """
    profile = find_patient_by_cytova_id(cytova_id)
    if profile is None:
        return None
    if not profile.account.is_active:
        return None
    if not _name_matches(first_name, profile.first_name):
        return None
    if not _name_matches(last_name, profile.last_name):
        return None
    if profile.date_of_birth != date_of_birth:
        return None
    return profile
