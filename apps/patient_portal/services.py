"""
Cytova — Patient Portal services.

Thin business-logic layer that HTTP views and the test suite both call
into. Keeping the orchestration here means the HTTP layer stays trivial
and the unit tests don't have to spin up a request cycle to exercise
the rules.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from common.email.service import get_email_service
from common.utils.crypto import generate_secure_token, hash_token

from .id_generator import generate_cytova_patient_id
from .models import (
    PatientAccount, PatientConsent, PatientEmailVerificationToken,
    PatientProfile,
)
from .tokens import PatientAccessToken, PatientRefreshToken

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EMAIL_VERIFICATION_TTL_HOURS = 24


def _verify_email_link(plaintext_token: str) -> str:
    """Build the public-facing verification URL. The base lives in
    settings so dev / staging / prod can each point at their own
    frontend host without code changes."""
    base = (
        getattr(settings, 'PATIENT_PORTAL_VERIFY_EMAIL_URL', None)
        or 'https://www.cytova.io/verify-email'
    )
    sep = '&' if '?' in base else '?'
    return f'{base}{sep}token={plaintext_token}'


def _issue_email_verification_token(account: PatientAccount, *, request=None) -> str:
    """Create a fresh single-use verification token for ``account`` and
    return the **plaintext** value (the database stores the hash only).

    Any outstanding tokens for the same account are invalidated first —
    the most recent issuance is the only one that should work, mirroring
    the staff-side password-reset behaviour.
    """
    # Invalidate any prior outstanding tokens — the new one is the only
    # one that should work going forward.
    PatientEmailVerificationToken.objects.filter(
        account=account, is_used=False,
    ).update(is_used=True, used_at=timezone.now())

    plaintext = generate_secure_token()
    PatientEmailVerificationToken.objects.create(
        account=account,
        token_hash=hash_token(plaintext),
        expires_at=timezone.now() + timedelta(hours=EMAIL_VERIFICATION_TTL_HOURS),
        created_by_ip=getattr(request, 'audit_ip', None) if request else None,
    )
    return plaintext


def _send_verification_email(
    account: PatientAccount, profile: PatientProfile, plaintext_token: str,
) -> None:
    """Dispatch the verification email. Failures are logged, not raised
    — signup must not 500 because SMTP is wobbly. The patient can
    re-trigger via a future resend endpoint (out of scope for this step)."""
    link = _verify_email_link(plaintext_token)
    result = get_email_service().send_patient_verification_email(
        recipient_email=account.email,
        recipient_name=profile.first_name,
        verify_link=link,
        expires_hours=EMAIL_VERIFICATION_TTL_HOURS,
    )
    if result.ok:
        logger.info(
            'Patient verification email sent: account_id=%s', account.id,
        )
    else:
        logger.error(
            'Patient verification email NOT delivered: account_id=%s error=%s',
            account.id, result.error,
        )


def register_patient_account(
    *,
    email: str,
    password: str,
    first_name: str,
    last_name: str,
    date_of_birth: date,
    accept_terms: bool,
    phone: str = '',
    request=None,
) -> PatientAccount:
    """
    Create the three rows that make up a new portal identity in a single
    transaction:

      1. ``PatientAccount`` (hashed password via ``set_password``).
      2. ``PatientProfile`` with a freshly-generated Cytova Patient ID.
      3. ``PatientConsent`` snapshotting the active terms / privacy
         versions plus the request's IP / user-agent.

    Errors
    ------
    - ``ValidationError({'accept_terms': ...})`` if the patient did not
      tick the consent box. The check happens BEFORE any DB write so a
      refused-consent attempt leaves no trace.
    - ``ValidationError({'email': ...})`` on duplicate email — the DB
      unique constraint catches the race and we surface it as a clean
      400.
    """
    if not accept_terms:
        raise ValidationError({
            'accept_terms': 'You must accept the Terms of Service and Privacy Policy.',
        })

    # ``request`` is optional: the service is reachable from background
    # jobs / tests / management commands. ``getattr`` with default lets
    # the audit-context middleware feed us IP/UA when we're inside an
    # HTTP request, and stays silent otherwise.
    ip_address = getattr(request, 'audit_ip', None)
    user_agent = getattr(request, 'audit_user_agent', '') or ''

    # Snapshot the *current* policy versions onto the consent row. A
    # later bump in settings does not retroactively change history —
    # which is the point of consent versioning.
    terms_version = settings.PATIENT_TERMS_VERSION
    privacy_version = settings.PATIENT_PRIVACY_VERSION

    try:
        with transaction.atomic():
            account = PatientAccount.objects.create_user(
                email=email,
                password=password,
            )
            profile = PatientProfile.objects.create(
                account=account,
                cytova_patient_id=generate_cytova_patient_id(),
                first_name=first_name,
                last_name=last_name,
                date_of_birth=date_of_birth,
                phone=phone,
            )
            PatientConsent.objects.create(
                account=account,
                terms_version=terms_version,
                privacy_version=privacy_version,
                accepted_at=timezone.now(),
                ip_address=ip_address,
                user_agent=user_agent,
            )
            # Issue a fresh email-verification token in the same
            # transaction so a failure to write the token rolls back the
            # account creation. The email itself is sent OUTSIDE the
            # transaction (after commit) — see below.
            verification_plaintext = _issue_email_verification_token(
                account, request=request,
            )
    except IntegrityError as exc:
        # Two unique constraints in play: email on PatientAccount and
        # cytova_patient_id on PatientProfile. Email collisions are an
        # actual user error (different person trying to reuse an
        # address); ID collisions are vanishingly rare and indicate a
        # generator/race bug worth surfacing loudly.
        message = str(exc).lower()
        if 'email' in message:
            raise ValidationError({
                'email': 'An account with this email already exists.',
            }) from exc
        logger.exception('Patient signup IntegrityError: %s', exc)
        raise

    # Deliberately NOT logging email, password, or DOB. The account ID
    # is enough for ops to trace the signup in the request log.
    logger.info('Patient portal account created: id=%s', account.id)

    # Send the verification email OUTSIDE the transaction so a slow /
    # failing SMTP provider can't hold the row lock. Failure is logged
    # internally; the signup still succeeds — patients can hit a
    # future "resend verification" endpoint.
    _send_verification_email(account, profile, verification_plaintext)

    return account


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------

class InvalidVerificationToken(Exception):
    """Raised when verify_email_token cannot consume the supplied token —
    unknown / expired / already used / belongs to an inactive account.
    A single exception type is intentional: we never tell the caller
    *why* verification failed, to avoid token-guessing oracles."""


def verify_email_token(plaintext_token: str, *, request=None) -> PatientAccount:
    """Consume a verification token and stamp the patient's
    ``email_verified_at``. Returns the verified account on success;
    raises ``InvalidVerificationToken`` for any failure mode.

    The lookup keys on the SHA-256 hash so the plaintext never appears
    in DB query logs. All other outstanding tokens for the same account
    are invalidated on success — once the inbox has been proven, older
    tokens shouldn't sit around.
    """
    if not plaintext_token:
        raise InvalidVerificationToken()

    token_hash = hash_token(plaintext_token)
    request_ip = getattr(request, 'audit_ip', None) if request else None

    try:
        token = PatientEmailVerificationToken.objects.select_related('account').get(
            token_hash=token_hash,
            is_used=False,
            expires_at__gt=timezone.now(),
        )
    except PatientEmailVerificationToken.DoesNotExist:
        # Never log the token itself, hashed or otherwise — only the IP
        # for forensic correlation against rate-limit logs.
        logger.warning(
            'Patient email verification with invalid/expired/used token: ip=%s',
            request_ip,
        )
        raise InvalidVerificationToken()

    account = token.account
    if not account.is_active:
        logger.warning(
            'Patient email verification refused — account inactive: id=%s ip=%s',
            account.id, request_ip,
        )
        raise InvalidVerificationToken()

    now = timezone.now()
    with transaction.atomic():
        if account.email_verified_at is None:
            account.email_verified_at = now
            account.save(update_fields=['email_verified_at', 'updated_at'])

        token.is_used = True
        token.used_at = now
        token.save(update_fields=['is_used', 'used_at'])

        # Defence in depth — invalidate any other outstanding tokens
        # for this account.
        PatientEmailVerificationToken.objects.filter(
            account=account, is_used=False,
        ).exclude(pk=token.pk).update(is_used=True, used_at=now)

    logger.info('Patient email verified: account_id=%s', account.id)
    return account


# ---------------------------------------------------------------------------
# Login + token issuance
# ---------------------------------------------------------------------------

class InvalidPatientCredentials(Exception):
    """Generic credential-failure exception. The caller MUST surface a
    non-distinguishing message (no "email exists" leak, no "wrong
    password" leak, no "not verified" hint to a bad password)."""


class EmailNotVerified(Exception):
    """Surfaced separately so the login endpoint can return a distinct
    error code to a *known-good* credential pair whose email isn't yet
    verified — UX needs to tell the user to check their inbox. This
    only fires AFTER a successful password check, so it never serves
    as an enumeration oracle."""


def authenticate_patient(*, email: str, password: str) -> PatientAccount:
    """Verify email + password against ``PatientAccount`` and return the
    account on success. Raises:
      - ``InvalidPatientCredentials`` for unknown email, wrong password,
        or inactive account.
      - ``EmailNotVerified`` only after the password check passes — so
        a wrong password against any state never leaks the verification
        status of the email.
    """
    normalized = (email or '').strip().lower()
    try:
        account = PatientAccount.objects.get(email=normalized)
    except PatientAccount.DoesNotExist:
        # Run a dummy password check to keep timing roughly constant
        # between known and unknown emails. The cost of a single PBKDF2
        # iteration is dominated by the constant-rounds setting, so
        # the timing signal is not perfectly flat — but a no-op return
        # would be much worse.
        PatientAccount().set_password(password)
        raise InvalidPatientCredentials()

    if not account.is_active or not account.check_password(password):
        raise InvalidPatientCredentials()

    if account.email_verified_at is None:
        raise EmailNotVerified()

    # Touch ``last_login`` for parity with the staff flow — useful for
    # support / audit even though patient sessions don't (yet) write
    # AuditLog entries.
    account.last_login = timezone.now()
    account.save(update_fields=['last_login'])
    return account


def revoke_shares_for_lab_request(
    *,
    tenant_schema: str,
    source_request_id,
    revoked_by_lab: str,
    request=None,
) -> int:
    """Revoke every active ``PatientSharedResult`` previously created
    from one lab-tenant request.

    Scoping
    -------
    - ``tenant_schema``: the snapshot schema name on the row. Required
      so a different lab tenant whose request happens to share the
      same UUID can never revoke a peer's share.
    - ``source_request_id``: the UUID of the originating
      ``AnalysisRequest`` in the lab schema, captured at share time.
      Soft reference (no FK across schemas).

    Only ``ACTIVE`` rows are affected. ``HIDDEN_BY_PATIENT`` rows are
    left alone — those are patient-controlled, not lab-controlled.
    Already-revoked rows are not touched again (idempotent).

    Side effects
    ------------
    - Stamps ``revoked_at`` + ``revoked_by_lab`` on each affected row.
    - Writes one ``PATIENT_RESULT_REVOKED_BY_LAB`` patient audit row
      per affected share. The corresponding tenant audit row is
      written by the caller in the lab tenant schema.

    Returns the number of rows actually revoked.
    """
    from .audit import write_event
    from .models import (
        PatientPortalAuditAction, PatientSharedResult, SharedResultStatus,
    )

    qs = PatientSharedResult.objects.filter(
        source_tenant_schema=tenant_schema or '',
        source_request_id=source_request_id,
        status=SharedResultStatus.ACTIVE,
    )

    affected = list(qs.values('id', 'patient_account_id', 'request_reference'))
    if not affected:
        return 0

    now = timezone.now()
    revoked_label = (revoked_by_lab or '')[:255]
    qs.update(
        status=SharedResultStatus.REVOKED,
        revoked_at=now,
        revoked_by_lab=revoked_label,
    )

    for row in affected:
        write_event(
            action=PatientPortalAuditAction.PATIENT_RESULT_REVOKED_BY_LAB.value,
            entity_type='PatientSharedResult',
            entity_id=row['id'],
            # FK target only — we don't need to fetch the full row.
            patient_account=PatientAccount(pk=row['patient_account_id']),
            request=request,
            metadata={
                'shared_result_id': row['id'],
                'source_request_reference': row['request_reference'],
                'revoked_by_lab': revoked_label,
            },
        )

    return len(affected)


def _record_outstanding_token(
    *,
    account: PatientAccount,
    token,
    token_type: str,
    request=None,
) -> None:
    """Persist a ``PatientOutstandingToken`` row for ``token``.

    The auth backend rejects any token whose JTI doesn't appear here,
    so issuance MUST write this row before the token is handed back
    over the wire. ``token.payload`` is the canonical place for jti +
    exp; we read both straight from the simplejwt payload.

    IP / UA come from the issuing request when available — useful for
    forensic review (e.g. listing all sessions for an account). When
    the caller is a service test or background job, both fall back to
    empty/null.
    """
    from datetime import datetime, timezone as _tz
    from .models import PatientOutstandingToken

    jti = token.payload.get('jti')
    exp_ts = token.payload.get('exp')
    if not jti or exp_ts is None:
        # Defensive: every simplejwt-issued token has both. If we ever
        # miss one we want to fail loudly at issuance rather than
        # accept an unverifiable token at auth time.
        raise RuntimeError(
            'Patient token missing jti/exp claim — cannot persist '
            'outstanding row.',
        )

    expires_at = datetime.fromtimestamp(exp_ts, tz=_tz.utc)
    PatientOutstandingToken.objects.create(
        patient_account=account,
        jti=jti,
        token_type=token_type,
        expires_at=expires_at,
        ip_address=getattr(request, 'audit_ip', None) if request else None,
        user_agent=(
            (getattr(request, 'audit_user_agent', '') or '')[:500]
            if request else ''
        ),
    )


def issue_patient_tokens(
    account: PatientAccount,
    *,
    request=None,
) -> dict:
    """Issue an access + refresh token pair for a verified patient
    account. Returns the wire payload the login + refresh endpoints
    surface. The shape mirrors the staff login response so the
    frontend can share its token-handling utilities.

    Side effect
    -----------
    Two ``PatientOutstandingToken`` rows are written (one per token)
    BEFORE the tokens are returned. The auth backend will refuse any
    token whose jti doesn't appear in this table, so missing the
    write would lock the patient out of every authenticated endpoint.
    """
    from .models import PatientTokenType

    profile = PatientProfile.objects.only(
        'first_name', 'last_name', 'cytova_patient_id',
    ).get(account=account)

    refresh = PatientRefreshToken.for_patient(account, profile=profile)
    access = PatientAccessToken.for_patient(account, profile=profile)

    _record_outstanding_token(
        account=account, token=access,
        token_type=PatientTokenType.ACCESS, request=request,
    )
    _record_outstanding_token(
        account=account, token=refresh,
        token_type=PatientTokenType.REFRESH, request=request,
    )

    return {
        'access_token': str(access),
        'refresh_token': str(refresh),
        'token_type': 'Bearer',
        'expires_in': int(access.lifetime.total_seconds()),
        'patient': {
            'id': str(account.id),
            'email': account.email,
            'cytova_patient_id': profile.cytova_patient_id,
            'first_name': profile.first_name,
            'last_name': profile.last_name,
        },
    }


def blacklist_patient_token_by_jti(jti: str) -> bool:
    """Mark a single outstanding token as revoked. Returns ``True``
    when a row was newly blacklisted, ``False`` when the jti is
    unknown or already blacklisted (idempotent)."""
    from .models import PatientBlacklistedToken, PatientOutstandingToken
    if not jti:
        return False
    try:
        outstanding = PatientOutstandingToken.objects.get(jti=jti)
    except PatientOutstandingToken.DoesNotExist:
        return False
    _, created = PatientBlacklistedToken.objects.get_or_create(token=outstanding)
    return created


def blacklist_all_tokens_for_account(account: PatientAccount) -> int:
    """Blacklist every outstanding token (access + refresh) for one
    patient. Used by:

      - logout-all (the patient asks to terminate every session)
      - password change / reset (defence in depth: stolen tokens are
        invalidated immediately)
      - account deactivation (future)

    Idempotent — already-blacklisted rows are skipped via
    ``ignore_conflicts``. Returns the number of NEW blacklist entries
    created.
    """
    from .models import PatientBlacklistedToken, PatientOutstandingToken

    outstanding_ids = list(
        PatientOutstandingToken.objects
        .filter(patient_account=account)
        .exclude(blacklist__isnull=False)
        .values_list('id', flat=True)
    )
    if not outstanding_ids:
        return 0
    rows = [
        PatientBlacklistedToken(token_id=token_id)
        for token_id in outstanding_ids
    ]
    PatientBlacklistedToken.objects.bulk_create(
        rows, ignore_conflicts=True,
    )
    return len(rows)


def get_active_consent(account: PatientAccount) -> Optional[PatientConsent]:
    """
    Return the most recently accepted consent row for an account, or
    ``None`` if the account has never accepted any version (shouldn't
    happen via the normal signup path, but defensively useful for the
    future "you must re-accept the new terms" gate).
    """
    return account.consents.order_by('-accepted_at').first()
