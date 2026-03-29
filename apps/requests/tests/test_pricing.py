"""
Tests for request item pricing — snapshot, rule resolution, manual override,
price_source traceability, and confirmation stability.

Covers:
- Exam definition stores reference unit price
- Item copies unit_price from exam definition at creation
- billed_price defaults to unit_price when no rule and no override
- Contextual rules: exam+partner, exam+source_type, exam-only, fallback
- Manual override sets billed_price and price_source=MANUAL_OVERRIDE
- price_source correctly tracks DEFAULT_PRICE, PRICING_RULE, MANUAL_OVERRIDE
- Confirmation flow works with pre-set pricing
- REJECTED items get zero prices at confirmation
- Existing request flows remain stable
"""
import pytest
from decimal import Decimal

from apps.catalog.models import ExamCategory, ExamDefinition, PricingRule, PricingType, SampleType
from apps.partners.models import OrganizationType
from apps.partners.services import PartnerOrganizationService
from apps.patients.models import Patient
from apps.requests.models import (
    AnalysisRequest, AnalysisRequestItem, PriceSource,
    RequestStatus, ItemStatus, ExecutionMode, SourceType, BillingMode,
)
from apps.requests.services import AnalysisRequestService, AnalysisRequestItemService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        national_id='NID-PRICE-001',
        first_name='Jane',
        last_name='Price',
        date_of_birth='1985-06-15',
        gender='FEMALE',
        created_by=lab_admin,
    )


@pytest.fixture()
def category():
    return ExamCategory.objects.create(name='Biochemistry', display_order=1)


@pytest.fixture()
def exam(category):
    return ExamDefinition.objects.create(
        category=category,
        code='GLU',
        name='Fasting Glucose',
        sample_type=SampleType.BLOOD,
        unit_price=Decimal('50.0000'),
    )


@pytest.fixture()
def exam_b(category):
    return ExamDefinition.objects.create(
        category=category,
        code='HBA1C',
        name='Glycated Hemoglobin',
        sample_type=SampleType.BLOOD,
        unit_price=Decimal('80.0000'),
    )


@pytest.fixture()
def partner(lab_admin, make_request):
    return PartnerOrganizationService.create(
        validated_data={
            'code': 'CLN-PRICE',
            'name': 'Price Test Clinic',
            'organization_type': OrganizationType.CLINIC,
        },
        created_by=lab_admin,
        request=make_request(lab_admin),
    )


def _create_request(patient, lab_admin, make_request, items=None, **kwargs):
    data = {
        'patient_id': patient.id,
        'items': items or [],
        **kwargs,
    }
    return AnalysisRequestService.create(
        validated_data=data,
        created_by=lab_admin,
        request=make_request(lab_admin),
    )


# ---------------------------------------------------------------------------
# Exam-level reference price
# ---------------------------------------------------------------------------

class TestExamUnitPrice:

    def test_exam_stores_unit_price(self, exam):
        assert exam.unit_price == Decimal('50.0000')

    def test_exam_default_unit_price_is_zero(self, category):
        exam = ExamDefinition.objects.create(
            category=category, code='TEST', name='Test',
            sample_type=SampleType.BLOOD,
        )
        assert exam.unit_price == 0


# ---------------------------------------------------------------------------
# Snapshot at creation — no rules
# ---------------------------------------------------------------------------

class TestDefaultPricing:

    def test_item_copies_unit_price_from_exam(self, patient, exam, lab_admin, make_request):
        ar = _create_request(patient, lab_admin, make_request, items=[
            {'exam_definition_id': exam.id},
        ])
        item = ar.items.first()
        assert item.unit_price == Decimal('50.0000')

    def test_billed_price_defaults_to_unit_price(self, patient, exam, lab_admin, make_request):
        ar = _create_request(patient, lab_admin, make_request, items=[
            {'exam_definition_id': exam.id},
        ])
        item = ar.items.first()
        assert item.billed_price == Decimal('50.0000')

    def test_price_source_is_default(self, patient, exam, lab_admin, make_request):
        ar = _create_request(patient, lab_admin, make_request, items=[
            {'exam_definition_id': exam.id},
        ])
        item = ar.items.first()
        assert item.price_source == PriceSource.DEFAULT_PRICE

    def test_pricing_rule_is_none(self, patient, exam, lab_admin, make_request):
        ar = _create_request(patient, lab_admin, make_request, items=[
            {'exam_definition_id': exam.id},
        ])
        item = ar.items.first()
        assert item.pricing_rule is None


