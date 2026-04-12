"""
Tests for request item pricing under the new 3-step workflow contract.

Contract (authoritative — matches ``RequestPricingResolver`` exactly):

    DIRECT_PATIENT
        billed_price = unit_price (always)

    PARTNER_ORGANIZATION
        billed_price = PartnerExamPrice.agreed_price
                       if an active row exists for (partner, exam)
        billed_price = unit_price
                       otherwise

Cross-cutting invariants also covered here:
- ``unit_price`` is always snapshotted from the current reference at
  request-item creation time and never retroactively mutated.
- REJECTED items get zero prices regardless of source or agreed pricing.
- A manual ``billed_price`` override, if passed, bypasses the resolver
  (this is the legacy draft-edit escape hatch; the new 3-step flow does
  not use it).
- Historical integrity: changing a reference price (exam unit_price or
  PartnerExamPrice.agreed_price) after creation has zero effect on
  existing persisted items.
"""
import pytest
from decimal import Decimal

from apps.catalog.models import ExamCategory, ExamDefinition, SampleType
from apps.partners.models import OrganizationType, PartnerExamPrice
from apps.partners.services import PartnerOrganizationService
from apps.patients.models import Patient
from apps.requests.models import (
    PriceSource, RequestStatus, ItemStatus, ExecutionMode,
    SourceType, BillingMode,
)
from apps.requests.services import AnalysisRequestService, AnalysisRequestItemService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD',
        document_number='NID-PRICE-001',
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


@pytest.fixture()
def other_partner(lab_admin, make_request):
    return PartnerOrganizationService.create(
        validated_data={
            'code': 'CLN-OTHER',
            'name': 'Other Clinic',
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
# DIRECT_PATIENT flow — billed_price always equals unit_price
# ---------------------------------------------------------------------------

class TestDirectPatientPricing:

    def test_item_copies_unit_price_from_exam(self, patient, exam, lab_admin, make_request):
        ar = _create_request(patient, lab_admin, make_request, items=[
            {'exam_definition_id': exam.id},
        ])
        item = ar.items.first()
        assert item.unit_price == Decimal('50.0000')

    def test_billed_price_equals_unit_price(self, patient, exam, lab_admin, make_request):
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

    def test_direct_patient_ignores_partner_exam_price(
        self, patient, exam, partner, lab_admin, make_request,
    ):
        """
        A PartnerExamPrice exists for ``partner`` but the request is
        DIRECT_PATIENT, so the agreed price is irrelevant — the item is
        billed at the exam reference ``unit_price``.
        """
        PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('35.0000'),
        )
        ar = _create_request(
            patient, lab_admin, make_request,
            source_type=SourceType.DIRECT_PATIENT,
            items=[{'exam_definition_id': exam.id}],
        )
        item = ar.items.first()
        assert item.billed_price == Decimal('50.0000')
        assert item.price_source == PriceSource.DEFAULT_PRICE


# ---------------------------------------------------------------------------
# PARTNER_ORGANIZATION flow — agreed price when present, else unit_price
# ---------------------------------------------------------------------------

class TestPartnerOrganizationPricing:

    def test_uses_agreed_price_when_active_row_exists(
        self, patient, exam, partner, lab_admin, make_request,
    ):
        PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('35.0000'),
        )
        ar = _create_request(
            patient, lab_admin, make_request,
            source_type=SourceType.PARTNER_ORGANIZATION,
            partner_organization_id=partner.id,
            billing_mode=BillingMode.PARTNER_BILLING,
            items=[{'exam_definition_id': exam.id}],
        )
        item = ar.items.first()
        assert item.unit_price == Decimal('50.0000')
        assert item.billed_price == Decimal('35.0000')
        assert item.price_source == PriceSource.PARTNER_AGREED_PRICE

    def test_falls_back_to_unit_price_when_no_agreed_row(
        self, patient, exam, partner, lab_admin, make_request,
    ):
        ar = _create_request(
            patient, lab_admin, make_request,
            source_type=SourceType.PARTNER_ORGANIZATION,
            partner_organization_id=partner.id,
            billing_mode=BillingMode.PARTNER_BILLING,
            items=[{'exam_definition_id': exam.id}],
        )
        item = ar.items.first()
        assert item.billed_price == Decimal('50.0000')
        assert item.price_source == PriceSource.DEFAULT_PRICE

    def test_ignores_inactive_agreed_price(
        self, patient, exam, partner, lab_admin, make_request,
    ):
        PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam,
            agreed_price=Decimal('35.0000'), is_active=False,
        )
        ar = _create_request(
            patient, lab_admin, make_request,
            source_type=SourceType.PARTNER_ORGANIZATION,
            partner_organization_id=partner.id,
            billing_mode=BillingMode.PARTNER_BILLING,
            items=[{'exam_definition_id': exam.id}],
        )
        item = ar.items.first()
        assert item.billed_price == Decimal('50.0000')
        assert item.price_source == PriceSource.DEFAULT_PRICE

    def test_agreed_price_is_partner_scoped(
        self, patient, exam, partner, other_partner, lab_admin, make_request,
    ):
        """
        Agreed price exists for ``other_partner``, but the request is
        booked under ``partner``. The ``other_partner`` row must NOT apply.
        """
        PartnerExamPrice.objects.create(
            partner=other_partner, exam_definition=exam, agreed_price=Decimal('25.0000'),
        )
        ar = _create_request(
            patient, lab_admin, make_request,
            source_type=SourceType.PARTNER_ORGANIZATION,
            partner_organization_id=partner.id,
            billing_mode=BillingMode.PARTNER_BILLING,
            items=[{'exam_definition_id': exam.id}],
        )
        item = ar.items.first()
        assert item.billed_price == Decimal('50.0000')
        assert item.price_source == PriceSource.DEFAULT_PRICE

    def test_mixed_items_one_agreed_one_not(
        self, patient, exam, exam_b, partner, lab_admin, make_request,
    ):
        """
        A request with two items: one has an agreed price, the other
        does not. Each item resolves independently.
        """
        PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('35.0000'),
        )
        ar = _create_request(
            patient, lab_admin, make_request,
            source_type=SourceType.PARTNER_ORGANIZATION,
            partner_organization_id=partner.id,
            billing_mode=BillingMode.PARTNER_BILLING,
            items=[
                {'exam_definition_id': exam.id},
                {'exam_definition_id': exam_b.id},
            ],
        )
        items = {i.exam_definition.code: i for i in ar.items.all()}
        assert items['GLU'].billed_price == Decimal('35.0000')
        assert items['GLU'].price_source == PriceSource.PARTNER_AGREED_PRICE
        assert items['HBA1C'].billed_price == Decimal('80.0000')
        assert items['HBA1C'].price_source == PriceSource.DEFAULT_PRICE


