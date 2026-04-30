"""
Tests for the audit log HTTP endpoint and the StaffUser display-name
properties that drive ``actor_display_name`` rendering.

Coverage:
  - GET /api/v1/audit/ resolves and returns 200 (route fix regression test)
  - response shape carries actor_display_name alongside actor_email
  - actor_display_name uses the StaffUser's full name when the user
    still exists in the tenant schema
  - falls back to actor_email when the actor record was hard-deleted
  - falls back to "System" for SYSTEM-type entries
  - StaffUser display_name / professional_display_name behave per spec
  - tenant isolation is preserved via the autouse ``_in_tenant_schema``
"""
from __future__ import annotations

import pytest
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.audit.models import ActorType, AuditAction, AuditLog


pytestmark = pytest.mark.no_auto_labels


@pytest.fixture(autouse=True)
def _usable_subscription(_test_tenant_schema, django_db_blocker):
    from apps.tenants.models import (
        Subscription, SubscriptionPlan, SubscriptionStatus, Tenant,
    )
    with django_db_blocker.unblock():
        with schema_context(get_public_schema_name()):
            plan, _ = SubscriptionPlan.objects.get_or_create(
                code='TEST_TRIAL',
                defaults={
                    'name': 'Test Trial', 'is_trial': True,
                    'trial_duration_days': 30, 'is_public': False,
                },
            )
            tenant = Tenant.objects.get(schema_name=_test_tenant_schema)
            Subscription.objects.get_or_create(
                tenant=tenant,
                status=SubscriptionStatus.TRIAL,
                defaults={'plan': plan},
            )
    yield


def _client(user) -> APIClient:
    client = APIClient()
    token = RefreshToken.for_user(user).access_token
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return client


def _get(user, qs: str = ''):
    """Hit the audit endpoint and return the paginated ``results`` array.
    The endpoint now returns ``{count, next, previous, results}``; tests
    that only need the rows can stay terse by reading through this helper.
    Pass an empty ``from``/``to`` to bypass the default current-month
    filter when older records are needed."""
    url = '/api/v1/audit/' + (f'?{qs}' if qs else '')
    resp = _client(user).get(url, HTTP_HOST='testlab.localhost')
    assert resp.status_code == 200, resp.content
    body = resp.json()
    inner = body.get('data', body)
    if isinstance(inner, dict) and 'results' in inner:
        return inner['results']
    return inner


def _get_envelope(user, qs: str = ''):
    """Same call but returns the full pagination envelope so tests can
    inspect ``count`` / ``next`` / ``previous``."""
    url = '/api/v1/audit/' + (f'?{qs}' if qs else '')
    resp = _client(user).get(url, HTTP_HOST='testlab.localhost')
    assert resp.status_code == 200, resp.content
    body = resp.json()
    return body.get('data', body)


# ---------------------------------------------------------------------------
# StaffUser display-name properties
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestStaffUserDisplayNames:

    def test_display_name_no_title(self, lab_admin):
        lab_admin.title = ''
        lab_admin.first_name = 'René'
        lab_admin.last_name = 'GOSSA'
        lab_admin.save(update_fields=['title', 'first_name', 'last_name'])
        assert lab_admin.display_name == 'René GOSSA'

    def test_display_name_falls_back_to_email_when_no_name(self, lab_admin):
        lab_admin.title = ''
        lab_admin.first_name = ''
        lab_admin.last_name = ''
        lab_admin.save(update_fields=['title', 'first_name', 'last_name'])
        assert lab_admin.display_name == lab_admin.email

    def test_professional_display_name_with_title(self, lab_admin):
        lab_admin.title = 'Dr'
        lab_admin.first_name = 'René'
        lab_admin.last_name = 'GOSSA'
        lab_admin.save(update_fields=['title', 'first_name', 'last_name'])
        assert lab_admin.professional_display_name == 'Dr René GOSSA'

    def test_professional_display_name_without_title(self, lab_admin):
        lab_admin.title = ''
        lab_admin.first_name = 'René'
        lab_admin.last_name = 'GOSSA'
        lab_admin.save(update_fields=['title', 'first_name', 'last_name'])
        # No title ⇒ identical to display_name.
        assert lab_admin.professional_display_name == 'René GOSSA'


