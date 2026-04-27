"""Development console provider — prints the rendered email to stdout."""
from __future__ import annotations

import logging

from .base import EmailMessage, EmailProvider, EmailResult

logger = logging.getLogger(__name__)


def _domain(email: str) -> str:
    return email.rsplit('@', 1)[-1] if '@' in (email or '') else 'unknown'


class ConsoleEmailProvider(EmailProvider):
    """Prints the email payload (text body) to stdout. Intended for local
    development only — verification codes are visible by design so the
    developer can complete the flow without an inbox."""

    name = 'console'

    def send(self, message: EmailMessage) -> EmailResult:
        bar = '=' * 60
        print(bar)
        print(f'[email:console] To:      {message.to_email}')
        print(f'[email:console] Subject: {message.subject}')
        print('-' * 60)
        print(message.text)
        print(bar, flush=True)
        # Recipient domain only — never the address itself, never the code.
        logger.debug('Console email delivered: provider=console recipient_domain=%s', _domain(message.to_email))
        return EmailResult(ok=True)
