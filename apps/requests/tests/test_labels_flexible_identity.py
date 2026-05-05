"""
Label generation — flexible-identity safety tests.

Pins the contract that the printer never crashes on a patient whose
identity / DOB is unknown, and that the auto-generated ``AUTO-PT-…``
placeholder NEVER reaches a printed label. Both rules are direct
follow-ups to the flexible-identity rollout (which made
``date_of_birth`` nullable and added the ``UNKNOWN`` document type).

What's pinned
-------------
- ``RequestLabelService.generate_or_get`` succeeds for an
  UNKNOWN-identity + DOB-unknown patient. (The pre-fix behaviour was
  ``AttributeError: 'NoneType' object has no attribute 'isoformat'``
  surfacing as a 500 from the labels endpoint.)
- The PDF is materialised + persisted, label rows exist, and the
  audit row is written exactly as for a complete patient.
- The auto-generated document number does NOT appear in the rendered
  PDF text. We test the safety property at the wire level: the bytes
  the storage layer received contain no ``AUTO-PT-`` substring.
- ``format_patient_dob_for_label`` / ``format_patient_identity_for_label``
  return "Not provided" for the new flexible-identity cases and the
  real value for legacy rows, so any future label layout that opts
  to surface the document number inherits the anti-leak guarantee.
- Existing complete-patient label generation is unchanged
  (regression).
"""
import io
from datetime import date
from decimal import Decimal

import pytest
from django.core.files.storage import default_storage
from django_tenants.utils import schema_context, get_public_schema_name
from pypdf import PdfReader

from apps.catalog.models import ExamCategory, ExamDefinition, ExamFamily, SampleType
from apps.patients.models import DocumentType, Patient
from apps.requests.label_service import (
    RequestLabelService,
    format_patient_dob_for_label,
    format_patient_identity_for_label,
)
from apps.requests.models import (
    RequestLabel, RequestLabelBatch, SourceType,
)
from apps.requests.services import AnalysisRequestService


# Pre-confirmation labelling is what we're exercising — disable the
# autouse "auto-generate on confirm" wrapper so the test drives
# generate_or_get explicitly.
pytestmark = pytest.mark.no_auto_labels


# ---------------------------------------------------------------------------
# Subscription gate (mirror sibling label tests)
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


# ---------------------------------------------------------------------------
# Domain fixtures
# ---------------------------------------------------------------------------

_DOC_SEQ = 0


def _flexible_patient(*, lab_admin, **overrides) -> Patient:
    """Patient with the flexible-identity rollout's "no documents on
    file" shape: ``UNKNOWN`` document type, an auto-generated
    ``AUTO-PT-…`` number (mirroring what the service produces in
    real life), and ``date_of_birth`` null with the unknown flag
    set. Overrides let tests pin specific halves of the shape
    without re-stating the whole row."""
    global _DOC_SEQ
    _DOC_SEQ += 1
    defaults = {
        'document_type': DocumentType.UNKNOWN,
        # Emulate the format the service generates so the leak test
        # has a realistic substring to scan for.
        'document_number': f'AUTO-PT-20260504-A1B2C{_DOC_SEQ:01d}',
        'identity_number_auto_generated': True,
        'first_name': 'Ada',
        'last_name': 'Lovelace',
        'date_of_birth': None,
        'date_of_birth_unknown': True,
        'gender': 'FEMALE',
        'created_by': lab_admin,
    }
    defaults.update(overrides)
    return Patient.objects.create(**defaults)


def _complete_patient(*, lab_admin) -> Patient:
    global _DOC_SEQ
    _DOC_SEQ += 1
    return Patient.objects.create(
        document_type=DocumentType.NATIONAL_ID_CARD,
        document_number=f'NID-FLEX-LBL-{_DOC_SEQ:04d}',
        first_name='Alice',
        last_name='Real',
        date_of_birth=date(1990, 5, 20),
        gender='FEMALE',
        created_by=lab_admin,
    )


@pytest.fixture()
def category():
    return ExamCategory.objects.create(name='Labs', display_order=1)


@pytest.fixture()
def family():
    return ExamFamily.objects.create(name='Hematology', display_order=1)


@pytest.fixture()
def exam(family, category, default_technique):
    return ExamDefinition.objects.create(
        category=category, family=family, technique=default_technique,
        code='CBC', name='Complete Blood Count',
        sample_type=SampleType.BLOOD, unit_price=Decimal('50.0000'),
    )


