"""
Result-list worklist-payload tests.

Pins the API contract the request-oriented frontend Results page
depends on:

  - Every row carries ``request_id`` (UUID), ``request_number``,
    ``request_public_reference``, ``patient_id`` and
    ``patient_display_name`` ("first last" composition).
  - The list endpoint stays at GET /api/v1/results/ — no new URL
    introduced. Existing fields stay backward-compatible.
  - The ``date_from`` / ``date_to`` filters narrow by submitted_at
    (with a created_at fallback for rows that never reached
    SUBMITTED) and accept ISO ``YYYY-MM-DD`` values.
  - The ``search`` filter matches the patient's first or last name
    in addition to the pre-existing exam/request matches.
  - Sensitive patient fields (DOB, document number, email, phone)
    are NEVER exposed on the worklist rows.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework.test import APIClient

from apps.catalog.models import ExamDefinition, ExamFamily, SampleType
from apps.patients.models import Patient
from apps.requests.models import SourceType
from apps.requests.services import (
    AnalysisRequestItemService, AnalysisRequestService,
)
from apps.results.services import ResultVersionService


API_RESULTS = '/api/v1/results/'


# ---------------------------------------------------------------------------
# Subscription seed (same shape as the other request/result test files)
# ---------------------------------------------------------------------------

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
                tenant=tenant, status=SubscriptionStatus.TRIAL,
                defaults={'plan': plan},
            )
    yield


@pytest.fixture()
def api_client(lab_admin):
    c = APIClient(HTTP_HOST='testlab.localhost')
    c.force_authenticate(user=lab_admin)
    return c


@pytest.fixture()
def family():
    return ExamFamily.objects.create(name='Worklist Family', display_order=1)


@pytest.fixture()
def exam(family, default_technique):
    return ExamDefinition.objects.create(
        family=family, technique=default_technique,
        code='WL-A', name='Worklist Exam A',
        sample_type=SampleType.BLOOD,
    )


@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-WORKLIST-001',
        first_name='Sophie', last_name='Worklist',
        date_of_birth=date(1990, 1, 2), gender='FEMALE',
        email='sophie.worklist@example.com',
        phone='+225 0000 0000',
        created_by=lab_admin,
    )


def _build_submitted_version(
    *, patient, exam, lab_admin, technician, make_request,
):
    ar = AnalysisRequestService.create(
        validated_data={
            'patient_id': patient.id,
            'source_type': SourceType.DIRECT_PATIENT,
            'items': [{'exam_definition_id': exam.id}],
        },
        created_by=lab_admin,
        request=make_request(lab_admin),
        confirm_after=True,
    )
    item = ar.items.get()
    AnalysisRequestItemService.mark_collected(
        item=item, collected_by=technician,
        request=make_request(technician),
    )
    v = ResultVersionService.create_draft(
        item=item, entered_by=technician,
        request=make_request(technician),
        result_value='5',
        values=[{'value': '5', 'is_abnormal': False}],
    )
    ResultVersionService.submit(
        version=v, submitted_by=technician,
        request=make_request(technician),
    )
    return ar, item.result_versions.get(is_current=True)


# ===========================================================================
# Payload shape
# ===========================================================================

@pytest.mark.django_db(transaction=True)
class TestWorklistPayload:

    def test_row_includes_request_and_patient_fields(
        self, api_client, patient, exam, lab_admin, technician, make_request,
    ):
        ar, v = _build_submitted_version(
            patient=patient, exam=exam,
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )

        resp = api_client.get(API_RESULTS)
        assert resp.status_code == 200
        rows = resp.json()['data']
        # Find the row tied to our request — the trial subscription
        # may seed unrelated rows in some setups, so be specific.
        row = next(r for r in rows if r['id'] == str(v.id))

        assert row['request_id'] == str(ar.id)
        assert row['request_number'] == ar.request_number
        assert row['request_public_reference'] == (ar.public_reference or '')
        assert row['patient_id'] == str(patient.id)
        assert row['patient_display_name'] == 'Sophie Worklist'

    def test_row_omits_sensitive_patient_fields(
        self, api_client, patient, exam, lab_admin, technician, make_request,
    ):
        _ar, v = _build_submitted_version(
            patient=patient, exam=exam,
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        resp = api_client.get(API_RESULTS)
        row = next(r for r in resp.json()['data'] if r['id'] == str(v.id))

        # Patient name is fine; DOB / doc number / email / phone
        # must not leak via the worklist payload.
        leaked = {'date_of_birth', 'document_number', 'email', 'phone'}
        assert not (leaked & set(row.keys())), (
            f'Worklist row leaked sensitive patient fields: '
            f'{leaked & set(row.keys())}'
        )

    def test_existing_fields_remain_for_backward_compat(
        self, api_client, patient, exam, lab_admin, technician, make_request,
    ):
        _ar, v = _build_submitted_version(
            patient=patient, exam=exam,
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        resp = api_client.get(API_RESULTS)
        row = next(r for r in resp.json()['data'] if r['id'] == str(v.id))

        # Fields the old UI / external integrations rely on.
        for f in (
            'id', 'item_id', 'exam_code', 'exam_name',
            'request_number', 'version_number', 'is_current',
            'status', 'is_abnormal', 'result_value', 'result_unit',
            'entered_by_email', 'entered_at',
            'submitted_at', 'validated_at', 'published_at',
            'files_count', 'created_at',
        ):
            assert f in row, f'Backward-compat field "{f}" disappeared from row'


# ===========================================================================
# Date filter (submitted_at with created_at fallback)
# ===========================================================================

@pytest.mark.django_db(transaction=True)
class TestWorklistDateFilters:

    def test_date_from_filters_by_submitted_at(
        self, api_client, patient, exam, lab_admin, technician, make_request,
    ):
        _ar, v = _build_submitted_version(
            patient=patient, exam=exam,
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        # Force submitted_at to 30 days ago — outside the
        # "last 7 days" window we'll query.
        v.submitted_at = timezone.now() - timedelta(days=30)
        v.save(update_fields=['submitted_at', 'updated_at'])

        cutoff = (date.today() - timedelta(days=7)).isoformat()
        resp = api_client.get(API_RESULTS, {'date_from': cutoff})
        ids = {r['id'] for r in resp.json()['data']}
        assert str(v.id) not in ids

    def test_date_to_filters_by_submitted_at(
        self, api_client, patient, exam, lab_admin, technician, make_request,
    ):
        _ar, v = _build_submitted_version(
            patient=patient, exam=exam,
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        # Submitted today (default). A date_to set yesterday must
        # exclude this row.
        cutoff = (date.today() - timedelta(days=1)).isoformat()
        resp = api_client.get(API_RESULTS, {'date_to': cutoff})
        ids = {r['id'] for r in resp.json()['data']}
        assert str(v.id) not in ids

    def test_default_current_month_range_includes_today_submission(
        self, api_client, patient, exam, lab_admin, technician, make_request,
    ):
        # The frontend uses 1st-of-month → today as its default
        # range; assert the backend honours it for a row submitted
        # today.
        _ar, v = _build_submitted_version(
            patient=patient, exam=exam,
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        today_d = date.today()
        first_of_month = today_d.replace(day=1).isoformat()
        resp = api_client.get(API_RESULTS, {
            'date_from': first_of_month,
            'date_to': today_d.isoformat(),
        })
        ids = {r['id'] for r in resp.json()['data']}
        assert str(v.id) in ids

    def test_draft_row_falls_back_to_created_at_window(
        self, api_client, patient, exam, lab_admin, technician, make_request,
    ):
        # Create a draft that NEVER reaches submitted_at; the
        # date_from filter must still find it via created_at.
        ar = AnalysisRequestService.create(
            validated_data={
                'patient_id': patient.id,
                'source_type': SourceType.DIRECT_PATIENT,
                'items': [{'exam_definition_id': exam.id}],
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
            confirm_after=True,
        )
        item = ar.items.get()
        AnalysisRequestItemService.mark_collected(
            item=item, collected_by=technician,
            request=make_request(technician),
        )
        v = ResultVersionService.create_draft(
            item=item, entered_by=technician,
            request=make_request(technician),
            result_value='3',
            values=[{'value': '3', 'is_abnormal': False}],
        )
        assert v.submitted_at is None

        today_d = date.today()
        resp = api_client.get(API_RESULTS, {
            'date_from': today_d.isoformat(),
            'date_to': today_d.isoformat(),
        })
        ids = {r['id'] for r in resp.json()['data']}
        assert str(v.id) in ids


# ===========================================================================
# Search by patient name
# ===========================================================================

@pytest.mark.django_db(transaction=True)
class TestWorklistSearch:

    def test_search_matches_patient_first_name(
        self, api_client, patient, exam, lab_admin, technician, make_request,
    ):
        _ar, v = _build_submitted_version(
            patient=patient, exam=exam,
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        resp = api_client.get(API_RESULTS, {'search': 'Sophie'})
        ids = {r['id'] for r in resp.json()['data']}
        assert str(v.id) in ids

    def test_search_matches_patient_last_name(
        self, api_client, patient, exam, lab_admin, technician, make_request,
    ):
        _ar, v = _build_submitted_version(
            patient=patient, exam=exam,
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        resp = api_client.get(API_RESULTS, {'search': 'Worklist'})
        ids = {r['id'] for r in resp.json()['data']}
        assert str(v.id) in ids

    def test_search_unrelated_term_excludes_the_row(
        self, api_client, patient, exam, lab_admin, technician, make_request,
    ):
        _ar, v = _build_submitted_version(
            patient=patient, exam=exam,
            lab_admin=lab_admin, technician=technician,
            make_request=make_request,
        )
        resp = api_client.get(API_RESULTS, {'search': 'NoMatchHere-ZZZ'})
        ids = {r['id'] for r in resp.json()['data']}
        assert str(v.id) not in ids
