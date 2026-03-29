"""
Cytova — Custom JWT Tokens

CytovaAccessToken extends the standard access token with role and email claims
so clients can read the user's role without a separate API call.

Registered in SIMPLE_JWT['AUTH_TOKEN_CLASSES'] so it is used by the simplejwt
token verification stack throughout the application.
"""
from rest_framework_simplejwt.tokens import AccessToken


class CytovaAccessToken(AccessToken):
    """
    Access token enriched with Cytova staff-user claims.

    Extra claims added to the JWT payload:
      role  — the user's current Role value (e.g. 'LAB_ADMIN')
      email — the user's email address (convenience; not used for auth)

    These are informational only. The canonical source of truth for role is
    the database; never trust a client-side role claim for authorization.
    """

    @classmethod
    def for_user(cls, user):
        token = super().for_user(user)
        token['role'] = user.role
        token['email'] = user.email
        # Include effective permissions for frontend convenience (informational only)
        from common.permission_checker import PermissionChecker
        token['permissions'] = sorted(PermissionChecker.get_effective_permissions(user))
        return token
