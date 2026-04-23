import logging
import os
import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.http import FileResponse
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.models import ActorType, AuditAction, AuditLog
from apps.labels.defaults import DEFAULTS_BY_MODE, get_defaults
from apps.labels.models import LabelPrintPreset
from common.permissions import IsAnyStaff, IsLabAdmin
from .models import LabSettings
from .serializers import (
    LabSettingsSerializer, LabSettingsUpdateSerializer, LogoUploadSerializer,
)

logger = logging.getLogger(__name__)


def _audit(*, actor, action: str, entity_id, diff: dict, request):
    AuditLog.objects.create(
        actor_type=ActorType.STAFF_USER,
        actor_id=actor.id,
        actor_email=actor.email,
        action=action,
        entity_type='LabSettings',
        entity_id=entity_id,
        diff=diff,
        ip_address=getattr(request, 'audit_ip', None),
        user_agent=getattr(request, 'audit_user_agent', ''),
    )


class LabSettingsView(APIView):
    """
    GET   /api/v1/lab-settings/  — any staff
    PATCH /api/v1/lab-settings/  — LAB_ADMIN
    """
    parser_classes = [JSONParser]

    def get_permissions(self):
        if self.request.method == 'PATCH':
            return [IsLabAdmin()]
        return [IsAnyStaff()]

    def get(self, request):
        settings = LabSettings.get_solo()
        return Response(LabSettingsSerializer(settings).data)

    def patch(self, request):
        settings = LabSettings.get_solo()
        serializer = LabSettingsUpdateSerializer(
            settings, data=request.data, partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(LabSettingsSerializer(settings).data)


class LabLogoView(APIView):
    """
    Manage the laboratory logo image.

    POST   /api/v1/lab-settings/logo/  — upload / replace (LAB_ADMIN)
    DELETE /api/v1/lab-settings/logo/  — clear (LAB_ADMIN)
    GET    /api/v1/lab-settings/logo/  — stream the image (any staff, for preview/PDF)

    The logo file is stored internally (never exposed as a public URL). The
    GET endpoint streams it through the authenticated backend so tenant
    isolation is preserved.
    """
    parser_classes = [MultiPartParser, JSONParser]

    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAnyStaff()]
        return [IsLabAdmin()]

    def get(self, request):
        settings = LabSettings.get_solo()
        if not settings.logo_file_key:
            raise NotFound('No logo uploaded.')
        try:
            f = default_storage.open(settings.logo_file_key, 'rb')
        except FileNotFoundError:
            raise NotFound('Logo file missing from storage.')
        content_type = _guess_content_type(settings.logo_file_key)
        return FileResponse(f, content_type=content_type)

    def post(self, request):
        serializer = LogoUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        file = serializer.validated_data['file']

        settings = LabSettings.get_solo()
        _, ext = os.path.splitext(getattr(file, 'name', 'logo'))
        ext = ext.lower() or '.png'
        file_key = f'lab-settings/logo/{uuid.uuid4().hex}{ext}'
        default_storage.save(file_key, ContentFile(file.read()))

        # Clean up previous logo (best-effort)
        old_key = settings.logo_file_key
        settings.logo_file_key = file_key
        settings.save(update_fields=['logo_file_key', 'updated_at'])
        if old_key and old_key != file_key:
            try:
                default_storage.delete(old_key)
            except Exception as e:  # noqa: BLE001 — best-effort cleanup
                logger.warning('Failed to delete old logo %s: %s', old_key, e)

        _audit(
            actor=request.user,
            action=AuditAction.UPDATE,
            entity_id=settings.id,
            diff={'after': {'logo_file_key': file_key}},
            request=request,
        )
        return Response(LabSettingsSerializer(settings).data)

    def delete(self, request):
        settings = LabSettings.get_solo()
        old_key = settings.logo_file_key
        if not old_key:
            return Response(LabSettingsSerializer(settings).data)

        settings.logo_file_key = ''
        settings.save(update_fields=['logo_file_key', 'updated_at'])
        try:
            default_storage.delete(old_key)
        except Exception as e:  # noqa: BLE001
            logger.warning('Failed to delete logo %s: %s', old_key, e)

        _audit(
            actor=request.user,
            action=AuditAction.UPDATE,
            entity_id=settings.id,
            diff={'after': {'logo_file_key': ''}},
            request=request,
        )
        return Response(LabSettingsSerializer(settings).data)


class LabelDefaultsView(APIView):
    """
    GET /api/v1/lab-settings/label-defaults/[?mode=A4_SHEET|THERMAL_ROLL]

    Returns the factory default layout values for the requested print
    mode, or the full map keyed by mode when no ``mode`` query param
    is supplied. The frontend uses this to pre-fill forms when a user
    switches print modes without persisting anything yet.
    """
    permission_classes = [IsAnyStaff]

    def get(self, request):
        mode = request.query_params.get('mode')
        if mode:
            try:
                return Response({'mode': mode, 'defaults': get_defaults(mode)})
            except ValueError:
                raise ValidationError(f'Unknown print mode: {mode!r}')
        return Response({'defaults': DEFAULTS_BY_MODE})


class LabelPresetListView(APIView):
    """
    GET /api/v1/lab-settings/label-presets/

    Lists the currently active platform-managed presets. Tenants read
    but cannot write — preset authoring happens via Cytova Admin.
    """
    permission_classes = [IsAnyStaff]

    def get(self, request):
        presets = LabelPrintPreset.objects.filter(is_active=True).order_by(
            'print_mode', 'name',
        )
        data = [
            {
                'id': str(p.id),
                'code': p.code,
                'name': p.name,
                'print_mode': p.print_mode,
                'is_system': p.is_system,
                **p.to_effective_config(),
            }
            for p in presets
        ]
        return Response({'results': data})


def _guess_content_type(file_key: str) -> str:
    ext = os.path.splitext(file_key)[1].lower()
    return {
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.gif': 'image/gif',
        '.svg': 'image/svg+xml',
    }.get(ext, 'application/octet-stream')
