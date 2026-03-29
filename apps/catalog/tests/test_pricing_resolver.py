"""
Tests for PricingResolver — contextual rule resolution.

Covers:
- Rule resolution by specificity (exam+partner, exam+source_type, exam-only)
- Priority ordering within same specificity
- Date bounds (start_date, end_date)
- Inactive rules are excluded
- Fallback to None when no rule matches
- billed_price computation (fixed price, percentage discount)
"""
import pytest
from datetime import date, timedelta
from decimal import Decimal

from apps.catalog.models import ExamCategory, ExamDefinition, PricingRule, PricingType, SampleType
from apps.catalog.services import PricingResolver
from apps.partners.models import PartnerOrganization, OrganizationType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
def partner():
    return PartnerOrganization.objects.create(
        code='CLN-A',
        name='Clinic Alpha',
        organization_type=OrganizationType.CLINIC,
    )


@pytest.fixture()
def partner_b():
    return PartnerOrganization.objects.create(
        code='CLN-B',
        name='Clinic Beta',
        organization_type=OrganizationType.HOSPITAL,
    )


# ---------------------------------------------------------------------------
# Resolution by specificity
# ---------------------------------------------------------------------------

class TestResolutionSpecificity:

    def test_no_rules_returns_none(self, exam):
        result = PricingResolver.resolve(exam)
        assert result is None

    def test_exam_only_rule(self, exam):
        rule = PricingRule.objects.create(
            exam_definition=exam,
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('40.0000'),
        )
        result = PricingResolver.resolve(exam)
        assert result == rule

    def test_exam_source_type_beats_exam_only(self, exam):
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
        result = PricingResolver.resolve(
            exam, source_type='PARTNER_ORGANIZATION',
        )
        assert result == source_rule

    def test_exam_partner_beats_exam_source_type(self, exam, partner):
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
            value=Decimal('30.0000'),
        )
        result = PricingResolver.resolve(
            exam, partner_organization=partner,
            source_type='PARTNER_ORGANIZATION',
        )
        assert result == partner_rule

    def test_partner_rule_does_not_match_other_partner(self, exam, partner, partner_b):
        PricingRule.objects.create(
            exam_definition=exam,
            partner_organization=partner,
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('30.0000'),
        )
        broad_rule = PricingRule.objects.create(
            exam_definition=exam,
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('40.0000'),
        )
        # partner_b should NOT match partner's rule; falls to exam-only
        result = PricingResolver.resolve(
            exam, partner_organization=partner_b,
            source_type='PARTNER_ORGANIZATION',
        )
        assert result == broad_rule

    def test_source_type_rule_does_not_match_other_source(self, exam):
        PricingRule.objects.create(
            exam_definition=exam,
            source_type='PARTNER_ORGANIZATION',
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('35.0000'),
        )
        broad_rule = PricingRule.objects.create(
            exam_definition=exam,
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('40.0000'),
        )
        result = PricingResolver.resolve(
            exam, source_type='DIRECT_PATIENT',
        )
        assert result == broad_rule

    def test_fallback_to_exam_only_when_no_specific_match(self, exam, partner):
        broad_rule = PricingRule.objects.create(
            exam_definition=exam,
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('40.0000'),
        )
        result = PricingResolver.resolve(
            exam, partner_organization=partner,
            source_type='PARTNER_ORGANIZATION',
        )
        assert result == broad_rule


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------

class TestPriorityOrdering:

    def test_higher_priority_wins(self, exam):
        PricingRule.objects.create(
            exam_definition=exam,
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('40.0000'),
            priority=1,
        )
        high_rule = PricingRule.objects.create(
            exam_definition=exam,
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('35.0000'),
            priority=10,
        )
        result = PricingResolver.resolve(exam)
        assert result == high_rule

    def test_same_priority_uses_most_recent(self, exam):
        PricingRule.objects.create(
            exam_definition=exam,
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('40.0000'),
            priority=0,
        )
        newer_rule = PricingRule.objects.create(
            exam_definition=exam,
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('35.0000'),
            priority=0,
        )
        result = PricingResolver.resolve(exam)
        assert result == newer_rule


# ---------------------------------------------------------------------------
# Date bounds
# ---------------------------------------------------------------------------

class TestDateBounds:

    def test_rule_not_yet_started(self, exam):
        PricingRule.objects.create(
            exam_definition=exam,
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('40.0000'),
            start_date=date.today() + timedelta(days=1),
        )
        assert PricingResolver.resolve(exam) is None

    def test_rule_already_ended(self, exam):
        PricingRule.objects.create(
            exam_definition=exam,
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('40.0000'),
            end_date=date.today() - timedelta(days=1),
        )
        assert PricingResolver.resolve(exam) is None

    def test_rule_within_bounds(self, exam):
        rule = PricingRule.objects.create(
            exam_definition=exam,
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('40.0000'),
            start_date=date.today() - timedelta(days=1),
            end_date=date.today() + timedelta(days=1),
        )
        assert PricingResolver.resolve(exam) == rule

    def test_rule_with_no_bounds(self, exam):
        rule = PricingRule.objects.create(
            exam_definition=exam,
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('40.0000'),
        )
        assert PricingResolver.resolve(exam) == rule


# ---------------------------------------------------------------------------
# Active / inactive
# ---------------------------------------------------------------------------

class TestActiveFlag:

    def test_inactive_rule_excluded(self, exam):
        PricingRule.objects.create(
            exam_definition=exam,
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('40.0000'),
            is_active=False,
        )
        assert PricingResolver.resolve(exam) is None

    def test_active_rule_included(self, exam):
        rule = PricingRule.objects.create(
            exam_definition=exam,
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('40.0000'),
            is_active=True,
        )
        assert PricingResolver.resolve(exam) == rule


# ---------------------------------------------------------------------------
# Billed price computation
# ---------------------------------------------------------------------------

class TestComputeBilledPrice:

    def test_fixed_price(self, exam):
        rule = PricingRule(
            exam_definition=exam,
            pricing_type=PricingType.FIXED_PRICE,
            value=Decimal('30.0000'),
        )
        result = PricingResolver.compute_billed_price(rule, Decimal('50.0000'))
        assert result == Decimal('30.0000')

    def test_percentage_discount(self, exam):
        rule = PricingRule(
            exam_definition=exam,
            pricing_type=PricingType.PERCENTAGE_DISCOUNT,
            value=Decimal('20.0000'),  # 20% off
        )
        result = PricingResolver.compute_billed_price(rule, Decimal('50.0000'))
        assert result == Decimal('40.0000')

    def test_100_percent_discount(self, exam):
        rule = PricingRule(
            exam_definition=exam,
            pricing_type=PricingType.PERCENTAGE_DISCOUNT,
            value=Decimal('100.0000'),
        )
        result = PricingResolver.compute_billed_price(rule, Decimal('50.0000'))
        assert result == Decimal('0.0000')

    def test_percentage_discount_rounding(self, exam):
        rule = PricingRule(
            exam_definition=exam,
            pricing_type=PricingType.PERCENTAGE_DISCOUNT,
            value=Decimal('33.3333'),
        )
        result = PricingResolver.compute_billed_price(rule, Decimal('100.0000'))
        # 100 - (100 * 33.3333 / 100) = 100 - 33.3333 = 66.6667
        assert result == Decimal('66.6667')
