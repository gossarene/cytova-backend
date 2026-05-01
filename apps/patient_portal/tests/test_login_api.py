"""
HTTP-level tests for ``POST /api/v1/patient-portal/login/`` and
``GET /api/v1/patient-portal/me/``.
"""
from __future__ import annotations

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from apps.patient_portal.services import register_patient_account


LOGIN_URL = '/api/v1/patient-portal/login/'
ME_URL = '/api/v1/patient-portal/me/'

PASSWORD = 'Strong-Pass-1234!'


@pytest.fixture(autouse=True)
def _reset_throttle_cache():
    cache.clear()
    yield
    cache.clear()


def _make_account(*, email: str = 'login@portal.test', verified: bool = True):
    """Sign up + (optionally) flip ``email_verified_at`` directly. We
    bypass the verification token flow because login behaviour is what
    we're testing, not verification — verification has its own suite."""
    account = register_patient_account(
        email=email,
        password=PASSWORD,
        first_name='Ada', last_name='Lovelace',
        date_of_birth='1990-05-17', accept_terms=True,
    )
    if verified:
        account.email_verified_at = timezone.now()
        account.save(update_fields=['email_verified_at'])
    return account


def _post_login(payload, host: str = 'testlab.localhost'):
    return APIClient().post(
        LOGIN_URL, data=payload, format='json', HTTP_HOST=host,
    )


def _envelope(resp):
    return resp.json()


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestPatientLogin:

    def test_login_returns_tokens_and_patient_payload(self):
        _make_account()
        resp = _post_login({'email': 'login@portal.test', 'password': PASSWORD})
        assert resp.status_code == 200, resp.content
        body = _envelope(resp)
        assert body['errors'] == []
        data = body['data']
        # Token shape mirrors staff login for frontend reuse.
        assert {
            'access_token', 'refresh_token', 'token_type',
            'expires_in', 'patient',
        } <= set(data)
        assert data['token_type'] == 'Bearer'
        assert data['expires_in'] > 0
        # Patient payload exposes only safe identity fields — no
        # password hash, no consent rows.
        assert set(data['patient']) == {
            'id', 'email', 'cytova_patient_id', 'first_name', 'last_name',
        }
        assert data['patient']['email'] == 'login@portal.test'

    def test_login_fails_for_unknown_email_with_401(self):
        resp = _post_login({'email': 'nobody@portal.test', 'password': PASSWORD})
        assert resp.status_code == 401, resp.content
        codes = {e['code'] for e in _envelope(resp)['errors']}
        assert 'INVALID_CREDENTIALS' in codes

    def test_login_fails_for_wrong_password_with_401(self):
        _make_account()
        resp = _post_login({'email': 'login@portal.test', 'password': 'wrong-pw-xxxx'})
        assert resp.status_code == 401, resp.content
        codes = {e['code'] for e in _envelope(resp)['errors']}
        assert 'INVALID_CREDENTIALS' in codes

    def test_login_fails_for_unverified_email_with_403(self):
        _make_account(email='unverified@portal.test', verified=False)
        resp = _post_login({
            'email': 'unverified@portal.test', 'password': PASSWORD,
        })
        # Distinct status + code so the UI can prompt "check your inbox"
        # — still requires the correct password to fire (no enumeration
        # leak via verification status).
        assert resp.status_code == 403, resp.content
        codes = {e['code'] for e in _envelope(resp)['errors']}
        assert 'EMAIL_NOT_VERIFIED' in codes

    def test_login_fails_for_inactive_account_with_401(self):
        account = _make_account(email='inactive@portal.test')
        account.is_active = False
        account.save(update_fields=['is_active'])
        resp = _post_login({
            'email': 'inactive@portal.test', 'password': PASSWORD,
        })
        # Same generic credential failure — never tell the caller the
        # account exists but is disabled.
        assert resp.status_code == 401, resp.content


# ---------------------------------------------------------------------------
# /me endpoint
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestPatientMeEndpoint:

    def _login_and_get_me(self, account):
        login = _post_login({'email': account.email, 'password': PASSWORD})
        assert login.status_code == 200, login.content
        token = login.json()['data']['access_token']
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        return client.get(ME_URL, HTTP_HOST='testlab.localhost')

    def test_me_returns_profile_for_authenticated_patient(self):
        account = _make_account(email='me@portal.test')
        resp = self._login_and_get_me(account)
        assert resp.status_code == 200, resp.content
        data = _envelope(resp)['data']
        assert data['email'] == 'me@portal.test'
        assert data['cytova_patient_id'].startswith('CV-')
        assert data['first_name'] == 'Ada'
        assert data['last_name'] == 'Lovelace'
        assert data['email_verified_at']

    def test_me_rejects_unauthenticated_request(self):
        resp = APIClient().get(ME_URL, HTTP_HOST='testlab.localhost')
        assert resp.status_code in (401, 403), resp.content

    def test_me_rejects_a_staff_jwt(self):
        # Issue a STAFF JWT (the standard CytovaAccessToken has no
        # ``user_type=PATIENT`` claim) and confirm the patient endpoint
        # refuses it. Catches the cross-flow leak the auth backend is
        # designed to prevent.
        from django_tenants.utils import schema_context
        from apps.users.models import StaffUser, Role
        from apps.authentication.tokens import CytovaAccessToken

        with schema_context('schema_testlab'):
            staff = StaffUser.objects.create_user(
                email='staff@testlab.io',
                password=PASSWORD,
                first_name='Staff', last_name='User',
                role=Role.LAB_ADMIN,
            )
            staff_token = str(CytovaAccessToken.for_user(staff))

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {staff_token}')
        resp = client.get(ME_URL, HTTP_HOST='testlab.localhost')
        # The patient JWT auth backend raises InvalidToken when the
        # ``user_type`` claim isn't ``PATIENT`` → 401.
        assert resp.status_code == 401, resp.content
