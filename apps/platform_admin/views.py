"""
Platform-admin auth API.

Two endpoints:
  - POST /api/v1/platform-admin/auth/login/  (no auth)
  - GET  /api/v1/platform-admin/auth/me/     (PlatformAdminJWT)

Both stay thin — the credential / audit / token-issue chain lives
in ``services.authenticate_and_issue_token``. The view's job is the
HTTP shape and the per-endpoint authn/perm wiring.
"""
from __future__ import annotations

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import PlatformAdminJWTAuthentication
from .permissions import IsPlatformAdmin
from .serializers import (
    PlatformAdminLoginSerializer, PlatformAdminProfileSerializer,
)
from .services import (
    InvalidCredentials, authenticate_and_issue_token, me,
)


class PlatformAdminLoginView(APIView):
    """``POST /api/v1/platform-admin/auth/login/``

    Accepts ``email`` + ``password``. Returns a Bearer access token
    or 401 with a generic error code. The error wording stays
    identical for "no such email", "wrong password", and "inactive
    account" — see ``services.authenticate_and_issue_token`` for the
    rationale.
    """
    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PlatformAdminLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            issued = authenticate_and_issue_token(
                email=serializer.validated_data['email'],
                password=serializer.validated_data['password'],
                request=request,
            )
        except InvalidCredentials:
            # Generic 401 — the service has already written the
            # failed-login audit row before raising.
            return Response(
                {
                    'data': None,
                    'meta': None,
                    'errors': [{
                        'code': 'AUTHENTICATION_FAILED',
                        'message': 'Invalid credentials.',
                        'field': None,
                        'detail': {},
                    }],
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        return Response({
            'access_token': issued.access_token,
            'token_type': 'Bearer',
            'expires_in': issued.expires_in,
            'admin': PlatformAdminProfileSerializer(issued.user).data,
        })


class PlatformAdminMeView(APIView):
    """``GET /api/v1/platform-admin/auth/me/``

    Returns the authenticated platform admin's profile. The auth
    class refuses tokens whose ``user_type`` is not
    ``'PLATFORM_ADMIN'``, so a tenant staff or patient token can
    never reach this serializer.
    """
    authentication_classes = [PlatformAdminJWTAuthentication]
    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        return Response(
            PlatformAdminProfileSerializer(me(request.user)).data,
        )
