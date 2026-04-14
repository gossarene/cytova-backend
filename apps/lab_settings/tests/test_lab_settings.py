"""Tests for the tenant-scoped LabSettings singleton."""
import pytest
from django_tenants.utils import schema_context, get_public_schema_name
from rest_framework.test import APIClient

from apps.lab_settings.models import LabSettings


API = '/api/v1/lab-settings/'


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
                    'name': 'Test Trial',
                    'is_trial': True,
                    'trial_duration_days': 30,
                    'is_public': False,
                },
            )
            tenant = Tenant.objects.get(schema_name=_test_tenant_schema)
            Subscription.objects.get_or_create(
                tenant=tenant,
                status=SubscriptionStatus.TRIAL,
                defaults={'plan': plan},
            )
    yield


@pytest.fixture()
def api_client():
    return APIClient(HTTP_HOST='testlab.localhost')


@pytest.fixture()
def admin_client(api_client, lab_admin):
    api_client.force_authenticate(user=lab_admin)
    return api_client


@pytest.fixture()
def viewer_client(api_client, viewer_auditor):
    api_client.force_authenticate(user=viewer_auditor)
    return api_client


class TestLabSettingsSingleton:

    def test_get_solo_creates_on_first_access(self):
        assert LabSettings.objects.count() == 0
        s = LabSettings.get_solo()
        assert s.pk is not None
        assert LabSettings.objects.count() == 1

        s2 = LabSettings.get_solo()
        assert s2.pk == s.pk

    def test_defaults_are_sane(self):
        s = LabSettings.get_solo()
        assert s.show_logo is True
        assert s.show_reference_ranges is True
        assert s.show_exam_technique is True
        assert s.lab_name == ''


class TestLabSettingsEndpoint:

    def test_admin_can_read(self, admin_client):
        resp = admin_client.get(API)
        assert resp.status_code == 200
        body = resp.json().get('data', resp.json())
        assert 'lab_name' in body
        assert 'show_exam_technique' in body

    def test_admin_can_update(self, admin_client):
        resp = admin_client.patch(API, {
            'lab_name': 'Acme Labs',
            'phone': '+1 555 0000',
            'show_exam_technique': False,
        }, format='json')
        assert resp.status_code == 200
        body = resp.json().get('data', resp.json())
        assert body['lab_name'] == 'Acme Labs'
        assert body['show_exam_technique'] is False

        # Persistence check
        s = LabSettings.get_solo()
        assert s.lab_name == 'Acme Labs'

    def test_viewer_can_read(self, viewer_client):
        resp = viewer_client.get(API)
        assert resp.status_code == 200

    def test_viewer_cannot_update(self, viewer_client):
        resp = viewer_client.patch(API, {'lab_name': 'Nope'}, format='json')
        assert resp.status_code == 403

    def test_unauthenticated_blocked(self, api_client):
        resp = api_client.get(API)
        assert resp.status_code == 401
