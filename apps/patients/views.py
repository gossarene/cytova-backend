"""
Cytova — Patients Views

PatientViewSet covers:
    GET    /patients/                       — list patients           (all staff)
    POST   /patients/                       — register patient        (receptionist, lab_admin)
    GET    /patients/{id}/                  — retrieve patient        (all staff)
    PATCH  /patients/{id}/                  — update patient info     (receptionist, lab_admin)
    POST   /patients/{id}/deactivate/       — deactivate patient      (lab_admin)
    POST   /patients/{id}/portal-account/   — create portal account   (receptionist, lab_admin)
    DELETE /patients/{id}/portal-account/   — remove portal account   (lab_admin)

Tenant isolation is automatic: the schema search_path is set per request
by TenantMiddleware, so all ORM queries are already scoped to this tenant.
No explicit tenant filter is needed anywhere in this module.
"""
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from common.permissions import IsAnyStaff, IsLabAdmin, IsReceptionistOrLabAdmin
from .filters import PatientFilter
from .models import Patient, PatientPortalAccount
from .serializers import (
    PatientListSerializer,
    PatientDetailSerializer,
    PatientCreateSerializer,
    PatientUpdateSerializer,
    PortalAccountCreateSerializer,
    PortalAccountSerializer,
)
from .services import PatientService


class PatientViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    filterset_class = PatientFilter
    search_fields = ['first_name', 'last_name', 'national_id']
    ordering_fields = ['last_name', 'first_name', 'national_id', 'created_at', 'date_of_birth']

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
        if self.action in ('list', 'retrieve'):
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
        serializer = PatientUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        if not serializer.validated_data:
            raise ValidationError('No fields provided for update.')
        patient = PatientService.update_patient(
            patient, dict(serializer.validated_data), request.user, request
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
