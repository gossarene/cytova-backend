"""
Tests for subscription-based tenant access enforcement.

These tests verify that the SubscriptionEnforcementMiddleware correctly
blocks or allows tenant API requests based on subscription status.

Uses transactional_db because tenant creation requires DDL.
"""
import pytest
from datetime import timedelta
from unittest.mock import patch

from django.test import RequestFactory
from django_tenants.utils import schema_context

from apps.tenants.models import (
    Domain,
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
    Tenant,
)
from apps.tenants.subscription_service import SubscriptionService
from common.middleware import SubscriptionEnforcementMiddleware


# Override the autouse tenant fixture — these tests manage their own tenants
@pytest.fixture(autouse=True)
def _in_tenant_schema():
    yield


@pytest.fixture()
def starter_plan():
    return SubscriptionPlan.objects.create(
        code='STARTER',
        name='Starter',
        trial_days=14,
    )


@pytest.fixture()
def _test_tenant(starter_plan):
    """Creates a tenant with a TRIAL subscription."""
    tenant = Tenant(
        name='Enforcement Lab',
        subdomain='enforce-lab',
        schema_name='schema_enforce_lab',
    )
    tenant.save()
    Domain.objects.create(
        domain='enforce-lab.localhost',
        tenant=tenant,
        is_primary=True,
    )
    subscription = SubscriptionService.create_trial(tenant, starter_plan)
    return tenant, subscription


def _make_tenant_request(tenant, path='/api/v1/patients/'):
    """Creates a request object as if CytovaTenantMiddleware resolved it."""
    factory = RequestFactory()
    request = factory.get(path)
    request.tenant = tenant
    request.tenant_schema = tenant.schema_name
    return request


def _make_public_request(path='/api/v1/onboarding/signup/'):
    """Creates a request object for the public schema."""
    factory = RequestFactory()
    request = factory.get(path)
    request.tenant_schema = 'public'
    return request


def _run_middleware(request):
    """Run the SubscriptionEnforcementMiddleware and return the response."""
    def dummy_get_response(req):
        from django.http import HttpResponse
        return HttpResponse('OK', status=200)

    mw = SubscriptionEnforcementMiddleware(dummy_get_response)
    return mw(request)


# ---------------------------------------------------------------------------
# Tests — allowed access
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestSubscriptionAllowed:

    def test_trial_subscription_allowed(self, _test_tenant):
        tenant, subscription = _test_tenant
        assert subscription.status == SubscriptionStatus.TRIAL

        response = _run_middleware(_make_tenant_request(tenant))
        assert response.status_code == 200

    def test_active_subscription_allowed(self, _test_tenant):
        tenant, subscription = _test_tenant
        SubscriptionService.activate(subscription)

        response = _run_middleware(_make_tenant_request(tenant))
        assert response.status_code == 200

    def test_public_schema_always_allowed(self):
        response = _run_middleware(_make_public_request())
        assert response.status_code == 200

    def test_health_endpoint_exempt(self, _test_tenant):
        tenant, subscription = _test_tenant
        # Expire the subscription
        Subscription.objects.filter(pk=subscription.pk).update(
            status=SubscriptionStatus.EXPIRED,
        )

        response = _run_middleware(_make_tenant_request(tenant, '/health/'))
        assert response.status_code == 200

    def test_auth_endpoint_exempt(self, _test_tenant):
        tenant, subscription = _test_tenant
        Subscription.objects.filter(pk=subscription.pk).update(
            status=SubscriptionStatus.EXPIRED,
        )

        response = _run_middleware(
            _make_tenant_request(tenant, '/api/v1/auth/login/')
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Tests — blocked access
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestSubscriptionBlocked:

    def test_expired_subscription_blocked(self, _test_tenant):
        tenant, subscription = _test_tenant
        Subscription.objects.filter(pk=subscription.pk).update(
            status=SubscriptionStatus.EXPIRED,
        )

        response = _run_middleware(_make_tenant_request(tenant))
        assert response.status_code == 403

        import json
        body = json.loads(response.content)
        assert body['errors'][0]['code'] == 'SUBSCRIPTION_EXPIRED'
        assert body['data'] is None

    def test_suspended_subscription_blocked(self, _test_tenant):
        tenant, subscription = _test_tenant
        SubscriptionService.activate(subscription)
        subscription.refresh_from_db()
        SubscriptionService.suspend(subscription, reason='Non-payment')

        response = _run_middleware(_make_tenant_request(tenant))
        assert response.status_code == 403

        import json
        body = json.loads(response.content)
        assert body['errors'][0]['code'] == 'SUBSCRIPTION_SUSPENDED'
        assert body['errors'][0]['detail']['subscription_status'] == 'SUSPENDED'

    def test_cancelled_subscription_blocked(self, _test_tenant):
        tenant, subscription = _test_tenant
        SubscriptionService.cancel(subscription, cancelled_by='platform')

        response = _run_middleware(_make_tenant_request(tenant))
        assert response.status_code == 403

        import json
        body = json.loads(response.content)
        assert body['errors'][0]['code'] == 'SUBSCRIPTION_CANCELLED'

    def test_no_subscription_blocked(self):
        tenant = Tenant(
            name='No Sub Lab',
            subdomain='nosub-lab',
            schema_name='schema_nosub_lab',
        )
        tenant.save()
        Domain.objects.create(
            domain='nosub-lab.localhost',
            tenant=tenant,
            is_primary=True,
        )

        response = _run_middleware(_make_tenant_request(tenant))
        assert response.status_code == 403

        import json
        body = json.loads(response.content)
        assert body['errors'][0]['code'] == 'SUBSCRIPTION_MISSING'


# ---------------------------------------------------------------------------
# Tests — error response format
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
class TestErrorResponseFormat:

    def test_error_envelope_structure(self, _test_tenant):
        tenant, subscription = _test_tenant
        Subscription.objects.filter(pk=subscription.pk).update(
            status=SubscriptionStatus.EXPIRED,
        )

        response = _run_middleware(_make_tenant_request(tenant))

        import json
        body = json.loads(response.content)

        # Standard Cytova error envelope
        assert 'data' in body
        assert 'meta' in body
        assert 'errors' in body
        assert body['data'] is None
        assert body['meta'] is None
        assert len(body['errors']) == 1

        error = body['errors'][0]
        assert 'code' in error
        assert 'message' in error
        assert 'field' in error
        assert 'detail' in error
        assert error['field'] is None

    def test_content_type_is_json(self, _test_tenant):
        tenant, subscription = _test_tenant
        Subscription.objects.filter(pk=subscription.pk).update(
            status=SubscriptionStatus.EXPIRED,
        )

        response = _run_middleware(_make_tenant_request(tenant))
        assert response['Content-Type'] == 'application/json'
