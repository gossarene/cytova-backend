"""
Cytova — Authentication Service

All authentication side-effects (token issuance, blacklisting, audit logging,
password reset token lifecycle) are centralised here.
Views stay thin — they validate input and delegate to this service.
"""
import logging
from datetime import timedelta

from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.settings import api_settings as jwt_settings

from apps.users.models import StaffUser, PasswordResetToken
from apps.audit.models import AuditLog, AuditAction, ActorType
from common.utils.crypto import generate_secure_token, hash_token
from .tokens import CytovaAccessToken

logger = logging.getLogger(__name__)

_PASSWORD_RESET_TTL_HOURS = 1


class AuthService:

    @staticmethod
    def login(user: StaffUser, request) -> dict:
        """Issue access + refresh tokens and write LOGIN audit record."""
        # Prefetch permission overrides before token generation to avoid
        # a lazy query inside PermissionChecker.get_effective_permissions().
        from django.db.models import prefetch_related_objects
        prefetch_related_objects([user], 'permission_overrides')

        refresh = RefreshToken.for_user(user)
        access = CytovaAccessToken.for_user(user)

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=user.id,
            actor_email=user.email,
            action=AuditAction.LOGIN,
            entity_type='StaffUser',
            entity_id=user.id,
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return {
            'access_token': str(access),
            'refresh_token': str(refresh),
            'token_type': 'Bearer',
            'expires_in': int(access.lifetime.total_seconds()),
            'user': {
                'id': str(user.id),
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'role': user.role,
            },
        }

    @staticmethod
    def record_failed_login(email: str, request) -> None:
        """Write LOGIN_FAILED audit record. Called on credential rejection."""
        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=None,
            actor_email=email[:255] if email else None,
            action=AuditAction.LOGIN_FAILED,
            entity_type='StaffUser',
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

    @staticmethod
    def refresh(old_refresh_token) -> dict:
        """
        Rotate refresh token: blacklist the old one, issue a new pair.
        Re-fetches the user to embed the latest role in the access token.
        """
        user_id = old_refresh_token[jwt_settings.USER_ID_CLAIM]
        user = StaffUser.objects.prefetch_related('permission_overrides').get(id=user_id)

        old_refresh_token.blacklist()

        new_refresh = RefreshToken.for_user(user)
        new_access = CytovaAccessToken.for_user(user)

        return {
            'access_token': str(new_access),
            'refresh_token': str(new_refresh),
            'token_type': 'Bearer',
            'expires_in': int(new_access.lifetime.total_seconds()),
        }

    @staticmethod
    def logout(refresh_token_str: str, request) -> None:
        """Blacklist the refresh token and write LOGOUT audit record."""
        try:
            RefreshToken(refresh_token_str).blacklist()
        except TokenError:
            pass  # Already blacklisted or invalid — treat as success

        user = getattr(request, 'user', None)
        if user and user.is_authenticated:
            AuditLog.objects.create(
                actor_type=ActorType.STAFF_USER,
                actor_id=user.id,
                actor_email=user.email,
                action=AuditAction.LOGOUT,
                entity_type='StaffUser',
                entity_id=user.id,
                ip_address=getattr(request, 'audit_ip', None),
                user_agent=getattr(request, 'audit_user_agent', ''),
            )

    @staticmethod
    def request_password_reset(email: str) -> None:
        """
        Generate and store a password reset token for the given email.
        Intentionally silent if the email is not found — prevents enumeration.
        Any previously unused tokens for the same user are invalidated.
        """
        try:
            user = StaffUser.objects.get(email=email, is_active=True)
        except StaffUser.DoesNotExist:
            return

        # Invalidate existing unused tokens before creating a new one
        PasswordResetToken.objects.filter(user=user, is_used=False).update(is_used=True)

        plaintext = generate_secure_token()
        PasswordResetToken.objects.create(
            user=user,
            token_hash=hash_token(plaintext),
            expires_at=timezone.now() + timedelta(hours=_PASSWORD_RESET_TTL_HOURS),
        )

        # TODO: dispatch send_password_reset_email Celery task (email app — Phase 2)
        logger.info('Password reset token generated for %s', email)

    @staticmethod
    def confirm_password_reset(plaintext_token: str, new_password: str) -> bool:
        """
        Consume a password reset token and set the new password.
        Returns True on success, False if the token is invalid or expired.
        """
        token_hash = hash_token(plaintext_token)
        try:
            reset_token = PasswordResetToken.objects.select_related('user').get(
                token_hash=token_hash,
                is_used=False,
                expires_at__gt=timezone.now(),
            )
        except PasswordResetToken.DoesNotExist:
            return False

        user = reset_token.user
        user.set_password(new_password)
        user.save(update_fields=['password', 'updated_at'])

        reset_token.is_used = True
        reset_token.save(update_fields=['is_used'])

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=user.id,
            actor_email=user.email,
            action=AuditAction.PASSWORD_RESET,
            entity_type='StaffUser',
            entity_id=user.id,
        )

        return True
