"""Tests for the EmailProvider abstraction.

Coverage:
  - Provider selection from EMAIL_PROVIDER setting (console / brevo / unknown / unset)
  - ConsoleEmailProvider prints to stdout and never raises
  - BrevoEmailProvider builds the correct request payload + headers
  - BrevoEmailProvider handles 2xx success (returns ok + message_id)
  - BrevoEmailProvider handles 4xx and network errors gracefully
  - BrevoEmailProvider never logs the verification code
  - Onboarding `start()` routes through EmailService
"""
from __future__ import annotations

import io
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest
import requests
from django.test import override_settings

from common.email import EmailMessage, EmailService, get_email_service
from common.email.providers.brevo import BREVO_API_URL, BrevoEmailProvider
from common.email.providers.console import ConsoleEmailProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msg(**overrides):
    base = {
        'to_email': 'alice@example.com',
        'to_name': 'Alice',
        'subject': 'Your Cytova verification code',
        'text': 'Plain text body with code 482917 inside.',
        'html': '<p>HTML body with code 482917 inside.</p>',
    }
    base.update(overrides)
    return EmailMessage(**base)


class _FakeResponse:
    """Stand-in for `requests.Response` — only the attributes the
    provider touches need to be implemented."""

    def __init__(self, status_code: int, body: dict | None = None, text: str = ''):
        self.status_code = status_code
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------

class TestProviderSelection:

    def test_default_is_console_when_unset(self):
        with override_settings(EMAIL_PROVIDER='', BREVO_API_KEY='ignored'):
            service = EmailService.from_settings()
            assert isinstance(service.provider, ConsoleEmailProvider)
            assert service.provider.name == 'console'

    def test_console_when_explicit(self):
        with override_settings(EMAIL_PROVIDER='console'):
            assert isinstance(EmailService.from_settings().provider, ConsoleEmailProvider)

    def test_brevo_when_configured(self):
        with override_settings(
            EMAIL_PROVIDER='brevo',
            BREVO_API_KEY='xkeysib-test',
            BREVO_SENDER_EMAIL='no-reply@cytova.io',
            BREVO_SENDER_NAME='Cytova',
        ):
            service = EmailService.from_settings()
            assert isinstance(service.provider, BrevoEmailProvider)
            assert service.provider.name == 'brevo'

    def test_brevo_without_api_key_raises_at_construction(self):
        # Programmer error — fail loudly so the misconfiguration is caught
        # at boot, not on the first verification email of the day.
        with override_settings(EMAIL_PROVIDER='brevo', BREVO_API_KEY=''):
            with pytest.raises(ValueError, match='BREVO_API_KEY'):
                EmailService.from_settings()

    def test_unknown_provider_falls_back_to_console(self):
        with override_settings(EMAIL_PROVIDER='resend'):
            assert isinstance(EmailService.from_settings().provider, ConsoleEmailProvider)

    def test_provider_selection_does_not_depend_on_debug(self):
        # The Brevo branch must work even with DEBUG=True (so devs can
        # smoke-test real delivery before deploying).
        with override_settings(
            DEBUG=True,
            EMAIL_PROVIDER='brevo',
            BREVO_API_KEY='xkeysib-test',
            BREVO_SENDER_EMAIL='no-reply@cytova.io',
        ):
            assert isinstance(EmailService.from_settings().provider, BrevoEmailProvider)


# ---------------------------------------------------------------------------
# Console provider
# ---------------------------------------------------------------------------

class TestConsoleProvider:

    def test_send_prints_text_body_to_stdout(self):
        provider = ConsoleEmailProvider()
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = provider.send(_msg())
        assert result.ok is True
        out = buf.getvalue()
        assert 'alice@example.com' in out
        assert 'Your Cytova verification code' in out
        assert '482917' in out  # by design — dev needs to see the code

    def test_send_never_raises_on_minimal_message(self):
        # No name, simple body — should still succeed.
        provider = ConsoleEmailProvider()
        with redirect_stdout(io.StringIO()):
            result = provider.send(_msg(to_name='', text='Hello'))
        assert result.ok is True


# ---------------------------------------------------------------------------
# Brevo provider — payload + headers
# ---------------------------------------------------------------------------

class TestBrevoPayload:

    def test_build_payload_minimal(self):
        provider = BrevoEmailProvider(
            api_key='xkeysib-x',
            sender_email='no-reply@cytova.io',
            sender_name='Cytova',
        )
        payload = provider.build_payload(_msg())
        assert payload == {
            'sender': {'name': 'Cytova', 'email': 'no-reply@cytova.io'},
            'to': [{'email': 'alice@example.com', 'name': 'Alice'}],
            'subject': 'Your Cytova verification code',
            'htmlContent': '<p>HTML body with code 482917 inside.</p>',
            'textContent': 'Plain text body with code 482917 inside.',
        }

    def test_build_payload_omits_recipient_name_when_empty(self):
        provider = BrevoEmailProvider(
            api_key='x', sender_email='from@cytova.io', sender_name='Cytova',
        )
        payload = provider.build_payload(_msg(to_name=''))
        assert payload['to'] == [{'email': 'alice@example.com'}]

    def test_send_calls_brevo_endpoint_with_api_key_header(self):
        provider = BrevoEmailProvider(
            api_key='xkeysib-secret',
            sender_email='from@cytova.io',
            sender_name='Cytova',
        )
        with patch('common.email.providers.brevo.requests.post') as post:
            post.return_value = _FakeResponse(201, {'messageId': 'msg-123'})
            result = provider.send(_msg())

        assert result.ok is True
        assert result.provider_message_id == 'msg-123'
        call = post.call_args
        assert call.args[0] == BREVO_API_URL
        assert call.kwargs['json']['subject'] == 'Your Cytova verification code'
        headers = call.kwargs['headers']
        assert headers['api-key'] == 'xkeysib-secret'
        assert headers['Content-Type'] == 'application/json'


