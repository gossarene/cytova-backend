"""
Cytova — Patient Portal HTTP views (public).

For now there's a single endpoint:

    POST /api/v1/patient-portal/signup/

It is mounted on both the platform/public URL conf
(``config.urls_public``) and the tenant URL conf (``config.urls``) so
the global signup is reachable from any host the deployment serves.
The endpoint never reads from or writes to a tenant schema — the
``apps.patient_portal`` tables live in the ``public`` schema (see
``apps/patient_portal/__init__.py``).

The endpoint:
- accepts JSON or form-encoded payloads,
- requires ``accept_terms=true``,
- creates a ``PatientAccount`` + ``PatientProfile`` + ``PatientConsent``
  in a single transaction via the service layer,
- returns a deliberately narrow response shape that exposes the
  Cytova Patient ID + email + account UUID + a human-readable message
  — never the password hash, never the audit trail.

Patient login, email verification, and the welcome email are explicitly
NOT implemented in this step.
"""
from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .audit import write_event as write_patient_audit
from .authentication import PatientJWTAuthentication
from .models import (
    PatientPortalAuditAction,
    PatientSharedResult, PatientSharedResultFile, SharedResultStatus,
)
from .serializers import (
    PatientLoginSerializer, PatientMeSerializer,
    PatientSharedResultSerializer, PatientSharedResultVersionSerializer,
    PatientSignupSerializer, PatientVerifyEmailSerializer,
    _CurrentVersionSerializer,
)
from .services import (
    EmailNotVerified, InvalidPatientCredentials, InvalidVerificationToken,
    authenticate_patient, issue_patient_tokens, verify_email_token,
)
from .throttles import (
    PatientLoginThrottle, PatientSignupThrottle, PatientVerifyEmailThrottle,
)

logger = logging.getLogger(__name__)


def _envelope(data=None, errors=None, http_status=status.HTTP_200_OK):
    """Cytova response envelope. Mirrors the helper used by the lab
    onboarding views so frontends can parse both flows identically."""
    return Response(
        {'data': data, 'meta': None, 'errors': errors or []},
        status=http_status,
    )


class PatientSignupView(APIView):
    """Public, unauthenticated, IP-throttled patient signup."""

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [PatientSignupThrottle]

    def post(self, request):
        serializer = PatientSignupSerializer(
            data=request.data, context={'request': request},
        )
        serializer.is_valid(raise_exception=True)
        # ``serializer.save()`` -> ``create()`` -> service. The service
        # raises ``ValidationError`` for accept_terms / duplicate email
        # paths; DRF's default exception handler routes those through
        # ``common.exceptions.cytova_exception_handler`` which produces
        # the same envelope shape as a successful response.
        account = serializer.save()

        # Pull the assigned Cytova Patient ID via the related profile
        # row created in the same transaction. Refetched explicitly
        # rather than ``account.profile`` to avoid trusting reverse
        # relation caching across the transaction boundary.
        from .models import PatientProfile
        profile = PatientProfile.objects.only('cytova_patient_id').get(account=account)

        return _envelope(
            data={
                'patient_account_id': str(account.id),
                'cytova_patient_id': profile.cytova_patient_id,
                'email': account.email,
                'message': 'Patient account created successfully.',
            },
            http_status=status.HTTP_201_CREATED,
        )


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------

class PatientVerifyEmailView(APIView):
    """``POST /verify-email/`` — consume a single-use verification token."""

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [PatientVerifyEmailThrottle]

    def post(self, request):
        serializer = PatientVerifyEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            account = verify_email_token(
                serializer.validated_data['token'], request=request,
            )
        except InvalidVerificationToken:
            # Single non-distinguishing error code so a token-fishing
            # caller can't differentiate between expired / used / wrong.
            return _envelope(
                errors=[{
                    'code': 'INVALID_OR_EXPIRED_TOKEN',
                    'message': 'This verification link is invalid or has expired.',
                    'field': 'token',
                    'detail': {},
                }],
                http_status=status.HTTP_400_BAD_REQUEST,
            )
        return _envelope(
            data={
                'patient_account_id': str(account.id),
                'email': account.email,
                'email_verified_at': account.email_verified_at.isoformat(),
                'message': 'Email verified successfully.',
            },
        )


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

