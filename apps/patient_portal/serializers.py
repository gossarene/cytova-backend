"""
Cytova — Patient Portal serializers.

The signup serializer is the input-validation surface for the future
HTTP endpoint and is also a convenient call site for tests so they
exercise validation + service together. Output rendering of patient
identities is intentionally deferred — the public-facing portal API
will likely return a narrower, audience-specific shape than the raw
model.
"""
from __future__ import annotations

from datetime import date

from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from .services import register_patient_account


class PatientSignupSerializer(serializers.Serializer):
    """
    Validates signup payload + delegates persistence to the service.

    Password validation goes through Django's installed
    ``AUTH_PASSWORD_VALIDATORS`` chain so the same minimum-length /
    common-password rules used for staff signups apply here too.

    ``confirm_password`` is a UI-side guardrail captured at the API
    boundary so the backend can reject mismatched re-types with a
    clean field error rather than letting the frontend surface its own
    inconsistent state. The service layer never sees this field.
    """
    email = serializers.EmailField()
    password = serializers.CharField(
        write_only=True,
        validators=[validate_password],
        # No max_length on the wire — Django stores hashes, not the
        # raw password, so length is bounded by the password
        # validators (and by HTTP body limits at the proxy).
    )
    confirm_password = serializers.CharField(write_only=True)
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    date_of_birth = serializers.DateField()
    phone = serializers.CharField(
        max_length=50, required=False, allow_blank=True, default='',
    )
    accept_terms = serializers.BooleanField()

    def validate_date_of_birth(self, value: date) -> date:
        if value > date.today():
            raise serializers.ValidationError(
                'Date of birth cannot be in the future.'
            )
        return value

    def validate(self, attrs):
        # Re-type guardrail. Compared after per-field validation so the
        # password is already normalised by ``validate_password``.
        if attrs.get('password') != attrs.get('confirm_password'):
            raise serializers.ValidationError({
                'confirm_password': 'Passwords do not match.',
            })
        return attrs

    def create(self, validated_data: dict):
        request = self.context.get('request')
        return register_patient_account(
            email=validated_data['email'],
            password=validated_data['password'],
            first_name=validated_data['first_name'],
            last_name=validated_data['last_name'],
            date_of_birth=validated_data['date_of_birth'],
            phone=validated_data.get('phone', ''),
            accept_terms=validated_data['accept_terms'],
            request=request,
        )


class PatientVerifyEmailSerializer(serializers.Serializer):
    """Single-field input shape for ``POST /verify-email/``."""
    token = serializers.CharField(min_length=10, max_length=200)


class PatientLoginSerializer(serializers.Serializer):
    """Email + password input for ``POST /login/``. No
    cross-field validation here — the view delegates to
    ``authenticate_patient`` which owns the credentials policy."""
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)


class PatientMeSerializer(serializers.Serializer):
    """Read-only projection of ``PatientAccount`` + ``PatientProfile``
    surfaced by ``GET /me/``. Built by hand (rather than as a
    ``ModelSerializer``) so the wire shape is decoupled from the
    underlying tables and reorganising either model never silently
    changes the API contract."""
    id = serializers.UUIDField(read_only=True)
    email = serializers.EmailField(read_only=True)
    email_verified_at = serializers.DateTimeField(read_only=True, allow_null=True)
    cytova_patient_id = serializers.CharField(read_only=True)
    first_name = serializers.CharField(read_only=True)
    last_name = serializers.CharField(read_only=True)
    date_of_birth = serializers.DateField(read_only=True)
    phone = serializers.CharField(read_only=True, allow_blank=True)


# ---------------------------------------------------------------------------
# Shared results (lab → patient portal snapshot read API)
# ---------------------------------------------------------------------------

class PatientSharedResultFileSerializer(serializers.Serializer):
    """Public projection of a ``PatientSharedResultFile`` row.

    ``storage_key`` is intentionally NOT included — it's an internal
    snapshot of the underlying tenant storage path that the future
    download endpoint needs but the patient never sees. ``download_url``
    is built from the opaque ``file_token`` so the patient frontend can
    hit the download endpoint without ever learning the storage
    location.
    """
    id = serializers.UUIDField(read_only=True)
    filename = serializers.CharField(read_only=True)
    download_url = serializers.SerializerMethodField()

    def get_download_url(self, obj) -> str:
        return f'/api/v1/patient-portal/results/files/{obj.file_token}/download/'


# Window during which a never-downloaded share counts as "New" in the
# patient UI. Anything older is just "Available" — keeps the badge
# meaningful for patients who log in regularly.
_NEW_SHARE_WINDOW_DAYS = 14


