"""
Tests for the closure-status lifecycle, decoupled from workflow status.

Coverage:
  - notification persistence on the AnalysisRequest row
  - VALIDATED + closure=OPEN → closure=DELIVERED on email success
    (workflow status NEVER changes — billing safety)
  - manual mark-delivered / archive set closure_status only
  - default list excludes DELIVERED + ARCHIVED via the lifecycle filter
  - lifecycle=delivered / archived / all surface the right buckets
  - status= filter only filters workflow values (no DELIVERED/ARCHIVED choice)
  - patient_summary surfaced on detail
  - closure_status surfaced on list + detail
  - billing-safety: a delivered/archived request with workflow VALIDATED
    is still picked up by the partner-billing query

Reuses the autouse `_in_tenant_schema` fixture so every write hits the
test tenant schema.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context

from apps.catalog.models import (
    ExamCategory, ExamDefinition, ExamFamily, ResultStructure, SampleType,
)
from apps.lab_settings.models import LabSettings
from apps.patients.models import Patient
from apps.requests.models import (
    AnalysisRequest, ClosureStatus, RequestStatus, SourceType,
)
from apps.requests.notification_service import RequestNotificationService
from apps.requests.report_service import RequestReportService
from apps.requests.services import (
    AnalysisRequestItemService, AnalysisRequestService,
)
from apps.results.services import ResultVersionService
from common.email import EmailService
from common.email.providers.base import EmailMessage, EmailResult


pytestmark = pytest.mark.no_auto_labels


# ---------------------------------------------------------------------------
# Fixtures
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
                tenant=tenant,
                status=SubscriptionStatus.TRIAL,
                defaults={'plan': plan},
            )
    yield


@pytest.fixture(autouse=True)
def _lab_defaults():
    lab = LabSettings.get_solo()
    lab.result_pdf_password_enabled = False
    lab.notification_enable_email = True
    lab.lab_name = 'Lifecycle Lab'
    lab.save(update_fields=[
        'result_pdf_password_enabled',
        'notification_enable_email',
        'lab_name',
        'updated_at',
    ])


@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD', document_number='NID-LIFE-001',
        first_name='Charlie', last_name='Lifecycle',
        date_of_birth=date(1985, 3, 21), gender='MALE',
        phone='12345678',
        email='charlie@example.com',
        created_by=lab_admin,
    )


@pytest.fixture()
def exam(default_technique):
    cat = ExamCategory.objects.create(name='C', display_order=1)
    fam = ExamFamily.objects.create(name='F', display_order=1)
    return ExamDefinition.objects.create(
        category=cat, family=fam, technique=default_technique,
        code='LIFE', name='LifecycleTest',
        sample_type=SampleType.BLOOD,
        result_structure=ResultStructure.SINGLE_VALUE,
        unit='u', reference_range='0-100',
        unit_price=Decimal('10'),
    )


@pytest.fixture
def email_capture(monkeypatch):
    captured: list[EmailMessage] = []

    class _Stub:
        name = 'stub'
        def send(self, message):  # noqa: D401
            captured.append(message)
            return EmailResult(ok=True)

    monkeypatch.setattr(
        'apps.requests.notification_service.get_email_service',
        lambda: EmailService(provider=_Stub()),
    )
    return captured


def _finalize_without_report(patient, lab_admin, technician, biologist, make_request, exam):
    """Same shape as :func:`_finalize_with_report` but stops just short of
    generating the report PDF. Used to exercise the report-required gate
    on closure transitions."""
    from apps.requests.label_service import RequestLabelService
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
    RequestLabelService.generate_or_get(ar, lab_admin, make_request(lab_admin))
    req_t = make_request(technician)
    req_b = make_request(biologist)
    for item in ar.items.all():
        AnalysisRequestItemService.mark_collected(item=item, collected_by=technician, request=req_t)
    for item in ar.items.select_related('exam_definition').all():
        item.refresh_from_db()
        v = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req_t,
            result_value='42', values=[{'value': '42', 'is_abnormal': False}], comments='',
        )
        ResultVersionService.submit(version=v, submitted_by=technician, request=req_t)
        v = item.result_versions.get(is_current=True)
        ResultVersionService.validate(version=v, validation_notes='OK', validated_by=biologist, request=req_b)
    ar.refresh_from_db()
    AnalysisRequestService.finalize_validation(analysis_request=ar, finalized_by=biologist, request=req_b)
    ar.refresh_from_db()
    # Deliberately skip RequestReportService.generate_or_get here.
    return ar


def _finalize_with_report(patient, lab_admin, technician, biologist, make_request, exam):
    from apps.requests.label_service import RequestLabelService
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
    RequestLabelService.generate_or_get(ar, lab_admin, make_request(lab_admin))
    req_t = make_request(technician)
    req_b = make_request(biologist)
    for item in ar.items.all():
        AnalysisRequestItemService.mark_collected(item=item, collected_by=technician, request=req_t)
    for item in ar.items.select_related('exam_definition').all():
        item.refresh_from_db()
        v = ResultVersionService.create_draft(
            item=item, entered_by=technician, request=req_t,
            result_value='42', values=[{'value': '42', 'is_abnormal': False}], comments='',
        )
        ResultVersionService.submit(version=v, submitted_by=technician, request=req_t)
        v = item.result_versions.get(is_current=True)
        ResultVersionService.validate(version=v, validation_notes='OK', validated_by=biologist, request=req_b)
    ar.refresh_from_db()
    AnalysisRequestService.finalize_validation(analysis_request=ar, finalized_by=biologist, request=req_b)
    ar.refresh_from_db()
    RequestReportService.generate_or_get(ar, biologist, make_request(biologist))
    ar.refresh_from_db()
    return ar


# ---------------------------------------------------------------------------
# Notification persistence + auto-deliver (closure_status only)
# ---------------------------------------------------------------------------

class TestNotificationPersistence:

    def test_email_send_stamps_notification_fields(
        self, patient, exam, lab_admin, technician, biologist, make_request, email_capture,
    ):
        ar = _finalize_with_report(patient, lab_admin, technician, biologist, make_request, exam)
        assert ar.status == RequestStatus.VALIDATED
        assert ar.closure_status == ClosureStatus.OPEN
        assert ar.notified_by_email_at is None

        RequestNotificationService.notify_patient(ar, make_request(lab_admin))
        ar.refresh_from_db()

        assert ar.notified_by_email_at is not None
        assert ar.notified_by_email_by_id == lab_admin.id
        assert ar.notification_count == 1
        assert ar.last_patient_notification_channel == 'EMAIL'

    def test_validated_request_auto_advances_closure_to_delivered(
        self, patient, exam, lab_admin, technician, biologist, make_request, email_capture,
    ):
        ar = _finalize_with_report(patient, lab_admin, technician, biologist, make_request, exam)
        assert ar.status == RequestStatus.VALIDATED
        assert ar.closure_status == ClosureStatus.OPEN

        RequestNotificationService.notify_patient(ar, make_request(lab_admin))
        ar.refresh_from_db()

        # Closure flipped to DELIVERED, but workflow status untouched →
        # billing's `status=VALIDATED` query still picks the row up.
        assert ar.closure_status == ClosureStatus.DELIVERED
        assert ar.status == RequestStatus.VALIDATED
        assert ar.delivered_at is not None
        assert ar.delivered_by_id == lab_admin.id

    def test_resend_does_not_revert_closure_or_status(
        self, patient, exam, lab_admin, technician, biologist, make_request, email_capture,
    ):
        ar = _finalize_with_report(patient, lab_admin, technician, biologist, make_request, exam)
        RequestNotificationService.notify_patient(ar, make_request(lab_admin))
        RequestNotificationService.notify_patient(ar, make_request(lab_admin))
        ar.refresh_from_db()
        assert ar.notification_count == 2
        assert ar.closure_status == ClosureStatus.DELIVERED
        assert ar.status == RequestStatus.VALIDATED

    def test_notify_does_not_reopen_archived_request(
        self, patient, exam, lab_admin, technician, biologist, make_request, email_capture,
    ):
        ar = _finalize_with_report(patient, lab_admin, technician, biologist, make_request, exam)
        # Archive first.
        AnalysisRequestService.archive(
            analysis_request=ar, actor=lab_admin, request=make_request(lab_admin),
        )
        # Now notify — closure must stay ARCHIVED.
        RequestNotificationService.notify_patient(ar, make_request(lab_admin))
        ar.refresh_from_db()
        assert ar.closure_status == ClosureStatus.ARCHIVED
        assert ar.status == RequestStatus.VALIDATED


# ---------------------------------------------------------------------------
# Manual closure transitions
# ---------------------------------------------------------------------------

class TestManualClosureTransitions:

    def test_mark_delivered_does_not_change_workflow_status(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_with_report(patient, lab_admin, technician, biologist, make_request, exam)
        ar = AnalysisRequestService.mark_delivered(
            analysis_request=ar, actor=lab_admin, request=make_request(lab_admin),
        )
        assert ar.closure_status == ClosureStatus.DELIVERED
        assert ar.status == RequestStatus.VALIDATED  # untouched
        assert ar.delivered_at is not None

    def test_mark_delivered_idempotent(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_with_report(patient, lab_admin, technician, biologist, make_request, exam)
        ar = AnalysisRequestService.mark_delivered(
            analysis_request=ar, actor=lab_admin, request=make_request(lab_admin),
        )
        first_at = ar.delivered_at
        ar = AnalysisRequestService.mark_delivered(
            analysis_request=ar, actor=lab_admin, request=make_request(lab_admin),
        )
        assert ar.delivered_at == first_at

    def test_archive_does_not_change_workflow_status(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_with_report(patient, lab_admin, technician, biologist, make_request, exam)
        ar = AnalysisRequestService.mark_delivered(
            analysis_request=ar, actor=lab_admin, request=make_request(lab_admin),
        )
        ar = AnalysisRequestService.archive(
            analysis_request=ar, actor=lab_admin, request=make_request(lab_admin),
        )
        assert ar.closure_status == ClosureStatus.ARCHIVED
        assert ar.status == RequestStatus.VALIDATED  # untouched
        assert ar.archived_at is not None

    def test_cannot_deliver_an_archived_request(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        from rest_framework.exceptions import ValidationError
        ar = _finalize_with_report(patient, lab_admin, technician, biologist, make_request, exam)
        AnalysisRequestService.archive(
            analysis_request=ar, actor=lab_admin, request=make_request(lab_admin),
        )
        with pytest.raises(ValidationError):
            AnalysisRequestService.mark_delivered(
                analysis_request=ar, actor=lab_admin, request=make_request(lab_admin),
            )

    # -- Report-required gate on closure ---------------------------------

    def test_mark_delivered_rejected_when_no_report(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        from rest_framework.exceptions import ValidationError
        ar = _finalize_without_report(patient, lab_admin, technician, biologist, make_request, exam)
        with pytest.raises(ValidationError, match='Generate the report'):
            AnalysisRequestService.mark_delivered(
                analysis_request=ar, actor=lab_admin, request=make_request(lab_admin),
            )
        ar.refresh_from_db()
        assert ar.closure_status == ClosureStatus.OPEN
        assert ar.delivered_at is None

    def test_archive_rejected_when_no_report(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        from rest_framework.exceptions import ValidationError
        ar = _finalize_without_report(patient, lab_admin, technician, biologist, make_request, exam)
        with pytest.raises(ValidationError, match='Generate the report'):
            AnalysisRequestService.archive(
                analysis_request=ar, actor=lab_admin, request=make_request(lab_admin),
            )
        ar.refresh_from_db()
        assert ar.closure_status == ClosureStatus.OPEN
        assert ar.archived_at is None

    def test_mark_delivered_succeeds_with_report(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_with_report(patient, lab_admin, technician, biologist, make_request, exam)
        ar = AnalysisRequestService.mark_delivered(
            analysis_request=ar, actor=lab_admin, request=make_request(lab_admin),
        )
        assert ar.closure_status == ClosureStatus.DELIVERED

    def test_archive_succeeds_with_report(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_with_report(patient, lab_admin, technician, biologist, make_request, exam)
        ar = AnalysisRequestService.archive(
            analysis_request=ar, actor=lab_admin, request=make_request(lab_admin),
        )
        assert ar.closure_status == ClosureStatus.ARCHIVED


# ---------------------------------------------------------------------------
# Lifecycle filter on the request list
# ---------------------------------------------------------------------------

class TestLifecycleFilter:
    """Drives the FilterSet directly for speed; the HTTP-level wiring is
    additionally exercised by ``TestArchivedRetrievalAndListing`` below."""

    def _make_rows(self, patient, lab_admin):
        # Two rows per closure value across mixed workflow statuses.
        # request_number / public_reference are unique-constrained.
        rows = [
            (RequestStatus.DRAFT,     ClosureStatus.OPEN),
            (RequestStatus.VALIDATED, ClosureStatus.OPEN),
            (RequestStatus.VALIDATED, ClosureStatus.DELIVERED),
            (RequestStatus.COMPLETED, ClosureStatus.DELIVERED),
            (RequestStatus.VALIDATED, ClosureStatus.ARCHIVED),
            (RequestStatus.CANCELLED, ClosureStatus.ARCHIVED),
        ]
        for i, (status, closure) in enumerate(rows):
            AnalysisRequest.objects.create(
                patient=patient,
                source_type=SourceType.DIRECT_PATIENT,
                status=status,
                closure_status=closure,
                created_by=lab_admin,
                request_number=f'REQ-LIFE-{i:04d}',
                public_reference=f'PUB-LIFE-{i:04d}',
            )

    def _filtered(self, query):
        from apps.requests.filters import AnalysisRequestFilter
        f = AnalysisRequestFilter(query, queryset=AnalysisRequest.objects.all())
        return list(f.qs.values_list('closure_status', flat=True))

    def test_default_returns_only_open(self, patient, lab_admin):
        self._make_rows(patient, lab_admin)
        closures = self._filtered({})
        assert all(c == ClosureStatus.OPEN for c in closures)
        assert len(closures) == 2

    def test_lifecycle_delivered_returns_only_delivered(self, patient, lab_admin):
        self._make_rows(patient, lab_admin)
        closures = self._filtered({'lifecycle': 'delivered'})
        assert all(c == ClosureStatus.DELIVERED for c in closures)
        assert len(closures) == 2

    def test_lifecycle_archived_returns_only_archived(self, patient, lab_admin):
        self._make_rows(patient, lab_admin)
        closures = self._filtered({'lifecycle': 'archived'})
        assert all(c == ClosureStatus.ARCHIVED for c in closures)
        assert len(closures) == 2

    def test_lifecycle_all_returns_every_closure(self, patient, lab_admin):
        self._make_rows(patient, lab_admin)
        closures = set(self._filtered({'lifecycle': 'all'}))
        assert closures == {
            ClosureStatus.OPEN,
            ClosureStatus.DELIVERED,
            ClosureStatus.ARCHIVED,
        }

    def test_legacy_include_archived_flag_still_works(self, patient, lab_admin):
        """Backward compat: clients in flight may still pass include_archived=true."""
        self._make_rows(patient, lab_admin)
        closures = set(self._filtered({'include_archived': True}))
        # OPEN + ARCHIVED, no DELIVERED.
        assert closures == {ClosureStatus.OPEN, ClosureStatus.ARCHIVED}

    def test_status_filter_only_offers_workflow_values(self):
        """The dropdown for ``status`` no longer offers DELIVERED/ARCHIVED."""
        from apps.requests.filters import AnalysisRequestFilter
        f = AnalysisRequestFilter({}, queryset=AnalysisRequest.objects.none())
        choices = [v for v, _ in f.filters['status'].extra['choices']]
        assert 'DELIVERED' not in choices
        assert 'ARCHIVED' not in choices
        # Workflow values still present.
        assert 'VALIDATED' in choices
        assert 'CANCELLED' in choices


# ---------------------------------------------------------------------------
# Detail serializer — closure_status + patient_summary
# ---------------------------------------------------------------------------

class TestDetailSerializerExtensions:

    def test_closure_status_and_patient_summary_returned(
        self, patient, exam, lab_admin, technician, biologist, make_request, email_capture,
    ):
        from apps.requests.serializers import AnalysisRequestDetailSerializer
        ar = _finalize_with_report(patient, lab_admin, technician, biologist, make_request, exam)
        RequestNotificationService.notify_patient(ar, make_request(lab_admin))
        ar.refresh_from_db()

        data = AnalysisRequestDetailSerializer(ar).data
        # Workflow + closure axes both present.
        assert data['status'] == RequestStatus.VALIDATED
        assert data['closure_status'] == ClosureStatus.DELIVERED
        # Patient summary surfaced (unchanged from previous turn).
        assert data['patient_summary']['full_name'] == 'Charlie Lifecycle'


# ---------------------------------------------------------------------------
# Billing safety — a delivered/archived request must still bill correctly
# ---------------------------------------------------------------------------

class TestBillingDecoupledFromClosure:
    """The motivating reason for this refactor: invoicing filters on
    ``status=VALIDATED``. After a request is delivered or archived, the
    workflow status MUST still be VALIDATED so the partner-billing query
    keeps finding the items."""

    @pytest.fixture()
    def partner(self):
        from apps.partners.models import PartnerOrganization
        return PartnerOrganization.objects.create(
            code='PART01', name='Billing Partner Inc.',
            contact_person='Pat', email='pat@partner.com', phone='1',
        )

    def _build_partner_request(self, patient, exam, lab_admin, technician, biologist, make_request, partner):
        from apps.requests.label_service import RequestLabelService
        ar = AnalysisRequestService.create(
            validated_data={
                'patient_id': patient.id,
                'source_type': SourceType.PARTNER_ORGANIZATION,
                'partner_organization_id': partner.id,
                'billing_mode': 'PARTNER_BILLING',
                'items': [{'exam_definition_id': exam.id}],
            },
            created_by=lab_admin,
            request=make_request(lab_admin),
            confirm_after=True,
        )
        RequestLabelService.generate_or_get(ar, lab_admin, make_request(lab_admin))
        req_t = make_request(technician)
        req_b = make_request(biologist)
        for item in ar.items.all():
            AnalysisRequestItemService.mark_collected(item=item, collected_by=technician, request=req_t)
        for item in ar.items.select_related('exam_definition').all():
            item.refresh_from_db()
            v = ResultVersionService.create_draft(
                item=item, entered_by=technician, request=req_t,
                result_value='42', values=[{'value': '42', 'is_abnormal': False}], comments='',
            )
            ResultVersionService.submit(version=v, submitted_by=technician, request=req_t)
            v = item.result_versions.get(is_current=True)
            ResultVersionService.validate(version=v, validation_notes='OK', validated_by=biologist, request=req_b)
        ar.refresh_from_db()
        AnalysisRequestService.finalize_validation(analysis_request=ar, finalized_by=biologist, request=req_b)
        ar.refresh_from_db()
        return ar

    def test_delivered_partner_request_is_still_billable(
        self, patient, exam, lab_admin, technician, biologist, make_request, partner,
    ):
        from datetime import date as _date, timedelta as _td
        ar = self._build_partner_request(
            patient, exam, lab_admin, technician, biologist, make_request, partner,
        )
        # Mark delivered — closure changes, workflow stays VALIDATED.
        AnalysisRequestService.mark_delivered(
            analysis_request=ar, actor=lab_admin, request=make_request(lab_admin),
        )
        ar.refresh_from_db()
        assert ar.status == RequestStatus.VALIDATED
        assert ar.closure_status == ClosureStatus.DELIVERED

        from apps.invoicing.services import _billable_items
        items = _billable_items(
            partner=partner,
            period_start=_date.today() - _td(days=1),
            period_end=_date.today() + _td(days=1),
        )
        assert any(item.analysis_request_id == ar.id for item in items)

    def test_archived_partner_request_is_still_billable(
        self, patient, exam, lab_admin, technician, biologist, make_request, partner,
    ):
        from datetime import date as _date, timedelta as _td
        ar = self._build_partner_request(
            patient, exam, lab_admin, technician, biologist, make_request, partner,
        )
        AnalysisRequestService.archive(
            analysis_request=ar, actor=lab_admin, request=make_request(lab_admin),
        )
        ar.refresh_from_db()
        assert ar.status == RequestStatus.VALIDATED  # untouched
        assert ar.closure_status == ClosureStatus.ARCHIVED

        from apps.invoicing.services import _billable_items
        items = _billable_items(
            partner=partner,
            period_start=_date.today() - _td(days=1),
            period_end=_date.today() + _td(days=1),
        )
        assert any(item.analysis_request_id == ar.id for item in items)


# ---------------------------------------------------------------------------
# Permission gating for new actions (unchanged contracts)
# ---------------------------------------------------------------------------

class TestNewActionPermissions:

    def _has_perm(self, action_name, user, make_request):
        from apps.requests.views import AnalysisRequestViewSet
        view = AnalysisRequestViewSet()
        view.action = action_name
        view.request = make_request(user)
        permissions = view.get_permissions()
        return all(p.has_permission(view.request, view) for p in permissions)

    def test_mark_delivered_lab_admin_allowed(self, lab_admin, make_request):
        assert self._has_perm('mark_delivered', lab_admin, make_request) is True

    def test_mark_delivered_receptionist_allowed(self, receptionist, make_request):
        assert self._has_perm('mark_delivered', receptionist, make_request) is True

    def test_mark_delivered_technician_blocked(self, technician, make_request):
        assert self._has_perm('mark_delivered', technician, make_request) is False

    def test_archive_lab_admin_allowed(self, lab_admin, make_request):
        assert self._has_perm('archive', lab_admin, make_request) is True

    def test_archive_receptionist_blocked(self, receptionist, make_request):
        assert self._has_perm('archive', receptionist, make_request) is False


# ---------------------------------------------------------------------------
# HTTP-level archive route + retrieval
# ---------------------------------------------------------------------------

class TestArchiveHttpRoute:

    def _client(self, user):
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import RefreshToken
        client = APIClient()
        token = RefreshToken.for_user(user).access_token
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        return client

    def test_archive_url_is_registered(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_with_report(
            patient, lab_admin, technician, biologist, make_request, exam,
        )
        resp = self._client(lab_admin).post(
            f'/api/v1/requests/{ar.id}/archive/',
            HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 200, resp.content
        ar.refresh_from_db()
        assert ar.closure_status == ClosureStatus.ARCHIVED
        assert ar.status == RequestStatus.VALIDATED  # workflow untouched

    def test_archive_response_carries_closure(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_with_report(
            patient, lab_admin, technician, biologist, make_request, exam,
        )
        resp = self._client(lab_admin).post(
            f'/api/v1/requests/{ar.id}/archive/',
            HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body['data']['closure_status'] == 'ARCHIVED'
        assert body['data']['status'] == 'VALIDATED'

    def test_receptionist_cannot_archive(
        self, patient, exam, lab_admin, technician, biologist, receptionist, make_request,
    ):
        ar = _finalize_with_report(
            patient, lab_admin, technician, biologist, make_request, exam,
        )
        resp = self._client(receptionist).post(
            f'/api/v1/requests/{ar.id}/archive/',
            HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# HTTP retrieval / listing — archived requests stay reachable
# ---------------------------------------------------------------------------

class TestArchivedRetrievalAndListing:

    def _client(self, user):
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import RefreshToken
        client = APIClient()
        token = RefreshToken.for_user(user).access_token
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        return client

    def test_archived_request_can_be_retrieved_by_detail(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_with_report(
            patient, lab_admin, technician, biologist, make_request, exam,
        )
        self._client(lab_admin).post(
            f'/api/v1/requests/{ar.id}/archive/', HTTP_HOST='testlab.localhost',
        )
        resp = self._client(lab_admin).get(
            f'/api/v1/requests/{ar.id}/', HTTP_HOST='testlab.localhost',
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body['data']['closure_status'] == 'ARCHIVED'

    def test_default_list_excludes_archived(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_with_report(
            patient, lab_admin, technician, biologist, make_request, exam,
        )
        self._client(lab_admin).post(
            f'/api/v1/requests/{ar.id}/archive/', HTTP_HOST='testlab.localhost',
        )
        resp = self._client(lab_admin).get(
            '/api/v1/requests/', HTTP_HOST='testlab.localhost',
        )
        ids = [row['id'] for row in resp.json()['data']]
        assert str(ar.id) not in ids

    def test_lifecycle_archived_surfaces_archived(
        self, patient, exam, lab_admin, technician, biologist, make_request,
    ):
        ar = _finalize_with_report(
            patient, lab_admin, technician, biologist, make_request, exam,
        )
        self._client(lab_admin).post(
            f'/api/v1/requests/{ar.id}/archive/', HTTP_HOST='testlab.localhost',
        )
        resp = self._client(lab_admin).get(
            '/api/v1/requests/?lifecycle=archived', HTTP_HOST='testlab.localhost',
        )
        ids = [row['id'] for row in resp.json()['data']]
        assert str(ar.id) in ids
