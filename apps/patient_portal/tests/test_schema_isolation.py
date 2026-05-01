"""
Hard guarantee that the patient portal tables exist in the public
schema and NOT in any lab tenant schema.

If this test fails, the registration of ``apps.patient_portal`` has
drifted into ``TENANT_APPS`` (or someone added a custom router that
materialises tenant copies) — which violates the strict isolation
rule that no patient-portal data lives inside a lab tenant schema.
"""
from __future__ import annotations

import pytest
from django.db import connection
from django_tenants.utils import (
    get_public_schema_name, get_tenant_model, schema_context,
)


PORTAL_TABLES = (
    'patient_portal_account',
    'patient_portal_profile',
    'patient_portal_consent',
)


def _table_exists_in_schema(schema: str, table: str) -> bool:
    """Use ``to_regclass`` so the probe is harmless on non-existent
    tables (it returns NULL rather than raising). The lookup respects
    the search_path, so we set it explicitly to the schema we care
    about for a yes/no answer."""
    qualified = f'{schema}.{table}'
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT to_regclass(%s) IS NOT NULL', [qualified],
        )
        return bool(cursor.fetchone()[0])


@pytest.mark.django_db(transaction=True)
class TestPortalTablesArePublicOnly:

    def test_tables_exist_in_public_schema(self):
        for table in PORTAL_TABLES:
            assert _table_exists_in_schema(get_public_schema_name(), table), (
                f'Expected {table} in the public schema — patient_portal '
                f'must be installed in SHARED_APPS so the table actually '
                f'gets created during migrate_schemas --shared.'
            )

    def test_tables_do_not_exist_in_lab_tenant_schema(self):
        # Probe the test tenant schema created by the root conftest.
        Tenant = get_tenant_model()
        schema = Tenant.objects.exclude(
            schema_name=get_public_schema_name(),
        ).values_list('schema_name', flat=True).first()
        assert schema, 'No lab tenant schema available — fix conftest setup.'

        for table in PORTAL_TABLES:
            assert not _table_exists_in_schema(schema, table), (
                f'Found {table} inside lab tenant schema "{schema}". '
                f'Patient portal tables must NOT live in lab schemas — '
                f'check that apps.patient_portal is in SHARED_APPS only, '
                f'not in TENANT_APPS.'
            )

    def test_row_written_in_public_not_in_tenant_schema(self):
        """Insert a row, then query the tenant schema directly via raw
        SQL to confirm the row is NOT physically present there. We
        can't use the ORM here because django-tenants sets
        ``search_path=<tenant>,public`` — an ORM query would find the
        row via the public fallback even though the data is genuinely
        public-only. Direct SQL with the schema qualifier is the
        unambiguous probe."""
        from apps.patient_portal.models import PatientAccount

        PatientAccount.objects.create_user(
            email='isolation@portal.test', password='x' * 12,
        )

        Tenant = get_tenant_model()
        lab_schema = Tenant.objects.exclude(
            schema_name=get_public_schema_name(),
        ).values_list('schema_name', flat=True).first()

        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT COUNT(*) FROM public.patient_portal_account '
                'WHERE email = %s',
                ['isolation@portal.test'],
            )
            assert cursor.fetchone()[0] == 1, (
                'Row should exist in public.patient_portal_account.'
            )

            # The tenant table doesn't exist at all (proven above), so
            # querying it raises — that's the "data physically isolated"
            # guarantee. We catch and re-raise with a clearer message
            # if it ever stops raising.
            from django.db.utils import ProgrammingError
            with pytest.raises(ProgrammingError):
                cursor.execute(
                    f'SELECT COUNT(*) FROM {lab_schema}.patient_portal_account',
                )
