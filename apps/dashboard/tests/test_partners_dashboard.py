"""
Tests for dashboard partner analytics and source-type metrics.
"""
import pytest
from datetime import date
from decimal import Decimal

from django.db.models import Count, DecimalField, F, Sum, Value
from django.db.models.functions import Coalesce

from apps.catalog.models import ExamCategory, ExamDefinition, PricingRule, SampleType
from apps.partners.models import OrganizationType
from apps.partners.services import PartnerOrganizationService
from apps.patients.models import Patient
from apps.requests.models import (
    AnalysisRequest,
    AnalysisRequestItem,
    BillingMode,
    RequestStatus,
    SourceType,
)
from apps.requests.services import AnalysisRequestService


@pytest.fixture()
def dashboard_data(lab_admin, make_request, default_technique):
    """
    Creates a realistic dataset:
    - 2 partner orgs
    - 3 direct patient requests (2 confirmed, 1 draft)
    - 2 partner requests (confirmed, with items + prices)
    """
    patient = Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='DASH-NID-001',
        first_name='Jane',
        last_name='Metrics',
        date_of_birth='1985-06-15',
        gender='FEMALE',
        created_by=lab_admin,
    )

    partner_a = PartnerOrganizationService.create(
        validated_data={
            'code': 'DASH-A',
            'name': 'Dashboard Clinic A',
            'organization_type': OrganizationType.CLINIC,
        },
        created_by=lab_admin,
        request=make_request(lab_admin),
    )
    partner_b = PartnerOrganizationService.create(
        validated_data={
            'code': 'DASH-B',
            'name': 'Dashboard Hospital B',
            'organization_type': OrganizationType.HOSPITAL,
        },
        created_by=lab_admin,
        request=make_request(lab_admin),
    )

    cat = ExamCategory.objects.create(name='Dashboard Cat', display_order=1)
    exam = ExamDefinition.objects.create(
        category=cat, technique=default_technique, code='DASH-CBC', name='Dashboard CBC',
        sample_type=SampleType.BLOOD,
    )
    PricingRule.objects.create(
        exam_definition=exam,
        unit_price='100.0000',
        billed_price='150.0000',
        effective_from=date(2020, 1, 1),
        effective_to=None,
        created_by=lab_admin,
    )

    req = make_request(lab_admin)

    # 2 direct patient requests — confirmed with 1 item each
    for _ in range(2):
        ar = AnalysisRequestService.create(
            validated_data={
                'patient_id': patient.id,
                'source_type': SourceType.DIRECT_PATIENT,
                'billing_mode': BillingMode.DIRECT_PAYMENT,
                'items': [{'exam_definition_id': exam.id}],
            },
            created_by=lab_admin,
            request=req,
        )
        AnalysisRequestService.confirm(
            analysis_request=ar, confirmed_by=lab_admin, request=req,
        )

    # 1 direct patient request — draft (no items, won't appear in confirmed metrics)
    AnalysisRequestService.create(
        validated_data={
            'patient_id': patient.id,
            'source_type': SourceType.DIRECT_PATIENT,
            'billing_mode': BillingMode.DIRECT_PAYMENT,
            'items': [],
        },
        created_by=lab_admin,
        request=req,
    )

    # 1 partner A request — confirmed
    ar_a = AnalysisRequestService.create(
        validated_data={
            'patient_id': patient.id,
            'source_type': SourceType.PARTNER_ORGANIZATION,
            'partner_organization_id': partner_a.id,
            'billing_mode': BillingMode.PARTNER_BILLING,
            'items': [{'exam_definition_id': exam.id}],
        },
        created_by=lab_admin,
        request=req,
    )
    AnalysisRequestService.confirm(
        analysis_request=ar_a, confirmed_by=lab_admin, request=req,
    )

    # 1 partner B request — confirmed
    ar_b = AnalysisRequestService.create(
        validated_data={
            'patient_id': patient.id,
            'source_type': SourceType.PARTNER_ORGANIZATION,
            'partner_organization_id': partner_b.id,
            'billing_mode': BillingMode.PARTNER_BILLING,
            'items': [{'exam_definition_id': exam.id}],
        },
        created_by=lab_admin,
        request=req,
    )
    AnalysisRequestService.confirm(
        analysis_request=ar_b, confirmed_by=lab_admin, request=req,
    )

    return {'partner_a': partner_a, 'partner_b': partner_b}


class TestDashboardRequestsBySourceType:

    def test_source_type_breakdown(self, dashboard_data):
        by_source = dict(
            AnalysisRequest.objects
            .values('source_type')
            .annotate(count=Count('id'))
            .values_list('source_type', 'count')
        )
        assert by_source[SourceType.DIRECT_PATIENT] == 3
        assert by_source[SourceType.PARTNER_ORGANIZATION] == 2


class TestDashboardPartnerMetrics:

    def test_ratio_direct_vs_partner(self, dashboard_data):
        confirmed_statuses = [
            RequestStatus.CONFIRMED,
            RequestStatus.IN_PROGRESS,
            RequestStatus.COMPLETED,
        ]
        confirmed_qs = AnalysisRequest.objects.filter(
            status__in=confirmed_statuses,
        )
        total = confirmed_qs.count()
        partner = confirmed_qs.filter(
            source_type=SourceType.PARTNER_ORGANIZATION,
        ).count()

        assert total == 4
        assert partner == 2
        assert total - partner == 2

    def test_revenue_by_partner(self, dashboard_data):
        confirmed_statuses = [
            RequestStatus.CONFIRMED,
            RequestStatus.IN_PROGRESS,
            RequestStatus.COMPLETED,
        ]
        revenue = list(
            AnalysisRequestItem.objects
            .filter(
                analysis_request__source_type=SourceType.PARTNER_ORGANIZATION,
                analysis_request__status__in=confirmed_statuses,
                billed_price__isnull=False,
            )
            .values(
                partner_id=F('analysis_request__partner_organization_id'),
            )
            .annotate(
                total_billed=Coalesce(
                    Sum('billed_price'),
                    Value(Decimal('0')),
                    output_field=DecimalField(),
                ),
                exam_count=Count('id'),
            )
            .order_by('-total_billed')
        )

        assert len(revenue) == 2
        for row in revenue:
            assert row['total_billed'] == Decimal('150.0000')
            assert row['exam_count'] == 1

    def test_exams_by_partner(self, dashboard_data):
        confirmed_statuses = [
            RequestStatus.CONFIRMED,
            RequestStatus.IN_PROGRESS,
            RequestStatus.COMPLETED,
        ]
        items_by_partner = dict(
            AnalysisRequestItem.objects
            .filter(
                analysis_request__source_type=SourceType.PARTNER_ORGANIZATION,
                analysis_request__status__in=confirmed_statuses,
            )
            .values(
                partner_code=F('analysis_request__partner_organization__code'),
            )
            .annotate(exam_count=Count('id'))
            .values_list('partner_code', 'exam_count')
        )

        assert items_by_partner['DASH-A'] == 1
        assert items_by_partner['DASH-B'] == 1
