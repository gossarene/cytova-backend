"""
Cytova — Patients Views

PatientViewSet covers:
    GET    /patients/                           — list patients           (all staff)
    POST   /patients/                           — register patient        (receptionist, lab_admin)
    GET    /patients/{id}/                      — retrieve patient        (all staff)
    PATCH  /patients/{id}/                      — update patient info     (receptionist, lab_admin)
    POST   /patients/{id}/deactivate/           — deactivate patient      (lab_admin)
    POST   /patients/{id}/portal-account/       — create portal account   (receptionist, lab_admin)
    DELETE /patients/{id}/portal-account/       — remove portal account   (lab_admin)
    GET    /patients/{id}/requests/             — recent requests         (all staff)
    GET    /patients/{id}/request-stats/        — request stats           (all staff)

Tenant isolation is automatic: the schema search_path is set per request
by TenantMiddleware, so all ORM queries are already scoped to this tenant.
No explicit tenant filter is needed anywhere in this module.
"""
from django.db.models import Count, Q
from rest_framework import serializers as drf_serializers, status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from common.permissions import IsAnyStaff, IsLabAdmin, IsReceptionistOrLabAdmin
from common.permission_checker import PermissionChecker
from apps.requests.models import AnalysisRequest
from .filters import PatientFilter
from .models import Patient, PatientPortalAccount
from .serializers import (
    PatientListSerializer,
    PatientDetailSerializer,
    PatientCreateSerializer,
    PatientUpdateSerializer,
    PatientIdentityUpdateSerializer,
    PortalAccountCreateSerializer,
    PortalAccountSerializer,
)
from .services import PatientService


# ---------------------------------------------------------------------------
# Compact serializer for patient-scoped request list
# ---------------------------------------------------------------------------

class PatientRequestSerializer(drf_serializers.ModelSerializer):
    """Compact request representation for the patient detail page."""
    items_count = drf_serializers.SerializerMethodField()
    partner_organization_name = drf_serializers.CharField(
        source='partner_organization.name', default=None, read_only=True,
    )

    class Meta:
        model = AnalysisRequest
        fields = [
            'id', 'request_number', 'status', 'source_type',
            'partner_organization_name',
            'items_count', 'created_at',
        ]

    def get_items_count(self, obj):
        return obj.items.count()


class PatientViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    filterset_class = PatientFilter
    search_fields = ['first_name', 'last_name', 'document_number']
    ordering_fields = ['last_name', 'first_name', 'document_number', 'created_at', 'date_of_birth']

    def get_queryset(self):
        return (
            Patient.objects
            .select_related('created_by')
            .prefetch_related('portal_account')
            .all()
        )

    def get_serializer_class(self):
        if self.action == 'list':
            return PatientListSerializer
        if self.action == 'create':
            return PatientCreateSerializer
        if self.action == 'partial_update':
            return PatientUpdateSerializer
        return PatientDetailSerializer

    def get_permissions(self):
        if self.action in ('list', 'retrieve', 'requests', 'request_stats'):
            return [IsAnyStaff()]
        if self.action in ('create', 'partial_update', 'portal_account_create'):
            return [IsReceptionistOrLabAdmin()]
        # deactivate, portal_account_delete
        return [IsLabAdmin()]

    # ------------------------------------------------------------------
    # Standard actions
    # ------------------------------------------------------------------

    def create(self, request):
        serializer = PatientCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        patient = PatientService.create_patient(
            dict(serializer.validated_data), request.user, request
        )
        return Response(
            PatientDetailSerializer(patient, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, pk=None):
        patient = self.get_object()

        # Separate identity fields from normal fields
        identity_fields = {'document_type', 'document_number'}
        has_identity_fields = bool(identity_fields & set(request.data.keys()))

        # Validate and collect identity field updates (if present)
        identity_data = {}
        if has_identity_fields:
            if not PermissionChecker.has_permission(request.user, 'patients.update_identity'):
                raise ValidationError({
                    'detail': 'You do not have permission to edit identity document fields.'
                })
            identity_serializer = PatientIdentityUpdateSerializer(
                data={k: v for k, v in request.data.items() if k in identity_fields},
                context={'patient': patient},
                partial=True,
            )
            identity_serializer.is_valid(raise_exception=True)
            identity_data = dict(identity_serializer.validated_data)

        # Validate normal fields
        normal_data_input = {k: v for k, v in request.data.items() if k not in identity_fields}
        normal_serializer = PatientUpdateSerializer(data=normal_data_input, partial=True)
        normal_serializer.is_valid(raise_exception=True)
        normal_data = dict(normal_serializer.validated_data)

        merged = {**normal_data, **identity_data}
        if not merged:
            raise ValidationError('No fields provided for update.')

        patient = PatientService.update_patient(
            patient, merged, request.user, request
        )
        return Response(PatientDetailSerializer(patient, context={'request': request}).data)

    # ------------------------------------------------------------------
    # Custom actions
    # ------------------------------------------------------------------

    @action(detail=True, methods=['post'])
    def deactivate(self, request, pk=None):
        patient = self.get_object()
        patient = PatientService.deactivate_patient(patient, request.user, request)
        return Response(PatientDetailSerializer(patient, context={'request': request}).data)

    @action(
        detail=True,
        methods=['post', 'delete'],
        url_path='portal-account',
        url_name='portal-account',
    )
    def portal_account(self, request, pk=None):
        patient = self.get_object()

        if request.method == 'POST':
            return self._create_portal_account(request, patient)
        return self._delete_portal_account(request, patient)

    def _create_portal_account(self, request, patient: Patient) -> Response:
        if patient.has_portal_account:
            raise ValidationError('This patient already has a portal account.')

        serializer = PortalAccountCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        account = PatientService.create_portal_account(
            patient=patient,
            email=serializer.validated_data['email'],
            created_by=request.user,
            request=request,
        )

        return Response(
            PortalAccountSerializer(account).data,
            status=status.HTTP_201_CREATED,
        )

    def _delete_portal_account(self, request, patient: Patient) -> Response:
        # Only LAB_ADMIN can delete — enforce here since the action handles two methods
        if not request.user.is_lab_admin:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied('Lab Admin role required to remove a portal account.')

        try:
            account = patient.portal_account
        except PatientPortalAccount.DoesNotExist:
            raise NotFound('This patient has no portal account.')

        PatientService.delete_portal_account(account, request.user, request)
        return Response(status=status.HTTP_204_NO_CONTENT)

    # ------------------------------------------------------------------
    # Patient-scoped request data
    # ------------------------------------------------------------------

    @action(detail=True, methods=['get'], url_path='requests')
    def requests(self, request, pk=None):
        """
        GET /patients/{id}/requests/?limit=5

        Returns the most recent analysis requests for this patient,
        ordered newest-first. Default limit: 5, max: 20.
        """
        patient = self.get_object()
        limit = min(int(request.query_params.get('limit', 5)), 20)

        qs = (
            AnalysisRequest.objects
            .filter(patient=patient)
            .select_related('patient', 'partner_organization')
            .prefetch_related('items')
            .order_by('-created_at')[:limit]
        )

        serializer = PatientRequestSerializer(qs, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'], url_path='request-stats')
    def request_stats(self, request, pk=None):
        """
        GET /patients/{id}/request-stats/

        Returns lightweight aggregated stats for this patient's requests.
        """
        patient = self.get_object()
        qs = AnalysisRequest.objects.filter(patient=patient)

        total = qs.count()

        by_status = {
            row['status']: row['count']
            for row in qs.values('status').annotate(count=Count('id')).order_by('status')
        }

        by_source = {
            row['source_type']: row['count']
            for row in qs.values('source_type').annotate(count=Count('id')).order_by('source_type')
        }

        return Response({
            'total_requests': total,
            'requests_by_status': by_status,
            'requests_by_source': by_source,
        })
