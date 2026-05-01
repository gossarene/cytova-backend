"""
HTTP-level tests for the patient-side shared-results endpoints:

- GET    /api/v1/patient-portal/results/
- GET    /api/v1/patient-portal/results/files/<file_token>/download/
- DELETE /api/v1/patient-portal/results/<id>/

These run against the public schema (the patient_portal tests
``conftest.py`` overrides the project's autouse to use the public
search_path), and seed shared-result rows directly via ORM rather
than going through the lab-side notify-cytova flow — keeping the
tests focused on the read/hide/download contract.
"""
from __future__ import annotations

import io
from datetime import date

import pytest
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone
from rest_framework.test import APIClient

from apps.patient_portal.models import (
    PatientAccount, PatientSharedResult, PatientSharedResultFile,
    SharedResultStatus,
)
from apps.patient_portal.services import (
    issue_patient_tokens, register_patient_account,
)


LIST_URL = '/api/v1/patient-portal/results/'
HIDE_URL = '/api/v1/patient-portal/results/{pk}/'
DOWNLOAD_URL = '/api/v1/patient-portal/results/files/{token}/download/'


@pytest.fixture(autouse=True)
def _reset_throttle_cache():
    cache.clear()
    yield
    cache.clear()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SEQ = 0


def _make_account(*, email_prefix='reader') -> PatientAccount:
    """Sign up + flip ``email_verified_at`` so login works without
    going through the verification flow."""
    global _SEQ
    _SEQ += 1
    account = register_patient_account(
        email=f'{email_prefix}-{_SEQ}@portal.test',
        password='Strong-Pass-1234!',
        first_name='Ada', last_name=f'Lovelace{_SEQ}',
        date_of_birth=date(1990, 5, 17), accept_terms=True,
    )
    account.email_verified_at = timezone.now()
    account.save(update_fields=['email_verified_at'])
    return account


def _auth_client(account: PatientAccount) -> APIClient:
    tokens = issue_patient_tokens(account)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {tokens["access_token"]}')
    return client


def _seed_shared(
    account: PatientAccount, *,
    source_name: str = 'Acme Lab',
    request_reference: str = 'REQ-2026-AAA1',
    status_value: str = SharedResultStatus.ACTIVE,
    storage_key: str = '',
    pdf_content: bytes | None = None,
    file_token_override: str | None = None,
) -> PatientSharedResult:
    """Create a shared result + one file row. If ``pdf_content`` is
    provided, write it to the configured storage so the download
    endpoint can stream it back. Returns the result row; the caller
    grabs ``result.files.first()`` for the file token."""
    shared = PatientSharedResult.objects.create(
        patient_account=account,
        source_type='DIRECT',
        source_name=source_name,
        request_reference=request_reference,
        request_date=date(2026, 4, 30),
        result_available_date=date(2026, 5, 1),
        status=status_value,
    )
    actual_storage_key = storage_key
    if pdf_content is not None and not actual_storage_key:
        actual_storage_key = f'patient-tests/{shared.id}.pdf'
        default_storage.save(actual_storage_key, ContentFile(pdf_content))

    PatientSharedResultFile.objects.create(
        shared_result=shared,
        file_token=file_token_override or f'tok_{shared.id.hex[:32]}',
        filename=f'report_{request_reference}.pdf',
        storage_key=actual_storage_key,
    )
    return shared


# ---------------------------------------------------------------------------
# List endpoint
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestSharedResultsList:

    def test_returns_only_own_active_results(self):
        me = _make_account(email_prefix='me')
        other = _make_account(email_prefix='other')
        mine_a = _seed_shared(me, request_reference='REQ-MINE-A')
        mine_b = _seed_shared(me, request_reference='REQ-MINE-B')
        _seed_shared(other, request_reference='REQ-OTHER')

        resp = _auth_client(me).get(LIST_URL, HTTP_HOST='testlab.localhost')
        assert resp.status_code == 200, resp.content
        body = resp.json()['data']['results']
        # Only my two rows are returned. Cross-patient isolation is the
        # primary guarantee of this endpoint.
        ids = {r['id'] for r in body}
        assert ids == {str(mine_a.id), str(mine_b.id)}

    def test_excludes_hidden_and_revoked_rows(self):
        me = _make_account()
        active = _seed_shared(me, request_reference='ACT')
        _seed_shared(me, request_reference='HIDDEN',
                     status_value=SharedResultStatus.HIDDEN_BY_PATIENT)
        _seed_shared(me, request_reference='REVOKED',
                     status_value=SharedResultStatus.REVOKED)

        resp = _auth_client(me).get(LIST_URL, HTTP_HOST='testlab.localhost')
        body = resp.json()['data']['results']
        ids = {r['id'] for r in body}
        assert ids == {str(active.id)}

    def test_orders_by_result_available_date_then_created_at(self):
        me = _make_account()
        # Three rows with controlled dates so the ordering claim is
        # observable. The newest result_available_date wins.
        old = _seed_shared(me, request_reference='OLD')
        old.result_available_date = date(2026, 1, 1)
        old.save(update_fields=['result_available_date'])
        mid = _seed_shared(me, request_reference='MID')
        mid.result_available_date = date(2026, 3, 1)
        mid.save(update_fields=['result_available_date'])
        new = _seed_shared(me, request_reference='NEW')
        new.result_available_date = date(2026, 5, 1)
        new.save(update_fields=['result_available_date'])

        resp = _auth_client(me).get(LIST_URL, HTTP_HOST='testlab.localhost')
        body = resp.json()['data']['results']
        assert [r['request_reference'] for r in body] == ['NEW', 'MID', 'OLD']

    def test_response_does_not_expose_storage_key(self):
        me = _make_account()
        _seed_shared(
            me, request_reference='LEAK-CHECK',
            storage_key='secret/internal/path/should-not-leak.pdf',
        )
        resp = _auth_client(me).get(LIST_URL, HTTP_HOST='testlab.localhost')
        body = resp.json()['data']['results']
        # Walk the entire JSON tree — the storage path string must
        # not appear anywhere, neither at top-level nor under ``files``.
        flat = repr(body)
        assert 'secret/internal/path' not in flat
        assert 'storage_key' not in flat
        # ``download_url`` IS exposed and uses the opaque token, not
        # the storage key.
        assert body[0]['files'][0]['download_url'].startswith(
            '/api/v1/patient-portal/results/files/',
        )

    def test_unauthenticated_request_rejected(self):
        resp = APIClient().get(LIST_URL, HTTP_HOST='testlab.localhost')
        assert resp.status_code in (401, 403), resp.content