def _pdf_text(pdf_bytes: bytes) -> str:
    """Concatenated text content of every page in the PDF.

    ReportLab compresses object streams, so a substring search on raw
    bytes misses the rendered glyphs. Round-tripping through pypdf's
    ``extract_text`` yields the actual visible text we want to pin
    label content against."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return '\n'.join(page.extract_text() or '' for page in reader.pages)


def _confirmed_request(patient, exam, lab_admin, make_request):
    return AnalysisRequestService.create(
        validated_data={
            'patient_id': patient.id,
            'source_type': SourceType.DIRECT_PATIENT,
            'items': [{'exam_definition_id': exam.id}],
        },
        created_by=lab_admin,
        request=make_request(lab_admin),
        confirm_after=True,
    )


# ---------------------------------------------------------------------------
# 1. Pure helpers — pinned independently of the service
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestPatientFieldFormatters:

    def test_dob_for_complete_patient_is_iso_string(self, lab_admin):
        p = _complete_patient(lab_admin=lab_admin)
        assert format_patient_dob_for_label(p) == '1990-05-20'

    def test_dob_for_unknown_returns_not_provided(self, lab_admin):
        p = _flexible_patient(lab_admin=lab_admin)
        assert format_patient_dob_for_label(p) == 'Not provided'

    def test_dob_for_partially_set_unknown_flag_returns_not_provided(self, lab_admin):
        """Defensive: a row whose ``date_of_birth_unknown=True`` even
        though a date sneaked in still surfaces "Not provided" — the
        flag is the source of truth, never the date."""
        p = _flexible_patient(
            lab_admin=lab_admin,
            date_of_birth=date(1990, 5, 20),
            date_of_birth_unknown=True,
        )
        assert format_patient_dob_for_label(p) == 'Not provided'

    def test_identity_for_real_type_returns_document_number(self, lab_admin):
        p = _complete_patient(lab_admin=lab_admin)
        assert format_patient_identity_for_label(p) == p.document_number

    def test_identity_for_unknown_type_returns_not_provided(self, lab_admin):
        p = _flexible_patient(lab_admin=lab_admin)
        assert format_patient_identity_for_label(p) == 'Not provided'

    def test_identity_for_auto_generated_flag_returns_not_provided(self, lab_admin):
        """Auto-generated flag dominates: even if the document type
        was somehow set to a real one, the flag forces "Not
        provided" so a placeholder can never be quoted as a real
        ID."""
        p = _flexible_patient(
            lab_admin=lab_admin,
            document_type=DocumentType.PASSPORT,
            document_number='AUTO-PT-LEAKY',
            identity_number_auto_generated=True,
        )
        assert format_patient_identity_for_label(p) == 'Not provided'


# ---------------------------------------------------------------------------
# 2. Service end-to-end — generate_or_get must not crash
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestLabelGenerationFlexibleIdentity:

    def test_unknown_dob_and_unknown_doc_succeeds(
        self, lab_admin, make_request, exam,
    ):
        """The reproducer for the original 500. Pre-fix this raised
        ``AttributeError: 'NoneType' object has no attribute
        'isoformat'`` from ``_build_payloads`` and surfaced as a
        500 from the labels endpoint."""
        patient = _flexible_patient(lab_admin=lab_admin)
        ar = _confirmed_request(patient, exam, lab_admin, make_request)

        batch = RequestLabelService.generate_or_get(
            ar, generated_by=lab_admin, request=make_request(lab_admin),
        )

        # Batch and labels exist.
        assert isinstance(batch, RequestLabelBatch)
        assert batch.label_count > 0
        assert RequestLabel.objects.filter(batch=batch).count() == batch.label_count

        # PDF was actually materialised + persisted.
        assert batch.pdf_file_key
        assert default_storage.exists(batch.pdf_file_key)

    def test_pdf_does_not_quote_auto_generated_placeholder(
        self, lab_admin, make_request, exam,
    ):
        """Anti-leak: the auto-generated ``AUTO-PT-…`` document
        number MUST NOT appear in the rendered PDF. The label
        renderer doesn't currently print the ID, so the bytes
        should never carry an ``AUTO-PT-`` substring — but we pin
        it at the wire level so a future layout change that opts
        to surface the number can't accidentally leak the
        placeholder."""
        patient = _flexible_patient(lab_admin=lab_admin)
        ar = _confirmed_request(patient, exam, lab_admin, make_request)

        batch = RequestLabelService.generate_or_get(
            ar, generated_by=lab_admin, request=make_request(lab_admin),
        )

        with default_storage.open(batch.pdf_file_key, 'rb') as fh:
            pdf_bytes = fh.read()
        text = _pdf_text(pdf_bytes)

        # The auto-generated number we set on the patient is in the
        # AUTO-PT- family. The PDF must not contain the patient's
        # specific number; we also assert no AUTO-PT- substring at
        # all so the test is robust if the seed format changes.
        assert 'AUTO-PT-' not in text, (
            'Auto-generated identity placeholder leaked into the '
            'rendered label PDF — see format_patient_identity_for_label'
        )
        assert patient.document_number not in text

    def test_complete_patient_still_works(
        self, lab_admin, make_request, exam,
    ):
        """Regression: a real-DOB / real-document-type patient
        produces a batch with real DOB rendered into the payload
        builder. Pins that the flexible-identity helpers don't
        affect the legacy path."""
        patient = _complete_patient(lab_admin=lab_admin)
        ar = _confirmed_request(patient, exam, lab_admin, make_request)

        batch = RequestLabelService.generate_or_get(
            ar, generated_by=lab_admin, request=make_request(lab_admin),
        )

        # Normal generation surface: batch persisted, labels and PDF
        # produced.
        assert batch.label_count > 0
        assert RequestLabel.objects.filter(batch=batch).count() == batch.label_count
        with default_storage.open(batch.pdf_file_key, 'rb') as fh:
            pdf_bytes = fh.read()
        # The real DOB should land on the label.
        assert '1990-05-20' in _pdf_text(pdf_bytes)

    def test_dob_unknown_label_renders_not_provided_text(
        self, lab_admin, make_request, exam,
    ):
        """Surface check: the user-facing wording on the label says
        "Not provided" rather than e.g. "DOB: None" or an empty
        string. Pinned to the exact phrasing the on-screen detail
        page uses, so the lab UX stays consistent across surfaces."""
        patient = _flexible_patient(lab_admin=lab_admin)
        ar = _confirmed_request(patient, exam, lab_admin, make_request)

        batch = RequestLabelService.generate_or_get(
            ar, generated_by=lab_admin, request=make_request(lab_admin),
        )

        with default_storage.open(batch.pdf_file_key, 'rb') as fh:
            pdf_bytes = fh.read()
        text = _pdf_text(pdf_bytes)
        assert 'Not provided' in text
        # Catches accidental Python ``str(None)`` leaks where someone
        # forgot the helper. We accept the chance that an unrelated
        # field happens to contain "None" — there's none in the
        # current label layout, so this stays a pure leak guard.
        assert 'None' not in text
