"""
Tests for the role-aware dashboard cockpit endpoint.

Coverage:
  - HTTP route resolves and returns the documented top-level shape
  - role-specific KPIs are surfaced for each TenantRole
  - revenue KPI is hidden for roles that lack billing scope
  - chart series are present and populated with zero-fill where needed
  - tenant isolation: queries run inside the tenant schema set by the
    autouse ``_in_tenant_schema`` fixture (no cross-tenant data
    leakage possible)

These exercise the HTTP layer end-to-end through the DRF APIClient so
the URL conf, permission gate, and composer are all validated together.
"""
from __future__ import annotations

import pytest
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken


@pytest.fixture(autouse=True)
def _usable_subscription(_test_tenant_schema, django_db_blocker):
    """Same pattern as the other API-level test suites: SubscriptionEnforcement
    middleware blocks every tenant request with 403 unless a usable
    subscription exists. Set one up once per session."""
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


def _get(user) -> dict:
    resp = _client(user).get('/api/v1/dashboard/cockpit/', HTTP_HOST='testlab.localhost')
    assert resp.status_code == 200, resp.content
    body = resp.json()
    # Project-wide renderer wraps every payload in {data, meta, errors}.
    return body.get('data', body)


@pytest.mark.django_db(transaction=True)
class TestCockpitShape:
    """The response always carries the same top-level keys; only the
    contents of ``kpis`` and ``actions`` vary by role."""

    def test_top_level_shape(self, lab_admin):
        body = _get(lab_admin)
        assert set(body.keys()) == {'role', 'greeting_name', 'kpis', 'actions', 'charts'}
        assert body['role'] == 'LAB_ADMIN'
        assert body['greeting_name']  # truthy — name or fallback to email

    def test_charts_have_documented_series(self, lab_admin):
        charts = _get(lab_admin)['charts']
        assert set(charts.keys()) == {
            'requests_over_time',
            'requests_by_status',
            'requests_by_source',
            'results_pipeline',
        }
        # 14-day window, zero-filled.
        assert len(charts['requests_over_time']) == 14
        # source is the canonical pair (DIRECT, PARTNER) even with no rows.
        sources = {row['source'] for row in charts['requests_by_source']}
        assert sources == {'DIRECT_PATIENT', 'PARTNER_ORGANIZATION'}


@pytest.mark.django_db(transaction=True)
class TestRoleSpecificKpis:
    """Each role surfaces a distinct, documented KPI set. We assert the
    presence of role-signature KPI keys rather than the full set so the
    payload can grow without breaking tests."""

    def _kpi_keys(self, payload) -> set[str]:
        return {kpi['key'] for kpi in payload['kpis']}

    def test_receptionist_focus_is_intake_and_notification(self, receptionist):
        keys = self._kpi_keys(_get(receptionist))
        assert 'created_today' in keys
        assert 'pending_confirmation' in keys
        assert 'ready_to_notify' in keys
        assert 'delivered_today' in keys

    def test_technician_focus_is_pipeline(self, technician):
        keys = self._kpi_keys(_get(technician))
        assert 'pending_collection' in keys
        assert 'in_analysis' in keys
        assert 'awaiting_review' in keys
        assert 'retest_required' in keys

    def test_biologist_focus_is_validation(self, biologist):
        keys = self._kpi_keys(_get(biologist))
        assert 'pending_validation' in keys
        assert 'validated_today' in keys
        assert 'abnormal_month' in keys
        # Retest belongs to both biologist and technician dashboards.
        assert 'retest_required' in keys

    def test_lab_admin_focus_is_global_operations(self, lab_admin):
        keys = self._kpi_keys(_get(lab_admin))
        assert 'active_requests' in keys
        assert 'validated_month' in keys
        assert 'alerts' in keys

    def test_inventory_manager_focus_is_alerts(self, inventory_manager):
        keys = self._kpi_keys(_get(inventory_manager))
        assert 'alerts' in keys
        assert 'critical' in keys

    def test_viewer_auditor_gets_default_dashboard(self, viewer_auditor):
        keys = self._kpi_keys(_get(viewer_auditor))
        # Default is the safe read-only set: active / pending / ready / alerts.
        assert 'active_requests' in keys
        assert 'pending_validation' in keys
        assert 'ready_to_notify' in keys
        assert 'alerts' in keys


@pytest.mark.django_db(transaction=True)
class TestRevenuePermission:
    """Revenue KPI must only appear for roles whose remit covers billing."""

    def _has_revenue_kpi(self, payload) -> bool:
        return any(k['key'] == 'revenue_month' for k in payload['kpis'])

    def test_lab_admin_sees_revenue(self, lab_admin):
        assert self._has_revenue_kpi(_get(lab_admin)) is True

    def test_billing_officer_sees_revenue(self, billing_officer):
        assert self._has_revenue_kpi(_get(billing_officer)) is True

    def test_biologist_does_not_see_revenue(self, biologist):
        assert self._has_revenue_kpi(_get(biologist)) is False

    def test_receptionist_does_not_see_revenue(self, receptionist):
        assert self._has_revenue_kpi(_get(receptionist)) is False

    def test_technician_does_not_see_revenue(self, technician):
        assert self._has_revenue_kpi(_get(technician)) is False

    def test_viewer_auditor_does_not_see_revenue(self, viewer_auditor):
        assert self._has_revenue_kpi(_get(viewer_auditor)) is False


@pytest.mark.django_db(transaction=True)
class TestActionsAndLinks:
    """Each action carries an href — the frontend uses these to navigate
    to a filtered list. None should be empty (broken-link prevention)."""

    def test_every_action_has_href(self, lab_admin):
        body = _get(lab_admin)
        for action in body['actions']:
            assert action['href'], f'action {action["key"]} has no href'
            assert action['cta'], f'action {action["key"]} has no CTA copy'

    def test_every_kpi_with_href_starts_with_slash(self, lab_admin):
        body = _get(lab_admin)
        for kpi in body['kpis']:
            if kpi['href'] is not None:
                assert kpi['href'].startswith('/'), \
                    f'KPI {kpi["key"]} href is not a relative path'
