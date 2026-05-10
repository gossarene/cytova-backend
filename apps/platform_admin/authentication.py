"""
Cytova — Platform Admin JWT authentication backend.

Mounted on the platform-admin views (``authentication_classes`` per
view). Refuses tokens whose ``user_type`` claim is anything other
than ``'PLATFORM_ADMIN'``, so:

  - Lab staff tokens (no ``user_type`` claim, or one that doesn't
    match) are rejected.
  - Patient portal tokens (``user_type='PATIENT'``) are rejected.
  - Any future actor type that we add later will not slip through
    this gate without explicit opt-in.

Combined with the URL routing — these endpoints live only on the
public-schema ``urls_public.py`` — a tenant-scoped JWT can't even
reach the route handler. This auth class is the second line of
defence.
"""
from __future__ import annotations

from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import (
    AuthenticationFailed, InvalidToken,
)
from rest_framework_simplejwt.settings import api_settings as jwt_settings

from .models import PlatformAdminUser
from .tokens import PLATFORM_ADMIN_USER_TYPE


class PlatformAdminJWTAuthentication(JWTAuthentication):
    """Resolve ``PlatformAdminUser`` from a Bearer access token."""

    def get_user(self, validated_token):
        if validated_token.get('user_type') != PLATFORM_ADMIN_USER_TYPE:
            raise InvalidToken(
                'Token is not a platform admin token. '
                'Use the platform admin login endpoint.'
            )

        user_id = validated_token.get(jwt_settings.USER_ID_CLAIM)
        if not user_id:
            raise InvalidToken('Token missing user identifier.')

        # ``is_active=True`` filter rejects deactivated administrators
        # at auth time. A token still in someone's browser stops working
        # the moment ``is_active`` flips to False — no need to wait
        # for the token to expire or to maintain a separate blacklist
        # for the foundation phase.
        try:
            return PlatformAdminUser.objects.get(id=user_id, is_active=True)
        except PlatformAdminUser.DoesNotExist:
            raise AuthenticationFailed(
                'Platform admin not found or inactive.'
            )