# ---------------------------------------------------------------------------
# Brevo provider — failure modes
# ---------------------------------------------------------------------------

class TestBrevoFailureHandling:

    def _provider(self):
        return BrevoEmailProvider(
            api_key='x', sender_email='from@cytova.io', sender_name='Cytova',
        )

    def test_http_4xx_returns_failure_without_raising(self):
        with patch('common.email.providers.brevo.requests.post') as post:
            post.return_value = _FakeResponse(400, body={}, text='{"message": "bad sender"}')
            result = self._provider().send(_msg())
        assert result.ok is False
        assert result.error == 'http_400'

    def test_network_error_returns_failure_without_raising(self):
        with patch(
            'common.email.providers.brevo.requests.post',
            side_effect=requests.exceptions.ConnectionError('dns'),
        ):
            result = self._provider().send(_msg())
        assert result.ok is False
        assert result.error == 'network_error'

    def test_timeout_returns_failure_without_raising(self):
        with patch(
            'common.email.providers.brevo.requests.post',
            side_effect=requests.exceptions.Timeout('slow'),
        ):
            result = self._provider().send(_msg())
        assert result.ok is False
        assert result.error == 'network_error'


# ---------------------------------------------------------------------------
# Logging hygiene — no verification code in Brevo logs
# ---------------------------------------------------------------------------

class TestBrevoNeverLogsCode:
    """Property of the provider: the verification code (which appears in
    `message.text` and `message.html`) must never reach the logger output
    for any Brevo code path — success, 4xx, or network error."""

    PROVIDER = BrevoEmailProvider

    def _provider(self):
        return self.PROVIDER(api_key='x', sender_email='from@cytova.io', sender_name='Cytova')

    def _assert_no_code(self, caplog):
        joined = ' '.join(rec.getMessage() for rec in caplog.records)
        assert '482917' not in joined, f'verification code leaked into logs: {joined!r}'

    def test_no_code_in_logs_on_success(self, caplog):
        with caplog.at_level('DEBUG', logger='common.email.providers.brevo'), \
             patch('common.email.providers.brevo.requests.post',
                   return_value=_FakeResponse(201, {'messageId': 'm'})):
            self._provider().send(_msg())
        self._assert_no_code(caplog)

    def test_no_code_in_logs_on_4xx(self, caplog):
        # The body excerpt (first 200 chars of response.text) is included in
        # the error log — make sure it doesn't echo our request body.
        echo_body = 'error: ' + 'x' * 50  # any non-code body is fine
        with caplog.at_level('DEBUG', logger='common.email.providers.brevo'), \
             patch('common.email.providers.brevo.requests.post',
                   return_value=_FakeResponse(400, text=echo_body)):
            self._provider().send(_msg())
        self._assert_no_code(caplog)

    def test_no_code_in_logs_on_network_error(self, caplog):
        with caplog.at_level('DEBUG', logger='common.email.providers.brevo'), \
             patch('common.email.providers.brevo.requests.post',
                   side_effect=requests.exceptions.ConnectionError('boom')):
            self._provider().send(_msg())
        self._assert_no_code(caplog)


# ---------------------------------------------------------------------------
# EmailService — service-level rendering
# ---------------------------------------------------------------------------

class TestEmailServiceRender:

    def test_send_verification_code_renders_subject_and_body(self):
        captured = {}

        class _StubProvider:
            name = 'stub'

            def send(self, message: EmailMessage):
                captured['message'] = message
                from common.email.providers.base import EmailResult
                return EmailResult(ok=True)

        service = EmailService(provider=_StubProvider())
        result = service.send_verification_code(
            recipient_email='alice@example.com',
            recipient_name='Alice',
            code='482917',
            expires_minutes=10,
        )
        assert result.ok is True
        msg: EmailMessage = captured['message']
        assert msg.subject == 'Your Cytova verification code'
        assert '482917' in msg.text
        assert '482917' in msg.html
        assert '10 minutes' in msg.text
        assert msg.to_email == 'alice@example.com'
        assert msg.to_name == 'Alice'


# ---------------------------------------------------------------------------
# Onboarding integration — start() goes through EmailService
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestOnboardingUsesEmailService:

    def test_start_invokes_email_service_with_code(self, monkeypatch):
        captured = {}

        class _StubProvider:
            name = 'stub'

            def send(self, message: EmailMessage):
                captured['message'] = message
                from common.email.providers.base import EmailResult
                return EmailResult(ok=True)

        # Force the onboarding flow to use our stub provider regardless of
        # EMAIL_PROVIDER. Patching the factory is enough because the service
        # reads it lazily on each call.
        monkeypatch.setattr(
            'apps.tenants.onboarding_service.get_email_service',
            lambda: EmailService(provider=_StubProvider()),
        )

        from apps.tenants.onboarding_service import OnboardingService
        registration = OnboardingService.start(
            first_name='Alice',
            last_name='Dupont',
            email=f'integration-{id(captured)}@example.com',
            phone='+33 1 00 00 00 00',
        )
        assert registration.id is not None
        msg = captured['message']
        # The 6-digit code must appear in the rendered body — but the row
        # itself stores only the hash.
        import re
        match = re.search(r'\b(\d{6})\b', msg.text)
        assert match, 'verification code missing from rendered email'
        assert match.group(1) not in registration.verification_code_hash
