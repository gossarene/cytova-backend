"""
Cytova — Catalog reference-data CRUD tests.

Covers the harmonised reference endpoints:
    /api/v1/catalog/families/
    /api/v1/catalog/sub-families/
    /api/v1/catalog/tube-types/
    /api/v1/catalog/techniques/
    /api/v1/catalog/sample-types/

For each entity the tests exercise list / create / partial_update /
deactivate plus the entity-specific invariants (sub-family family filter,
uniqueness, permissions, exam-definition integration). HTTP layer is used
so the route wiring, permissions, filters and serializers are all covered
together — unit-level coverage of the serializers themselves lives in
``test_exam_metadata.py``.
"""
import pytest
from decimal import Decimal

from django_tenants.utils import schema_context, get_public_schema_name
from rest_framework.test import APIClient

from apps.catalog.models import (
    ExamFamily, ExamSubFamily, TubeType, ExamTechnique,
    ExamDefinition, SampleType,
)
from apps.audit.models import AuditLog


API = '/api/v1/catalog'


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _usable_subscription(_test_tenant_schema, django_db_blocker):
    """
    SubscriptionEnforcementMiddleware returns 403 for any tenant request
    whose tenant has no usable (TRIAL/ACTIVE) subscription. Unit-level
    catalog tests bypass the middleware by calling serializers directly,
    but this module hits the HTTP layer — so we must attach a usable
    subscription to the session-scoped test tenant.

    Creation happens in the public schema because Subscription and
    SubscriptionPlan live in SHARED_APPS. We use get_or_create so
    repeated runs in the same session (pytest-django keeps the test DB)
    don't collide on unique ``code``.
    """
    from apps.tenants.models import (
        Tenant, Subscription, SubscriptionPlan, SubscriptionStatus,
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
    # HTTP_HOST must match the test tenant's primary domain so the
    # django-tenants middleware routes requests into the tenant schema
    # that conftest._test_tenant_schema creates once per session.
    return APIClient(HTTP_HOST='testlab.localhost')


@pytest.fixture()
def admin_client(api_client, lab_admin):
    api_client.force_authenticate(user=lab_admin)
    return api_client


@pytest.fixture()
def tech_client(api_client, technician):
    api_client.force_authenticate(user=technician)
    return api_client


@pytest.fixture()
def family():
    return ExamFamily.objects.create(name='Hematology', display_order=1)


@pytest.fixture()
def other_family():
    return ExamFamily.objects.create(name='Biochemistry', display_order=2)


@pytest.fixture()
def sub_family(family):
    return ExamSubFamily.objects.create(family=family, name='Coagulation')


@pytest.fixture()
def tube_type():
    return TubeType.objects.create(name='EDTA')


@pytest.fixture()
def technique():
    return ExamTechnique.objects.create(name='Spectrophotometry')


def _data(resp):
    """Unwraps the {data, meta, errors} envelope used by the project."""
    body = resp.json()
    return body.get('data', body)


# ---------------------------------------------------------------------------
# Families
# ---------------------------------------------------------------------------

class TestExamFamilyEndpoint:

    def test_list_returns_active_and_inactive(self, admin_client, family):
        inactive = ExamFamily.objects.create(name='Immunology', is_active=False)
        resp = admin_client.get(f'{API}/families/')
        assert resp.status_code == 200
        ids = [row['id'] for row in _data(resp)]
        assert str(family.id) in ids
        assert str(inactive.id) in ids

    def test_list_filter_is_active(self, admin_client, family):
        ExamFamily.objects.create(name='Immunology', is_active=False)
        resp = admin_client.get(f'{API}/families/?is_active=true')
        assert resp.status_code == 200
        ids = [row['id'] for row in _data(resp)]
        assert str(family.id) in ids
        assert len(ids) == 1

    def test_list_search_by_name(self, admin_client, family):
        ExamFamily.objects.create(name='Biochemistry', display_order=2)
        resp = admin_client.get(f'{API}/families/?search=Hema')
        assert resp.status_code == 200
        ids = [row['id'] for row in _data(resp)]
        assert str(family.id) in ids
        assert len(ids) == 1

    def test_retrieve(self, admin_client, family):
        resp = admin_client.get(f'{API}/families/{family.id}/')
        assert resp.status_code == 200
        assert _data(resp)['name'] == 'Hematology'

    def test_create_as_admin(self, admin_client):
        resp = admin_client.post(
            f'{API}/families/',
            {'name': 'Microbiology', 'description': 'Bacterio/viro', 'display_order': 5},
            format='json',
        )
        assert resp.status_code == 201
        assert _data(resp)['name'] == 'Microbiology'
        assert ExamFamily.objects.filter(name='Microbiology').exists()

    def test_create_rejects_duplicate_name(self, admin_client, family):
        resp = admin_client.post(
            f'{API}/families/', {'name': 'Hematology'}, format='json',
        )
        assert resp.status_code == 400

    def test_create_forbidden_for_non_admin(self, tech_client):
        resp = tech_client.post(
            f'{API}/families/', {'name': 'X'}, format='json',
        )
        assert resp.status_code == 403

    def test_partial_update(self, admin_client, family):
        resp = admin_client.patch(
            f'{API}/families/{family.id}/',
            {'description': 'Updated'},
            format='json',
        )
        assert resp.status_code == 200
        family.refresh_from_db()
        assert family.description == 'Updated'

    def test_partial_update_rejects_conflicting_name(self, admin_client, family, other_family):
        resp = admin_client.patch(
            f'{API}/families/{other_family.id}/',
            {'name': 'Hematology'},
            format='json',
        )
        assert resp.status_code == 400

    def test_partial_update_allows_same_name(self, admin_client, family):
        """Updating a family with its own name must not collide with itself."""
        resp = admin_client.patch(
            f'{API}/families/{family.id}/',
            {'name': 'Hematology', 'description': 'Blood analyses'},
            format='json',
        )
        assert resp.status_code == 200

    def test_deactivate(self, admin_client, family):
        resp = admin_client.post(f'{API}/families/{family.id}/deactivate/')
        assert resp.status_code == 200
        family.refresh_from_db()
        assert family.is_active is False

    def test_deactivate_writes_audit(self, admin_client, family):
        before = AuditLog.objects.filter(entity_type='ExamFamily').count()
        admin_client.post(f'{API}/families/{family.id}/deactivate/')
        after = AuditLog.objects.filter(entity_type='ExamFamily').count()
        assert after == before + 1


# ---------------------------------------------------------------------------
# Sub-families
# ---------------------------------------------------------------------------

class TestExamSubFamilyEndpoint:

    def test_list_all(self, admin_client, sub_family):
        resp = admin_client.get(f'{API}/sub-families/')
        assert resp.status_code == 200
        ids = [row['id'] for row in _data(resp)]
        assert str(sub_family.id) in ids

    def test_list_filter_by_family_id(self, admin_client, family, other_family, sub_family):
        """
        The cascading frontend dropdown depends on this: sub-families must be
        filterable by their parent family id, and the filter must be strict.
        """
        other_sub = ExamSubFamily.objects.create(family=other_family, name='Enzymes')

        resp = admin_client.get(f'{API}/sub-families/?family_id={family.id}')
        assert resp.status_code == 200
        ids = [row['id'] for row in _data(resp)]
        assert str(sub_family.id) in ids
        assert str(other_sub.id) not in ids

    def test_list_includes_family_name(self, admin_client, sub_family):
        resp = admin_client.get(f'{API}/sub-families/')
        row = next(r for r in _data(resp) if r['id'] == str(sub_family.id))
        assert row['family_name'] == 'Hematology'

    def test_retrieve(self, admin_client, sub_family):
        resp = admin_client.get(f'{API}/sub-families/{sub_family.id}/')
        assert resp.status_code == 200
        body = _data(resp)
        assert body['name'] == 'Coagulation'
        assert body['family_id'] == str(sub_family.family_id)

    def test_create(self, admin_client, family):
        resp = admin_client.post(
            f'{API}/sub-families/',
            {'family_id': str(family.id), 'name': 'Hemostasis'},
            format='json',
        )
        assert resp.status_code == 201
        assert ExamSubFamily.objects.filter(family=family, name='Hemostasis').exists()

    def test_create_rejects_unknown_family(self, admin_client):
        import uuid
        resp = admin_client.post(
            f'{API}/sub-families/',
            {'family_id': str(uuid.uuid4()), 'name': 'Orphan'},
            format='json',
        )
        assert resp.status_code == 400

    def test_create_rejects_inactive_family(self, admin_client, family):
        family.is_active = False
        family.save()
        resp = admin_client.post(
            f'{API}/sub-families/',
            {'family_id': str(family.id), 'name': 'Whatever'},
            format='json',
        )
        assert resp.status_code == 400

    def test_create_rejects_duplicate_within_family(self, admin_client, sub_family):
        resp = admin_client.post(
            f'{API}/sub-families/',
            {'family_id': str(sub_family.family_id), 'name': 'Coagulation'},
            format='json',
        )
        assert resp.status_code == 400

    def test_create_same_name_allowed_across_families(self, admin_client, sub_family, other_family):
        """Sub-family name uniqueness is scoped per family, not global."""
        resp = admin_client.post(
            f'{API}/sub-families/',
            {'family_id': str(other_family.id), 'name': 'Coagulation'},
            format='json',
        )
        assert resp.status_code == 201

    def test_partial_update_name(self, admin_client, sub_family):
        resp = admin_client.patch(
            f'{API}/sub-families/{sub_family.id}/',
            {'name': 'Coag panel'},
            format='json',
        )
        assert resp.status_code == 200
        sub_family.refresh_from_db()
        assert sub_family.name == 'Coag panel'

    def test_partial_update_rejects_duplicate_within_family(self, admin_client, family, sub_family):
        ExamSubFamily.objects.create(family=family, name='Hemostasis')
        resp = admin_client.patch(
            f'{API}/sub-families/{sub_family.id}/',
            {'name': 'Hemostasis'},
            format='json',
        )
        assert resp.status_code == 400

    def test_deactivate(self, admin_client, sub_family):
        resp = admin_client.post(f'{API}/sub-families/{sub_family.id}/deactivate/')
        assert resp.status_code == 200
        sub_family.refresh_from_db()
        assert sub_family.is_active is False

    def test_create_forbidden_for_non_admin(self, tech_client, family):
        resp = tech_client.post(
            f'{API}/sub-families/',
            {'family_id': str(family.id), 'name': 'Nope'},
            format='json',
        )
        assert resp.status_code == 403

    def test_list_allowed_for_non_admin(self, tech_client, sub_family):
        """Read access is open to any authenticated staff."""
        resp = tech_client.get(f'{API}/sub-families/')
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tube types
# ---------------------------------------------------------------------------

class TestTubeTypeEndpoint:

    def test_list(self, admin_client, tube_type):
        resp = admin_client.get(f'{API}/tube-types/')
        assert resp.status_code == 200
        ids = [row['id'] for row in _data(resp)]
        assert str(tube_type.id) in ids

    def test_create(self, admin_client):
        resp = admin_client.post(
            f'{API}/tube-types/',
            {'name': 'Citrate', 'description': 'For coag tests'},
            format='json',
        )
        assert resp.status_code == 201
        assert TubeType.objects.filter(name='Citrate').exists()

    def test_create_rejects_duplicate(self, admin_client, tube_type):
        resp = admin_client.post(
            f'{API}/tube-types/', {'name': 'EDTA'}, format='json',
        )
        assert resp.status_code == 400

    def test_partial_update(self, admin_client, tube_type):
        resp = admin_client.patch(
            f'{API}/tube-types/{tube_type.id}/',
            {'description': 'Lavender cap'},
            format='json',
        )
        assert resp.status_code == 200
        tube_type.refresh_from_db()
        assert tube_type.description == 'Lavender cap'

    def test_partial_update_same_name_allowed(self, admin_client, tube_type):
        resp = admin_client.patch(
            f'{API}/tube-types/{tube_type.id}/',
            {'name': 'EDTA', 'description': 'unchanged name'},
            format='json',
        )
        assert resp.status_code == 200

    def test_deactivate(self, admin_client, tube_type):
        resp = admin_client.post(f'{API}/tube-types/{tube_type.id}/deactivate/')
        assert resp.status_code == 200
        tube_type.refresh_from_db()
        assert tube_type.is_active is False

    def test_create_forbidden_for_non_admin(self, tech_client):
        resp = tech_client.post(
            f'{API}/tube-types/', {'name': 'Heparin'}, format='json',
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Techniques
# ---------------------------------------------------------------------------

class TestExamTechniqueEndpoint:

    def test_list(self, admin_client, technique):
        resp = admin_client.get(f'{API}/techniques/')
        assert resp.status_code == 200
        ids = [row['id'] for row in _data(resp)]
        assert str(technique.id) in ids

    def test_create(self, admin_client):
        resp = admin_client.post(
            f'{API}/techniques/',
            {'name': 'PCR', 'description': 'Polymerase chain reaction'},
            format='json',
        )
        assert resp.status_code == 201
        assert ExamTechnique.objects.filter(name='PCR').exists()

    def test_create_rejects_duplicate(self, admin_client, technique):
        resp = admin_client.post(
            f'{API}/techniques/', {'name': 'Spectrophotometry'}, format='json',
        )
        assert resp.status_code == 400

    def test_partial_update(self, admin_client, technique):
        resp = admin_client.patch(
            f'{API}/techniques/{technique.id}/',
            {'description': 'UV-Vis absorbance'},
            format='json',
        )
        assert resp.status_code == 200
        technique.refresh_from_db()
        assert technique.description == 'UV-Vis absorbance'

    def test_deactivate(self, admin_client, technique):
        resp = admin_client.post(f'{API}/techniques/{technique.id}/deactivate/')
        assert resp.status_code == 200
        technique.refresh_from_db()
        assert technique.is_active is False

    def test_create_forbidden_for_non_admin(self, tech_client):
        resp = tech_client.post(
            f'{API}/techniques/', {'name': 'ELISA'}, format='json',
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Sample types — read-only enum listing
# ---------------------------------------------------------------------------

class TestSampleTypeEndpoint:

    def test_list_returns_all_choices(self, admin_client):
        resp = admin_client.get(f'{API}/sample-types/')
        assert resp.status_code == 200
        body = _data(resp)
        values = {row['value'] for row in body}
        assert values == {c[0] for c in SampleType.choices}

    def test_list_payload_shape(self, admin_client):
        resp = admin_client.get(f'{API}/sample-types/')
        row = _data(resp)[0]
        assert set(row.keys()) == {'value', 'label'}

    def test_list_allowed_for_non_admin(self, tech_client):
        resp = tech_client.get(f'{API}/sample-types/')
        assert resp.status_code == 200

    def test_create_not_allowed(self, admin_client):
        """Sample types are a fixed taxonomy — write must 405."""
        resp = admin_client.post(
            f'{API}/sample-types/', {'value': 'PLASMA', 'label': 'Plasma'},
            format='json',
        )
        assert resp.status_code == 405


# ---------------------------------------------------------------------------
# ExamDefinition integration with the harmonised references
# ---------------------------------------------------------------------------

class TestExamDefinitionIntegration:
    """
    Ensures the family-required / sub-family-optional rule introduced in the
    previous step is still honoured end-to-end when using the new reference
    endpoints as the source of truth.
    """

    def test_create_exam_with_family_only(self, admin_client, family):
        resp = admin_client.post(
            f'{API}/exams/',
            {
                'family_id': str(family.id),
                'code': 'CBC',
                'name': 'Complete Blood Count',
                'sample_type': 'BLOOD',
                'unit_price': '50.0000',
            },
            format='json',
        )
        assert resp.status_code == 201, resp.content
        body = _data(resp)
        assert body['family']['id'] == str(family.id)
        assert body['sub_family'] is None

    def test_create_exam_with_matching_sub_family(
        self, admin_client, family, sub_family, tube_type, technique,
    ):
        resp = admin_client.post(
            f'{API}/exams/',
            {
                'family_id': str(family.id),
                'sub_family_id': str(sub_family.id),
                'tube_type_id': str(tube_type.id),
                'technique_id': str(technique.id),
                'fasting_required': True,
                'code': 'PT',
                'name': 'Prothrombin Time',
                'sample_type': 'BLOOD',
                'unit_price': '40.0000',
            },
            format='json',
        )
        assert resp.status_code == 201, resp.content
        body = _data(resp)
        assert body['sub_family']['id'] == str(sub_family.id)
        assert body['tube_type']['id'] == str(tube_type.id)
        assert body['technique']['id'] == str(technique.id)

    def test_create_exam_rejects_mismatched_sub_family(
        self, admin_client, family, other_family,
    ):
        foreign = ExamSubFamily.objects.create(family=other_family, name='Enzymes')
        resp = admin_client.post(
            f'{API}/exams/',
            {
                'family_id': str(family.id),
                'sub_family_id': str(foreign.id),
                'code': 'BAD',
                'name': 'Bad',
                'sample_type': 'BLOOD',
            },
            format='json',
        )
        assert resp.status_code == 400

    def test_update_exam_set_sub_family(self, admin_client, family, sub_family):
        exam = ExamDefinition.objects.create(
            family=family,
            code='UPD',
            name='Update Target',
            sample_type=SampleType.BLOOD,
            unit_price=Decimal('10.0000'),
        )
        resp = admin_client.patch(
            f'{API}/exams/{exam.id}/',
            {'sub_family_id': str(sub_family.id)},
            format='json',
        )
        assert resp.status_code == 200, resp.content
        exam.refresh_from_db()
        assert exam.sub_family_id == sub_family.id

    def test_update_exam_rejects_stale_sub_family_on_family_change(
        self, admin_client, family, sub_family, other_family,
    ):
        exam = ExamDefinition.objects.create(
            family=family, sub_family=sub_family,
            code='STALE', name='Stale',
            sample_type=SampleType.BLOOD, unit_price=Decimal('10.0000'),
        )
        resp = admin_client.patch(
            f'{API}/exams/{exam.id}/',
            {'family_id': str(other_family.id)},  # leaves sub_family dangling
            format='json',
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Reactivation
#
# Lifecycle model for every deactivatable reference entity:
#     CREATE → (active) →[deactivate]→ (inactive) →[reactivate]→ (active)
# Both transitions are idempotent — repeating the same action on a record
# already in that state is a 200 no-op and does NOT write a duplicate
# audit entry (so AuditLog counts reflect real lifecycle transitions only).
# ---------------------------------------------------------------------------

class TestFamilyReactivate:

    def test_reactivates_inactive_family(self, admin_client, family):
        family.is_active = False
        family.save()

        resp = admin_client.post(f'{API}/families/{family.id}/reactivate/')
        assert resp.status_code == 200
        family.refresh_from_db()
        assert family.is_active is True

    def test_reactivate_writes_audit(self, admin_client, family):
        family.is_active = False
        family.save()
        before = AuditLog.objects.filter(
            entity_type='ExamFamily', action='REACTIVATE',
        ).count()

        admin_client.post(f'{API}/families/{family.id}/reactivate/')

        after = AuditLog.objects.filter(
            entity_type='ExamFamily', action='REACTIVATE',
        ).count()
        assert after == before + 1

    def test_reactivate_idempotent_when_already_active(self, admin_client, family):
        """Calling reactivate on an already-active record returns 200 but writes no audit."""
        before = AuditLog.objects.filter(
            entity_type='ExamFamily', action='REACTIVATE',
        ).count()

        resp = admin_client.post(f'{API}/families/{family.id}/reactivate/')
        assert resp.status_code == 200

        after = AuditLog.objects.filter(
            entity_type='ExamFamily', action='REACTIVATE',
        ).count()
        assert after == before  # no duplicate transition recorded
        family.refresh_from_db()
        assert family.is_active is True

    def test_reactivate_forbidden_for_non_admin(self, tech_client, family):
        family.is_active = False
        family.save()
        resp = tech_client.post(f'{API}/families/{family.id}/reactivate/')
        assert resp.status_code == 403

    def test_roundtrip_deactivate_then_reactivate(self, admin_client, family):
        d = admin_client.post(f'{API}/families/{family.id}/deactivate/')
        assert d.status_code == 200
        r = admin_client.post(f'{API}/families/{family.id}/reactivate/')
        assert r.status_code == 200
        family.refresh_from_db()
        assert family.is_active is True


class TestSubFamilyReactivate:

    def test_reactivates_inactive_sub_family(self, admin_client, sub_family):
        sub_family.is_active = False
        sub_family.save()

        resp = admin_client.post(f'{API}/sub-families/{sub_family.id}/reactivate/')
        assert resp.status_code == 200
        sub_family.refresh_from_db()
        assert sub_family.is_active is True

    def test_rejects_when_parent_family_inactive(self, admin_client, sub_family):
        """Reactivating a sub-family whose parent is inactive creates a zombie — reject."""
        sub_family.is_active = False
        sub_family.save()
        family = sub_family.family
        family.is_active = False
        family.save()

        resp = admin_client.post(f'{API}/sub-families/{sub_family.id}/reactivate/')
        assert resp.status_code == 400
        sub_family.refresh_from_db()
        assert sub_family.is_active is False  # not reactivated

    def test_reactivate_after_family_is_reactivated(self, admin_client, sub_family):
        """Expected recovery flow: reactivate parent first, then child."""
        sub_family.is_active = False
        sub_family.save()
        family = sub_family.family
        family.is_active = False
        family.save()

        r1 = admin_client.post(f'{API}/families/{family.id}/reactivate/')
        assert r1.status_code == 200
        r2 = admin_client.post(f'{API}/sub-families/{sub_family.id}/reactivate/')
        assert r2.status_code == 200

        sub_family.refresh_from_db()
        assert sub_family.is_active is True

    def test_reactivate_writes_audit(self, admin_client, sub_family):
        sub_family.is_active = False
        sub_family.save()
        before = AuditLog.objects.filter(
            entity_type='ExamSubFamily', action='REACTIVATE',
        ).count()

        admin_client.post(f'{API}/sub-families/{sub_family.id}/reactivate/')

        after = AuditLog.objects.filter(
            entity_type='ExamSubFamily', action='REACTIVATE',
        ).count()
        assert after == before + 1

    def test_reactivate_idempotent_when_already_active(self, admin_client, sub_family):
        resp = admin_client.post(f'{API}/sub-families/{sub_family.id}/reactivate/')
        assert resp.status_code == 200
        sub_family.refresh_from_db()
        assert sub_family.is_active is True

    def test_reactivate_forbidden_for_non_admin(self, tech_client, sub_family):
        sub_family.is_active = False
        sub_family.save()
        resp = tech_client.post(f'{API}/sub-families/{sub_family.id}/reactivate/')
        assert resp.status_code == 403


class TestTubeTypeReactivate:

    def test_reactivates_inactive_tube_type(self, admin_client, tube_type):
        tube_type.is_active = False
        tube_type.save()

        resp = admin_client.post(f'{API}/tube-types/{tube_type.id}/reactivate/')
        assert resp.status_code == 200
        tube_type.refresh_from_db()
        assert tube_type.is_active is True

    def test_reactivate_writes_audit(self, admin_client, tube_type):
        tube_type.is_active = False
        tube_type.save()
        before = AuditLog.objects.filter(
            entity_type='TubeType', action='REACTIVATE',
        ).count()

        admin_client.post(f'{API}/tube-types/{tube_type.id}/reactivate/')

        after = AuditLog.objects.filter(
            entity_type='TubeType', action='REACTIVATE',
        ).count()
        assert after == before + 1

    def test_reactivate_idempotent_when_already_active(self, admin_client, tube_type):
        resp = admin_client.post(f'{API}/tube-types/{tube_type.id}/reactivate/')
        assert resp.status_code == 200
        tube_type.refresh_from_db()
        assert tube_type.is_active is True

    def test_reactivate_forbidden_for_non_admin(self, tech_client, tube_type):
        tube_type.is_active = False
        tube_type.save()
        resp = tech_client.post(f'{API}/tube-types/{tube_type.id}/reactivate/')
        assert resp.status_code == 403


class TestTechniqueReactivate:

    def test_reactivates_inactive_technique(self, admin_client, technique):
        technique.is_active = False
        technique.save()

        resp = admin_client.post(f'{API}/techniques/{technique.id}/reactivate/')
        assert resp.status_code == 200
        technique.refresh_from_db()
        assert technique.is_active is True

    def test_reactivate_writes_audit(self, admin_client, technique):
        technique.is_active = False
        technique.save()
        before = AuditLog.objects.filter(
            entity_type='ExamTechnique', action='REACTIVATE',
        ).count()

        admin_client.post(f'{API}/techniques/{technique.id}/reactivate/')

        after = AuditLog.objects.filter(
            entity_type='ExamTechnique', action='REACTIVATE',
        ).count()
        assert after == before + 1

    def test_reactivate_idempotent_when_already_active(self, admin_client, technique):
        resp = admin_client.post(f'{API}/techniques/{technique.id}/reactivate/')
        assert resp.status_code == 200
        technique.refresh_from_db()
        assert technique.is_active is True

    def test_reactivate_forbidden_for_non_admin(self, tech_client, technique):
        technique.is_active = False
        technique.save()
        resp = tech_client.post(f'{API}/techniques/{technique.id}/reactivate/')
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Exam Definition — Edit (partial update)
#
# Covers the edit lifecycle for exam definitions: price updates, metadata
# updates, immutable code enforcement, FK clears, coherence on update, and
# historical integrity (existing AnalysisRequestItem rows keep their
# snapshotted unit_price when the reference price changes on the parent
# ExamDefinition).
# ---------------------------------------------------------------------------

class TestExamDefinitionUpdate:

    @pytest.fixture()
    def exam(self, family):
        return ExamDefinition.objects.create(
            family=family,
            code='UPD-CBC',
            name='Complete Blood Count',
            sample_type=SampleType.BLOOD,
            unit_price=Decimal('50.0000'),
        )

    def test_update_name(self, admin_client, exam):
        resp = admin_client.patch(
            f'{API}/exams/{exam.id}/',
            {'name': 'Complete Blood Count (Full)'},
            format='json',
        )
        assert resp.status_code == 200, resp.content
        exam.refresh_from_db()
        assert exam.name == 'Complete Blood Count (Full)'

    def test_update_unit_price(self, admin_client, exam):
        resp = admin_client.patch(
            f'{API}/exams/{exam.id}/',
            {'unit_price': '65.0000'},
            format='json',
        )
        assert resp.status_code == 200, resp.content
        exam.refresh_from_db()
        assert exam.unit_price == Decimal('65.0000')

    def test_update_turnaround_hours(self, admin_client, exam):
        resp = admin_client.patch(
            f'{API}/exams/{exam.id}/',
            {'turnaround_hours': 6},
            format='json',
        )
        assert resp.status_code == 200, resp.content
        exam.refresh_from_db()
        assert exam.turnaround_hours == 6

    def test_update_fasting_required(self, admin_client, exam):
        resp = admin_client.patch(
            f'{API}/exams/{exam.id}/',
            {'fasting_required': True},
            format='json',
        )
        assert resp.status_code == 200, resp.content
        exam.refresh_from_db()
        assert exam.fasting_required is True

    def test_update_description(self, admin_client, exam):
        resp = admin_client.patch(
            f'{API}/exams/{exam.id}/',
            {'description': 'Comprehensive hematology panel.'},
            format='json',
        )
        assert resp.status_code == 200, resp.content
        exam.refresh_from_db()
        assert exam.description == 'Comprehensive hematology panel.'

    def test_update_tube_type_and_technique(
        self, admin_client, exam, tube_type, technique,
    ):
        resp = admin_client.patch(
            f'{API}/exams/{exam.id}/',
            {
                'tube_type_id': str(tube_type.id),
                'technique_id': str(technique.id),
            },
            format='json',
        )
        assert resp.status_code == 200, resp.content
        exam.refresh_from_db()
        assert exam.tube_type_id == tube_type.id
        assert exam.technique_id == technique.id

    # -- Immutable code -----------------------------------------------------

    def test_update_rejects_code_change(self, admin_client, exam):
        """
        Any attempt to include ``code`` in the payload is rejected with a
        400 and a field-scoped error, even if the value is different.
        This catches clients that mistakenly think code is editable.
        """
        resp = admin_client.patch(
            f'{API}/exams/{exam.id}/',
            {'code': 'NEW-CODE'},
            format='json',
        )
        assert resp.status_code == 400
        errors = resp.json().get('errors', [])
        assert any(e.get('field') == 'code' for e in errors), errors
        exam.refresh_from_db()
        assert exam.code == 'UPD-CBC'  # unchanged

    def test_update_rejects_code_even_if_same_value(self, admin_client, exam):
        """
        Sending ``code`` with the existing value is still rejected — the
        endpoint has a strict "code is not a PATCH-able field" contract.
        Clients must not even attempt to echo it back.
        """
        resp = admin_client.patch(
            f'{API}/exams/{exam.id}/',
            {'code': 'UPD-CBC'},
            format='json',
        )
        assert resp.status_code == 400

    def test_update_other_fields_alongside_code_rejection_is_atomic(
        self, admin_client, exam,
    ):
        """
        If the payload contains both ``code`` (illegal) and a legal field,
        the whole PATCH must fail and nothing is persisted.
        """
        resp = admin_client.patch(
            f'{API}/exams/{exam.id}/',
            {'code': 'NEW-CODE', 'unit_price': '999.0000'},
            format='json',
        )
        assert resp.status_code == 400
        exam.refresh_from_db()
        assert exam.code == 'UPD-CBC'
        assert exam.unit_price == Decimal('50.0000')  # not changed

    # -- Family / sub-family coherence --------------------------------------

    def test_update_family_and_sub_family_atomically(
        self, admin_client, exam, other_family,
    ):
        new_sub = ExamSubFamily.objects.create(family=other_family, name='Enzymes')
        resp = admin_client.patch(
            f'{API}/exams/{exam.id}/',
            {
                'family_id': str(other_family.id),
                'sub_family_id': str(new_sub.id),
            },
            format='json',
        )
        assert resp.status_code == 200, resp.content
        exam.refresh_from_db()
        assert exam.family_id == other_family.id
        assert exam.sub_family_id == new_sub.id

    def test_update_rejects_mismatched_sub_family(
        self, admin_client, exam, other_family,
    ):
        foreign = ExamSubFamily.objects.create(family=other_family, name='Enzymes')
        resp = admin_client.patch(
            f'{API}/exams/{exam.id}/',
            {'sub_family_id': str(foreign.id)},  # parent = family, not other_family
            format='json',
        )
        assert resp.status_code == 400

    def test_update_clears_sub_family_with_null(self, admin_client, family, sub_family):
        """Explicitly setting sub_family_id to null must actually clear it
        on the instance — not be silently dropped."""
        exam = ExamDefinition.objects.create(
            family=family,
            sub_family=sub_family,
            code='CLR',
            name='Clearable',
            sample_type=SampleType.BLOOD,
            unit_price=Decimal('10.0000'),
        )
        resp = admin_client.patch(
            f'{API}/exams/{exam.id}/',
            {'sub_family_id': None},
            format='json',
        )
        assert resp.status_code == 200, resp.content
        exam.refresh_from_db()
        assert exam.sub_family_id is None

    # -- Audit logging ------------------------------------------------------

    def test_update_writes_audit_log_with_before_after(self, admin_client, exam, lab_admin):
        before_count = AuditLog.objects.filter(
            entity_type='ExamDefinition', action='UPDATE', entity_id=exam.id,
        ).count()

        resp = admin_client.patch(
            f'{API}/exams/{exam.id}/',
            {'unit_price': '75.0000', 'name': 'CBC v2'},
            format='json',
        )
        assert resp.status_code == 200

        entries = AuditLog.objects.filter(
            entity_type='ExamDefinition', action='UPDATE', entity_id=exam.id,
        )
        assert entries.count() == before_count + 1

        entry = entries.order_by('-timestamp').first()
        # Who + when
        assert entry.actor_id == lab_admin.id
        assert entry.actor_email == lab_admin.email
        assert entry.timestamp is not None
        # Which fields + before/after
        diff = entry.diff or {}
        assert 'before' in diff and 'after' in diff
        assert diff['before'].get('unit_price') == '50.0000'
        assert diff['after'].get('unit_price') == '75.0000'
        assert diff['before'].get('name') == 'Complete Blood Count'
        assert diff['after'].get('name') == 'CBC v2'

    def test_update_audit_only_contains_changed_fields(self, admin_client, exam):
        """Untouched columns must not appear in the diff as 'changed'."""
        admin_client.patch(
            f'{API}/exams/{exam.id}/',
            {'name': 'Renamed'},
            format='json',
        )
        entry = AuditLog.objects.filter(
            entity_type='ExamDefinition', action='UPDATE', entity_id=exam.id,
        ).order_by('-timestamp').first()
        diff = entry.diff or {}
        assert set(diff.get('after', {}).keys()) == {'name'}
        assert set(diff.get('before', {}).keys()) == {'name'}

    def test_update_forbidden_for_non_admin(self, tech_client, exam):
        resp = tech_client.patch(
            f'{API}/exams/{exam.id}/',
            {'unit_price': '99.0000'},
            format='json',
        )
        assert resp.status_code == 403
        exam.refresh_from_db()
        assert exam.unit_price == Decimal('50.0000')

    # -- Historical integrity on reference price changes --------------------

    def test_update_unit_price_does_not_affect_existing_request_items(
        self, admin_client, exam, lab_admin,
    ):
        """
        The load-bearing guarantee: existing AnalysisRequestItem rows keep
        their frozen unit_price even after the parent ExamDefinition's
        reference price is updated. The data model enforces this via a
        denormalised column, but we lock the behaviour in with a test so
        a future well-meaning refactor can't silently break it.
        """
        from datetime import date
        from apps.patients.models import Patient
        from apps.requests.models import AnalysisRequest, AnalysisRequestItem

        patient = Patient.objects.create(
            document_number='TST-HIST-001',
            first_name='Historical',
            last_name='Preservation',
            date_of_birth=date(1990, 1, 1),
            gender='MALE',
        )
        req = AnalysisRequest.objects.create(
            patient=patient,
            created_by=lab_admin,
        )
        item = AnalysisRequestItem.objects.create(
            analysis_request=req,
            exam_definition=exam,
            unit_price=Decimal('50.0000'),
            billed_price=Decimal('50.0000'),
        )

        # Change the reference price via the admin endpoint.
        resp = admin_client.patch(
            f'{API}/exams/{exam.id}/',
            {'unit_price': '200.0000'},
            format='json',
        )
        assert resp.status_code == 200

        # Parent exam is updated...
        exam.refresh_from_db()
        assert exam.unit_price == Decimal('200.0000')

        # ...but the historical request item keeps its snapshot.
        item.refresh_from_db()
        assert item.unit_price == Decimal('50.0000')
        assert item.billed_price == Decimal('50.0000')
