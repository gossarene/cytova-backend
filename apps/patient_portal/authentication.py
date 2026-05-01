"""
Cytova — Patient Portal JWT authentication backend.

Drops into a view's ``authentication_classes`` to authenticate patient
portal requests. Refuses to load tokens whose ``user_type`` claim is
not ``'PATIENT'`` so staff tokens (which carry ``role`` + permissions
claims but no ``user_type``) cannot be used to access patient endpoints
even if they have somehow been passed through the same Authorization
header. Mirrors the platform-admin auth class in
``apps/tenants/authentication.py``.

The backend is mounted per-view rather than globally — the global
``DEFAULT_AUTHENTICATION_CLASSES`` keeps using the standard staff JWT
class so existing lab endpoints are untouched.
"""
from __future__ import annotations

from django.utils import timezone
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import (
    AuthenticationFailed, InvalidToken,
)
from rest_framework_simplejwt.settings import api_settings as jwt_settings

from .models import PatientAccount, PatientOutstandingToken
from .tokens import PATIENT_USER_TYPE


def _assert_token_active(jti: str) -> None:
    """Reject any patient token whose ``jti`` is unknown to our
    Outstanding table or has been blacklisted.

    Single ``select_related`` join keeps the auth path at one indexed
    lookup. The unique index on ``jti`` and the OneToOne reverse
    relation on ``blacklist`` guarantee O(1) cost per request.
    """
    if not jti:
        raise InvalidToken('Token missing jti claim.')
    try:
        outstanding = (
            PatientOutstandingToken.objects
            .select_related('blacklist')
            .only('id', 'expires_at', 'blacklist__id')
            .get(jti=jti)
        )
    except PatientOutstandingToken.DoesNotExist:
        # Either issued before the tracking table existed, or someone
        # crafted a token with a fresh jti. Either way: refuse.
        raise InvalidToken('Token is not recognised.')

    # Belt-and-braces — simplejwt already verifies ``exp`` cryptographically
    # via ``UntypedToken.check_exp``. Re-check against the stored expiry so
    # a clock skew that briefly makes a token "valid" still fails.
    if outstanding.expires_at <= timezone.now():
        raise InvalidToken('Token has expired.')

    # ``select_related`` made ``blacklist`` cheap to access — no
    # follow-up SELECT.
    try:
        if outstanding.blacklist is not None:
            raise InvalidToken('Token has been revoked.')
    except PatientOutstandingToken.blacklist.RelatedObjectDoesNotExist:
        # Reverse OneToOne resolves to a descriptor that raises when
        # there's no blacklist row — exactly the success path.
        pass


class PatientJWTAuthentication(JWTAuthentication):
    """Resolve the authenticated ``PatientAccount`` from a Bearer token.

    Two guard checks run before the DB lookup:
      1. ``user_type`` claim must equal ``'PATIENT'`` — rejects staff
         tokens, platform admin tokens, and any future actor type.
      2. The ``sub`` claim (configured by simplejwt as ``USER_ID_CLAIM``)
         must be present.

    The lookup itself enforces ``is_active=True`` so a deactivated
    account immediately stops being able to call ``/me`` even if a
    valid token is still in their browser.
    """

    def get_user(self, validated_token):
        if validated_token.get('user_type') != PATIENT_USER_TYPE:
            raise InvalidToken(
                'Token is not a patient portal token. '
                'Use the patient login endpoint.'
            )

        account_id = validated_token.get(jwt_settings.USER_ID_CLAIM)
        if not account_id:
            raise InvalidToken('Token missing user identifier.')

        # Blacklist + outstanding-token check happens BEFORE the
        # account lookup so a revoked token never resolves to a real
        # row. Single indexed query — see ``_assert_token_active``.
        _assert_token_active(validated_token.get('jti'))

        try:
            return PatientAccount.objects.get(id=account_id, is_active=True)
        except PatientAccount.DoesNotExist:
            raise AuthenticationFailed('Patient account not found or inactive.')
