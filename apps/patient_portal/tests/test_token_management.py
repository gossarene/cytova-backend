"""
End-to-end tests for the patient-portal token blacklist + rotation +
logout + cleanup machinery.

Each test runs in isolation against the public schema (the
patient_portal tests ``conftest.py`` overrides the project's autouse
to ``schema_context(public)``). The lab-tenant schema is unaffected
by anything here — patient tokens are entirely independent of the
simplejwt OutstandingToken / BlacklistedToken tables.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from apps.patient_portal.models import (
    PatientAccount, PatientBlacklistedToken, PatientOutstandingToken,
    PatientTokenType,
)
from apps.patient_portal.services import (
    blacklist_all_tokens_for_account, issue_patient_tokens,
    register_patient_account,
)


LOGIN_URL = '/api/v1/patient-portal/login/'
LOGOUT_URL = '/api/v1/patient-portal/logout/'
REFRESH_URL = '/api/v1/patient-portal/refresh/'
ME_URL = '/api/v1/patient-portal/me/'


@pytest.fixture(autouse=True)
def _reset_throttle_cache():
    cache.clear()
    yield
    cache.clear()


_SEQ = 0


def _make_account(*, email_prefix='token', verified: bool = True) -> PatientAccount:
    global _SEQ
    _SEQ += 1
    account = register_patient_account(
        email=f'{email_prefix}-{_SEQ}@portal.test',
        password='Strong-Pass-1234!',
        first_name='Ada', last_name=f'TokenTest{_SEQ}',
        date_of_birth=date(1990, 5, 17), accept_terms=True,
    )
    if verified:
        account.email_verified_at = timezone.now()
        account.save(update_fields=['email_verified_at'])
    return account


def _login_via_api(email: str, password: str = 'Strong-Pass-1234!') -> dict:
    """Hit the real login endpoint and return the wire payload — gives
    us tokens whose Outstanding rows were written through the live
    issuance path (instead of bypassing via the service)."""
    client = APIClient()
    resp = client.post(
        LOGIN_URL, data={'email': email, 'password': password},
        format='json', HTTP_HOST='testlab.localhost',
    )
    assert resp.status_code == 200, resp.content
    return resp.json()['data']


def _auth_client(access_token: str) -> APIClient:
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {access_token}')
    return c


# ---------------------------------------------------------------------------
# Issuance
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestIssuance:

    def test_login_creates_two_outstanding_rows(self):
        account = _make_account()
        _login_via_api(account.email)
        # One ACCESS row + one REFRESH row, both for this account.
        rows = PatientOutstandingToken.objects.filter(patient_account=account)
        assert rows.count() == 2
        types = sorted(rows.values_list('token_type', flat=True))
        assert types == [PatientTokenType.ACCESS, PatientTokenType.REFRESH]

    def test_login_records_ip_and_user_agent(self):
        account = _make_account()
        client = APIClient()
        client.post(
            LOGIN_URL, data={'email': account.email, 'password': 'Strong-Pass-1234!'},
            format='json',
            HTTP_HOST='testlab.localhost',
            HTTP_USER_AGENT='Mozilla/5.0 (Test Suite)',
            REMOTE_ADDR='198.51.100.7',
        )
        row = PatientOutstandingToken.objects.filter(
            patient_account=account, token_type=PatientTokenType.ACCESS,
        ).first()
        assert row is not None
        # AuditContextMiddleware populates audit_ip / audit_user_agent
        # from the request — assert they reach the row.
        assert row.user_agent == 'Mozilla/5.0 (Test Suite)'
        assert row.ip_address == '198.51.100.7'


# ---------------------------------------------------------------------------
# Auth check enforces blacklist + outstanding presence
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestAuthChecks:

    def test_valid_token_is_accepted(self):
        account = _make_account()
        tokens = _login_via_api(account.email)
        resp = _auth_client(tokens['access_token']).get(
            ME_URL, HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 200, resp.content

    def test_blacklisted_access_token_is_rejected(self):
        account = _make_account()
        tokens = _login_via_api(account.email)
        # Blacklist the access token directly.
        access_row = PatientOutstandingToken.objects.get(
            patient_account=account, token_type=PatientTokenType.ACCESS,
        )
        PatientBlacklistedToken.objects.create(token=access_row)
        resp = _auth_client(tokens['access_token']).get(
            ME_URL, HTTP_HOST='testlab.localhost',
        )
        # 401 is the right code for "valid signature but no longer
        # accepted" — DRF maps simplejwt's InvalidToken to 401.
        assert resp.status_code == 401, resp.content

    def test_token_missing_outstanding_row_is_rejected(self):
        """A token issued with a valid signature but whose Outstanding
        row has been deleted (or never existed) must fail. Simulates
        the post-cleanup case: the row is gone, the token shouldn't
        suddenly become trustworthy again."""
        account = _make_account()
        tokens = _login_via_api(account.email)
        PatientOutstandingToken.objects.filter(
            patient_account=account, token_type=PatientTokenType.ACCESS,
        ).delete()
        resp = _auth_client(tokens['access_token']).get(
            ME_URL, HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 401, resp.content


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestLogout:

    def test_logout_blacklists_current_access_token(self):
        account = _make_account()
        tokens = _login_via_api(account.email)
        client = _auth_client(tokens['access_token'])

        resp = client.post(
            LOGOUT_URL, data={}, format='json',
            HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 204, resp.content

        # Same token now refused on /me.
        resp_me = client.get(ME_URL, HTTP_HOST='testlab.localhost')
        assert resp_me.status_code == 401, resp_me.content

        # Refresh token NOT blacklisted (we only blacklisted the access
        # token; the refresh row stays available for rotation).
        refresh_row = PatientOutstandingToken.objects.get(
            patient_account=account, token_type=PatientTokenType.REFRESH,
        )
        assert not hasattr(refresh_row, 'blacklist') or \
            PatientBlacklistedToken.objects.filter(token=refresh_row).count() == 0

    def test_logout_with_refresh_blacklists_both(self):
        account = _make_account()
        tokens = _login_via_api(account.email)
        client = _auth_client(tokens['access_token'])

        resp = client.post(
            LOGOUT_URL,
            data={'refresh_token': tokens['refresh_token']},
            format='json', HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 204, resp.content

        # Both jti rows are now blacklisted — count(blacklist) == 2.
        n = PatientBlacklistedToken.objects.filter(
            token__patient_account=account,
        ).count()
        assert n == 2

    def test_logout_all_sessions_blacklists_every_token(self):
        account = _make_account()
        # Two simulated logins (different browsers).
        tokens_a = _login_via_api(account.email)
        _login_via_api(account.email)
        # 4 outstanding rows now (2 sessions × access+refresh).
        assert PatientOutstandingToken.objects.filter(
            patient_account=account,
        ).count() == 4

        client = _auth_client(tokens_a['access_token'])
        resp = client.post(
            LOGOUT_URL, data={'all_sessions': True}, format='json',
            HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 204, resp.content

        # Every outstanding row has a blacklist entry.
        n_outstanding = PatientOutstandingToken.objects.filter(
            patient_account=account,
        ).count()
        n_blacklisted = PatientBlacklistedToken.objects.filter(
            token__patient_account=account,
        ).count()
        assert n_outstanding == 4
        assert n_blacklisted == 4


# ---------------------------------------------------------------------------
# Refresh + rotation
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestRefreshRotation:

    def test_refresh_rotates_tokens_and_invalidates_old_refresh(self):
        account = _make_account()
        tokens = _login_via_api(account.email)
        old_refresh = tokens['refresh_token']

        # Rotate.
        client = APIClient()
        resp = client.post(
            REFRESH_URL,
            data={'refresh_token': old_refresh},
            format='json', HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 200, resp.content
        new_tokens = resp.json()['data']
        assert new_tokens['access_token'] != tokens['access_token']
        assert new_tokens['refresh_token'] != old_refresh

        # The new access token works on /me.
        resp_me = _auth_client(new_tokens['access_token']).get(
            ME_URL, HTTP_HOST='testlab.localhost',
        )
        assert resp_me.status_code == 200, resp_me.content

        # The old refresh token cannot be reused — second call returns 401.
        resp_replay = client.post(
            REFRESH_URL, data={'refresh_token': old_refresh},
            format='json', HTTP_HOST='testlab.localhost',
        )
        assert resp_replay.status_code == 401, resp_replay.content

    def test_refresh_with_garbage_returns_401(self):
        client = APIClient()
        resp = client.post(
            REFRESH_URL, data={'refresh_token': 'not-a-real-token'},
            format='json', HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 401, resp.content
        codes = {e['code'] for e in resp.json()['errors']}
        assert 'INVALID_REFRESH_TOKEN' in codes

    def test_refresh_with_blacklisted_token_returns_401(self):
        account = _make_account()
        tokens = _login_via_api(account.email)
        # Blacklist the refresh row directly.
        refresh_row = PatientOutstandingToken.objects.get(
            patient_account=account, token_type=PatientTokenType.REFRESH,
        )
        PatientBlacklistedToken.objects.create(token=refresh_row)

        client = APIClient()
        resp = client.post(
            REFRESH_URL, data={'refresh_token': tokens['refresh_token']},
            format='json', HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 401, resp.content


# ---------------------------------------------------------------------------
# Password-change service hook (blacklist_all_tokens_for_account)
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestPasswordChangeRevocation:

    def test_blacklist_all_invalidates_every_session(self):
        account = _make_account()
        tokens_a = _login_via_api(account.email)
        tokens_b = _login_via_api(account.email)

        n = blacklist_all_tokens_for_account(account)
        # Two sessions × (access + refresh) = 4 newly blacklisted.
        assert n == 4

        # Both access tokens now refused.
        for t in (tokens_a['access_token'], tokens_b['access_token']):
            resp = _auth_client(t).get(ME_URL, HTTP_HOST='testlab.localhost')
            assert resp.status_code == 401, resp.content

    def test_blacklist_all_is_idempotent(self):
        account = _make_account()
        _login_via_api(account.email)
        first = blacklist_all_tokens_for_account(account)
        second = blacklist_all_tokens_for_account(account)
        # Second call shouldn't double-blacklist anything.
        assert first == 2
        assert second == 0


# ---------------------------------------------------------------------------
# Cleanup management command
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestCleanupCommand:

    def test_cleanup_deletes_expired_rows(self):
        account = _make_account()
        # Issue one fresh pair (won't be expired) + plant an expired row.
        issue_patient_tokens(account)
        expired = PatientOutstandingToken.objects.create(
            patient_account=account,
            jti='manually-aged-jti',
            token_type=PatientTokenType.ACCESS,
            expires_at=timezone.now() - timedelta(days=1),
        )
        # Blacklist it too — the cascade should drop the blacklist row.
        PatientBlacklistedToken.objects.create(token=expired)

        before = PatientOutstandingToken.objects.filter(
            patient_account=account,
        ).count()
        assert before == 3  # 2 fresh + 1 expired

        call_command('cleanup_patient_tokens')

        # Only the fresh pair survives.
        after = PatientOutstandingToken.objects.filter(
            patient_account=account,
        ).count()
        assert after == 2
        # Cascade dropped the blacklist row tied to the expired token.
        assert not PatientBlacklistedToken.objects.filter(token=expired).exists()