class PatientSharedResultSerializer(serializers.ModelSerializer):
    """Public projection of a ``PatientSharedResult`` row plus its
    files. Used by ``GET /api/v1/patient-portal/results/``.

    Adds UX-friendly fields the frontend can render without re-deriving
    them on every paint:
      - ``status_label``: friendly text matching the spec (no raw
        enum values shown to patients).
      - ``is_new``: true when never downloaded AND created in the
        last ``_NEW_SHARE_WINDOW_DAYS`` days.

    Internal fields (``storage_key``, ``source_tenant_schema``,
    ``source_request_id``, ``revoked_by_lab``, ``email_notification_*``)
    are deliberately NOT exposed here.
    """
    files = PatientSharedResultFileSerializer(many=True, read_only=True)
    status_label = serializers.SerializerMethodField()
    is_new = serializers.SerializerMethodField()

    class Meta:
        from .models import PatientSharedResult as _PSR
        model = _PSR
        fields = [
            'id', 'source_type', 'source_name', 'request_reference',
            'request_date', 'result_available_date',
            'status', 'status_label', 'is_new',
            'last_downloaded_at', 'download_count',
            'created_at', 'files',
            # Patient-facing version metadata (Phase 1 → 4 lineage):
            # the frontend renders a "Version N" badge on the result
            # card and conditionally shows a "View versions" button
            # when ``report_version_number > 1``. Exposing these here
            # avoids a per-card extra round-trip just to fetch the
            # version number; the full version history still lives at
            # ``/results/{id}/versions/``.
            'report_version_number', 'shared_at', 'shared_channel',
        ]
        read_only_fields = fields

    _LABELS = {
        'ACTIVE': 'Available',
        'HIDDEN_BY_PATIENT': 'Hidden',
        'REVOKED': 'No longer available',
    }

    def get_status_label(self, obj) -> str:
        return self._LABELS.get(obj.status, 'Available')

    def get_is_new(self, obj) -> bool:
        from datetime import timedelta
        from django.utils import timezone as _tz
        if obj.last_downloaded_at is not None:
            return False
        if obj.download_count > 0:
            return False
        cutoff = _tz.now() - timedelta(days=_NEW_SHARE_WINDOW_DAYS)
        return obj.created_at >= cutoff


# ---------------------------------------------------------------------------
# Shared result versions (version history per source request)
# ---------------------------------------------------------------------------

class PatientSharedResultVersionSerializer(serializers.Serializer):
    """One row in the patient-facing version history of a shared
    result. Distinct from ``PatientSharedResultSerializer`` because:

    - the version-history view doesn't need the lab name / dates / "is
      new" badge — those are list-page concerns,
    - it adds a ``status`` field whose semantics are version-line-local
      (``CURRENT`` vs. ``SUPERSEDED``) rather than the patient-portal
      lifecycle (``ACTIVE`` / ``HIDDEN_BY_PATIENT`` / ``REVOKED``),
    - it exposes a single ``download_url`` (the version's own PDF) so
      the UI can render a per-version download button without having
      to know that the underlying model has a ``files`` relation.

    ``storage_key`` and ``file_token`` are NEVER serialised — the
    download URL embeds the opaque file token only because the
    download endpoint expects it.
    """
    id = serializers.UUIDField(read_only=True)
    version_number = serializers.IntegerField(
        source='report_version_number', read_only=True, allow_null=True,
    )
    shared_at = serializers.DateTimeField(read_only=True, allow_null=True)
    shared_channel = serializers.CharField(read_only=True, allow_blank=True)
    status = serializers.SerializerMethodField()
    download_url = serializers.SerializerMethodField()

    def get_status(self, obj) -> str:
        """``CURRENT`` for the row marked current_for_patient, otherwise
        ``SUPERSEDED``. The patient view never sees REVOKED or
        HIDDEN_BY_PATIENT rows in the version list — those are filtered
        upstream — so two values are sufficient."""
        return 'CURRENT' if obj.is_current_for_patient else 'SUPERSEDED'

    def get_download_url(self, obj):
        """Resolve to the file_token of this version's PDF. The view
        passes a prefetched ``files`` queryset so this never triggers
        an N+1 query. Returns ``None`` when the version has no file
        (defensive — every Notify-Cytova share creates exactly one
        file row)."""
        files = list(obj.files.all())
        if not files:
            return None
        return f'/api/v1/patient-portal/results/files/{files[0].file_token}/download/'


class _CurrentVersionSerializer(serializers.Serializer):
    """Compact projection used in the ``current_version`` field of the
    versions endpoint response. Mirrors the spec example: no ``id`` or
    ``status`` (the parent envelope already names this row as the
    current one)."""
    version_number = serializers.IntegerField(
        source='report_version_number', read_only=True, allow_null=True,
    )
    shared_at = serializers.DateTimeField(read_only=True, allow_null=True)
    shared_channel = serializers.CharField(read_only=True, allow_blank=True)
    download_url = serializers.SerializerMethodField()

    def get_download_url(self, obj):
        files = list(obj.files.all())
        if not files:
            return None
        return f'/api/v1/patient-portal/results/files/{files[0].file_token}/download/'


# ---------------------------------------------------------------------------
# Logout + refresh
# ---------------------------------------------------------------------------

class PatientLogoutSerializer(serializers.Serializer):
    """Optional fields:
      - ``refresh_token`` — if supplied, that specific refresh row is
        also blacklisted alongside the access token from the
        Authorization header. Without it, only the access token is
        blacklisted.
      - ``all_sessions`` — when true, blacklist every outstanding
        token for the authenticated account (kills every device /
        browser the patient is signed in from).
    """
    refresh_token = serializers.CharField(required=False, allow_blank=True)
    all_sessions = serializers.BooleanField(required=False, default=False)


class PatientRefreshSerializer(serializers.Serializer):
    """Single field — the refresh token string. Validation happens in
    the service (signature check + blacklist check + jti lookup);
    keeping the serializer thin lets us return the same envelope
    regardless of which sub-cause refused."""
    refresh_token = serializers.CharField()
