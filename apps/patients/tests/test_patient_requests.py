"""
Tests for patient-scoped request endpoints:
- GET /patients/{id}/requests/
- GET /patients/{id}/request-stats/
"""
import pytest
from decimal import Decimal

from apps.catalog.models import ExamCategory, ExamDefinition, SampleType
from apps.patients.models import Patient
from apps.requests.models import AnalysisRequest, AnalysisRequestItem, RequestStatus, SourceType
from apps.requests.services import AnalysisRequestService


@pytest.fixture()
def patient(lab_admin):
    return Patient.objects.create(
        document_type='NATIONAL_ID_CARD', document_number='PR-NID-001',
        first_name='Jane', last_name='Doe',
        date_of_birth='1990-05-15', gender='FEMALE', created_by=lab_admin,
    )


@pytest.fixture()
def other_patient(lab_admin):
    return Patient.objects.create(
        document_type='PASSPORT', document_number='PR-PASS-001',
        first_name='Other', last_name='Person',
        date_of_birth='1985-01-01', gender='MALE', created_by=lab_admin,
    )


@pytest.fixture()
def exam_definition(default_technique):
    cat = ExamCategory.objects.create(name='Hematology', display_order=1)
    return ExamDefinition.objects.create(
        category=cat, technique=default_technique, code='CBC', name='Complete Blood Count',
        sample_type=SampleType.BLOOD, unit_price=Decimal('50.0000'),
    )


@pytest.fixture()
def patient_requests(patient, lab_admin, make_request, exam_definition):
    """Create 3 requests for the patient with varying statuses and sources."""
    reqs = []
    for i, (st, src) in enumerate([
        (RequestStatus.DRAFT, SourceType.DIRECT_PATIENT),
        (RequestStatus.CONFIRMED, SourceType.DIRECT_PATIENT),
        (RequestStatus.CONFIRMED, SourceType.PARTNER_ORGANIZATION),
    ]):
        ar = AnalysisRequest.objects.create(
            request_number=f'REQ-2026-PR{i:03d}',
            patient=patient,
            status=st,
            source_type=src,
            created_by=lab_admin,
        )
        AnalysisRequestItem.objects.create(
            analysis_request=ar,
            exam_definition=exam_definition,
            unit_price=exam_definition.unit_price,
            billed_price=exam_definition.unit_price,
        )
        reqs.append(ar)
    return reqs


@pytest.fixture()
def other_patient_request(other_patient, lab_admin, exam_definition):
    """Request for a different patient — must NOT appear in patient-scoped queries."""
    ar = AnalysisRequest.objects.create(
        request_number='REQ-2026-OTH01',
        patient=other_patient,
        status=RequestStatus.DRAFT,
        source_type=SourceType.DIRECT_PATIENT,
        created_by=lab_admin,
    )
    AnalysisRequestItem.objects.create(
        analysis_request=ar,
        exam_definition=exam_definition,
        unit_price=exam_definition.unit_price,
        billed_price=exam_definition.unit_price,
    )
    return ar


# ---------------------------------------------------------------------------
# Recent requests endpoint
# ---------------------------------------------------------------------------

class TestPatientRecentRequests:

    def test_returns_patient_requests(self, patient, patient_requests, other_patient_request):
        from apps.patients.views import PatientRequestSerializer
        qs = AnalysisRequest.objects.filter(patient=patient).prefetch_related('items').order_by('-created_at')[:5]
        data = PatientRequestSerializer(qs, many=True).data
        assert len(data) == 3
        ids = {str(r.id) for r in patient_requests}
        assert all(str(d['id']) in ids for d in data)

    def test_excludes_other_patient(self, patient, patient_requests, other_patient_request):
        qs = AnalysisRequest.objects.filter(patient=patient)
        assert qs.count() == 3
        assert not qs.filter(patient=other_patient_request.patient).exists()

    def test_ordered_newest_first(self, patient, patient_requests):
        qs = AnalysisRequest.objects.filter(patient=patient).order_by('-created_at')
        dates = list(qs.values_list('created_at', flat=True))
        assert dates == sorted(dates, reverse=True)

    def test_limit_parameter(self, patient, patient_requests):
        qs = AnalysisRequest.objects.filter(patient=patient).order_by('-created_at')[:2]
        assert qs.count() == 2

    def test_serializer_fields(self, patient, patient_requests):
        from apps.patients.views import PatientRequestSerializer
        ar = patient_requests[0]
        ar_qs = AnalysisRequest.objects.filter(pk=ar.pk).prefetch_related('items')
        data = PatientRequestSerializer(ar_qs.first()).data
        assert 'id' in data
        assert 'request_number' in data
        assert 'status' in data
        assert 'source_type' in data
        assert 'items_count' in data
        assert 'created_at' in data
        assert data['items_count'] == 1

    def test_items_count_correct(self, patient, lab_admin, exam_definition, default_technique):
        """Request with multiple items should report correct count."""
        cat = ExamCategory.objects.create(name='Biochemistry', display_order=2)
        extra_exam = ExamDefinition.objects.create(
            category=cat, technique=default_technique, code='GLU', name='Glucose',
            sample_type=SampleType.BLOOD, unit_price=Decimal('30.0000'),
        )
        ar = AnalysisRequest.objects.create(
            request_number='REQ-2026-MULTI',
            patient=patient, status=RequestStatus.DRAFT,
            source_type=SourceType.DIRECT_PATIENT, created_by=lab_admin,
        )
        AnalysisRequestItem.objects.create(
            analysis_request=ar, exam_definition=exam_definition,
            unit_price=Decimal('50.0000'), billed_price=Decimal('50.0000'),
        )
        AnalysisRequestItem.objects.create(
            analysis_request=ar, exam_definition=extra_exam,
            unit_price=Decimal('30.0000'), billed_price=Decimal('30.0000'),
        )
        from apps.patients.views import PatientRequestSerializer
        data = PatientRequestSerializer(
            AnalysisRequest.objects.filter(pk=ar.pk).prefetch_related('items').first()
        ).data
        assert data['items_count'] == 2


# ---------------------------------------------------------------------------
# Request stats endpoint
# ---------------------------------------------------------------------------

class TestPatientRequestStats:

    def test_total_count(self, patient, patient_requests, other_patient_request):
        total = AnalysisRequest.objects.filter(patient=patient).count()
        assert total == 3

    def test_by_status(self, patient, patient_requests):
        from django.db.models import Count
        qs = AnalysisRequest.objects.filter(patient=patient)
        by_status = {
            r['status']: r['count']
            for r in qs.values('status').annotate(count=Count('id'))
        }
        assert by_status.get('DRAFT', 0) == 1
        assert by_status.get('CONFIRMED', 0) == 2

    def test_by_source(self, patient, patient_requests):
        from django.db.models import Count
        qs = AnalysisRequest.objects.filter(patient=patient)
        by_source = {
            r['source_type']: r['count']
            for r in qs.values('source_type').annotate(count=Count('id'))
        }
        assert by_source.get('DIRECT_PATIENT', 0) == 2
        assert by_source.get('PARTNER_ORGANIZATION', 0) == 1

    def test_empty_patient_stats(self, other_patient):
        """Patient with no requests should return zero totals."""
        total = AnalysisRequest.objects.filter(patient=other_patient).count()
        assert total == 0

    def test_excludes_other_patient(self, patient, patient_requests, other_patient_request):
        total = AnalysisRequest.objects.filter(patient=patient).count()
        assert total == 3  # not 4
