"""
Tests for the dashboard analytics endpoint (ranked insights).

Coverage:
  - HTTP route resolves and returns the documented top-level shape
  - top_exams reflects items grouped by exam, ordered desc, capped at 5
  - top_partners aggregates billed amounts at the item level + counts
    distinct parent requests for the Volume toggle
  - abnormal_exams returns count + total per exam, ordered by abnormal
    count desc
  - tenant isolation is handled by the autouse ``_in_tenant_schema``
    fixture (no cross-tenant data leakage possible)

Exercised through DRF APIClient so URL conf, permission gate, and view
logic are validated together.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.catalog.models import ExamCategory, ExamDefinition, SampleType
from apps.partners.models import OrganizationType
from apps.partners.services import PartnerOrganizationService
from apps.patients.models import Patient
from apps.requests.models import (
    AnalysisRequest,
    BillingMode,
    SourceType,
)
from apps.requests.services import AnalysisRequestService
from apps.results.models import ResultStatus, ResultVersion


@pytest.fixture(autouse=True)
def _usable_subscription(_test_tenant_schema, django_db_blocker):
    """Same pattern as the cockpit suite — middleware needs a usable sub."""
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
    resp = _client(user).get('/api/v1/dashboard/analytics/', HTTP_HOST='testlab.localhost')
    assert resp.status_code == 200, resp.content
    body = resp.json()
    return body.get('data', body)


@pytest.fixture()
def analytics_data(lab_admin, make_request, default_technique):
    """
    Builds a realistic ranked dataset:
      - 2 partners + 1 direct patient
      - 3 exams: CBC, GLU, CREA (CBC most-requested, GLU has abnormals)
      - Items priced via PricingRule (billed_price=150 per item)
    """
    patient = Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='AN-NID-001',
        first_name='Jane', last_name='Analytics',
        date_of_birth='1985-06-15', gender='FEMALE',
        created_by=lab_admin,
    )

    partner_a = PartnerOrganizationService.create(
        validated_data={
            'code': 'AN-A', 'name': 'Analytics Clinic A',
            'organization_type': OrganizationType.CLINIC,
        },
        created_by=lab_admin, request=make_request(lab_admin),
    )
    partner_b = PartnerOrganizationService.create(
        validated_data={
            'code': 'AN-B', 'name': 'Analytics Hospital B',
            'organization_type': OrganizationType.HOSPITAL,
        },
        created_by=lab_admin, request=make_request(lab_admin),
    )

    cat = ExamCategory.objects.create(name='Analytics Cat', display_order=1)
    exams = {}
    # Alias → (db code, display name). Aliases are short for readability below;
    # display names are what the API returns.
    exam_specs = [('CBC', 'AN-CBC', 'CBC'),
                  ('GLU', 'AN-GLU', 'Glucose'),
                  ('CREA', 'AN-CREA', 'Creatinine')]
    for alias, code, name in exam_specs:
        exams[alias] = ExamDefinition.objects.create(
            category=cat, technique=default_technique,
            code=code, name=name, sample_type=SampleType.BLOOD,
            unit_price=Decimal('150.0000'),
        )

    req = make_request(lab_admin)

    def _create(source, partner, exam_names):
        kwargs = {
            'patient_id': patient.id,
            'source_type': source,
            'billing_mode': (BillingMode.PARTNER_BILLING if partner
                             else BillingMode.DIRECT_PAYMENT),
            'items': [{'exam_definition_id': exams[n].id} for n in exam_names],
        }
        if partner:
            kwargs['partner_organization_id'] = partner.id
        ar = AnalysisRequestService.create(
            validated_data=kwargs, created_by=lab_admin, request=req,
        )
        AnalysisRequestService.confirm(
            analysis_request=ar, confirmed_by=lab_admin, request=req,
        )
        return ar

    # CBC requested 4×, GLU 3×, CREA 1× → ranking is CBC > GLU > CREA
    _create(SourceType.PARTNER_ORGANIZATION, partner_a, ['CBC', 'GLU'])         # A: 1+1
    _create(SourceType.PARTNER_ORGANIZATION, partner_a, ['CBC', 'GLU', 'CREA']) # A: 1+1+1 → 5 items
    _create(SourceType.PARTNER_ORGANIZATION, partner_b, ['CBC', 'GLU'])         # B: 1+1
    _create(SourceType.DIRECT_PATIENT,        None,    ['CBC'])                 # direct

    # Publish 3 GLU result versions, 2 abnormal; 2 CBC versions, 0 abnormal.
    glu_items = list(
        AnalysisRequest.objects
        .filter(items__exam_definition__code='AN-GLU')
        .values_list('items__id', flat=True)[:3]
    )
    cbc_items = list(
        AnalysisRequest.objects
        .filter(items__exam_definition__code='AN-CBC')
        .values_list('items__id', flat=True)[:2]
    )
    now = timezone.now()
    # Glucose: 3 validated versions, 2 abnormal. Status is VALIDATED (not
    # PUBLISHED) because that is the realistic state right after biologist
    # validation but before the request's final PDF report is generated —
    # the bug the analytics card was missing.
    for i, item_id in enumerate(glu_items):
        ResultVersion.objects.create(
            item_id=item_id, version_number=1, is_current=True,
            status=ResultStatus.VALIDATED,
            result_value=str(120 + i), is_abnormal=(i < 2),
            entered_by=lab_admin, entered_at=now,
            validated_by=lab_admin, validated_at=now,
        )
    # CBC: 2 validated versions, 0 abnormal — used to prove non-abnormal
    # results contribute to ``total`` but not to ``count``.
    for item_id in cbc_items:
        ResultVersion.objects.create(
            item_id=item_id, version_number=1, is_current=True,
            status=ResultStatus.VALIDATED,
            result_value='normal', is_abnormal=False,
            entered_by=lab_admin, entered_at=now,
            validated_by=lab_admin, validated_at=now,
        )

    return {
        'partner_a': partner_a, 'partner_b': partner_b,
        'exams': exams,
    }


@pytest.mark.django_db(transaction=True)
class TestAnalyticsShape:

    def test_top_level_keys(self, analytics_data, lab_admin):
        body = _get(lab_admin)
        assert set(body.keys()) == {'top_exams', 'top_partners', 'abnormal_exams'}

    def test_lists_capped_at_five(self, analytics_data, lab_admin):
        body = _get(lab_admin)
        assert len(body['top_exams']) <= 5
        assert len(body['top_partners']) <= 5
        assert len(body['abnormal_exams']) <= 5


@pytest.mark.django_db(transaction=True)
class TestTopExams:

    def test_ranking_order(self, analytics_data, lab_admin):
        body = _get(lab_admin)
        names = [row['name'] for row in body['top_exams']]
        # CBC requested 4×, GLU 3×, CREA 1× → CBC first, GLU second, CREA last
        assert names[0] == 'CBC'
        assert names[1] == 'Glucose'
        assert names[-1] == 'Creatinine'

    def test_counts_match_items(self, analytics_data, lab_admin):
        rows = {r['name']: r['count'] for r in _get(lab_admin)['top_exams']}
        assert rows['CBC'] == 4
        assert rows['Glucose'] == 3
        assert rows['Creatinine'] == 1

    def test_includes_code_and_name(self, analytics_data, lab_admin):
        for row in _get(lab_admin)['top_exams']:
            assert {'code', 'name', 'count'} <= set(row.keys())


@pytest.mark.django_db(transaction=True)
class TestTopPartners:

    def test_only_partner_sourced_appear(self, analytics_data, lab_admin):
        names = [row['name'] for row in _get(lab_admin)['top_partners']]
        assert 'Analytics Clinic A' in names
        assert 'Analytics Hospital B' in names
        # Direct patient requests are excluded
        assert all('Direct' not in n for n in names)

    def test_partner_a_ranks_above_b(self, analytics_data, lab_admin):
        # A has 5 items @ 150, B has 2 items @ 150 → A first
        rows = _get(lab_admin)['top_partners']
        assert rows[0]['name'] == 'Analytics Clinic A'

    def test_amount_is_string_and_sums_billed_price(self, analytics_data, lab_admin):
        rows = {r['name']: r for r in _get(lab_admin)['top_partners']}
        a = rows['Analytics Clinic A']
        # Decimal serialised as string (see view docstring)
        assert isinstance(a['amount'], str)
        # 5 items × 150 = 750
        assert Decimal(a['amount']) == Decimal('750.0000')

    def test_requests_count_is_distinct_parents(self, analytics_data, lab_admin):
        rows = {r['name']: r for r in _get(lab_admin)['top_partners']}
        # A has 2 distinct AnalysisRequests (5 items split across them)
        assert rows['Analytics Clinic A']['requests'] == 2
        assert rows['Analytics Hospital B']['requests'] == 1


@pytest.mark.django_db(transaction=True)
class TestAbnormalExams:

    def test_only_exams_with_abnormals_appear(self, analytics_data, lab_admin):
        names = [r['name'] for r in _get(lab_admin)['abnormal_exams']]
        assert 'Glucose' in names
        # CBC has results but none abnormal
        assert 'CBC' not in names
        # CREA has no validated results at all
        assert 'Creatinine' not in names

    def test_counts_and_totals(self, analytics_data, lab_admin):
        glu = next(r for r in _get(lab_admin)['abnormal_exams'] if r['name'] == 'Glucose')
        # 2 abnormal out of 3 validated versions
        assert glu['count'] == 2
        assert glu['total'] == 3

    def test_shape(self, analytics_data, lab_admin):
        for row in _get(lab_admin)['abnormal_exams']:
            assert {'code', 'name', 'count', 'total'} <= set(row.keys())

    def test_validated_status_is_counted_even_without_publication(
        self, analytics_data, lab_admin,
    ):
        """Regression: the original query filtered ``status=PUBLISHED`` only,
        which hid every result the biologist had validated but whose
        request's final PDF had not yet been generated. The fixture above
        creates rows with status=VALIDATED (not PUBLISHED) — they must
        appear in the response."""
        # Confirm none of the fixture rows are PUBLISHED — otherwise the
        # regression coverage is fake.
        from apps.results.models import ResultVersion
        assert ResultVersion.objects.filter(status=ResultStatus.PUBLISHED).count() == 0
        assert ResultVersion.objects.filter(status=ResultStatus.VALIDATED).count() > 0

        glu = next(
            (r for r in _get(lab_admin)['abnormal_exams'] if r['name'] == 'Glucose'),
            None,
        )
        assert glu is not None, 'Validated abnormal results must surface'
        assert glu['count'] == 2

    def test_published_status_is_also_counted(
        self, analytics_data, lab_admin, default_technique,
    ):
        """``PUBLISHED`` is a downstream of ``VALIDATED`` — both must
        contribute. This adds one more abnormal version in PUBLISHED state
        and re-checks the count."""
        from django.utils import timezone
        from apps.requests.models import AnalysisRequestItem
        from apps.results.models import ResultVersion

        # Find an as-yet-resultless CREA item so we don't collide with the
        # existing GLU/CBC current-version constraints.
        crea_item = AnalysisRequestItem.objects.filter(
            exam_definition__code='AN-CREA',
            result_versions__isnull=True,
        ).first()
        assert crea_item is not None
        now = timezone.now()
        ResultVersion.objects.create(
            item=crea_item, version_number=1, is_current=True,
            status=ResultStatus.PUBLISHED,
            result_value='999', is_abnormal=True,
            entered_by=lab_admin, entered_at=now,
            validated_by=lab_admin, validated_at=now,
            published_at=now,
        )
        crea = next(
            (r for r in _get(lab_admin)['abnormal_exams'] if r['name'] == 'Creatinine'),
            None,
        )
        assert crea is not None
        assert crea['count'] == 1
        assert crea['total'] == 1

    def test_period_filter_excludes_old_validations(
        self, analytics_data, lab_admin,
    ):
        """Versions validated before the start of the current month must
        not be counted. We backdate one of the existing fixture rows and
        confirm it drops out of the abnormal count."""
        from datetime import timedelta
        from apps.results.models import ResultVersion

        glu_versions = list(
            ResultVersion.objects
            .filter(item__exam_definition__code='AN-GLU', is_abnormal=True)
            .order_by('id')
        )
        assert len(glu_versions) >= 2  # sanity

        # Move one abnormal GLU version 60 days back — outside this month.
        old = glu_versions[0]
        old.validated_at = old.validated_at - timedelta(days=60)
        old.save(update_fields=['validated_at'])

        glu = next(r for r in _get(lab_admin)['abnormal_exams'] if r['name'] == 'Glucose')
        # Was 2 abnormal; the backdated one should now be excluded.
        assert glu['count'] == 1
        # Total likewise drops by one (the backdated row was validated too).
        assert glu['total'] == 2
