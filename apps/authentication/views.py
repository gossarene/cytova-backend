"""
Cytova — Authentication Views

All views are thin: validate input, delegate to AuthService, return response.
Token issuance, blacklisting, and audit logging happen in the service layer.
"""
from rest_framework import serializers as drf_serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from common.permissions import IsAnyStaff
from .serializers import (
    LoginSerializer,
    TokenRefreshSerializer,
    LogoutSerializer,
    PasswordResetRequestSerializer,
    PasswordResetConfirmSerializer,
)
from .services import AuthService


class LoginRateThrottle(AnonRateThrottle):
    """5 attempts per  minute per IP — matches DEFAULT_THROTTLE_RATES['auth_login']."""
    scope = 'auth_login'


class LoginView(APIView):
    """
    POST /api/v1/auth/login/

    Authenticate with email + password. Returns access token, refresh token,
    and a snapshot of the authenticated user's profile.
    Failed attempts are rate-limited and audit-logged.
    """
    permission_classes = [AllowAny]
    throttle_classes = [LoginRateThrottle]

    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={'request': request})
        try:
            serializer.is_valid(raise_exception=True)
        except drf_serializers.ValidationError:
            AuthService.record_failed_login(
                request.data.get('email', ''), request
            )
            raise

        data = AuthService.login(serializer.validated_data['user'], request)
        return Response({'data': data, 'meta': None, 'errors': []})


class TokenRefreshView(APIView):
    """
    POST /api/v1/auth/refresh/

    Rotate refresh token: blacklist the old token and issue a new access +
    refresh pair with up-to-date claims (role re-read from DB).
    """
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = TokenRefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = AuthService.refresh(serializer.validated_data['token'])
        return Response({'data': data, 'meta': None, 'errors': []})


class LogoutView(APIView):
    """
    POST /api/v1/auth/logout/

    Blacklist the refresh token (access token expires naturally via TTL).
    Writes a LOGOUT audit record. Returns 204 No Content.
    """
    permission_classes = [IsAnyStaff]

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        AuthService.logout(serializer.validated_data['refresh_token'], request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class PasswordResetRequestView(APIView):
    """
    POST /api/v1/auth/password-reset/request/

    Send a password reset email. Always returns 204 — even if the email is
    not found — to prevent user enumeration.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        AuthService.request_password_reset(serializer.validated_data['email'])
        return Response(status=status.HTTP_204_NO_CONTENT)


class PasswordResetConfirmView(APIView):
    """
    POST /api/v1/auth/password-reset/confirm/

    Consume a password reset token and set a new password.
    Returns 204 on success, 400 if the token is invalid or expired.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        success = AuthService.confirm_password_reset(
            serializer.validated_data['token'],
            serializer.validated_data['password'],
        )

        if not success:
            return Response(
                {
                    'data': None,
                    'meta': None,
                    'errors': [{
                        'code': 'INVALID_VALUE',
                        'message': 'Invalid or expired password reset token.',
                        'field': 'token',
                        'detail': {},
                    }],
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(status=status.HTTP_204_NO_CONTENT)
