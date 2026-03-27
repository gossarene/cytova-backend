"""
Cytova — Platform Admin JWT Authentication

A specialised JWTAuthentication class that looks up PlatformAdmin (public schema)
instead of StaffUser (tenant schema). Only accepts tokens with
user_type='PLATFORM_ADMIN' to prevent a tenant staff token from being used
on the platform admin API.
"""
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, AuthenticationFailed
from rest_framework_simplejwt.settings import api_settings as jwt_settings

from apps.tenants.models import PlatformAdmin


class PlatformAdminJWTAuthentication(JWTAuthentication):

    def get_user(self, validated_token):
        if validated_token.get('user_type') != 'PLATFORM_ADMIN':
            raise InvalidToken(
                'Token is not a platform admin token. '
                'Use the tenant login endpoint for staff access.'
            )

        user_id = validated_token.get(jwt_settings.USER_ID_CLAIM)
        if not user_id:
            raise InvalidToken('Token missing user identifier.')

        try:
            return PlatformAdmin.objects.get(id=user_id, is_active=True)
        except PlatformAdmin.DoesNotExist:
            raise AuthenticationFailed('Platform admin not found or inactive.')