class PatientLoginView(APIView):
    """``POST /login/`` — authenticate a verified patient + issue tokens."""

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [PatientLoginThrottle]

    def post(self, request):
        serializer = PatientLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data['email']
        password = serializer.validated_data['password']
        try:
            account = authenticate_patient(email=email, password=password)
        except InvalidPatientCredentials:
            # Generic wording for both "no such account" and "wrong
            # password" — never leak which one. The throttle handles
            # brute-force above this layer.
            logger.info(
                'Patient login failed (invalid credentials): ip=%s',
                getattr(request, 'audit_ip', None),
            )
            return _envelope(
                errors=[{
                    'code': 'INVALID_CREDENTIALS',
                    'message': 'Email or password is incorrect.',
                    'field': None,
                    'detail': {},
                }],
                http_status=status.HTTP_401_UNAUTHORIZED,
            )
        except EmailNotVerified:
            # Distinct from invalid credentials so the UI can prompt
            # "check your inbox". Only fires after the password is
            # correct, so it doesn't help an enumerator.
            return _envelope(
                errors=[{
                    'code': 'EMAIL_NOT_VERIFIED',
                    'message': 'Please verify your email before signing in.',
                    'field': 'email',
                    'detail': {},
                }],
                http_status=status.HTTP_403_FORBIDDEN,
            )

        return _envelope(data=issue_patient_tokens(account, request=request))


# ---------------------------------------------------------------------------
# /me
# ---------------------------------------------------------------------------

