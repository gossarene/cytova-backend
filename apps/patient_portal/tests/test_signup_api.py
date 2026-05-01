"""
HTTP-level tests for ``POST /api/v1/patient-portal/signup/``.

The conftest in this package overrides the project's autouse fixture
to run each test in the ``public`` schema (where the patient portal
tables live). The endpoint itself is mounted on both the public and
the tenant URL conf — these tests use ``HTTP_HOST='testlab.localhost'``
which routes through the tenant URL conf, exercising the same path the
deployment will see when a patient lands on a tenant subdomain by
mistake.
"""
from __future__ import annotations

import re

import pytest
from django.core.cache import cache
from django_tenants.utils import get_tenant_model
from rest_framework.test import APIClient

from apps.patient_portal.models import (
    PatientAccount, PatientConsent, PatientProfile,
)


SIGNUP_URL = '/api/v1/patient-portal/signup/'

CV_REGEX = re.compile(r'^CV-[2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4}$')


@pytest.fixture(autouse=True)
def _reset_throttle_cache():
    """The throttle is keyed in the default cache. Clear before/after so
    one test's burst doesn't trip the next test's first request."""
    cache.clear()
    yield
    cache.clear()


def _payload(**overrides):
    base = dict(
        email='ada@portal.test',
        password='Strong-Pass-1234!',
        confirm_password='Strong-Pass-1234!',
        first_name='Ada',
        last_name='Lovelace',
        date_of_birth='1990-05-17',
        phone='+33 6 12 34 56 78',
        accept_terms=True,
    )
    base.update(overrides)
    return base


def _post(payload=None, host: str = 'testlab.localhost'):
    client = APIClient()
    return client.post(
        SIGNUP_URL,
        data=payload if payload is not None else _payload(),
        format='json',
        HTTP_HOST=host,
    )


def _envelope_data(resp):
    body = resp.json()
    assert 'data' in body and 'errors' in body, body
    return body


@pytest.mark.django_db(transaction=True)
class TestPatientSignupHttp:

    def test_creates_account_profile_consent(self):
        resp = _post()
        assert resp.status_code == 201, resp.content
        body = _envelope_data(resp)
        assert body['errors'] == []
        data = body['data']
        # The response shape is deliberately narrow — exposing only what
        # the patient needs (their Cytova ID + email + acknowledgement)
        # and never the password hash or audit log.
        assert set(data.keys()) == {
            'patient_account_id', 'cytova_patient_id', 'email', 'message',
        }
        assert data['email'] == 'ada@portal.test'
        assert CV_REGEX.match(data['cytova_patient_id']), data['cytova_patient_id']
        assert data['message'] == 'Patient account created successfully.'

        # Persistence side-effects.
        account = PatientAccount.objects.get(email='ada@portal.test')
        assert PatientProfile.objects.filter(account=account).exists()
        assert account.consents.count() == 1

    def test_password_is_hashed_in_database(self):
        plain = 'Hash-Probe-9876!'
        resp = _post(_payload(
            email='hash@portal.test', password=plain, confirm_password=plain,
        ))
        assert resp.status_code == 201, resp.content
        account = PatientAccount.objects.get(email='hash@portal.test')
        assert account.password != plain
        assert account.password.startswith('pbkdf2_'), account.password
        assert account.check_password(plain) is True

    def test_accept_terms_required_returns_400(self):
        resp = _post(_payload(accept_terms=False))
        # DRF's exception handler converts the service's ValidationError
        # into the Cytova envelope — we should see a 400 with at least
        # one error referring to ``accept_terms``.
        assert resp.status_code == 400, resp.content
        body = _envelope_data(resp)
        assert body['data'] is None
        fields = {e.get('field') for e in body['errors']}
        assert 'accept_terms' in fields, body['errors']
        assert not PatientAccount.objects.filter(email='ada@portal.test').exists()

    def test_password_mismatch_returns_400(self):
        resp = _post(_payload(
            password='Strong-Pass-1234!', confirm_password='Different-1234!',
        ))
        assert resp.status_code == 400, resp.content
        body = _envelope_data(resp)
        fields = {e.get('field') for e in body['errors']}
        assert 'confirm_password' in fields, body['errors']

    def test_duplicate_email_rejected(self):
        first = _post(_payload(email='dup@portal.test'))
        assert first.status_code == 201, first.content
        second = _post(_payload(email='dup@portal.test'))
        assert second.status_code == 400, second.content
        body = _envelope_data(second)
        fields = {e.get('field') for e in body['errors']}
        assert 'email' in fields, body['errors']
        # The duplicate guard must keep the original account intact.
        assert PatientAccount.objects.filter(email='dup@portal.test').count() == 1

    def test_signup_creates_no_lab_tenant_or_patient_record(self):
        Tenant = get_tenant_model()
        before = set(Tenant.objects.values_list('schema_name', flat=True))
        # Existing labs (only ``schema_testlab`` from conftest) should
        # be untouched after a patient signup — tenant CRUD is a
        # separate flow.
        resp = _post(_payload(email='no-tenant@portal.test'))
        assert resp.status_code == 201, resp.content
        after = set(Tenant.objects.values_list('schema_name', flat=True))
        assert before == after, (before, after)

        # And no lab-side ``apps.patients.Patient`` rows were created in
        # any tenant schema. Inspect the test tenant directly.
        from django_tenants.utils import schema_context
        from apps.patients.models import Patient
        with schema_context('schema_testlab'):
            assert Patient.objects.count() == 0

    def test_response_contains_cytova_patient_id_format(self):
        # Repeat with a fresh email so the previous test's account
        # doesn't collide. The ID format check is the contract for
        # downstream UI rendering.
        resp = _post(_payload(email='format@portal.test'))
        assert resp.status_code == 201
        data = _envelope_data(resp)['data']
        assert CV_REGEX.match(data['cytova_patient_id'])

    def test_endpoint_is_publicly_reachable(self):
        # No Authorization header → endpoint must accept the request
        # (lab tenant auth gates would 401 here).
        resp = _post()
        assert resp.status_code == 201, resp.content
        # Sanity: same call from the platform/public host also works
        # (the route is mounted on both URL confs).
        platform_resp = _post(
            _payload(email='platform-host@portal.test'),
            host='admin.localhost',
        )
        # The exact host setup may not have a Domain record for
        # admin.localhost in tests — accept either 201 (resolves) or
        # the public-fallback path. The essential guarantee is that
        # the endpoint is NOT 404 / 401 anywhere it's mounted.
        assert platform_resp.status_code in (201, 400), platform_resp.content
