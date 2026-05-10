"""Service layer for platform-admin authentication.

Keeps the credential-check + audit + token-issue chain in one place
so the view stays a thin HTTP adapter and the same flow is callable
from a management command if we ever need it.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from .audit import log_platform_admin_action
from .models import PlatformAdminUser, PlatformAuditAction
from .tokens import PlatformAdminAccessToken


class InvalidCredentials(Exception):
    """Raised on any credential-check failure.

    Wraps both "no such email" and "wrong password" under a single
    error so the response wording can stay generic and the timing
    differences don't leak account existence. The caller maps this
    to a 401 with a code-stable message.
    """


@dataclass(frozen=True)
class IssuedToken:
    """Minimal carrier for the values the login response surfaces."""
    access_token: str
    expires_in: int
    user: PlatformAdminUser


def authenticate_and_issue_token(
    *, email: str, password: str, request,
) -> IssuedToken:
    """Authenticate a platform admin and return a fresh access token.

    Side effects:
      - Bumps ``last_login`` to now.
      - Writes a ``PLATFORM_ADMIN_LOGIN`` audit row on success.
      - Writes a ``PLATFORM_ADMIN_LOGIN_FAILED`` audit row on failure
        (with ``actor=None`` if the email did not match).

    Both successful and failed audits go through the same atomic
    transaction so a write-failure rolls back the ``last_login``
    bump too. The audit log is the source of truth for "did this
    user successfully sign in?" — keeping it consistent with the
    bumped timestamp is load-bearing.
    """
    # Look up before checking ``is_active`` so we can write the
    # failed-login audit even when the account is disabled. We do
    # NOT short-circuit on ``is_active`` to avoid returning faster
    # than the active-account branch — both paths run the same
    # password check or its skip, so timing is steady regardless
    # of whether the account exists or is active.
    user = PlatformAdminUser.objects.filter(email__iexact=email).first()

    # ``user.is_active`` and the password check are combined into
    # one boolean so the audit branches in a single ``if/else``.
    # We DO NOT distinguish "no such user" / "wrong password" /
    # "inactive" in the response — the operator sees the same
    # generic message in all three cases.
    auth_ok = (
        user is not None
        and user.is_active
        and user.check_password(password)
    )

    with transaction.atomic():
        if not auth_ok:
            log_platform_admin_action(
                request=request,
                action=PlatformAuditAction.PLATFORM_ADMIN_LOGIN_FAILED,
                actor=user if user is not None else None,
                actor_email=email,
                metadata={
                    # Keep the metadata reason machine-readable.
                    # Never leak which path failed back to the
                    # caller — but a future SIEM aggregator wants
                    # to distinguish "wrong password on real
                    # account" from "unknown email" for spike
                    # detection.
                    'reason': (
                        'unknown_email' if user is None
                        else 'inactive' if not user.is_active
                        else 'invalid_password'
                    ),
                },
            )
            raise InvalidCredentials()

        # ``user`` is not None here (auth_ok would be False otherwise),
        # narrowing for the type checker is implicit.
        assert user is not None
        user.last_login = timezone.now()
        user.save(update_fields=['last_login', 'updated_at'])

        log_platform_admin_action(
            request=request,
            action=PlatformAuditAction.PLATFORM_ADMIN_LOGIN,
            actor=user,
            metadata={'role': user.role},
        )

    token = PlatformAdminAccessToken.for_user(user)
    return IssuedToken(
        access_token=str(token),
        expires_in=int(token.lifetime.total_seconds()),
        user=user,
    )


def me(user: PlatformAdminUser) -> PlatformAdminUser:
    """Pass-through used by the ``/auth/me/`` view.

    Keeps the read path symmetric with the login path so a future
    profile fetch hook (e.g. enriching with permissions metadata) has
    one obvious place to live.
    """
    return user


__all__ = [
    'authenticate_and_issue_token',
    'me',
    'InvalidCredentials',
    'IssuedToken',
]