# ---------------------------------------------------------------------------
# Hide (DELETE) endpoint
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestSharedResultsHide:

    def test_delete_marks_row_hidden_without_destroying_it(self):
        me = _make_account()
        shared = _seed_shared(me, request_reference='HIDE-ME')
        resp = _auth_client(me).delete(
            HIDE_URL.format(pk=shared.id), HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 204, resp.content
        shared.refresh_from_db()
        assert shared.status == SharedResultStatus.HIDDEN_BY_PATIENT
        # Row + file row preserved — the lab tenant audit trail is
        # untouched, and the original PDF is still on storage.
        assert PatientSharedResult.objects.filter(pk=shared.id).exists()
        assert shared.files.count() == 1

    def test_delete_hides_from_list(self):
        me = _make_account()
        shared = _seed_shared(me, request_reference='LIST-AFTER-HIDE')
        client = _auth_client(me)
        client.delete(HIDE_URL.format(pk=shared.id), HTTP_HOST='testlab.localhost')
        resp = client.get(LIST_URL, HTTP_HOST='testlab.localhost')
        assert resp.json()['data']['results'] == []

    def test_delete_other_patients_row_returns_404(self):
        me = _make_account()
        other = _make_account()
        their_row = _seed_shared(other, request_reference='NOT-MINE')
        resp = _auth_client(me).delete(
            HIDE_URL.format(pk=their_row.id), HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 404, resp.content
        # Their row is untouched.
        their_row.refresh_from_db()
        assert their_row.status == SharedResultStatus.ACTIVE

    def test_delete_unknown_id_returns_404(self):
        me = _make_account()
        resp = _auth_client(me).delete(
            HIDE_URL.format(pk='00000000-0000-0000-0000-000000000000'),
            HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 404, resp.content


# ---------------------------------------------------------------------------
# Download endpoint
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestSharedResultDownload:

    PDF_BYTES = b'%PDF-1.4\n%fake-pdf-bytes-for-test\n%%EOF'

    def test_owner_can_download_pdf(self):
        me = _make_account()
        shared = _seed_shared(me, pdf_content=self.PDF_BYTES)
        token = shared.files.first().file_token
        resp = _auth_client(me).get(
            DOWNLOAD_URL.format(token=token), HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 200, resp.content
        assert resp['Content-Type'] == 'application/pdf'
        assert resp['Content-Disposition'].startswith('attachment')
        # FileResponse streams in chunks; concatenate to assert.
        body = b''.join(resp.streaming_content) if resp.streaming else resp.content
        assert body == self.PDF_BYTES

    def test_other_patients_token_returns_404(self):
        me = _make_account()
        other = _make_account()
        their = _seed_shared(other, pdf_content=self.PDF_BYTES)
        token = their.files.first().file_token
        resp = _auth_client(me).get(
            DOWNLOAD_URL.format(token=token), HTTP_HOST='testlab.localhost',
        )
        # 404, NOT 403 — never let the caller distinguish "not yours"
        # from "doesn't exist".
        assert resp.status_code == 404, resp.content

    def test_unknown_token_returns_404(self):
        me = _make_account()
        resp = _auth_client(me).get(
            DOWNLOAD_URL.format(token='nonexistent-token-value'),
            HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 404, resp.content

    def test_hidden_share_cannot_be_downloaded(self):
        me = _make_account()
        shared = _seed_shared(me, pdf_content=self.PDF_BYTES)
        shared.status = SharedResultStatus.HIDDEN_BY_PATIENT
        shared.save(update_fields=['status'])
        token = shared.files.first().file_token
        resp = _auth_client(me).get(
            DOWNLOAD_URL.format(token=token), HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 404, resp.content

    def test_revoked_share_cannot_be_downloaded(self):
        me = _make_account()
        shared = _seed_shared(me, pdf_content=self.PDF_BYTES)
        shared.status = SharedResultStatus.REVOKED
        shared.save(update_fields=['status'])
        token = shared.files.first().file_token
        resp = _auth_client(me).get(
            DOWNLOAD_URL.format(token=token), HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 404, resp.content

    def test_missing_storage_object_returns_404_not_500(self):
        me = _make_account()
        # Storage key points at a path that doesn't exist on storage.
        # The endpoint must catch the FileNotFoundError and return 404
        # rather than crashing the worker.
        shared = _seed_shared(me)
        sfile = shared.files.first()
        sfile.storage_key = 'patient-tests/not-on-disk.pdf'
        sfile.save(update_fields=['storage_key'])
        resp = _auth_client(me).get(
            DOWNLOAD_URL.format(token=sfile.file_token),
            HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 404, resp.content

    def test_unauthenticated_request_rejected(self):
        me = _make_account()
        shared = _seed_shared(me, pdf_content=self.PDF_BYTES)
        token = shared.files.first().file_token
        resp = APIClient().get(
            DOWNLOAD_URL.format(token=token), HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code in (401, 403), resp.content
