"""
Cytova — Patient Result Access Service

Generates secure, time-limited tokens that grant unauthenticated access
to a specific result PDF. Designed for the "Notify patient" workflow:
the lab generates a token, later sends it via SMS/email (not in scope
here), and the patient opens a link to view/download their report.

Security:
    - 64-char hex token (256 bits, ``secrets.token_hex(32)``)
    - Default TTL: 48 hours
    - One token per generation call (not reused)
    - Revocable via ``is_active = False``
    - PDF streamed via ``FileResponse`` — raw storage key never exposed
"""
import secrets
from datetime import timedelta

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.requests.models import (
    AnalysisRequest, AnalysisRequestReport, ResultAccessToken,
)


DEFAULT_TTL_HOURS = 48


class ResultAccessService:

    @staticmethod
    def get_active_token(analysis_request: AnalysisRequest):
        """Return the active non-expired token, or None."""
        return (
            ResultAccessToken.objects
            .filter(
                analysis_request=analysis_request,
                is_active=True,
                expires_at__gt=timezone.now(),
            )
            .order_by('-created_at')
            .first()
        )

    @staticmethod
    def get_or_create_token(
        analysis_request: AnalysisRequest,
        ttl_hours: int = DEFAULT_TTL_HOURS,
    ) -> ResultAccessToken:
        """
        Return the active non-expired token if one exists, otherwise
        create a new one. Avoids duplicate tokens.
        """
        existing = ResultAccessService.get_active_token(analysis_request)
        if existing is not None:
            return existing
        return ResultAccessService.create_token(analysis_request, ttl_hours)

    @staticmethod
    def create_token(
        analysis_request: AnalysisRequest,
        ttl_hours: int = DEFAULT_TTL_HOURS,
    ) -> ResultAccessToken:
        """
        Always create a new token (used for explicit regeneration).
        Deactivates any existing active tokens first.
        """
        report = (
            AnalysisRequestReport.objects
            .filter(analysis_request=analysis_request, is_current=True)
            .first()
        )
        if report is None or not report.pdf_file_key:
            raise ValidationError(
                'Cannot create access link: no report PDF has been generated.'
            )

        # Deactivate previous tokens
        ResultAccessToken.objects.filter(
            analysis_request=analysis_request, is_active=True,
        ).update(is_active=False)

        return ResultAccessToken.objects.create(
            token=secrets.token_hex(32),
            analysis_request=analysis_request,
            patient=analysis_request.patient,
            report_file_key=report.pdf_file_key,
            expires_at=timezone.now() + timedelta(hours=ttl_hours),
        )

    @staticmethod
    def validate_token(token_str: str) -> ResultAccessToken:
        """
        Look up and validate a token string.

        Returns the token if valid; raises ``ValidationError`` otherwise.
        """
        try:
            tok = ResultAccessToken.objects.select_related(
                'analysis_request', 'patient',
            ).get(token=token_str)
        except ResultAccessToken.DoesNotExist:
            raise ValidationError('Invalid or unknown access link.')

        if not tok.is_active:
            raise ValidationError('This access link has been revoked.')

        if tok.expires_at <= timezone.now():
            raise ValidationError('This access link has expired.')

        return tok

    @staticmethod
    def revoke_token(token: ResultAccessToken) -> None:
        token.is_active = False
        token.save(update_fields=['is_active', 'updated_at'])