# ---------------------------------------------------------------------------
# Audit endpoint shape + 200 OK (was 404 before this fix)
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestAuditEndpoint:

    def test_endpoint_resolves(self, lab_admin):
        # No fixtures — empty list is a valid response. The point of this
        # test is that the route exists at all (regression: the include
        # was previously commented out).
        resp = _client(lab_admin).get('/api/v1/audit/', HTTP_HOST='testlab.localhost')
        assert resp.status_code == 200, resp.content

    def test_response_shape_includes_actor_display_name(self, lab_admin):
        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=lab_admin.id, actor_email=lab_admin.email,
            action=AuditAction.UPDATE,
            entity_type='AnalysisRequest', entity_id=lab_admin.id,
        )
        body = _get(lab_admin)
        assert len(body) >= 1
        row = body[0]
        assert {
            'id', 'actor_type', 'actor_id', 'actor_email', 'actor_display_name',
            'action', 'entity_type', 'entity_id',
            'diff', 'ip_address', 'timestamp',
        } <= set(row.keys())


# ---------------------------------------------------------------------------
# actor_display_name fallback chain
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestActorDisplayName:

    def test_uses_live_user_display_name(self, lab_admin):
        lab_admin.title = 'Dr'
        lab_admin.first_name = 'René'
        lab_admin.last_name = 'GOSSA'
        lab_admin.save(update_fields=['title', 'first_name', 'last_name'])
        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=lab_admin.id, actor_email=lab_admin.email,
            action=AuditAction.LOGIN,
            entity_type='Auth', entity_id=lab_admin.id,
        )
        body = _get(lab_admin)
        # Audit log uses display_name (no title) — title is reserved for
        # medical/signature contexts.
        assert body[0]['actor_display_name'] == 'René GOSSA'

    def test_falls_back_to_email_when_actor_was_hard_deleted(self, lab_admin):
        # Use a random UUID that points to no live StaffUser. The
        # snapshotted email on the audit row is the only attribution
        # surface — exactly what production looks like after a user
        # was deactivated.
        import uuid
        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=uuid.uuid4(),
            actor_email='ghost@example.com',
            action=AuditAction.UPDATE,
            entity_type='Patient', entity_id=lab_admin.id,
        )
        body = _get(lab_admin)
        # Pull the row we just inserted (newest-first ordering).
        row = next(r for r in body if r['actor_email'] == 'ghost@example.com')
        assert row['actor_display_name'] == 'ghost@example.com'

    def test_system_actor_renders_as_system(self, lab_admin):
        AuditLog.objects.create(
            actor_type=ActorType.SYSTEM,
            actor_id=None, actor_email=None,
            action=AuditAction.UPDATE,
            entity_type='Schedule', entity_id=lab_admin.id,
        )
        body = _get(lab_admin)
        row = next(r for r in body if r['actor_type'] == ActorType.SYSTEM)
        assert row['actor_display_name'] == 'System'


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestAuditPagination:

    def test_response_envelope_shape(self, lab_admin):
        env = _get_envelope(lab_admin)
        # Project envelope wraps in {data, ...}; pagination envelope sits inside.
        assert {'count', 'next', 'previous', 'results'} <= set(env.keys())

    def test_page_size_param(self, lab_admin):
        for i in range(7):
            AuditLog.objects.create(
                actor_type=ActorType.STAFF_USER,
                actor_id=lab_admin.id, actor_email=lab_admin.email,
                action=AuditAction.UPDATE,
                entity_type=f'Filler{i}', entity_id=lab_admin.id,
            )
        env = _get_envelope(lab_admin, 'page_size=3')
        assert len(env['results']) == 3
        assert env['count'] >= 7
        assert env['next']  # there should be a next page link


@pytest.mark.django_db(transaction=True)
class TestAuditDateRange:

    def test_default_filters_to_current_month(self, lab_admin):
        from datetime import datetime, timedelta
        now = timezone.now()
        # An entry from 60 days ago — should NOT appear in the default
        # response (default = current calendar month only).
        old = AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=lab_admin.id, actor_email=lab_admin.email,
            action=AuditAction.UPDATE,
            entity_type='AncientEntity', entity_id=lab_admin.id,
        )
        # Bypass the model's append-only guard with a direct UPDATE.
        AuditLog.objects.filter(pk=old.pk).update(timestamp=now - timedelta(days=60))
        # A fresh entry from today
        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=lab_admin.id, actor_email=lab_admin.email,
            action=AuditAction.UPDATE,
            entity_type='RecentEntity', entity_id=lab_admin.id,
        )
        body = _get(lab_admin)
        entity_types = {r['entity_type'] for r in body}
        assert 'RecentEntity' in entity_types
        assert 'AncientEntity' not in entity_types

    def test_explicit_from_widens_window(self, lab_admin):
        from datetime import timedelta
        now = timezone.now()
        old = AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=lab_admin.id, actor_email=lab_admin.email,
            action=AuditAction.UPDATE,
            entity_type='AncientEntity2', entity_id=lab_admin.id,
        )
        AuditLog.objects.filter(pk=old.pk).update(timestamp=now - timedelta(days=60))
        # Pass a from date 90 days back to widen the window.
        from_str = (now - timedelta(days=90)).date().isoformat()
        body = _get(lab_admin, f'from={from_str}')
        assert any(r['entity_type'] == 'AncientEntity2' for r in body)


