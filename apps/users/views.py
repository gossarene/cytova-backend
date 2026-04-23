"""
Cytova — Users Views

UserViewSet covers:
    GET    /users/                       — list staff users          (users.view)
    POST   /users/                       — create staff user         (users.create)
    GET    /users/{id}/                  — retrieve staff user       (users.view)
    PATCH  /users/{id}/                  — update name / role        (users.update)
    POST   /users/{id}/deactivate/       — deactivate user           (users.deactivate)
    POST   /users/{id}/activate/         — reactivate user           (users.activate)
    POST   /users/{id}/assign-role/      — change role               (users.assign_role)
    GET    /users/{id}/permissions/      — effective permissions      (users.view)
    POST   /users/{id}/permissions/      — grant/revoke/remove       (users.manage_permissions)
    GET    /users/me/                    — own profile               (all staff)
    PATCH  /users/me/                    — update own name/password  (all staff)
    GET    /users/roles/                 — list available roles       (all staff)
    GET    /users/permissions-catalog/   — list all permissions       (all staff)
"""
import logging
import os
import uuid

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.http import FileResponse
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.parsers import JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from common.permissions import IsAnyStaff, RequiresPermission
from .filters import StaffUserFilter
from .models import StaffUser, Role, UserPermissionOverride
from .serializers import (
    StaffUserListSerializer,
    StaffUserDetailSerializer,
    StaffUserCreateSerializer,
    StaffUserUpdateSerializer,
    SignatureUploadSerializer,
    MeSerializer,
    MeUpdateSerializer,
    RoleAssignSerializer,
    PermissionOverrideSerializer,
    UserPermissionOverrideSerializer,
)
from .services import UserService

logger = logging.getLogger(__name__)


class UserViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    filterset_class = StaffUserFilter
    search_fields = ['first_name', 'last_name', 'email']
    ordering_fields = ['last_name', 'first_name', 'created_at', 'role']

    def get_queryset(self):
        return (
            StaffUser.objects
            .select_related('created_by')
            .prefetch_related('permission_overrides')
            .all()
        )

    def get_serializer_class(self):
        if self.action == 'list':
            return StaffUserListSerializer
        if self.action == 'create':
            return StaffUserCreateSerializer
        if self.action == 'partial_update':
            return StaffUserUpdateSerializer
        return StaffUserDetailSerializer

    def get_permissions(self):
        if self.action in ('me', 'my_signature', 'roles', 'permissions_catalog'):
            return [IsAnyStaff()]
        if self.action in ('list', 'retrieve', 'user_permissions'):
            return [RequiresPermission('users.view')()]
        if self.action == 'create':
            return [RequiresPermission('users.create')()]
        if self.action == 'partial_update':
            return [RequiresPermission('users.update')()]
        if self.action == 'deactivate':
            return [RequiresPermission('users.deactivate')()]
        if self.action == 'activate':
            return [RequiresPermission('users.activate')()]
        if self.action == 'assign_role':
            return [RequiresPermission('users.assign_role')()]
        if self.action == 'manage_permissions':
            return [RequiresPermission('users.manage_permissions')()]
        # Default: require users.view
        return [RequiresPermission('users.view')()]

    # ------------------------------------------------------------------
    # Standard actions
    # ------------------------------------------------------------------

    def create(self, request):
        serializer = StaffUserCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = UserService.create_user(
            dict(serializer.validated_data), request.user, request
        )
        return Response(
            StaffUserDetailSerializer(user, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, pk=None):
        user = self.get_object()
        serializer = StaffUserUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        user = UserService.update_user(
            user, dict(serializer.validated_data), request.user, request
        )
        return Response(StaffUserDetailSerializer(user, context={'request': request}).data)

    # ------------------------------------------------------------------
    # Custom actions — user lifecycle
    # ------------------------------------------------------------------

    @action(detail=False, methods=['get', 'patch'], url_path='me')
    def me(self, request):
        """GET or PATCH own profile. Available to all authenticated staff."""
        if request.method == 'GET':
            return Response(MeSerializer(request.user, context={'request': request}).data)

        serializer = MeUpdateSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        user = UserService.update_me(request.user, dict(serializer.validated_data), request)
        return Response(MeSerializer(user, context={'request': request}).data)

    @action(detail=True, methods=['post'])
    def deactivate(self, request, pk=None):
        """Deactivate a staff user. Cannot deactivate yourself or last lab admin."""
        user = self.get_object()
        if user.id == request.user.id:
            raise PermissionDenied('You cannot deactivate your own account.')
        try:
            user = UserService.deactivate_user(user, request.user, request)
        except DjangoValidationError as e:
            raise ValidationError(e.message)
        return Response(StaffUserDetailSerializer(user, context={'request': request}).data)

    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        """Reactivate a previously deactivated staff user."""
        user = self.get_object()
        user = UserService.activate_user(user, request.user, request)
        return Response(StaffUserDetailSerializer(user, context={'request': request}).data)

    # ------------------------------------------------------------------
    # Custom actions — RBAC
    # ------------------------------------------------------------------

    @action(detail=True, methods=['post'], url_path='assign-role')
    def assign_role(self, request, pk=None):
        """Assign a new role to a user. Audit-logged."""
        user = self.get_object()
        serializer = RoleAssignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = UserService.assign_role(
                user, serializer.validated_data['role'], request.user, request,
            )
        except DjangoValidationError as e:
            raise ValidationError(e.message)
        return Response(StaffUserDetailSerializer(user, context={'request': request}).data)

    @action(detail=True, methods=['get'], url_path='permissions')
    def user_permissions(self, request, pk=None):
        """List effective permissions for a user (role defaults + overrides)."""
        user = self.get_object()
        from common.permission_checker import PermissionChecker
        from common.role_permissions import get_role_permissions

        effective = PermissionChecker.get_effective_permissions(user)
        role_defaults = get_role_permissions(user.role)
        overrides = UserPermissionOverride.objects.filter(user=user)

        return Response({
            'user_id': str(user.id),
            'role': user.role,
            'role_permissions': sorted(role_defaults),
            'overrides': UserPermissionOverrideSerializer(overrides, many=True).data,
            'effective_permissions': sorted(effective),
        })

    @action(detail=True, methods=['post'], url_path='manage-permissions')
    def manage_permissions(self, request, pk=None):
        """Grant, revoke, or remove a permission override for a user."""
        user = self.get_object()
        serializer = PermissionOverrideSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            if data['action'] == 'grant':
                UserService.grant_permission(
                    user, data['permission_code'], request.user,
                    data.get('reason', ''), request,
                )
            elif data['action'] == 'revoke':
                UserService.revoke_permission(
                    user, data['permission_code'], request.user,
                    data.get('reason', ''), request,
                )
            elif data['action'] == 'remove':
                UserService.remove_permission_override(
                    user, data['permission_code'], request.user, request,
                )
        except DjangoValidationError as e:
            raise ValidationError(e.message)

        return Response({'status': 'ok'})

    @action(detail=False, methods=['get'])
    def roles(self, request):
        """List all available tenant roles and their default permission sets."""
        from common.role_permissions import get_role_permissions

        result = []
        for role_value, role_label in Role.choices:
            result.append({
                'code': role_value,
                'label': role_label,
                'permissions': sorted(get_role_permissions(role_value)),
            })
        return Response(result)

    @action(detail=False, methods=['get'], url_path='permissions-catalog')
    def permissions_catalog(self, request):
        """List all registered permissions, grouped by module."""
        from common.permissions_registry import PermissionRegistry

        by_module = PermissionRegistry.by_module()
        result = {}
        for module, perms in sorted(by_module.items()):
            result[module] = [
                {'code': p.code, 'description': p.description}
                for p in sorted(perms, key=lambda x: x.code)
            ]
        return Response(result)

    # ------------------------------------------------------------------
    # Signature management — own signature (any staff, scoped to self)
    # ------------------------------------------------------------------

    @action(
        detail=False,
        methods=['get', 'post', 'delete'],
        url_path='me/signature',
        parser_classes=[MultiPartParser, JSONParser],
    )
    def my_signature(self, request):
        """
        Manage the authenticated user's signature image.

        POST   — upload / replace (multipart file)
        GET    — stream the image
        DELETE — clear
        """
        user = request.user
        if request.method == 'GET':
            return self._stream_signature(user)
        if request.method == 'DELETE':
            return self._clear_signature(user)
        return self._upload_signature(user, request)

    def _upload_signature(self, user, request):
        serializer = SignatureUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        file = serializer.validated_data['file']

        _, ext = os.path.splitext(getattr(file, 'name', 'sig'))
        ext = ext.lower() or '.png'
        file_key = f'user-signatures/{user.id}/{uuid.uuid4().hex}{ext}'
        default_storage.save(file_key, ContentFile(file.read()))

        old_key = user.signature_file_key
        user.signature_file_key = file_key
        user.save(update_fields=['signature_file_key', 'updated_at'])
        if old_key and old_key != file_key:
            try:
                default_storage.delete(old_key)
            except Exception:  # noqa: BLE001
                logger.warning('Failed to delete old signature %s', old_key)

        return Response(MeSerializer(user, context={'request': request}).data)

    def _stream_signature(self, user):
        if not user.signature_file_key:
            raise NotFound('No signature uploaded.')
        try:
            f = default_storage.open(user.signature_file_key, 'rb')
        except FileNotFoundError:
            raise NotFound('Signature file missing from storage.')
        ext = os.path.splitext(user.signature_file_key)[1].lower()
        ct = {'.png': 'image/png', '.jpg': 'image/jpeg',
              '.jpeg': 'image/jpeg', '.gif': 'image/gif'}.get(ext, 'application/octet-stream')
        return FileResponse(f, content_type=ct)

    def _clear_signature(self, user):
        old_key = user.signature_file_key
        if not old_key:
            return Response(MeSerializer(user, context={'request': user}).data)
        user.signature_file_key = ''
        user.save(update_fields=['signature_file_key', 'updated_at'])
        try:
            default_storage.delete(old_key)
        except Exception:  # noqa: BLE001
            logger.warning('Failed to delete signature %s', old_key)
        return Response(MeSerializer(user, context={'request': user}).data)
