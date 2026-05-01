"""
Override the project's root autouse ``_in_tenant_schema`` fixture for
the patient_portal test suite.

Patient portal tables live in the ``public`` schema (see
``apps/patient_portal/__init__.py``), so writes need to happen with
``search_path=public``. The root conftest wraps every test in
``schema_context('schema_testlab')`` — which would route INSERTs to the
lab tenant's search_path and quietly hit the public schema only on
fallback. Explicit is better: this autouse runs each test in the
public schema directly.
"""
import pytest
from django_tenants.utils import get_public_schema_name, schema_context


@pytest.fixture(autouse=True)
def _in_public_schema(_test_tenant_schema, db):
    """Run patient_portal tests against the ``public`` schema. The root
    conftest still creates the lab tenant schema once per session — we
    rely on it being present so the cross-schema isolation tests have
    something real to compare against."""
    with schema_context(get_public_schema_name()):
        yield
