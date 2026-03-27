"""
Cytova — Users Views

UserViewSet covers:
    GET    /users/                 — list staff users          (LAB_ADMIN)
    POST   /users/                 — create staff user         (LAB_ADMIN)
    GET    /users/{id}/            — retrieve staff user       (LAB_ADMIN)
    PATCH  /users/{id}/            — update name / role        (LAB_ADMIN)
    POST   /users/{id}/deactivate/ — deactivate user           (LAB_ADMIN)
    POST   /users/{id}/activate/   — reactivate user           (LAB_ADMIN)
    GET    /users/me/              — own profile               (all staff)
    PATCH  /users/me/              — update own name/password  (all staff)
"""
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from common.permissions import IsLabAdmin, IsAnyStaff
from .filters import StaffUserFilter
from .models import StaffUser
from .serializers import (
    StaffUserListSerializer,
    StaffUserDetailSerializer,
    StaffUserCreateSerializer,
    StaffUserUpdateSerializer,
    MeSerializer,
    MeUpdateSerializer,
)
from .services import UserService


class UserViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    filterset_class = StaffUserFilter
    search_fields = ['first_name', 'last_name', 'email']
    ordering_fields = ['last_name', 'first_name', 'created_at', 'role']

    def get_queryset(self):
        return StaffUser.objects.select_related('created_by').all()

    def get_serializer_class(self):
        if self.action == 'list':
            return StaffUserListSerializer
        if self.action == 'create':
            return StaffUserCreateSerializer
        if self.action == 'partial_update':
            return StaffUserUpdateSerializer
        return StaffUserDetailSerializer

    def get_permissions(self):
        if self.action == 'me':
            return [IsAnyStaff()]
        return [IsLabAdmin()]

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
    # Custom actions
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
        """Deactivate a staff user. A lab admin cannot deactivate their own account."""
        user = self.get_object()
        if user.id == request.user.id:
            raise PermissionDenied('You cannot deactivate your own account.')
        user = UserService.deactivate_user(user, request.user, request)
        return Response(StaffUserDetailSerializer(user, context={'request': request}).data)

    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        """Reactivate a previously deactivated staff user."""
        user = self.get_object()
        user = UserService.activate_user(user, request.user, request)
        return Response(StaffUserDetailSerializer(user, context={'request': request}).data)
