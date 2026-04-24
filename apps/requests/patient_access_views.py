"""
Cytova — Public Patient Access Views

These endpoints require NO authentication — they are accessed by
patients using a secure token link. The token itself is the credential.

    GET  /r/{token}/                → metadata (password_required flag)
    POST /r/{token}/verify-password/ → validate password, return download grant
    GET  /r/{token}/download/        → stream the PDF (requires prior verify)

Security:
    - Token validated on every request (exists, active, not expired)
    - Password verified before download is allowed
    - PDF streamed via FileResponse — raw storage key never exposed
"""
import hashlib
import hmac
import logging

from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)
from django.http import FileResponse, JsonResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.lab_settings.models import LabSettings
from .patient_access import ResultAccessService
from .pdf_protection import derive_password


def _validate_or_403(token_str):
    try:
        return ResultAccessService.validate_token(token_str), None
    except Exception as e:
        msg = str(e.detail[0]) if hasattr(e, 'detail') else str(e)
        return None, JsonResponse({'error': msg}, status=403)


def _make_grant(token_str: str, secret: str) -> str:
    """HMAC-based one-time download grant derived from token + lab secret."""
    return hmac.new(
        secret.encode(), token_str.encode(), hashlib.sha256,
    ).hexdigest()[:32]


@api_view(['GET'])
@permission_classes([AllowAny])
def result_access(request, token):
    """Return metadata + whether password entry is required."""
    tok, err = _validate_or_403(token)
    if err:
        return err
    settings = LabSettings.get_solo()
    # Do NOT expose patient name or reference before password verification.
    return Response({
        'expires_at': tok.expires_at.isoformat(),
        'downloadable': bool(tok.report_file_key),
        'password_required': settings.result_pdf_password_enabled,
        'password_hint': settings.result_pdf_password_hint if settings.result_pdf_password_enabled else '',
    })


MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


@api_view(['POST'])
@permission_classes([AllowAny])
def verify_password(request, token):
    """Verify the patient-entered password with brute-force protection."""
    from datetime import timedelta
    from django.utils import timezone as tz

    tok, err = _validate_or_403(token)
    if err:
        return err

    # Check lockout
    if tok.is_locked:
        remaining = int((tok.locked_until - tz.now()).total_seconds())
        return JsonResponse({
            'error': 'Too many failed attempts. Please try again later.',
            'retry_after': max(remaining, 0),
        }, status=429)

    entered = (request.data.get('password') or '').strip()
    if not entered:
        return JsonResponse({'error': 'Password is required.'}, status=400)

    settings = LabSettings.get_solo()
    patient = tok.patient
    ar = tok.analysis_request
    identity = {
        'patient_name': f'{patient.last_name}, {patient.first_name}',
        'request_reference': ar.public_reference or ar.request_number,
    }
    if not settings.result_pdf_password_enabled:
        tok.verified_at = tz.now()
        tok.save(update_fields=['verified_at', 'updated_at'])
        return Response({'valid': True, 'download_grant': 'none', **identity})

    try:
        expected = derive_password(tok.analysis_request, settings)
    except Exception:
        return JsonResponse(
            {'error': 'Cannot verify password — required patient data missing.'},
            status=400,
        )

    if entered != expected:
        tok.failed_attempts += 1
        update_fields = ['failed_attempts', 'updated_at']
        if tok.failed_attempts >= MAX_FAILED_ATTEMPTS:
            tok.locked_until = tz.now() + timedelta(minutes=LOCKOUT_MINUTES)
            update_fields.append('locked_until')
            logger.warning(
                'Access token %s locked after %d failed attempts',
                token[:8], tok.failed_attempts,
            )
        tok.save(update_fields=update_fields)
        remaining_attempts = max(0, MAX_FAILED_ATTEMPTS - tok.failed_attempts)
        return JsonResponse({
            'error': 'Incorrect password.',
            'remaining_attempts': remaining_attempts,
        }, status=403)

    # Success — reset attempts
    tok.failed_attempts = 0
    tok.locked_until = None
    tok.verified_at = tz.now()
    tok.save(update_fields=[
        'failed_attempts', 'locked_until', 'verified_at', 'updated_at',
    ])
    grant = _make_grant(token, settings.lab_secret_code or 'cytova')
    return Response({'valid': True, 'download_grant': grant, **identity})


@api_view(['GET'])
@permission_classes([AllowAny])
def result_download(request, token):
    """Stream the result PDF. Requires download_grant query param when password-protected."""
    tok, err = _validate_or_403(token)
    if err:
        return err

    settings = LabSettings.get_solo()
    if settings.result_pdf_password_enabled:
        grant = request.query_params.get('grant', '')
        expected_grant = _make_grant(token, settings.lab_secret_code or 'cytova')
        if not grant or grant != expected_grant:
            return JsonResponse(
                {'error': 'Download not authorized. Please verify your password first.'},
                status=403,
            )

    if not tok.report_file_key:
        return JsonResponse({'error': 'Report file not available.'}, status=404)
    try:
        file_obj = default_storage.open(tok.report_file_key, 'rb')
    except FileNotFoundError:
        return JsonResponse({'error': 'Report file not found.'}, status=404)
    ref = tok.analysis_request.public_reference or tok.analysis_request.request_number
    return FileResponse(
        file_obj,
        content_type='application/pdf',
        as_attachment=True,
        filename=f'report_{ref}.pdf',
    )
