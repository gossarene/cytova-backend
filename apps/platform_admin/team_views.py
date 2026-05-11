"""
Cytova Core — platform-admin team management API.

Endpoints (mounted on the public-schema URL conf only, under
``/api/v1/platform-admin/team/``):

  GET  /team/                       — list active + inactive admins
  GET  /team/{id}/                  — single admin detail
  POST /team/                       — create a new admin (SUPER_ADMIN only)
  POST /team/{id}/deactivate/       — disable login   (SUPER_ADMIN only)
  POST /team/{id}/reactivate/       — re-enable login (SUPER_ADMIN only)
  POST /team/{id}/change-role/      — switch role     (SUPER_ADMIN only)

Permission contract
-------------------
- Read (list / retrieve): any active platform admin. Even non-write
  roles benefit from seeing who else is on the team — a SUPPORT
  admin needs to know who to escalate to.
- Write (create / deactivate / reactivate / change-role): only
  SUPER_ADMIN. The view layer enforces this with the existing
  ``HasPlatformRole`` permission class.

Audit
-----
Every write writes one ``PlatformAdminAuditLog`` row with the
matching ``PLATFORM_ADMIN_*`` action. Metadata snapshot is
deliberately narrow:
  - target_admin_id (UUID)
  - target_email   (string)
  - before / after for the changed attribute(s).

The metadata NEVER includes the temporary password, the password
hash, JWT tokens, or any other secret. ``CreatedAdmin.temporary_password``
is returned in the HTTP response body only.

Backed by ``apps.platform_admin.team_service`` for the actual
state transitions + last-super-admin invariants.
"""
from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import action
from rest_framework.mixins import (
    CreateModelMixin, ListModelMixin, RetrieveModelMixin,
)
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from .audit import log_platform_admin_action
from .authentication import PlatformAdminJWTAuthentication
from .models import PlatformAdminRole, PlatformAdminUser, PlatformAuditAction
from .permissions import HasPlatformRole, IsPlatformAdmin
from .serializers import (
    PlatformAdminTeamChangeRoleSerializer,
    PlatformAdminTeamCreateSerializer,
    PlatformAdminTeamMemberSerializer,
)
from . import team_service


SUPER_ADMIN_ONLY = [PlatformAdminRole.SUPER_ADMIN]


class PlatformAdminTeamViewSet(
    ListModelMixin, RetrieveModelMixin, CreateModelMixin, GenericViewSet,
):
    """Team viewset.

    Inherits only the read + create mixins — destroy is not surfaced
    so the URL space cannot accidentally route a DELETE into a
    real deletion. Deactivation is the soft-delete primitive.

    Authentication is platform-admin JWT for every endpoint;
    per-action role gating is layered on top of that via
    ``get_permissions``.
    """
    authentication_classes = [PlatformAdminJWTAuthentication]
    queryset = PlatformAdminUser.objects.all().order_by('-created_at')
    serializer_class = PlatformAdminTeamMemberSerializer

    def get_permissions(self):
        # Reads: any active platform admin.
        # Writes: SUPER_ADMIN only.
        if self.action in {'list', 'retrieve'}:
            return [IsPlatformAdmin()]
        permission = HasPlatformRole()
        # ``HasPlatformRole`` reads ``required_platform_roles`` off the
        # view — assign it dynamically per action.
        self.required_platform_roles = SUPER_ADMIN_ONLY
        return [permission]

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create(self, request, *args, **kwargs):
        serializer = PlatformAdminTeamCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        result = team_service.create_admin(
            email=serializer.validated_data['email'],
            first_name=serializer.validated_data.get('first_name', ''),
            last_name=serializer.validated_data.get('last_name', ''),
            role=serializer.validated_data['role'],
        )

        log_platform_admin_action(
            request=request,
            action=PlatformAuditAction.PLATFORM_ADMIN_CREATED,
            actor=request.user,
            entity_type='PlatformAdminUser',
            entity_id=result.user.id,
            # NEVER include the temporary password / hash. Only the
            # target identity + the role we just assigned.
            metadata={
                'target_admin_id': str(result.user.id),
                'target_email': result.user.email,
                'role': result.user.role,
            },
        )

        body = PlatformAdminTeamMemberSerializer(result.user).data
        # The temporary password is returned ONCE here and never
        # surfaced again. Caller MUST relay it to the new admin
        # out-of-band and prompt them to change it on first sign-in.
        body['temporary_password'] = result.temporary_password
        return Response(body, status=status.HTTP_201_CREATED)

    # ------------------------------------------------------------------
    # Lifecycle actions
    # ------------------------------------------------------------------

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, pk=None):
        target = self.get_object()
        before_active = target.is_active
        target = team_service.deactivate_admin(
            target=target, actor=request.user,
        )

        log_platform_admin_action(
            request=request,
            action=PlatformAuditAction.PLATFORM_ADMIN_DEACTIVATED,
            actor=request.user,
            entity_type='PlatformAdminUser',
            entity_id=target.id,
            metadata={
                'target_admin_id': str(target.id),
                'target_email': target.email,
                'before': {'is_active': before_active},
                'after': {'is_active': target.is_active},
            },
        )
        return Response(PlatformAdminTeamMemberSerializer(target).data)

    @action(detail=True, methods=['post'], url_path='reactivate')
    def reactivate(self, request, pk=None):
        target = self.get_object()
        before_active = target.is_active
        target = team_service.reactivate_admin(target=target)

        log_platform_admin_action(
            request=request,
            action=PlatformAuditAction.PLATFORM_ADMIN_REACTIVATED,
            actor=request.user,
            entity_type='PlatformAdminUser',
            entity_id=target.id,
            metadata={
                'target_admin_id': str(target.id),
                'target_email': target.email,
                'before': {'is_active': before_active},
                'after': {'is_active': target.is_active},
            },
        )
        return Response(PlatformAdminTeamMemberSerializer(target).data)

    @action(detail=True, methods=['post'], url_path='change-role')
    def change_role(self, request, pk=None):
        target = self.get_object()
        before_role = target.role
        serializer = PlatformAdminTeamChangeRoleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        target = team_service.change_role(
            target=target,
            new_role=serializer.validated_data['role'],
            actor=request.user,
        )

        log_platform_admin_action(
            request=request,
            action=PlatformAuditAction.PLATFORM_ADMIN_ROLE_CHANGED,
            actor=request.user,
            entity_type='PlatformAdminUser',
            entity_id=target.id,
            metadata={
                'target_admin_id': str(target.id),
                'target_email': target.email,
                'before': {'role': before_role},
                'after': {'role': target.role},
            },
        )
        return Response(PlatformAdminTeamMemberSerializer(target).data)
