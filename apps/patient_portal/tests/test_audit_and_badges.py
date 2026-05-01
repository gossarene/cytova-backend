"""
Tests for the patient-side audit log + UX-friendly fields exposed by
``GET /api/v1/patient-portal/results/``.

These run against the public schema (the patient_portal tests
``conftest.py`` overrides the project's autouse to use the public
search_path) and seed shared-result rows directly via ORM rather than
going through the lab-side notify-cytova flow.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone
from rest_framework.test import APIClient

from apps.patient_portal.models import (
    PatientAccount, PatientPortalAuditAction, PatientPortalAuditLog,
    PatientSharedResult, PatientSharedResultFile, SharedResultStatus,
)
from apps.patient_portal.services import (
    issue_patient_tokens, register_patient_account,
)


LIST_URL = '/api/v1/patient-portal/results/'
HIDE_URL = '/api/v1/patient-portal/results/{pk}/'
DOWNLOAD_URL = '/api/v1/patient-portal/results/files/{token}/download/'

PDF_BYTES = b'%PDF-1.4\n%fake-pdf\n%%EOF'


@pytest.fixture(autouse=True)
def _reset_throttle_cache():
    cache.clear()
    yield
    cache.clear()


# ---------------------------------------------------------------------------
# Helpers (parallel to test_shared_results_api.py)
# ---------------------------------------------------------------------------

_SEQ = 0


def _make_account(*, email_prefix='audit') -> PatientAccount:
    global _SEQ
    _SEQ += 1
    account = register_patient_account(
        email=f'{email_prefix}-{_SEQ}@portal.test',
        password='Strong-Pass-1234!',
        first_name='Ada', last_name=f'Test{_SEQ}',
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
    request_reference: str = 'REQ-2026-AAA1',
    status_value: str = SharedResultStatus.ACTIVE,
    pdf_content: bytes | None = None,
    created_offset_days: int = 0,
) -> PatientSharedResult:
    shared = PatientSharedResult.objects.create(
        patient_account=account,
        source_type='DIRECT',
        source_name='Acme Lab',
        request_reference=request_reference,
        request_date=date(2026, 4, 30),
        result_available_date=date(2026, 5, 1),
        status=status_value,
    )
    if created_offset_days:
        # Backdate to test the "is_new" cutoff. ``created_at`` has a
        # default + db_index=True; updating directly avoids the auto-now
        # behaviour inherited from default=timezone.now.
        new_ts = timezone.now() - timedelta(days=created_offset_days)
        PatientSharedResult.objects.filter(pk=shared.id).update(created_at=new_ts)
        shared.refresh_from_db()
    storage_key = ''
    if pdf_content is not None:
        storage_key = f'patient-tests/{shared.id}.pdf'
        default_storage.save(storage_key, ContentFile(pdf_content))
    PatientSharedResultFile.objects.create(
        shared_result=shared,
        file_token=f'tok_{shared.id.hex[:32]}',
        filename=f'report_{request_reference}.pdf',
        storage_key=storage_key,
    )
    return shared


def _audit_rows_for(account: PatientAccount, *, action: str | None = None):
    qs = PatientPortalAuditLog.objects.filter(patient_account=account)
    if action:
        qs = qs.filter(action=action)
    return list(qs.order_by('created_at'))


# ---------------------------------------------------------------------------
# Friendly status fields on the list endpoint
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestListUxFields:

    def test_status_label_and_is_new_for_fresh_share(self):
        me = _make_account()
        _seed_shared(me, request_reference='REQ-FRESH')
        body = _auth_client(me).get(LIST_URL, HTTP_HOST='testlab.localhost').json()
        row = body['data']['results'][0]
        # Friendly text replaces the raw enum value for patient consumption.
        assert row['status_label'] == 'Available'
        # No download → is_new=True for a recent row.
        assert row['is_new'] is True
        assert row['download_count'] == 0
        assert row['last_downloaded_at'] is None

    def test_is_new_false_when_too_old(self):
        me = _make_account()
        _seed_shared(me, request_reference='REQ-OLD', created_offset_days=60)
        body = _auth_client(me).get(LIST_URL, HTTP_HOST='testlab.localhost').json()
        row = body['data']['results'][0]
        assert row['is_new'] is False

    def test_is_new_false_after_download(self):
        me = _make_account()
        shared = _seed_shared(
            me, request_reference='REQ-DL', pdf_content=PDF_BYTES,
        )
        token = shared.files.first().file_token
        # Hit the download endpoint to bump the counters.
        client = _auth_client(me)
        client.get(DOWNLOAD_URL.format(token=token), HTTP_HOST='testlab.localhost')
        # Re-list and confirm is_new flipped + counters reflect the hit.
        body = client.get(LIST_URL, HTTP_HOST='testlab.localhost').json()
        row = body['data']['results'][0]
        assert row['is_new'] is False
        assert row['download_count'] >= 1
        assert row['last_downloaded_at'] is not None


# ---------------------------------------------------------------------------
# Download writes audit + bumps counters
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestDownloadAuditAndCounters:

    def test_download_writes_audit_event(self):
        me = _make_account()
        shared = _seed_shared(me, pdf_content=PDF_BYTES)
        token = shared.files.first().file_token

        resp = _auth_client(me).get(
            DOWNLOAD_URL.format(token=token), HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 200, resp.content

        rows = _audit_rows_for(
            me, action=PatientPortalAuditAction.PATIENT_RESULT_DOWNLOADED.value,
        )
        assert len(rows) == 1
        row = rows[0]
        assert row.entity_type == 'PatientSharedResultFile'
        # Allow-listed metadata only; storage_key / file_token / PII never
        # appear in the audit JSON.
        assert set(row.metadata.keys()) == {
            'shared_result_id', 'file_id', 'download_count_after',
        }
        assert 'storage_key' not in str(row.metadata)
        assert token not in str(row.metadata)

    def test_download_bumps_counters(self):
        me = _make_account()
        shared = _seed_shared(me, pdf_content=PDF_BYTES)
        token = shared.files.first().file_token
        client = _auth_client(me)
        # Three downloads — each one bumps the count.
        for _ in range(3):
            assert client.get(
                DOWNLOAD_URL.format(token=token), HTTP_HOST='testlab.localhost',
            ).status_code == 200
        shared.refresh_from_db()
        assert shared.download_count == 3
        assert shared.last_downloaded_at is not None
        assert shared.first_viewed_at is not None
        assert shared.first_viewed_at <= shared.last_downloaded_at

    def test_failed_download_does_not_write_audit(self):
        me = _make_account()
        # Token that doesn't exist → 404, no audit row written.
        assert _auth_client(me).get(
            DOWNLOAD_URL.format(token='no-such-token'),
            HTTP_HOST='testlab.localhost',
        ).status_code == 404
        assert _audit_rows_for(
            me, action=PatientPortalAuditAction.PATIENT_RESULT_DOWNLOADED.value,
        ) == []


# ---------------------------------------------------------------------------
# Hide writes audit (and is idempotent)
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestHideAudit:

    def test_hide_writes_audit_row_first_time_only(self):
        me = _make_account()
        shared = _seed_shared(me)
        client = _auth_client(me)

        assert client.delete(
            HIDE_URL.format(pk=shared.id), HTTP_HOST='testlab.localhost',
        ).status_code == 204
        # Idempotent re-hide — must NOT produce a second audit row.
        assert client.delete(
            HIDE_URL.format(pk=shared.id), HTTP_HOST='testlab.localhost',
        ).status_code == 204

        rows = _audit_rows_for(
            me,
            action=PatientPortalAuditAction.PATIENT_RESULT_HIDDEN_BY_PATIENT.value,
        )
        assert len(rows) == 1
        assert rows[0].metadata == {'shared_result_id': str(shared.id)}


# ---------------------------------------------------------------------------
# Audit metadata allow-list — defence in depth against future leaks
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestAuditAllowList:

    def test_unknown_metadata_keys_are_dropped(self):
        from apps.patient_portal.audit import write_event

        me = _make_account()
        shared = _seed_shared(me)
        write_event(
            action=PatientPortalAuditAction.PATIENT_RESULT_DOWNLOADED.value,
            entity_type='PatientSharedResultFile',
            entity_id=shared.files.first().id,
            patient_account=me,
            metadata={
                # Allowed keys — should round-trip.
                'shared_result_id': shared.id,
                'file_id': shared.files.first().id,
                'download_count_after': 1,
                # Forbidden keys — should be silently dropped.
                'storage_key': '/private/storage/path.pdf',
                'file_token': 'TOTALLY_SECRET_TOKEN',
                'first_name': 'Ada',
                'date_of_birth': '1990-01-01',
                'medical_value': '7.4 mmol/L',
            },
        )
        row = _audit_rows_for(
            me, action=PatientPortalAuditAction.PATIENT_RESULT_DOWNLOADED.value,
        )[-1]
        assert set(row.metadata.keys()) == {
            'shared_result_id', 'file_id', 'download_count_after',
        }
        flat = repr(row.metadata)
        for forbidden in ('storage_key', 'TOTALLY_SECRET_TOKEN',
                          'first_name', 'date_of_birth', 'medical_value',
                          '/private/storage'):
            assert forbidden not in flat
