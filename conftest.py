"""
Cytova — Root conftest

Every test runs inside an isolated tenant schema (autouse fixture).
The schema is created once per session; data is cleaned between tests
by transactional_db rollback.
"""
import pytest
from django.test import RequestFactory
from django_tenants.utils import schema_context

from apps.users.models import StaffUser, Role


# ---------------------------------------------------------------------------
# Session-scoped: create the test tenant schema once
# ---------------------------------------------------------------------------

@pytest.fixture(scope='session')
def _test_tenant_schema(django_db_setup, django_db_blocker):
    """
    Create the test tenant and its schema+migrations once per test session.
    DDL is not transactional in PostgreSQL, so the schema persists across
    tests. The test database itself is dropped at session end by pytest-django.
    """
    with django_db_blocker.unblock():
        from apps.tenants.models import Tenant, Domain
        tenant = Tenant(
            name='Test Lab',
            subdomain='testlab',
            schema_name='schema_testlab',
        )
        tenant.save()  # auto_create_schema=True → CREATE SCHEMA + migrate
        Domain.objects.create(
            domain='testlab.localhost',
            tenant=tenant,
            is_primary=True,
        )
    return tenant.schema_name


# ---------------------------------------------------------------------------
# Autouse: every test runs inside the tenant schema
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _in_tenant_schema(_test_tenant_schema, db):
    """
    Wraps every test in schema_context so all ORM queries target the
    tenant schema. The db fixture wraps each test in a transaction that
    is rolled back at test end — cleaning up all data automatically.
    """
    with schema_context(_test_tenant_schema):
        yield


# ---------------------------------------------------------------------------
# User fixtures — no manual schema context needed (autouse handles it)
# ---------------------------------------------------------------------------

@pytest.fixture()
def lab_admin():
    return StaffUser.objects.create_user(
        email='admin@testlab.io',
        password='testpass123!',
        first_name='Admin',
        last_name='User',
        role=Role.LAB_ADMIN,
    )


@pytest.fixture()
def receptionist():
    return StaffUser.objects.create_user(
        email='reception@testlab.io',
        password='testpass123!',
        first_name='Reception',
        last_name='User',
        role=Role.RECEPTIONIST,
    )


@pytest.fixture()
def technician():
    return StaffUser.objects.create_user(
        email='tech@testlab.io',
        password='testpass123!',
        first_name='Tech',
        last_name='User',
        role=Role.TECHNICIAN,
    )


@pytest.fixture()
def viewer_auditor():
    return StaffUser.objects.create_user(
        email='viewer@testlab.io',
        password='testpass123!',
        first_name='Viewer',
        last_name='User',
        role=Role.VIEWER_AUDITOR,
    )


@pytest.fixture()
def biologist():
    return StaffUser.objects.create_user(
        email='bio@testlab.io',
        password='testpass123!',
        first_name='Bio',
        last_name='User',
        role=Role.BIOLOGIST,
    )


@pytest.fixture()
def billing_officer():
    return StaffUser.objects.create_user(
        email='billing@testlab.io',
        password='testpass123!',
        first_name='Billing',
        last_name='User',
        role=Role.BILLING_OFFICER,
    )


@pytest.fixture()
def inventory_manager():
    return StaffUser.objects.create_user(
        email='inventory@testlab.io',
        password='testpass123!',
        first_name='Inventory',
        last_name='User',
        role=Role.INVENTORY_MANAGER,
    )


# ---------------------------------------------------------------------------
# Shared catalog fixture — technique is non-null on ExamDefinition
# ---------------------------------------------------------------------------

@pytest.fixture()
def default_technique():
    from apps.catalog.models import ExamTechnique
    t, _ = ExamTechnique.objects.get_or_create(
        name='Default Test Technique',
        defaults={'description': 'Shared test fixture', 'is_active': True},
    )
    return t


# ---------------------------------------------------------------------------
# Request factory with audit attributes
# ---------------------------------------------------------------------------

@pytest.fixture()
def make_request():
    """Returns a function that creates a fake request with audit context."""
    factory = RequestFactory()

    def _make(user):
        req = factory.get('/')
        req.user = user
        req.audit_ip = '127.0.0.1'
        req.audit_user_agent = 'pytest'
        return req

    return _make


# ---------------------------------------------------------------------------
# Post-confirmation label auto-generation for legacy tests
# ---------------------------------------------------------------------------
#
# The production workflow is: confirm request → generate labels (explicit API
# call) → collect specimens. That separation is preserved in the live service.
#
# Historical test suites (in ``requests/``, ``results/``, etc.) predate the
# "labels required before collection" rule: they call
# ``AnalysisRequestService.create(..., confirm_after=True)`` and go straight
# to ``mark_collected``. Rewriting every one of them would be noisy and risk
# subtle drift across ~30 test call-sites.
#
# Instead, this autouse fixture installs a test-only wrapper around
# ``AnalysisRequestService.create`` that transparently generates labels
# whenever ``confirm_after=True``. Production behaviour is unchanged —
# this patch only exists inside the pytest session.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _auto_generate_labels_on_confirm(request, _test_tenant_schema, db, monkeypatch):
    # Tests that explicitly manage label generation (``apps/requests/tests/
    # test_labels.py`` and the dedicated collection-enforcement suite) opt
    # out by declaring ``pytestmark = pytest.mark.no_auto_labels`` at module
    # level — see those files.
    if request.node.get_closest_marker('no_auto_labels'):
        yield
        return

    from apps.requests.services import AnalysisRequestService
    from apps.requests.label_service import RequestLabelService

    original_create = AnalysisRequestService.create

    @staticmethod
    def wrapped_create(*args, **kwargs):
        ar = original_create(*args, **kwargs)
        if kwargs.get('confirm_after') and ar is not None:
            created_by = kwargs.get('created_by')
            request = kwargs.get('request')
            if created_by is not None and request is not None:
                try:
                    RequestLabelService.generate_or_get(
                        analysis_request=ar,
                        generated_by=created_by,
                        request=request,
                    )
                except Exception:  # noqa: BLE001
                    # Some tests confirm requests with configurations that
                    # are not label-eligible (e.g. all items rejected). Fall
                    # back silently so those tests keep their pre-existing
                    # semantics.
                    pass
        return ar

    monkeypatch.setattr(AnalysisRequestService, 'create', wrapped_create)
    yield
