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
    render_biologist_request_ready,
    render_password_reset,
    render_patient_result_ready,
    render_patient_shared_result,
    render_patient_verification,
    render_technician_result_rejected,
    render_verification,
)

logger = logging.getLogger(__name__)

VERIFICATION_SUBJECT = 'Your Cytova verification code'
PASSWORD_RESET_SUBJECT = 'Reset your Cytova password'
# Patient-result-ready subject is no longer a static constant — Phase 2
# of the customisable-templates rollout moved it into
# ``LabSettings.patient_result_email_subject_template``. The renderer
# falls back to ``"Your lab result is ready"`` when the operator's
# template is empty, so the visible default copy is unchanged.
PATIENT_VERIFY_SUBJECT = 'Verify your Cytova account'
PATIENT_SHARED_RESULT_SUBJECT = 'New lab result available in Cytova'
# Internal-staff workflow subjects (biologist + technician). Kept as
# constants so SIEM filters and audit logs can match against them.
BIOLOGIST_REVIEW_READY_SUBJECT = '[Cytova] Request ready for biological validation'
TECH_RESULT_REJECTED_SUBJECT = '[Cytova] A result you submitted was rejected'

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
        request_reference: str = '',
        subject_template: str = '',
        body_template: str = '',
    ) -> EmailResult:
        """Notify a patient that their result is ready, by email.

        Carries only the secure access URL — no medical data, no result
        values, no diagnosis, no exam details. The patient consumes the
        URL via the existing tenant-isolated patient-access flow, which
        handles authentication (PDF password) and brute-force protection.

        Phase 2 of the customisable-templates rollout added the
        ``subject_template`` / ``body_template`` / ``request_reference``
        kwargs. The caller (``RequestNotificationService``) reads the
        operator-customised templates from ``LabSettings`` and threads
        them in. Empty templates fall back to the canonical hard-coded
        copy so any caller still on the pre-Phase-2 contract sends the
        same email it always has — back-compat is byte-for-byte
        preserved on the fallback path.

        The four-variable allow-list (``patient_first_name``,
        ``lab_name``, ``result_link``, ``request_reference``) is the
        structural confidentiality guarantee — even an operator
        attempting to paste medical content into a template gets the
        literal placeholder back, never the value.

        Same delivery contract as other EmailService methods — failures
        return ``EmailResult(ok=False, error=...)`` rather than raising,
        so the caller can record the failed attempt without crashing the
        notify-patient endpoint.
        """
        subject, html_body, text_body = render_patient_result_ready(
            first_name=recipient_name,
            secure_link=secure_link,
            lab_name=lab_name,
            request_reference=request_reference,
            subject_template=subject_template,
            body_template=body_template,
        )
        message = EmailMessage(
            to_email=recipient_email,
            to_name=recipient_name or '',
            # Use the rendered subject directly. The rendered subject
            # falls back to ``PATIENT_RESULT_READY_SUBJECT`` (the
            # canonical default) when ``subject_template`` is empty,
            # so back-compat is preserved.
            subject=subject,
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


    def send_patient_verification_email(
        self,
        *,
        recipient_email: str,
        recipient_name: str,
        verify_link: str,
        expires_hours: int = 24,
    ) -> EmailResult:
        """Render and dispatch the patient-portal email-verification email.

        Same delivery contract as the other ``send_*`` methods — failures
        return ``EmailResult(ok=False, error=...)`` rather than raising,
        so the signup flow doesn't crash if SMTP is down. The verification
        link contains the raw single-use token; this method does not log
        the link under any provider, and callers MUST NOT log it either.
        """
        html_body, text_body = render_patient_verification(
            first_name=recipient_name,
            verify_url=verify_link,
            expires_hours=expires_hours,
        )
        message = EmailMessage(
            to_email=recipient_email,
            to_name=recipient_name or '',
            subject=PATIENT_VERIFY_SUBJECT,
            text=text_body,
            html=html_body,
        )
        return self.provider.send(message)


    def send_patient_shared_result_email(
        self,
        *,
        recipient_email: str,
        view_url: str,
    ) -> EmailResult:
        """Notify a patient that a lab has shared a result with their
        Cytova patient space.

        Confidentiality contract — caller MUST NOT pass any medical
        content. The template renders a generic "log in to Cytova"
        prompt + the CTA URL, and nothing else. The patient sees the
        actual result only after authenticating to the portal and
        downloading via the per-file token endpoint.

        Same delivery contract as the other ``send_*`` methods —
        failures return ``EmailResult(ok=False, error=...)`` rather
        than raising, so the caller can record the failure without
        crashing the share.
        """
        html_body, text_body = render_patient_shared_result(view_url=view_url)
        message = EmailMessage(
            to_email=recipient_email,
            to_name='',
            subject=PATIENT_SHARED_RESULT_SUBJECT,
            text=text_body,
            html=html_body,
        )
        return self.provider.send(message)


    def send_biologist_review_ready_email(
        self,
        *,
        recipient_email: str,
        recipient_name: str,
        request_reference: str,
        exam_names: list[str],
        review_url: str,
    ) -> EmailResult:
        """Notify a biologist that an analysis request has all its
        results submitted and is awaiting biological validation.

        Internal-staff path. The template renders only the request
        reference, the exam name list, and the CTA URL — never a
        result value, never patient PII. Failures are returned via
        ``EmailResult(ok=False)``; the calling notification service
        flips its log row to ``FAILED`` rather than rolling back
        the underlying workflow transaction (the staff result
        submission MUST succeed even if SMTP is down).
        """
        html_body, text_body = render_biologist_request_ready(
            first_name=recipient_name,
            request_reference=request_reference,
            exam_names=exam_names,
            review_url=review_url,
        )
        message = EmailMessage(
            to_email=recipient_email,
            to_name=recipient_name or '',
            subject=BIOLOGIST_REVIEW_READY_SUBJECT,
            text=text_body,
            html=html_body,
        )
        return self.provider.send(message)


    def send_technician_result_rejected_email(
        self,
        *,
        recipient_email: str,
        recipient_name: str,
        request_reference: str,
        exam_name: str,
        rejection_notes: str,
        review_url: str,
    ) -> EmailResult:
        """Notify a technician that their submitted result was
        rejected by a biologist and needs to be re-entered.

        Same delivery contract as the other ``send_*`` methods.
        ``rejection_notes`` is operator-written feedback — the
        caller is responsible for keeping clinical content out of
        it; the template treats it as plain text and escapes it
        when rendering HTML.
        """
        html_body, text_body = render_technician_result_rejected(
            first_name=recipient_name,
            request_reference=request_reference,
            exam_name=exam_name,
            rejection_notes=rejection_notes,
            review_url=review_url,
        )
        message = EmailMessage(
            to_email=recipient_email,
            to_name=recipient_name or '',
            subject=TECH_RESULT_REJECTED_SUBJECT,
            text=text_body,
            html=html_body,
        )
        return self.provider.send(message)


def get_email_service() -> EmailService:
    """Resolve the active EmailService. Reads settings on every call so
    test overrides via ``@override_settings`` are picked up immediately."""
    return EmailService.from_settings()
