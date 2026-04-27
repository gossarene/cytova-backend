"""EmailService — single entry point for transactional email delivery.

Domain code (e.g. onboarding) calls ``get_email_service().send_verification_code(...)``.
The active provider is resolved from settings on every call so tests using
``@override_settings`` see the change without a cache reset.
"""
from __future__ import annotations

import logging

from django.conf import settings

from .providers.base import EmailMessage, EmailProvider, EmailResult
from .providers.brevo import BrevoEmailProvider
from .providers.console import ConsoleEmailProvider
from .templates import (
    render_password_reset,
    render_patient_result_ready,
    render_verification,
)

logger = logging.getLogger(__name__)

VERIFICATION_SUBJECT = 'Your Cytova verification code'
PASSWORD_RESET_SUBJECT = 'Reset your Cytova password'
PATIENT_RESULT_READY_SUBJECT = 'Your lab result is ready'

# Provider name → human-readable label used in startup logs.
_PROVIDERS = ('console', 'brevo')


class EmailService:
    """Holds an `EmailProvider` and renders domain messages onto it.

    Resolved from settings via ``EmailService.from_settings()`` or the
    convenience ``get_email_service()`` factory below.
    """

    def __init__(self, provider: EmailProvider):
        self.provider = provider

    # ----- Construction ------------------------------------------------

    @classmethod
    def from_settings(cls) -> 'EmailService':
        provider_name = (getattr(settings, 'EMAIL_PROVIDER', '') or 'console').strip().lower()
        if provider_name not in _PROVIDERS:
            logger.warning(
                'Unknown EMAIL_PROVIDER=%r; falling back to console.',
                provider_name,
            )
            provider_name = 'console'

        if provider_name == 'brevo':
            provider: EmailProvider = BrevoEmailProvider(
                api_key=getattr(settings, 'BREVO_API_KEY', ''),
                sender_email=getattr(settings, 'BREVO_SENDER_EMAIL', ''),
                sender_name=getattr(settings, 'BREVO_SENDER_NAME', 'Cytova'),
            )
        else:
            provider = ConsoleEmailProvider()

        logger.info('EmailService configured: provider=%s', provider.name)
        return cls(provider=provider)

    # ----- Domain operations ------------------------------------------

    def send_verification_code(
        self,
        *,
        recipient_email: str,
        recipient_name: str,
        code: str,
        expires_minutes: int,
    ) -> EmailResult:
        """Render and dispatch a verification-code email. Never raises —
        delivery failures are returned as ``EmailResult(ok=False, error=...)``
        so the caller decides how to handle them. The verification code is
        passed straight to the rendered template; this method does not
        log it under any provider."""
        html_body, text_body = render_verification(
            first_name=recipient_name,
            code=code,
            expires_minutes=expires_minutes,
        )
        message = EmailMessage(
            to_email=recipient_email,
            to_name=recipient_name or '',
            subject=VERIFICATION_SUBJECT,
            text=text_body,
            html=html_body,
        )
        return self.provider.send(message)

    def send_patient_result_ready_email(
        self,
        *,
        recipient_email: str,
        recipient_name: str,
        secure_link: str,
        lab_name: str = '',
    ) -> EmailResult:
        """Notify a patient that their result is ready, by email.

        Carries only the secure access URL — no medical data, no result
        values, no diagnosis, no exam details. The patient consumes the
        URL via the existing tenant-isolated patient-access flow, which
        handles authentication (PDF password) and brute-force protection.

        Same delivery contract as other EmailService methods — failures
        return ``EmailResult(ok=False, error=...)`` rather than raising,
        so the caller can record the failed attempt without crashing the
        notify-patient endpoint.
        """
        html_body, text_body = render_patient_result_ready(
            first_name=recipient_name,
            secure_link=secure_link,
            lab_name=lab_name,
        )
        message = EmailMessage(
            to_email=recipient_email,
            to_name=recipient_name or '',
            subject=PATIENT_RESULT_READY_SUBJECT,
            text=text_body,
            html=html_body,
        )
        return self.provider.send(message)

    def send_password_reset_email(
        self,
        *,
        recipient_email: str,
        recipient_name: str,
        reset_link: str,
        expires_minutes: int = 30,
    ) -> EmailResult:
        """Render and dispatch a password-reset email. Same delivery contract
        as ``send_verification_code`` — failures return EmailResult(ok=False).

        The reset link contains the raw single-use token; this method does
        not log it under any provider. Caller is responsible for never
        logging the link either."""
        html_body, text_body = render_password_reset(
            first_name=recipient_name,
            reset_url=reset_link,
            expires_minutes=expires_minutes,
        )
        message = EmailMessage(
            to_email=recipient_email,
            to_name=recipient_name or '',
            subject=PASSWORD_RESET_SUBJECT,
            text=text_body,
            html=html_body,
        )
        return self.provider.send(message)


def get_email_service() -> EmailService:
    """Resolve the active EmailService. Reads settings on every call so
    test overrides via ``@override_settings`` are picked up immediately."""
    return EmailService.from_settings()
