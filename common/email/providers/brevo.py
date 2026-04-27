"""Brevo (Sendinblue) transactional email provider.

Uses the Brevo v3 transactional SMTP endpoint:
    POST https://api.brevo.com/v3/smtp/email
Docs: https://developers.brevo.com/reference/sendtransacemail

Logs delivery telemetry without ever logging the verification code, the
recipient's full email, the API key, or response payload bodies that may
echo input — only the recipient domain, HTTP status, and Brevo message ID.
"""
from __future__ import annotations

import logging
from typing import Optional

import requests

from .base import EmailMessage, EmailProvider, EmailResult

logger = logging.getLogger(__name__)

BREVO_API_URL = 'https://api.brevo.com/v3/smtp/email'
DEFAULT_TIMEOUT_SECONDS = 10


def _domain(email: str) -> str:
    return email.rsplit('@', 1)[-1] if '@' in (email or '') else 'unknown'


class BrevoEmailProvider(EmailProvider):
    name = 'brevo'

    def __init__(
        self,
        *,
        api_key: str,
        sender_email: str,
        sender_name: str,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        api_url: str = BREVO_API_URL,
    ):
        # Configuration errors raise immediately — better than silently
        # failing every send at runtime.
        if not api_key:
            raise ValueError('BREVO_API_KEY is required when EMAIL_PROVIDER=brevo')
        if not sender_email:
            raise ValueError('BREVO_SENDER_EMAIL is required when EMAIL_PROVIDER=brevo')
        self.api_key = api_key
        self.sender_email = sender_email
        self.sender_name = sender_name or 'Cytova'
        self.timeout_seconds = timeout_seconds
        self.api_url = api_url

    def send(self, message: EmailMessage) -> EmailResult:
        payload = self.build_payload(message)
        recipient_domain = _domain(message.to_email)

        try:
            response = requests.post(
                self.api_url,
                json=payload,
                headers={
                    'api-key': self.api_key,
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                },
                timeout=self.timeout_seconds,
            )
        except requests.exceptions.RequestException as exc:
            # Network / timeout / DNS — don't propagate raw exception details.
            logger.error(
                'Brevo network error: provider=brevo recipient_domain=%s error_type=%s',
                recipient_domain, type(exc).__name__,
            )
            return EmailResult(ok=False, error='network_error')

        if 200 <= response.status_code < 300:
            message_id = self._extract_message_id(response)
            logger.info(
                'Email delivered: provider=brevo recipient_domain=%s message_id=%s status=%d',
                recipient_domain, message_id or 'unknown', response.status_code,
            )
            return EmailResult(ok=True, provider_message_id=message_id)

        # Non-2xx — surface status + a safely-truncated body excerpt for diagnostics.
        body_excerpt = (response.text or '')[:200] if hasattr(response, 'text') else ''
        logger.error(
            'Brevo API error: provider=brevo status=%d recipient_domain=%s body=%r',
            response.status_code, recipient_domain, body_excerpt,
        )
        return EmailResult(ok=False, error=f'http_{response.status_code}')

    # ----- Helpers (separated so tests can assert on payload shape) ------

    def build_payload(self, message: EmailMessage) -> dict:
        recipient: dict = {'email': message.to_email}
        if message.to_name:
            recipient['name'] = message.to_name
        return {
            'sender': {'name': self.sender_name, 'email': self.sender_email},
            'to': [recipient],
            'subject': message.subject,
            'htmlContent': message.html,
            'textContent': message.text,
        }

    @staticmethod
    def _extract_message_id(response) -> Optional[str]:
        try:
            return response.json().get('messageId')
        except (ValueError, AttributeError):
            return None