# ---------------------------------------------------------------------------
# Contextual pricing rules
# ---------------------------------------------------------------------------

class TestContextualPricing:

    def test_exam_only_rule_applies(self, patient, exam, lab_admin, make_request):
        rule = PricingRule.objects.create(
            exam_definition=exam,
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('40.0000'),
        )
        ar = _create_request(patient, lab_admin, make_request, items=[
            {'exam_definition_id': exam.id},
        ])
        item = ar.items.first()
        assert item.billed_price == Decimal('40.0000')
        assert item.pricing_rule == rule
        assert item.price_source == PriceSource.PRICING_RULE

    def test_source_type_rule_applies(self, patient, exam, partner, lab_admin, make_request):
        PricingRule.objects.create(
            exam_definition=exam,
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('40.0000'),
        )
        source_rule = PricingRule.objects.create(
            exam_definition=exam,
            source_type='PARTNER_ORGANIZATION',
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('35.0000'),
        )
        ar = _create_request(
            patient, lab_admin, make_request,
            source_type=SourceType.PARTNER_ORGANIZATION,
            partner_organization_id=partner.id,
            billing_mode=BillingMode.PARTNER_BILLING,
            items=[{'exam_definition_id': exam.id}],
        )
        item = ar.items.first()
        assert item.billed_price == Decimal('35.0000')
        assert item.pricing_rule == source_rule

    def test_partner_rule_applies(self, patient, exam, partner, lab_admin, make_request):
        PricingRule.objects.create(
            exam_definition=exam,
            source_type='PARTNER_ORGANIZATION',
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('35.0000'),
        )
        partner_rule = PricingRule.objects.create(
            exam_definition=exam,
            partner_organization=partner,
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('25.0000'),
        )
        ar = _create_request(
            patient, lab_admin, make_request,
            source_type=SourceType.PARTNER_ORGANIZATION,
            partner_organization_id=partner.id,
            billing_mode=BillingMode.PARTNER_BILLING,
            items=[{'exam_definition_id': exam.id}],
        )
        item = ar.items.first()
        assert item.billed_price == Decimal('25.0000')
        assert item.pricing_rule == partner_rule

    def test_percentage_discount_rule(self, patient, exam, lab_admin, make_request):
        rule = PricingRule.objects.create(
            exam_definition=exam,
            pricing_type=PricingType.PERCENTAGE_DISCOUNT,
            value=Decimal('20.0000'),  # 20% off
        )
        ar = _create_request(patient, lab_admin, make_request, items=[
            {'exam_definition_id': exam.id},
        ])
        item = ar.items.first()
        # 50 - 20% = 40
        assert item.billed_price == Decimal('40.0000')
        assert item.pricing_rule == rule
        assert item.price_source == PriceSource.PRICING_RULE

    def test_no_rule_falls_back_to_unit_price(self, patient, exam, lab_admin, make_request):
        ar = _create_request(patient, lab_admin, make_request, items=[
            {'exam_definition_id': exam.id},
        ])
        item = ar.items.first()
        assert item.billed_price == exam.unit_price
        assert item.price_source == PriceSource.DEFAULT_PRICE


# ---------------------------------------------------------------------------
# Manual override
# ---------------------------------------------------------------------------

