"""
Platform admin JWT token.

Carries three claims beyond the simplejwt defaults:

  - ``user_type='PLATFORM_ADMIN'`` — drives audience separation. The
    matching ``PlatformAdminJWTAuthentication`` refuses any other
    value, so a lab staff token (which has no ``user_type``) or a
    patient token (which has ``user_type='PATIENT'``) cannot be used
    to call platform-admin endpoints.

  - ``role`` — the user's ``PlatformAdminRole`` enum value. Permission
    classes branch on this without re-fetching the user row, making
    role-gated requests cheap.

  - ``email`` — convenience for log enrichment / debugging. Not
    authoritative — every authenticated request still resolves the
    full ``PlatformAdminUser`` row by id from the database.

Refresh token policy
--------------------
The foundation phase issues an access token only. Refresh-token
rotation is a separate concern (token blacklist, refresh endpoint,
front-end refresh loop) and orthogonal to the auth contract this
phase pins. Adding it later doesn't change any of the claims or the
auth class.
"""
from __future__ import annotations

from rest_framework_simplejwt.tokens import AccessToken

PLATFORM_ADMIN_USER_TYPE = 'PLATFORM_ADMIN'


class PlatformAdminAccessToken(AccessToken):
    """``AccessToken`` subclass that pins the platform-admin claims.

    The token's ``user_id`` claim still uses simplejwt's default
    ``USER_ID_FIELD`` ('id'), so ``get_user`` lookups pass through
    the standard path — only the audience claims are extra.
    """

    @classmethod
    def for_user(cls, user) -> 'PlatformAdminAccessToken':
        token = super().for_user(user)
        token['user_type'] = PLATFORM_ADMIN_USER_TYPE
        token['role'] = user.role
        token['email'] = user.email
        return token
