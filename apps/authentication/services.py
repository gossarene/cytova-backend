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
from common.email import get_email_service
from common.utils.crypto import generate_secure_token, hash_token
from common.utils.url import build_tenant_frontend_url
from .tokens import CytovaAccessToken

logger = logging.getLogger(__name__)

# 30 minutes per security policy. Reset URLs going stale faster also
# limits exposure if a user forwards the email by accident.
PASSWORD_RESET_TTL_MINUTES = 30
PASSWORD_RESET_FRONTEND_PATH = '/reset-password'


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
    def request_password_reset(email: str, request) -> None:
        """Generate and dispatch a password-reset email for ``email``.

        Operates strictly within the **current tenant schema** — this method
        is invoked from a tenant-routed view (subdomain Host header has
        already been resolved by CytovaTenantMiddleware), so the implicit
        ``StaffUser.objects`` query never crosses tenant boundaries.

        Always silent on the user-existence axis (returns ``None`` whether
        the email matches or not) so callers can return the same envelope
        without leaking enumeration. The reset link is built from the
        request host so the email link points at the same tenant subdomain
        the request came from — never a globally configured domain.

        Side effects on hit:
          - any previously unused tokens for the user are invalidated
          - a new token (single-use, 30 min TTL) is persisted (hash only)
          - a reset email is dispatched via ``EmailService``
        Email-delivery failures are logged but never raised — the user
        gets the same generic 204 either way (no enumeration leak).
        """
        request_ip = getattr(request, 'audit_ip', None)
        try:
            user = StaffUser.objects.get(email=email, is_active=True)
        except StaffUser.DoesNotExist:
            logger.info(
                'Password reset requested for unknown email: ip=%s host=%s',
                request_ip, request.get_host(),
            )
            return

        # Invalidate existing unused tokens before creating a new one.
        # Marks both is_used and used_at so the audit trail stays consistent.
        PasswordResetToken.objects.filter(user=user, is_used=False).update(
            is_used=True, used_at=timezone.now(),
        )

        plaintext = generate_secure_token()
        token = PasswordResetToken.objects.create(
            user=user,
            token_hash=hash_token(plaintext),
            expires_at=timezone.now() + timedelta(minutes=PASSWORD_RESET_TTL_MINUTES),
            created_by_ip=request_ip,
        )

        # Build the tenant-aware frontend link. Host comes from the
        # incoming request, never a hardcoded domain — links generated
        # for tenant A can never point at tenant B.
        reset_link = build_tenant_frontend_url(
            request,
            f'{PASSWORD_RESET_FRONTEND_PATH}?token={plaintext}',
        )

        result = get_email_service().send_password_reset_email(
            recipient_email=user.email,
            recipient_name=user.first_name,
            reset_link=reset_link,
            expires_minutes=PASSWORD_RESET_TTL_MINUTES,
        )

        if result.ok:
            logger.info(
                'Password reset email sent: user_id=%s host=%s ip=%s token_id=%s',
                user.id, request.get_host(), request_ip, token.id,
            )
        else:
            # Don't propagate — email enumeration defence keeps the response
            # generic regardless of delivery success. Operator sees the
            # failure in logs and can investigate (provider returned a
            # structured error already).
            logger.error(
                'Password reset email NOT delivered: user_id=%s host=%s ip=%s token_id=%s provider_error=%s',
                user.id, request.get_host(), request_ip, token.id, result.error,
            )

    @staticmethod
    def confirm_password_reset(plaintext_token: str, new_password: str, request=None) -> bool:
        """Consume a password-reset token and set the new password.

        Returns True on success; False for unknown / expired / already-used
        tokens. The token is hashed before lookup, so the database is queried
        with the hash only — the plaintext never touches the query log.

        Side effects on success:
          - the consumed token is marked is_used + used_at
          - any other unused tokens for the same user are invalidated
            (defence in depth — the user just demonstrated control of the
            email, anything older is moot)
          - PASSWORD_RESET audit record written
        """
        token_hash = hash_token(plaintext_token)
        request_ip = getattr(request, 'audit_ip', None) if request else None

        try:
            reset_token = PasswordResetToken.objects.select_related('user').get(
                token_hash=token_hash,
                is_used=False,
                expires_at__gt=timezone.now(),
            )
        except PasswordResetToken.DoesNotExist:
            # Never log the token (hashed or plaintext). IP only.
            logger.warning(
                'Password reset attempted with invalid/expired/used token: ip=%s',
                request_ip,
            )
            return False

        user = reset_token.user
        user.set_password(new_password)
        user.save(update_fields=['password', 'updated_at'])

        now = timezone.now()
        reset_token.is_used = True
        reset_token.used_at = now
        reset_token.save(update_fields=['is_used', 'used_at'])

        # Invalidate every other outstanding token for this user — the
        # current one just proved control of the inbox; older ones are
        # not needed and shouldn't sit around.
        PasswordResetToken.objects.filter(
            user=user, is_used=False,
        ).exclude(pk=reset_token.pk).update(is_used=True, used_at=now)

        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=user.id,
            actor_email=user.email,
            action=AuditAction.PASSWORD_RESET,
            entity_type='StaffUser',
            entity_id=user.id,
            ip_address=request_ip,
        )

        logger.info('Password reset successful: user_id=%s ip=%s', user.id, request_ip)
        return True
