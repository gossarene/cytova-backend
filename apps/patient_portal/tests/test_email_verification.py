"""
Tests for email verification — service-level + endpoint-level.

The signup flow (covered in ``test_signup_api.py``) now writes a real
``PatientEmailVerificationToken`` and emits a verification email. The
project's default test email provider is ``console`` (no real SMTP),
so signup-time emails just print to stdout — no special mocking
required.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from apps.patient_portal.models import (
    PatientAccount, PatientEmailVerificationToken, PatientProfile,
)
from apps.patient_portal.services import (
    EMAIL_VERIFICATION_TTL_HOURS,
    InvalidVerificationToken,
    register_patient_account,
    verify_email_token,
)
from common.utils.crypto import generate_secure_token, hash_token


VERIFY_URL = '/api/v1/patient-portal/verify-email/'


@pytest.fixture(autouse=True)
def _reset_throttle_cache():
    cache.clear()
    yield
    cache.clear()


def _signup(email: str = 'verify@portal.test') -> PatientAccount:
    return register_patient_account(
        email=email,
        password='Strong-Pass-1234!',
        first_name='Ada',
        last_name='Lovelace',
        date_of_birth='1990-05-17',
        accept_terms=True,
    )


def _latest_token_plaintext_for(_account: PatientAccount) -> str:
    """Fetch the most recent verification token by reissuing a fresh
    one so the test holds the plaintext. The signup-time token's
    plaintext is sent in email and never returned by the service —
    that's by design. For tests we generate a parallel known token by
    invalidating the existing one and inserting a new row directly."""
    plaintext = generate_secure_token()
    PatientEmailVerificationToken.objects.filter(
        account=_account, is_used=False,
    ).update(is_used=True, used_at=timezone.now())
    PatientEmailVerificationToken.objects.create(
        account=_account,
        token_hash=hash_token(plaintext),
        expires_at=timezone.now() + timedelta(hours=EMAIL_VERIFICATION_TTL_HOURS),
    )
    return plaintext


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestVerifyEmailService:

    def test_signup_creates_outstanding_verification_token(self):
        account = _signup()
        # Exactly one outstanding token after signup; the plaintext was
        # emailed and is not retrievable, but the row exists.
        assert PatientEmailVerificationToken.objects.filter(
            account=account, is_used=False,
        ).count() == 1

    def test_verify_marks_account_verified_and_consumes_token(self):
        account = _signup()
        plaintext = _latest_token_plaintext_for(account)
        assert account.email_verified_at is None

        verified = verify_email_token(plaintext)
        verified.refresh_from_db()
        assert verified.email_verified_at is not None
        token = PatientEmailVerificationToken.objects.get(
            token_hash=hash_token(plaintext),
        )
        assert token.is_used is True
        assert token.used_at is not None

    def test_unknown_token_raises(self):
        with pytest.raises(InvalidVerificationToken):
            verify_email_token('this-token-was-never-issued')

    def test_expired_token_raises(self):
        account = _signup()
        plaintext = _latest_token_plaintext_for(account)
        # Backdate the expiry.
        PatientEmailVerificationToken.objects.filter(
            token_hash=hash_token(plaintext),
        ).update(expires_at=timezone.now() - timedelta(minutes=1))
        with pytest.raises(InvalidVerificationToken):
            verify_email_token(plaintext)

    def test_used_token_cannot_be_reused(self):
        account = _signup()
        plaintext = _latest_token_plaintext_for(account)
        verify_email_token(plaintext)
        # Same token, second call must fail.
        with pytest.raises(InvalidVerificationToken):
            verify_email_token(plaintext)

    def test_other_outstanding_tokens_invalidated_on_success(self):
        account = _signup()
        # Create TWO outstanding tokens, verify one — the other must
        # also be marked used.
        keep = _latest_token_plaintext_for(account)
        other_plain = generate_secure_token()
        PatientEmailVerificationToken.objects.create(
            account=account,
            token_hash=hash_token(other_plain),
            expires_at=timezone.now() + timedelta(hours=24),
        )
        verify_email_token(keep)
        other = PatientEmailVerificationToken.objects.get(
            token_hash=hash_token(other_plain),
        )
        assert other.is_used is True


# ---------------------------------------------------------------------------
# HTTP endpoint
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestVerifyEmailEndpoint:

    def test_post_with_valid_token_returns_200_and_verifies(self):
        account = _signup(email='endpoint@portal.test')
        plaintext = _latest_token_plaintext_for(account)
        client = APIClient()
        resp = client.post(
            VERIFY_URL, data={'token': plaintext}, format='json',
            HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 200, resp.content
        body = resp.json()
        assert body['errors'] == []
        assert body['data']['email'] == 'endpoint@portal.test'
        assert body['data']['email_verified_at']

        account.refresh_from_db()
        assert account.email_verified_at is not None

    def test_post_with_invalid_token_returns_400_with_generic_code(self):
        client = APIClient()
        resp = client.post(
            VERIFY_URL, data={'token': 'definitely-not-a-real-token'},
            format='json', HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 400, resp.content
        codes = {e['code'] for e in resp.json()['errors']}
        assert 'INVALID_OR_EXPIRED_TOKEN' in codes

    def test_post_with_expired_token_returns_400(self):
        account = _signup(email='expired@portal.test')
        plaintext = _latest_token_plaintext_for(account)
        PatientEmailVerificationToken.objects.filter(
            token_hash=hash_token(plaintext),
        ).update(expires_at=timezone.now() - timedelta(minutes=1))
        client = APIClient()
        resp = client.post(
            VERIFY_URL, data={'token': plaintext}, format='json',
            HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 400, resp.content