class TestManualOverride:

    def test_manual_billed_price_at_creation(self, patient, exam, lab_admin, make_request):
        ar = _create_request(patient, lab_admin, make_request, items=[
            {'exam_definition_id': exam.id, 'billed_price': Decimal('60.0000')},
        ])
        item = ar.items.first()
        assert item.billed_price == Decimal('60.0000')
        assert item.unit_price == Decimal('50.0000')
        assert item.price_source == PriceSource.MANUAL_OVERRIDE
        assert item.pricing_rule is None

    def test_manual_override_beats_rule(self, patient, exam, lab_admin, make_request):
        PricingRule.objects.create(
            exam_definition=exam,
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('40.0000'),
        )
        ar = _create_request(patient, lab_admin, make_request, items=[
            {'exam_definition_id': exam.id, 'billed_price': Decimal('55.0000')},
        ])
        item = ar.items.first()
        assert item.billed_price == Decimal('55.0000')
        assert item.price_source == PriceSource.MANUAL_OVERRIDE

    def test_manual_override_via_update(self, patient, exam, lab_admin, make_request):
        ar = _create_request(patient, lab_admin, make_request, items=[
            {'exam_definition_id': exam.id},
        ])
        item = ar.items.first()
        assert item.price_source == PriceSource.DEFAULT_PRICE

        item = AnalysisRequestItemService.update(
            item=item,
            validated_data={'billed_price': Decimal('65.0000')},
            updated_by=lab_admin,
            request=make_request(lab_admin),
        )
        assert item.billed_price == Decimal('65.0000')
        assert item.price_source == PriceSource.MANUAL_OVERRIDE

    def test_null_override_re_resolves(self, patient, exam, lab_admin, make_request):
        """Setting billed_price=None re-resolves from rules."""
        PricingRule.objects.create(
            exam_definition=exam,
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('40.0000'),
        )
        ar = _create_request(patient, lab_admin, make_request, items=[
            {'exam_definition_id': exam.id, 'billed_price': Decimal('99.0000')},
        ])
        item = ar.items.first()
        assert item.price_source == PriceSource.MANUAL_OVERRIDE

        item = AnalysisRequestItemService.update(
            item=item,
            validated_data={'billed_price': None},
            updated_by=lab_admin,
            request=make_request(lab_admin),
        )
        assert item.billed_price == Decimal('40.0000')
        assert item.price_source == PriceSource.PRICING_RULE


# ---------------------------------------------------------------------------
# Price source traceability
# ---------------------------------------------------------------------------

class TestPriceSourceTraceability:

    def test_default_price_source(self, patient, exam, lab_admin, make_request):
        ar = _create_request(patient, lab_admin, make_request, items=[
            {'exam_definition_id': exam.id},
        ])
        item = ar.items.first()
        assert item.price_source == PriceSource.DEFAULT_PRICE

    def test_pricing_rule_source(self, patient, exam, lab_admin, make_request):
        PricingRule.objects.create(
            exam_definition=exam,
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('40.0000'),
        )
        ar = _create_request(patient, lab_admin, make_request, items=[
            {'exam_definition_id': exam.id},
        ])
        item = ar.items.first()
        assert item.price_source == PriceSource.PRICING_RULE

    def test_manual_override_source(self, patient, exam, lab_admin, make_request):
        ar = _create_request(patient, lab_admin, make_request, items=[
            {'exam_definition_id': exam.id, 'billed_price': Decimal('99.0000')},
        ])
        item = ar.items.first()
        assert item.price_source == PriceSource.MANUAL_OVERRIDE


# ---------------------------------------------------------------------------
# Confirmation stability
# ---------------------------------------------------------------------------

