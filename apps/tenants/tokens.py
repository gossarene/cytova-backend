"""
Platform admin JWT access token.
Includes user_type='PLATFORM_ADMIN' so PlatformAdminJWTAuthentication can
distinguish these tokens from per-tenant StaffUser tokens.
"""
from rest_framework_simplejwt.tokens import AccessToken


class PlatformAdminAccessToken(AccessToken):

    @classmethod
    def for_user(cls, platform_admin):
        token = super().for_user(platform_admin)
        token['user_type'] = 'PLATFORM_ADMIN'
        token['email'] = platform_admin.email
        return token