# ---------------------------------------------------------------------------
# Historical integrity — persisted items are snapshots
# ---------------------------------------------------------------------------

class TestHistoricalIntegrity:

    def test_changing_unit_price_after_creation_does_not_affect_item(
        self, patient, exam, lab_admin, make_request,
    ):
        ar = _create_request(patient, lab_admin, make_request, items=[
            {'exam_definition_id': exam.id},
        ])
        item = ar.items.first()
        original_unit = item.unit_price
        original_billed = item.billed_price

        exam.unit_price = Decimal('999.0000')
        exam.save()

        item.refresh_from_db()
        assert item.unit_price == original_unit
        assert item.billed_price == original_billed

    def test_changing_agreed_price_after_creation_does_not_affect_item(
        self, patient, exam, partner, lab_admin, make_request,
    ):
        agreed = PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('35.0000'),
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

        agreed.agreed_price = Decimal('99.0000')
        agreed.save()

        item.refresh_from_db()
        assert item.billed_price == Decimal('35.0000')  # unchanged


# ---------------------------------------------------------------------------
# Manual override escape hatch (legacy draft-edit flow)
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

    def test_manual_override_beats_partner_agreed(
        self, patient, exam, partner, lab_admin, make_request,
    ):
        PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('35.0000'),
        )
        ar = _create_request(
            patient, lab_admin, make_request,
            source_type=SourceType.PARTNER_ORGANIZATION,
            partner_organization_id=partner.id,
            billing_mode=BillingMode.PARTNER_BILLING,
            items=[{'exam_definition_id': exam.id, 'billed_price': Decimal('42.0000')}],
        )
        item = ar.items.first()
        assert item.billed_price == Decimal('42.0000')
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

    def test_null_override_re_resolves_to_agreed_or_unit(
        self, patient, exam, partner, lab_admin, make_request,
    ):
        """Setting ``billed_price=None`` re-resolves through the new
        resolver — picking up PartnerExamPrice when applicable."""
        PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('35.0000'),
        )
        ar = _create_request(
            patient, lab_admin, make_request,
            source_type=SourceType.PARTNER_ORGANIZATION,
            partner_organization_id=partner.id,
            billing_mode=BillingMode.PARTNER_BILLING,
            items=[{'exam_definition_id': exam.id, 'billed_price': Decimal('99.0000')}],
        )
        item = ar.items.first()
        assert item.price_source == PriceSource.MANUAL_OVERRIDE

        item = AnalysisRequestItemService.update(
            item=item,
            validated_data={'billed_price': None},
            updated_by=lab_admin,
            request=make_request(lab_admin),
        )
        assert item.billed_price == Decimal('35.0000')
        assert item.price_source == PriceSource.PARTNER_AGREED_PRICE


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

    def test_partner_agreed_price_source(
        self, patient, exam, partner, lab_admin, make_request,
    ):
        PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('35.0000'),
        )
        ar = _create_request(
            patient, lab_admin, make_request,
            source_type=SourceType.PARTNER_ORGANIZATION,
            partner_organization_id=partner.id,
            billing_mode=BillingMode.PARTNER_BILLING,
            items=[{'exam_definition_id': exam.id}],
        )
        item = ar.items.first()
        assert item.price_source == PriceSource.PARTNER_AGREED_PRICE

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

    def test_confirm_preserves_manual_override(
        self, patient, exam, lab_admin, make_request,
    ):
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

    def test_confirm_preserves_partner_agreed(
        self, patient, exam, partner, lab_admin, make_request,
    ):
        PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam, agreed_price=Decimal('35.0000'),
        )
        ar = _create_request(
            patient, lab_admin, make_request,
            source_type=SourceType.PARTNER_ORGANIZATION,
            partner_organization_id=partner.id,
            billing_mode=BillingMode.PARTNER_BILLING,
            items=[{'exam_definition_id': exam.id}],
        )
        ar = AnalysisRequestService.confirm(
            analysis_request=ar,
            confirmed_by=lab_admin,
            request=make_request(lab_admin),
        )
        item = ar.items.first()
        assert item.billed_price == Decimal('35.0000')
        assert item.price_source == PriceSource.PARTNER_AGREED_PRICE

    def test_confirm_no_items_fails(self, patient, lab_admin, make_request):
        ar = _create_request(patient, lab_admin, make_request)
        with pytest.raises(Exception, match='no items'):
            AnalysisRequestService.confirm(
                analysis_request=ar,
                confirmed_by=lab_admin,
                request=make_request(lab_admin),
            )

    def test_add_item_after_create_uses_new_resolver(
        self, patient, exam, exam_b, partner, lab_admin, make_request,
    ):
        """Items added to a DRAFT request after creation go through the
        same resolver as inline items."""
        PartnerExamPrice.objects.create(
            partner=partner, exam_definition=exam_b, agreed_price=Decimal('70.0000'),
        )
        ar = _create_request(
            patient, lab_admin, make_request,
            source_type=SourceType.PARTNER_ORGANIZATION,
            partner_organization_id=partner.id,
            billing_mode=BillingMode.PARTNER_BILLING,
            items=[{'exam_definition_id': exam.id}],
        )
        item = AnalysisRequestService.add_item(
            analysis_request=ar,
            validated_data={'exam_definition_id': exam_b.id},
            added_by=lab_admin,
            request=make_request(lab_admin),
        )
        assert item.unit_price == Decimal('80.0000')
        assert item.billed_price == Decimal('70.0000')
        assert item.price_source == PriceSource.PARTNER_AGREED_PRICE


# ---------------------------------------------------------------------------
# Execution mode change re-pricing
# ---------------------------------------------------------------------------

class TestExecutionModeRepricing:

    def test_switching_to_rejected_zeros_prices(
        self, patient, exam, lab_admin, make_request,
    ):
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

    def test_switching_from_rejected_back_to_internal(
        self, patient, exam, lab_admin, make_request,
    ):
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