@pytest.mark.django_db(transaction=True)
class TestAuditSearch:

    def test_search_matches_actor_email(self, lab_admin):
        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=lab_admin.id, actor_email='very-distinct-actor@golab.io',
            action=AuditAction.UPDATE,
            entity_type='Patient', entity_id=lab_admin.id,
        )
        body = _get(lab_admin, 'search=very-distinct-actor')
        assert any(r['actor_email'] == 'very-distinct-actor@golab.io' for r in body)

    def test_search_matches_action(self, lab_admin):
        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=lab_admin.id, actor_email=lab_admin.email,
            action=AuditAction.PUBLISH,
            entity_type='ResultVersion', entity_id=lab_admin.id,
        )
        body = _get(lab_admin, 'search=PUBLISH')
        assert all(r['action'] == 'PUBLISH' for r in body)

    def test_search_matches_ip(self, lab_admin):
        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=lab_admin.id, actor_email=lab_admin.email,
            action=AuditAction.LOGIN,
            entity_type='Auth', entity_id=lab_admin.id,
            ip_address='198.51.100.42',
        )
        body = _get(lab_admin, 'search=198.51.100.42')
        assert any(r['ip_address'] == '198.51.100.42' for r in body)


@pytest.mark.django_db(transaction=True)
class TestAuditDiffMasking:

    def test_password_field_is_masked(self, lab_admin):
        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=lab_admin.id, actor_email=lab_admin.email,
            action=AuditAction.UPDATE,
            entity_type='StaffUser', entity_id=lab_admin.id,
            diff={'before': {'password': 'plaintext'},
                  'after':  {'password': 'changed'}},
        )
        body = _get(lab_admin)
        row = next(r for r in body if r['entity_type'] == 'StaffUser')
        assert row['diff']['before']['password'] == '***'
        assert row['diff']['after']['password'] == '***'

    def test_token_field_is_masked(self, lab_admin):
        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=lab_admin.id, actor_email=lab_admin.email,
            action=AuditAction.UPDATE,
            entity_type='ApiCredential', entity_id=lab_admin.id,
            diff={'after': {'api_token': 'sk-live-abcd1234',
                            'name': 'Public name'}},
        )
        body = _get(lab_admin)
        row = next(r for r in body if r['entity_type'] == 'ApiCredential')
        assert row['diff']['after']['api_token'] == '***'
        # Non-sensitive sibling stays visible.
        assert row['diff']['after']['name'] == 'Public name'

    def test_pdf_password_and_verification_code_masked(self, lab_admin):
        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=lab_admin.id, actor_email=lab_admin.email,
            action=AuditAction.PUBLISH,
            entity_type='Result', entity_id=lab_admin.id,
            diff={'after': {'pdf_password': 'C4t!7p',
                            'verification_code': '123456',
                            'access_link': 'https://r/example/abc'}},
        )
        body = _get(lab_admin)
        row = next(r for r in body if r['entity_type'] == 'Result')
        for k in ('pdf_password', 'verification_code', 'access_link'):
            assert row['diff']['after'][k] == '***', k


@pytest.mark.django_db(transaction=True)
class TestAuditFilters:

    def test_action_filter(self, lab_admin):
        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=lab_admin.id, actor_email=lab_admin.email,
            action=AuditAction.CREATE,
            entity_type='Patient', entity_id=lab_admin.id,
        )
        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=lab_admin.id, actor_email=lab_admin.email,
            action=AuditAction.UPDATE,
            entity_type='Patient', entity_id=lab_admin.id,
        )
        body = _get(lab_admin, 'action=CREATE')
        assert body, 'expected at least one CREATE row'
        assert all(r['action'] == 'CREATE' for r in body)
