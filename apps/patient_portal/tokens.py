"""
Cytova — Patient Portal JWT tokens.

The patient portal uses ``simplejwt`` access/refresh tokens that are
deliberately distinguishable from staff tokens via the ``user_type``
claim. ``PatientJWTAuthentication`` (see ``authentication.py``) refuses
any token whose ``user_type`` is not ``'PATIENT'`` so a leaked staff
token cannot be used to read a patient's profile, and vice versa.

Refresh-token rotation is intentionally NOT wired into the standard
simplejwt blacklist for patients: the blacklist tables live in tenant
schemas (``rest_framework_simplejwt.token_blacklist`` is in
``TENANT_APPS``), and patient accounts live in the public schema. A
follow-up step will add a public-schema ``PatientTokenBlacklist`` and
a refresh endpoint; for now the access-token TTL acts as the practical
upper bound on credential validity.
"""
from __future__ import annotations

from rest_framework_simplejwt.settings import api_settings as jwt_settings
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken


PATIENT_USER_TYPE = 'PATIENT'


def _attach_patient_claims(token, account, *, profile=None) -> None:
    """Stamp the patient-discriminator claims onto a token. Centralised so
    access + refresh stay in lockstep — the auth backend reads
    ``user_type`` from either flavour to gate access."""
    token['user_type'] = PATIENT_USER_TYPE
    token['email'] = account.email
    if profile is not None:
        # ``cytova_patient_id`` is informational on the wire (the canonical
        # source is still the DB) but having it in the JWT lets the
        # frontend show the ID without a /me round-trip on first paint.
        token['cytova_patient_id'] = profile.cytova_patient_id


class PatientAccessToken(AccessToken):
    """Access token enriched with patient-account claims."""

    @classmethod
    def for_patient(cls, account, *, profile=None) -> 'PatientAccessToken':
        token = super().for_user(account)
        _attach_patient_claims(token, account, profile=profile)
        return token


class PatientRefreshToken(RefreshToken):
    """Refresh token carrying the same discriminator claim. Issued
    alongside ``PatientAccessToken`` at login. NOT rotated through the
    simplejwt blacklist (see module docstring).

    ``for_patient`` reimplements the relevant parts of
    ``Token.for_user`` WITHOUT calling ``OutstandingToken.objects.create``:
    that table's ``user`` FK is bound to ``AUTH_USER_MODEL`` (StaffUser,
    per-tenant) and a patient instance fails the FK check at issuance
    time. The follow-up step that adds patient refresh + logout will
    introduce a public-schema ``PatientTokenBlacklist`` and a parallel
    outstanding-token model keyed on PatientAccount.
    """

    @classmethod
    def for_patient(cls, account, *, profile=None) -> 'PatientRefreshToken':
        token = cls()
        # Mirrors simplejwt's ``Token.for_user`` claim layout minus the
        # OutstandingToken write. ``str(uuid)`` matches what
        # ``AUTH_TOKEN_CLASSES`` produce for staff (UUIDs are coerced to
        # strings before going onto the wire).
        token[jwt_settings.USER_ID_CLAIM] = str(getattr(
            account, jwt_settings.USER_ID_FIELD,
        ))
        _attach_patient_claims(token, account, profile=profile)
        return token
