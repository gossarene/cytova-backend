"""
Run platform-admin tests against the public schema.

The platform-admin app is in ``SHARED_APPS``, so its tables live in
the public schema only. The root ``_in_tenant_schema`` autouse
fixture wraps every test in ``schema_context('schema_testlab')`` —
that's the wrong search_path for our writes. Override here, mirroring
the patient_portal conftest.
"""
import pytest
from django_tenants.utils import get_public_schema_name, schema_context


@pytest.fixture(autouse=True)
def _in_public_schema(_test_tenant_schema, db):
    with schema_context(get_public_schema_name()):
        yield