class TestConfirmationStability:

    def test_confirm_with_priced_items(self, patient, exam, lab_admin, make_request):
        ar = _create_request(patient, lab_admin, make_request, items=[
            {'exam_definition_id': exam.id},
        ])
        ar = AnalysisRequestService.confirm(
            analysis_request=ar,
            confirmed_by=lab_admin,
            request=make_request(lab_admin),
        )
        assert ar.status == RequestStatus.CONFIRMED
        item = ar.items.first()
        assert item.unit_price == Decimal('50.0000')
        assert item.billed_price == Decimal('50.0000')

    def test_confirm_preserves_manual_override(self, patient, exam, lab_admin, make_request):
        ar = _create_request(patient, lab_admin, make_request, items=[
            {'exam_definition_id': exam.id, 'billed_price': Decimal('75.0000')},
        ])
        ar = AnalysisRequestService.confirm(
            analysis_request=ar,
            confirmed_by=lab_admin,
            request=make_request(lab_admin),
        )
        item = ar.items.first()
        assert item.billed_price == Decimal('75.0000')
        assert item.price_source == PriceSource.MANUAL_OVERRIDE

    def test_confirm_rejected_items_get_zero_prices(self, patient, exam, lab_admin, make_request):
        ar = _create_request(patient, lab_admin, make_request, items=[
            {'exam_definition_id': exam.id, 'execution_mode': ExecutionMode.REJECTED,
             'rejection_reason': 'Not needed'},
        ])
        ar = AnalysisRequestService.confirm(
            analysis_request=ar,
            confirmed_by=lab_admin,
            request=make_request(lab_admin),
        )
        item = ar.items.first()
        assert item.unit_price == 0
        assert item.billed_price == 0
        assert item.status == ItemStatus.REJECTED

    def test_confirm_no_items_fails(self, patient, lab_admin, make_request):
        ar = _create_request(patient, lab_admin, make_request)
        with pytest.raises(Exception, match='no items'):
            AnalysisRequestService.confirm(
                analysis_request=ar,
                confirmed_by=lab_admin,
                request=make_request(lab_admin),
            )

    def test_add_item_after_create_also_priced(self, patient, exam, exam_b, lab_admin, make_request):
        ar = _create_request(patient, lab_admin, make_request, items=[
            {'exam_definition_id': exam.id},
        ])
        PricingRule.objects.create(
            exam_definition=exam_b,
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('70.0000'),
        )
        item = AnalysisRequestService.add_item(
            analysis_request=ar,
            validated_data={'exam_definition_id': exam_b.id},
            added_by=lab_admin,
            request=make_request(lab_admin),
        )
        assert item.unit_price == Decimal('80.0000')
        assert item.billed_price == Decimal('70.0000')
        assert item.price_source == PriceSource.PRICING_RULE


# ---------------------------------------------------------------------------
# Execution mode change re-pricing
# ---------------------------------------------------------------------------

class TestExecutionModeRepricing:

    def test_switching_to_rejected_zeros_prices(self, patient, exam, lab_admin, make_request):
        ar = _create_request(patient, lab_admin, make_request, items=[
            {'exam_definition_id': exam.id},
        ])
        item = ar.items.first()
        assert item.billed_price == Decimal('50.0000')

        item = AnalysisRequestItemService.update(
            item=item,
            validated_data={'execution_mode': ExecutionMode.REJECTED},
            updated_by=lab_admin,
            request=make_request(lab_admin),
        )
        assert item.unit_price == 0
        assert item.billed_price == 0

    def test_switching_from_rejected_back_to_internal(self, patient, exam, lab_admin, make_request):
        ar = _create_request(patient, lab_admin, make_request, items=[
            {'exam_definition_id': exam.id, 'execution_mode': ExecutionMode.REJECTED,
             'rejection_reason': 'Test'},
        ])
        item = ar.items.first()
        assert item.unit_price == 0

        item = AnalysisRequestItemService.update(
            item=item,
            validated_data={'execution_mode': ExecutionMode.INTERNAL},
            updated_by=lab_admin,
            request=make_request(lab_admin),
        )
        assert item.unit_price == Decimal('50.0000')
        assert item.billed_price == Decimal('50.0000')
        assert item.price_source == PriceSource.DEFAULT_PRICE