class PatientMeView(APIView):
    """``GET /me/`` — return the authenticated patient's profile."""

    authentication_classes = [PatientJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .models import PatientProfile

        account = request.user
        profile = PatientProfile.objects.get(account=account)
        data = PatientMeSerializer({
            'id': account.id,
            'email': account.email,
            'email_verified_at': account.email_verified_at,
            'cytova_patient_id': profile.cytova_patient_id,
            'first_name': profile.first_name,
            'last_name': profile.last_name,
            'date_of_birth': profile.date_of_birth,
            'phone': profile.phone,
        }).data
        return _envelope(data=data)


# ---------------------------------------------------------------------------
# Shared results: list / hide / download
# ---------------------------------------------------------------------------

# Single status value the patient is allowed to see / interact with.
# HIDDEN_BY_PATIENT and REVOKED rows are filtered everywhere — the
# patient view treats both as "gone" with no distinction (the lab
# audit trail keeps the difference).
_PATIENT_VISIBLE_STATUSES = (SharedResultStatus.ACTIVE,)


class PatientSharedResultListView(APIView):
    """``GET /api/v1/patient-portal/results/``

    Lists the authenticated patient's shared results, ordered by the
    most recent ``result_available_date`` and falling back to
    ``created_at`` for ties / missing dates. Hidden + revoked rows are
    excluded — they remain in the DB so the lab tenant retains the
    audit trail.

    Per-source-request roll-up: when the lab has shared multiple
    versions of the same result with the patient (Phase 2+ supersession
    flow), only the row marked ``is_current_for_patient=True`` is shown
    here. Older shared versions remain accessible — but only via the
    dedicated version-history endpoint
    (``GET /results/{id}/versions/``). This keeps the patient list
    clean while still letting the patient retrieve historical PDFs on
    demand. Pre-Phase-2 rows default to ``is_current_for_patient=True``
    so the list contract is unchanged for legacy data.
    """
    authentication_classes = [PatientJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = (
            PatientSharedResult.objects
            .filter(
                patient_account=request.user,
                status__in=_PATIENT_VISIBLE_STATUSES,
                is_current_for_patient=True,
            )
            .prefetch_related('files')
            .order_by('-result_available_date', '-created_at')
        )
        data = PatientSharedResultSerializer(qs, many=True).data
        return _envelope(data={'results': data})


class PatientSharedResultVersionsView(APIView):
    """``GET /api/v1/patient-portal/results/{id}/versions/``

    Returns the version history of a shared result *as the patient saw
    it* — that is, only versions the lab actively shared/notified to
    this patient for the underlying source request. Lab-only versions
    that the lab regenerated internally without sharing are NEVER
    materialised in the patient portal database, so they are
    structurally invisible here; revoked and patient-hidden versions
    are filtered out.

    Resolution
    ----------
    The path ``{id}`` is any ``PatientSharedResult.id`` belonging to the
    authenticated patient (typically the current row, but a stale
    bookmark of a superseded row also resolves correctly). The view
    looks up that row, derives the (patient_account, source tenant
    schema, source request UUID) group, and lists every active row in
    that group ordered by ``report_version_number`` descending —
    newest first. The current version is the row with
    ``is_current_for_patient=True``; if every version in the group has
    been demoted (e.g. the most recent share was later revoked), the
    response's ``current_version`` is ``null``.

    Privacy
    -------
    Cross-patient access is blocked by the initial filter on
    ``patient_account=request.user``: an unknown id, an id belonging
    to a different account, and a hidden/revoked id all return the
    same 404 with no detail — never an enumeration oracle. No internal
    fields (``storage_key``, ``patient_storage_key``, ``file_token``,
    ``source_tenant_schema``, ``source_request_id``) are ever
    serialised.

    Tenant isolation
    ----------------
    The version group is keyed on the captured ``source_tenant_schema``
    in addition to ``source_request_id`` so a hypothetical UUID
    collision across two tenants could never bleed history across
    labs.

    Legacy rows
    -----------
    Rows that pre-date the Phase-2 supersession migration may have
    ``source_request_id=None``. For those we treat the requested row
    as a singleton version line — the only thing we can safely
    correlate it with is itself, so the response shows just that row.
    """
    authentication_classes = [PatientJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        # Step 1: resolve the URL id under the patient's ownership and
        # under a patient-visible status. Hidden/revoked rows look the
        # same as "not found" from the patient's side.
        try:
            anchor = PatientSharedResult.objects.get(
                pk=pk,
                patient_account=request.user,
                status__in=_PATIENT_VISIBLE_STATUSES,
            )
        except PatientSharedResult.DoesNotExist:
            return _envelope(
                errors=[{
                    'code': 'NOT_FOUND',
                    'message': 'Shared result not found.',
                    'field': None,
                    'detail': {},
                }],
                http_status=status.HTTP_404_NOT_FOUND,
            )

        # Step 2: resolve the version group. Pre-Phase-2 rows without a
        # captured source_request_id are treated as singletons — there
        # is no safe correlation key for them so we do not roll up.
        if anchor.source_request_id is None:
            group_qs = PatientSharedResult.objects.filter(pk=anchor.pk)
        else:
            group_qs = PatientSharedResult.objects.filter(
                patient_account=request.user,
                source_tenant_schema=anchor.source_tenant_schema,
                source_request_id=anchor.source_request_id,
                status__in=_PATIENT_VISIBLE_STATUSES,
            )

        # Step 3: collect rows ordered by version desc. Two-key sort:
        # ``report_version_number`` is the primary lab-side ordinal;
        # ``shared_at`` is the secondary fallback for legacy rows that
        # never received a version_number stamp (kept defensive for
        # pre-Phase-2 data — Notify-Cytova always populates
        # report_version_number).
        rows = list(
            group_qs
            .prefetch_related('files')
            .order_by('-report_version_number', '-shared_at', '-created_at')
        )

        current = next((r for r in rows if r.is_current_for_patient), None)

        return _envelope(data={
            'result_id': str(pk),
            'current_version': (
                _CurrentVersionSerializer(current).data
                if current is not None else None
            ),
            'versions': PatientSharedResultVersionSerializer(
                rows, many=True,
            ).data,
        })


class PatientSharedResultHideView(APIView):
    """``DELETE /api/v1/patient-portal/results/{id}/``

    Soft-hides a shared result from the patient's portal view. The row
    is preserved (status flipped to ``HIDDEN_BY_PATIENT``) so the lab
    tenant's audit trail and the original PDF on storage are untouched
    — exactly what the spec requires.

    Returns 404 for unknown IDs *or* IDs that don't belong to the
    authenticated patient: never let a caller distinguish "not yours"
    from "doesn't exist", to avoid an enumeration oracle.
    """
    authentication_classes = [PatientJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        try:
            shared = PatientSharedResult.objects.get(
                pk=pk, patient_account=request.user,
            )
        except PatientSharedResult.DoesNotExist:
            return _envelope(
                errors=[{
                    'code': 'NOT_FOUND',
                    'message': 'Shared result not found.',
                    'field': None,
                    'detail': {},
                }],
                http_status=status.HTTP_404_NOT_FOUND,
            )
        # Idempotent — re-hiding an already-hidden row is a no-op,
        # and we don't write a duplicate audit row for a no-op.
        if shared.status != SharedResultStatus.HIDDEN_BY_PATIENT:
            shared.status = SharedResultStatus.HIDDEN_BY_PATIENT
            shared.save(update_fields=['status'])
            write_patient_audit(
                action=PatientPortalAuditAction.PATIENT_RESULT_HIDDEN_BY_PATIENT.value,
                entity_type='PatientSharedResult',
                entity_id=shared.id,
                patient_account=request.user,
                request=request,
                metadata={'shared_result_id': shared.id},
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


class PatientSharedResultDownloadView(APIView):
    """``GET /api/v1/patient-portal/results/files/{file_token}/download/``

    Streams the PDF tied to a ``PatientSharedResultFile`` row. Every
    failure mode (unknown token, file owned by a different patient,
    revoked / hidden share, missing storage object) surfaces as a 404
    with no detail — a clean enumeration-resistant surface.

    The ``storage_key`` field is read on the server only and never
    returned. The patient's browser receives the bytes via
    ``FileResponse`` (Django streams in chunks) with the snapshotted
    suggested filename.
    """
    authentication_classes = [PatientJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, file_token):
        try:
            sfile = (
                PatientSharedResultFile.objects
                .select_related('shared_result__patient_account')
                .get(file_token=file_token)
            )
        except PatientSharedResultFile.DoesNotExist:
            return self._not_found()

        shared = sfile.shared_result
        if shared.patient_account_id != request.user.id:
            return self._not_found()
        if shared.status not in _PATIENT_VISIBLE_STATUSES:
            return self._not_found()

        # Prefer the patient-owned copy when present; fall back to
        # the lab-side snapshot so legacy rows + copy-time failures
        # still serve the file. ``effective_storage_key`` encodes
        # the rule on the model so it stays consistent across
        # callers.
        storage_key = sfile.effective_storage_key
        if not storage_key:
            return self._not_found()

        from django.core.files.storage import default_storage
        from django.http import FileResponse

        try:
            file_obj = default_storage.open(storage_key, 'rb')
        except FileNotFoundError:
            # Underlying blob was moved/deleted on storage — surface
            # the same 404 the patient would see for an invalid token.
            # Logged at WARNING so ops can investigate the missing file
            # without leaking the storage path to the client.
            logger.warning(
                'Patient portal download: storage object missing '
                'shared_result_id=%s file_id=%s',
                shared.id, sfile.id,
            )
            return self._not_found()

        # Bump activity counters BEFORE streaming so a partial /
        # interrupted download is still recorded — the patient
        # demonstrably had access. Increments via DB-level F() so
        # concurrent downloads don't lose updates.
        from django.db.models import F
        from django.utils import timezone as _tz
        now = _tz.now()
        update_fields = {
            'last_downloaded_at': now,
            'download_count': F('download_count') + 1,
        }
        if shared.first_viewed_at is None:
            update_fields['first_viewed_at'] = now
        PatientSharedResult.objects.filter(pk=shared.id).update(**update_fields)

        # Re-read the post-update count so the audit row carries the
        # actual stored value rather than the stale pre-increment one.
        new_count = (
            PatientSharedResult.objects
            .filter(pk=shared.id)
            .values_list('download_count', flat=True)
            .first()
        )

        # Audit the successful download via the patient-side log. The
        # service-level INFO line below is for ops; the audit row is
        # the user-facing record.
        write_patient_audit(
            action=PatientPortalAuditAction.PATIENT_RESULT_DOWNLOADED.value,
            entity_type='PatientSharedResultFile',
            entity_id=sfile.id,
            patient_account=request.user,
            request=request,
            metadata={
                'shared_result_id': shared.id,
                'file_id': sfile.id,
                'download_count_after': new_count,
            },
        )

        # Patient PII (name, DOB, email) is never logged — only the IDs
        # already known to both sides.
        logger.info(
            'Patient portal download: patient_account_id=%s '
            'shared_result_id=%s file_id=%s ip=%s',
            request.user.id, shared.id, sfile.id,
            getattr(request, 'audit_ip', None),
        )

        return FileResponse(
            file_obj,
            content_type='application/pdf',
            as_attachment=True,
            filename=sfile.filename or 'result.pdf',
        )

    def _not_found(self) -> Response:
        return _envelope(
            errors=[{
                'code': 'NOT_FOUND',
                'message': 'File not found.',
                'field': None,
                'detail': {},
            }],
            http_status=status.HTTP_404_NOT_FOUND,
        )


# ---------------------------------------------------------------------------
# Logout + Refresh
# ---------------------------------------------------------------------------

class PatientLogoutView(APIView):
    """``POST /api/v1/patient-portal/logout/``

    Blacklist:
      - the access token from the Authorization header (always),
      - the refresh token from the body (when supplied),
      - every outstanding token for the account (when ``all_sessions=true``).

    Returns ``204 No Content`` on success — there's nothing for the
    client to read; their stored tokens are immediately useless.
    """
    authentication_classes = [PatientJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from .serializers import PatientLogoutSerializer
        from .services import (
            blacklist_all_tokens_for_account, blacklist_patient_token_by_jti,
        )
        from .tokens import PatientRefreshToken
        from rest_framework_simplejwt.exceptions import (
            InvalidToken, TokenError,
        )

        serializer = PatientLogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Blacklist the access token via its jti — ``request.auth`` is
        # the validated simplejwt token instance the auth backend
        # produced.
        access_jti = (
            request.auth.payload.get('jti') if request.auth is not None else None
        )
        if access_jti:
            blacklist_patient_token_by_jti(access_jti)

        # Optional refresh token from body. Decode to extract jti +
        # confirm it really is a patient refresh token before
        # blacklisting; we never trust a free-text input.
        raw_refresh = serializer.validated_data.get('refresh_token') or ''
        if raw_refresh:
            try:
                rt = PatientRefreshToken(raw_refresh)
                if rt.payload.get('user_type') == 'PATIENT':
                    refresh_jti = rt.payload.get('jti')
                    if refresh_jti:
                        blacklist_patient_token_by_jti(refresh_jti)
            except (TokenError, InvalidToken):
                # Silently ignore — the access blacklist still
                # applied. We don't leak whether the refresh was
                # well-formed.
                pass

        if serializer.validated_data.get('all_sessions'):
            blacklist_all_tokens_for_account(request.user)

        return Response(status=status.HTTP_204_NO_CONTENT)


class PatientRefreshView(APIView):
    """``POST /api/v1/patient-portal/refresh/``

    Token rotation: validates the supplied refresh token, blacklists
    it, and issues a fresh access + refresh pair. The new refresh
    token has a new jti — stealing the old one after rotation gains
    nothing.
    """
    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request):
        from .serializers import PatientRefreshSerializer
        from .services import (
            blacklist_patient_token_by_jti, issue_patient_tokens,
        )
        from .authentication import _assert_token_active
        from .tokens import PATIENT_USER_TYPE, PatientRefreshToken
        from rest_framework_simplejwt.exceptions import (
            InvalidToken, TokenError,
        )

        serializer = PatientRefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        raw_refresh = serializer.validated_data['refresh_token']
        try:
            rt = PatientRefreshToken(raw_refresh)
        except (TokenError, InvalidToken):
            return _envelope(
                errors=[{
                    'code': 'INVALID_REFRESH_TOKEN',
                    'message': 'Refresh token is invalid or expired.',
                    'field': 'refresh_token',
                    'detail': {},
                }],
                http_status=status.HTTP_401_UNAUTHORIZED,
            )

        if rt.payload.get('user_type') != PATIENT_USER_TYPE:
            return _envelope(
                errors=[{
                    'code': 'INVALID_REFRESH_TOKEN',
                    'message': 'Refresh token is not a patient token.',
                    'field': 'refresh_token', 'detail': {},
                }],
                http_status=status.HTTP_401_UNAUTHORIZED,
            )

        # Same blacklist + outstanding check used by every
        # authenticated request.
        try:
            _assert_token_active(rt.payload.get('jti'))
        except InvalidToken as exc:
            return _envelope(
                errors=[{
                    'code': 'INVALID_REFRESH_TOKEN',
                    'message': str(exc.detail) if hasattr(exc, 'detail') else 'Refresh token has been revoked.',
                    'field': 'refresh_token', 'detail': {},
                }],
                http_status=status.HTTP_401_UNAUTHORIZED,
            )

        # Resolve the account from the validated token and issue a
        # fresh pair. Blacklist the old refresh BEFORE issuing the
        # new one so a race that interleaves two refresh calls can't
        # both end up holding live tokens.
        from .models import PatientAccount
        from rest_framework_simplejwt.settings import api_settings as jwt_settings
        account_id = rt.payload.get(jwt_settings.USER_ID_CLAIM)
        try:
            account = PatientAccount.objects.get(id=account_id, is_active=True)
        except PatientAccount.DoesNotExist:
            return _envelope(
                errors=[{
                    'code': 'INVALID_REFRESH_TOKEN',
                    'message': 'Patient account not found or inactive.',
                    'field': 'refresh_token', 'detail': {},
                }],
                http_status=status.HTTP_401_UNAUTHORIZED,
            )

        blacklist_patient_token_by_jti(rt.payload.get('jti'))
        return _envelope(data=issue_patient_tokens(account, request=request))
