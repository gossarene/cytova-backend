"""
Phase 2 — Patient-portal shared-result version history.

Tests the ``GET /api/v1/patient-portal/results/{id}/versions/`` endpoint
and the supersession-aware behavior of the existing list endpoint.

These tests seed the public-schema rows directly via ORM rather than
walking the lab-side Notify-Cytova flow — keeping the focus on the
read-side contract. The Notify-Cytova writer path is exercised by
``apps/requests/tests/test_notify_cytova_versioning.py`` (Phase 1).

Invariants under test
---------------------
- only the authenticated patient's own versions are visible,
- lab-only (regenerated-but-not-shared) versions are structurally
  invisible — they never get a row in the patient portal,
- supersession order is preserved: newer ``report_version_number``
  first, the row with ``is_current_for_patient=True`` is the only
  ``CURRENT`` row,
- HIDDEN_BY_PATIENT and REVOKED rows are excluded from both the list
  and the version-history payload,
- the list endpoint shows only the current version per source request,
  while the versions endpoint exposes the full patient-visible
  history,
- internal fields (``storage_key``, ``patient_storage_key``,
  ``file_token``, ``source_tenant_schema``, ``source_request_id``)
  never appear in any response,
- per-version download tokens still resolve after a newer version is
  shared (older versions remain downloadable as long as they are
  ACTIVE).
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone
from rest_framework.test import APIClient

from apps.patient_portal.models import (
    PatientAccount, PatientSharedChannel, PatientSharedResult,
    PatientSharedResultFile, SharedResultStatus,
)
from apps.patient_portal.services import (
    issue_patient_tokens, register_patient_account,
)


LIST_URL = '/api/v1/patient-portal/results/'
VERSIONS_URL = '/api/v1/patient-portal/results/{pk}/versions/'
DOWNLOAD_URL = '/api/v1/patient-portal/results/files/{token}/download/'


@pytest.fixture(autouse=True)
def _reset_throttle_cache():
    cache.clear()
    yield
    cache.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEQ = 0


def _make_account(*, prefix='ver') -> PatientAccount:
    """Sign up + flip ``email_verified_at`` so token issuance works."""
    global _SEQ
    _SEQ += 1
    account = register_patient_account(
        email=f'{prefix}-{_SEQ}@portal.test',
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


def _seed_version(
    account: PatientAccount,
    *,
    source_request_id,
    source_tenant_schema: str = 'schema_testlab',
    request_reference: str = 'REQ-V',
    report_version_number: int = 1,
    is_current_for_patient: bool = True,
    status_value: str = SharedResultStatus.ACTIVE,
    shared_channel: str = PatientSharedChannel.CYTOVA,
    pdf_content: bytes | None = None,
    shared_at_offset_days: int = 0,
) -> PatientSharedResult:
    """Seed one ``PatientSharedResult`` + one file row mimicking what
    the Notify-Cytova writer would have produced for a given version.

    Test rows are tied to a caller-supplied ``source_request_id`` so
    multiple versions of the same logical result share the same group
    key. ``shared_at_offset_days`` lets the caller stagger timestamps
    to make ordering claims observable."""
    now = timezone.now()
    shared_at = now + timedelta(days=shared_at_offset_days)
    shared = PatientSharedResult.objects.create(
        patient_account=account,
        source_type='DIRECT',
        source_name='Acme Lab',
        request_reference=request_reference,
        request_date=date(2026, 4, 30),
        result_available_date=date(2026, 5, 1),
        status=status_value,
        source_tenant_schema=source_tenant_schema,
        source_request_id=source_request_id,
        report_version_number=report_version_number,
        report_generated_at=shared_at,
        shared_at=shared_at,
        shared_channel=shared_channel,
        is_current_for_patient=is_current_for_patient,
    )

    storage_key = ''
    if pdf_content is not None:
        storage_key = (
            f'patient-tests/versions/{shared.id}-v{report_version_number}.pdf'
        )
        default_storage.save(storage_key, ContentFile(pdf_content))

    PatientSharedResultFile.objects.create(
        shared_result=shared,
        # Keep tokens stable + collision-free across versions so tests
        # can assert per-version download behavior unambiguously.
        file_token=f'tok_{shared.id.hex[:16]}_v{report_version_number}',
        filename=f'report_v{report_version_number}.pdf',
        storage_key=storage_key,
    )
    return shared


# ---------------------------------------------------------------------------
# 1. Versions endpoint — happy path with v1 + v2 shared
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestVersionsEndpointHappyPath:

    def _seed_v1_v2(self, me):
        request_id = uuid.uuid4()
        v1 = _seed_version(
            me, source_request_id=request_id, request_reference='REQ-V12',
            report_version_number=1, is_current_for_patient=False,
            shared_at_offset_days=-5,
        )
        v2 = _seed_version(
            me, source_request_id=request_id, request_reference='REQ-V12',
            report_version_number=2, is_current_for_patient=True,
            shared_at_offset_days=0,
        )
        return v1, v2

    def test_returns_both_versions_newest_first_with_current_pointer(self):
        me = _make_account()
        v1, v2 = self._seed_v1_v2(me)

        resp = _auth_client(me).get(
            VERSIONS_URL.format(pk=v2.id), HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 200, resp.content
        body = resp.json()['data']

        assert body['result_id'] == str(v2.id)

        # ``current_version`` mirrors the spec example shape: no id /
        # no status, just the four fields the UI needs to render the
        # primary download CTA.
        cur = body['current_version']
        assert cur['version_number'] == 2
        assert cur['shared_channel'] == 'CYTOVA'
        assert cur['shared_at'] is not None
        assert cur['download_url'].startswith(
            '/api/v1/patient-portal/results/files/',
        )
        assert set(cur.keys()) == {
            'version_number', 'shared_at', 'shared_channel', 'download_url',
        }

        # ``versions`` is newest first (v2, v1) — the spec example
        # shows version 2 ahead of version 1.
        assert [v['version_number'] for v in body['versions']] == [2, 1]

        v2_payload = body['versions'][0]
        v1_payload = body['versions'][1]
        assert v2_payload['id'] == str(v2.id)
        assert v2_payload['status'] == 'CURRENT'
        assert v1_payload['id'] == str(v1.id)
        assert v1_payload['status'] == 'SUPERSEDED'
        # Per-row contract — no internal field ever surfaces.
        assert set(v2_payload.keys()) == {
            'id', 'version_number', 'shared_at',
            'shared_channel', 'status', 'download_url',
        }

    def test_resolves_full_history_even_when_pk_is_a_superseded_row(self):
        """A stale link to v1 (e.g. an old browser tab) must still
        resolve to the same version line — the patient never gets
        404'd just because the lab shared a newer version."""
        me = _make_account()
        v1, v2 = self._seed_v1_v2(me)

        resp = _auth_client(me).get(
            VERSIONS_URL.format(pk=v1.id), HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 200, resp.content
        body = resp.json()['data']
        # ``result_id`` echoes the URL pk (so the patient can correlate
        # the response to the request they made), but the version line
        # is the same and ``current_version`` still points at v2.
        assert body['result_id'] == str(v1.id)
        assert body['current_version']['version_number'] == 2
        assert [v['version_number'] for v in body['versions']] == [2, 1]


# ---------------------------------------------------------------------------
# 2. Versions endpoint — privacy: cross-patient, hidden, revoked, unknown
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestVersionsEndpointPrivacy:

    def test_other_patients_pk_returns_404(self):
        me = _make_account()
        other = _make_account(prefix='other')
        their = _seed_version(
            other, source_request_id=uuid.uuid4(), report_version_number=1,
        )
        resp = _auth_client(me).get(
            VERSIONS_URL.format(pk=their.id), HTTP_HOST='testlab.localhost',
        )
        # 404, never 403 — never let the caller distinguish "not yours"
        # from "doesn't exist".
        assert resp.status_code == 404, resp.content

    def test_other_patients_versions_never_leak_into_my_history(self):
        """Even if I happen to know one of MY shared_result IDs, the
        version-line query must be scoped to my account — not just the
        ``source_request_id`` — so a UUID guess from another tenant
        (or a different patient on the same request) cannot bleed in."""
        me = _make_account()
        other = _make_account(prefix='other')
        request_id = uuid.uuid4()
        # Same logical request_id, but the lab shared with two
        # different patients (e.g. the lab fixed a wrong recipient).
        their = _seed_version(
            other, source_request_id=request_id, report_version_number=1,
        )
        mine = _seed_version(
            me, source_request_id=request_id, report_version_number=1,
        )

        resp = _auth_client(me).get(
            VERSIONS_URL.format(pk=mine.id), HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 200
        version_ids = {v['id'] for v in resp.json()['data']['versions']}
        assert version_ids == {str(mine.id)}
        assert str(their.id) not in version_ids

    def test_unknown_uuid_returns_404(self):
        me = _make_account()
        resp = _auth_client(me).get(
            VERSIONS_URL.format(pk=uuid.uuid4()),
            HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 404, resp.content

    def test_hidden_pk_returns_404(self):
        me = _make_account()
        hidden = _seed_version(
            me, source_request_id=uuid.uuid4(),
            status_value=SharedResultStatus.HIDDEN_BY_PATIENT,
        )
        resp = _auth_client(me).get(
            VERSIONS_URL.format(pk=hidden.id),
            HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 404, resp.content

    def test_revoked_pk_returns_404(self):
        me = _make_account()
        revoked = _seed_version(
            me, source_request_id=uuid.uuid4(),
            status_value=SharedResultStatus.REVOKED,
        )
        resp = _auth_client(me).get(
            VERSIONS_URL.format(pk=revoked.id),
            HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 404, resp.content

    def test_unauthenticated_request_rejected(self):
        me = _make_account()
        row = _seed_version(me, source_request_id=uuid.uuid4())
        resp = APIClient().get(
            VERSIONS_URL.format(pk=row.id),
            HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code in (401, 403), resp.content


# ---------------------------------------------------------------------------
# 3. Versions endpoint — filters HIDDEN/REVOKED siblings out of the list
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestVersionsListFilters:

    def test_revoked_sibling_excluded_from_history(self):
        """If the lab revoked v1 but later shared v2, the patient
        must not see v1 in their history — REVOKED is "gone" from
        the patient view at every layer."""
        me = _make_account()
        request_id = uuid.uuid4()
        _seed_version(
            me, source_request_id=request_id, report_version_number=1,
            is_current_for_patient=False,
            status_value=SharedResultStatus.REVOKED,
            shared_at_offset_days=-5,
        )
        v2 = _seed_version(
            me, source_request_id=request_id, report_version_number=2,
            is_current_for_patient=True,
            shared_at_offset_days=0,
        )
        resp = _auth_client(me).get(
            VERSIONS_URL.format(pk=v2.id), HTTP_HOST='testlab.localhost',
        )
        body = resp.json()['data']
        assert [v['version_number'] for v in body['versions']] == [2]

    def test_hidden_sibling_excluded_from_history(self):
        me = _make_account()
        request_id = uuid.uuid4()
        _seed_version(
            me, source_request_id=request_id, report_version_number=1,
            is_current_for_patient=False,
            status_value=SharedResultStatus.HIDDEN_BY_PATIENT,
            shared_at_offset_days=-5,
        )
        v2 = _seed_version(
            me, source_request_id=request_id, report_version_number=2,
            is_current_for_patient=True,
            shared_at_offset_days=0,
        )
        resp = _auth_client(me).get(
            VERSIONS_URL.format(pk=v2.id), HTTP_HOST='testlab.localhost',
        )
        body = resp.json()['data']
        assert [v['version_number'] for v in body['versions']] == [2]


# ---------------------------------------------------------------------------
# 4. Versions endpoint — no internal fields leak
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestVersionsEndpointNoLeaks:

    def test_no_storage_key_or_internal_linkage_in_payload(self):
        me = _make_account()
        request_id = uuid.uuid4()
        _seed_version(
            me, source_request_id=request_id, report_version_number=1,
            is_current_for_patient=False, shared_at_offset_days=-5,
            pdf_content=b'%PDF-1.4 v1\n%%EOF',
        )
        v2 = _seed_version(
            me, source_request_id=request_id, report_version_number=2,
            is_current_for_patient=True,
            pdf_content=b'%PDF-1.4 v2\n%%EOF',
        )
        # Spike known internal-string markers into the row so a
        # serializer that accidentally widened its field set would
        # surface them.
        v2_file = v2.files.first()
        v2_file.patient_storage_key = (
            'patient-results/leak-marker-internal-path/'
            f'{v2.id}/leak.pdf'
        )
        v2_file.save(update_fields=['patient_storage_key'])

        resp = _auth_client(me).get(
            VERSIONS_URL.format(pk=v2.id), HTTP_HOST='testlab.localhost',
        )
        flat = resp.content.decode()
        # Internal storage paths and identifiers must never appear.
        assert 'storage_key' not in flat
        assert 'patient_storage_key' not in flat
        assert 'leak-marker-internal-path' not in flat
        assert 'source_tenant_schema' not in flat
        assert 'source_request_id' not in flat
        assert 'pdf_file_key' not in flat
        # The opaque file_token should NOT be exposed as a top-level
        # field; the only place it surfaces is embedded inside the
        # download_url path.
        assert '"file_token"' not in flat


# ---------------------------------------------------------------------------
# 5. List endpoint — only the current version per source request
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestListShowsOnlyCurrent:

    def test_default_list_excludes_superseded_versions(self):
        """Spec section 4: "Default list should show only the current
        shared version per source request." Sharing v2 supersedes v1
        — the list must surface v2 only; v1 is reachable via the
        version-history endpoint."""
        me = _make_account()
        request_id = uuid.uuid4()
        _seed_version(
            me, source_request_id=request_id, request_reference='REQ-LIST-V12',
            report_version_number=1, is_current_for_patient=False,
            shared_at_offset_days=-5,
        )
        v2 = _seed_version(
            me, source_request_id=request_id, request_reference='REQ-LIST-V12',
            report_version_number=2, is_current_for_patient=True,
        )
        resp = _auth_client(me).get(LIST_URL, HTTP_HOST='testlab.localhost')
        body = resp.json()['data']['results']
        assert [r['id'] for r in body] == [str(v2.id)]


# ---------------------------------------------------------------------------
# 6. Per-version downloads — superseded versions remain downloadable
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestSupersededVersionsDownloadable:

    def test_superseded_version_pdf_still_downloads(self):
        """Spec section 9: each shared version's PDF stays downloadable
        — the lab does NOT serve "the latest" dynamically. After v2 is
        shared, v1's token must still stream v1's bytes."""
        me = _make_account()
        request_id = uuid.uuid4()
        v1_bytes = b'%PDF-1.4 version 1 content\n%%EOF'
        v2_bytes = b'%PDF-1.4 version 2 content\n%%EOF'
        v1 = _seed_version(
            me, source_request_id=request_id, report_version_number=1,
            is_current_for_patient=False, shared_at_offset_days=-5,
            pdf_content=v1_bytes,
        )
        _seed_version(
            me, source_request_id=request_id, report_version_number=2,
            is_current_for_patient=True,
            pdf_content=v2_bytes,
        )
        client = _auth_client(me)

        v1_token = v1.files.first().file_token
        resp = client.get(
            DOWNLOAD_URL.format(token=v1_token),
            HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 200, resp.content
        body = b''.join(resp.streaming_content) if resp.streaming else resp.content
        # Critical: v1's URL streams v1's bytes — never v2's.
        assert body == v1_bytes


# ---------------------------------------------------------------------------
# 7. Edge case — singleton history works for legacy rows w/o source_request_id
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestLegacySingletonHistory:

    def test_legacy_row_without_source_request_id_returns_singleton_line(self):
        """Pre-Phase-2 rows may have ``source_request_id=None``. The
        endpoint must not roll up across all such rows (which would
        leak unrelated history); it returns the requested row only."""
        me = _make_account()
        legacy_a = PatientSharedResult.objects.create(
            patient_account=me, source_type='DIRECT', source_name='Old Lab',
            request_reference='LEGACY-A',
            request_date=date(2025, 1, 1),
            result_available_date=date(2025, 1, 2),
            status=SharedResultStatus.ACTIVE,
            # No source_request_id, no source_tenant_schema, no version.
            is_current_for_patient=True,
        )
        PatientSharedResultFile.objects.create(
            shared_result=legacy_a,
            file_token=f'tok_legacy_{legacy_a.id.hex[:16]}',
            filename='legacy_a.pdf',
        )
        legacy_b = PatientSharedResult.objects.create(
            patient_account=me, source_type='DIRECT', source_name='Old Lab',
            request_reference='LEGACY-B',
            request_date=date(2025, 2, 1),
            result_available_date=date(2025, 2, 2),
            status=SharedResultStatus.ACTIVE,
            is_current_for_patient=True,
        )
        PatientSharedResultFile.objects.create(
            shared_result=legacy_b,
            file_token=f'tok_legacy_{legacy_b.id.hex[:16]}',
            filename='legacy_b.pdf',
        )

        resp = _auth_client(me).get(
            VERSIONS_URL.format(pk=legacy_a.id),
            HTTP_HOST='testlab.localhost',
        )
        body = resp.json()['data']
        # Only legacy_a is returned — not legacy_b — even though both
        # have ``source_request_id=None``. Treating them as a single
        # group would leak unrelated history.
        assert [v['id'] for v in body['versions']] == [str(legacy_a.id)]
