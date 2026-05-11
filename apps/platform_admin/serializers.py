"""DRF serializers for the platform-admin auth + tenant + patient surface."""
from __future__ import annotations

from rest_framework import serializers

from apps.patient_portal.models import PatientAccount
from apps.tenants.models import SubscriptionPlan, Tenant

from .models import PlatformAdminUser


class PlatformAdminLoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)


class PlatformAdminTeamMemberSerializer(serializers.ModelSerializer):
    """Read shape for ``/team/`` list + detail + action responses.

    Mirrors ``PlatformAdminProfileSerializer`` but is the canonical
    name used by the team API surface. Kept distinct so a future
    profile-only fields change (e.g. preferences) doesn't churn the
    team list contract.
    """
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = PlatformAdminUser
        fields = [
            'id', 'email', 'first_name', 'last_name', 'full_name',
            'role', 'is_active', 'last_login',
            'created_at', 'updated_at',
        ]
        read_only_fields = fields

    def get_full_name(self, obj) -> str:
        return obj.full_name


class PlatformAdminTeamCreateSerializer(serializers.Serializer):
    """Input for ``POST /team/``."""
    # Match django-tenants email handling — case-insensitive uniqueness
    # is enforced in the service. Validation here just normalises.
    email = serializers.EmailField()
    first_name = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default='',
    )
    last_name = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default='',
    )
    role = serializers.ChoiceField(
        # Sourced lazily so a new role added to the enum is picked up
        # without a re-import. Each ``ChoiceField`` instance freezes the
        # choices at definition time, but that's fine — the import sits
        # at module top-level so any value change requires a restart
        # anyway.
        choices=[],
    )

    def __init__(self, *args, **kwargs):
        # ``ChoiceField.choices`` can't reference an import that isn't
        # in scope at class-body evaluation order. Wire it up here.
        super().__init__(*args, **kwargs)
        from .models import PlatformAdminRole
        self.fields['role'].choices = PlatformAdminRole.choices


class PlatformAdminTeamChangeRoleSerializer(serializers.Serializer):
    """Input for ``POST /team/{id}/change-role/``."""
    role = serializers.ChoiceField(choices=[])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .models import PlatformAdminRole
        self.fields['role'].choices = PlatformAdminRole.choices


class PlatformAdminProfileSerializer(serializers.ModelSerializer):
    """Read-only profile shape returned by ``/auth/me/``.

    Deliberately minimal — only the fields the back-office UI needs
    to render the navbar and gate role-aware controls. Anything
    sensitive (password hash, audit history) is never serialised.
    """
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = PlatformAdminUser
        fields = [
            'id', 'email', 'first_name', 'last_name', 'full_name',
            'role', 'is_active', 'last_login',
            'created_at', 'updated_at',
        ]
        read_only_fields = fields

    def get_full_name(self, obj) -> str:
        return obj.full_name


class PlatformTenantSerializer(serializers.ModelSerializer):
    """Read-only tenant projection for the platform-admin tenant list.

    All sourced fields live in the public schema:
      - ``Tenant``        — name, subdomain, is_active, created_at
      - ``Domain``        — primary domain, surfaced as ``domain_url``
      - ``Subscription``  — newest row's status / trial_end_date

    Tenant-schema tables are never queried — the platform admin
    surface deliberately stays at the metadata layer. Cross-schema
    joins would also break the django-tenants isolation contract.

    Performance:
      The view layer pre-selects ``domains`` and
      ``subscriptions__plan`` via ``prefetch_related`` so resolving
      these methods does not issue per-row queries.
    """
    slug = serializers.CharField(source='subdomain', read_only=True)
    domain_url = serializers.SerializerMethodField()
    subscription_status = serializers.SerializerMethodField()
    trial_end_date = serializers.SerializerMethodField()

    class Meta:
        model = Tenant
        fields = [
            'id', 'name', 'slug', 'domain_url',
            'is_active', 'created_at',
            'trial_end_date', 'subscription_status',
        ]
        read_only_fields = fields

    def _primary_domain(self, obj):
        # ``domains`` is prefetched on the queryset — iterating in
        # Python beats hitting the DB for a ``filter(is_primary=True)``
        # query that would defeat the prefetch. Falls back to the
        # first domain if none is flagged primary.
        domains = list(obj.domains.all())
        if not domains:
            return None
        for d in domains:
            if getattr(d, 'is_primary', False):
                return d
        return domains[0]

    def get_domain_url(self, obj) -> str | None:
        domain = self._primary_domain(obj)
        if domain is None:
            return None
        return f'https://{domain.domain}'

    def _latest_subscription(self, obj):
        # Same prefetch-friendly pattern: sort the prefetched list
        # in Python rather than re-querying with ``order_by``. The
        # newest subscription wins so a recently-cancelled plan
        # surfaces over an older active one — the platform admin
        # cares about *current* state, not historical.
        subs = list(obj.subscriptions.all())
        if not subs:
            return None
        subs.sort(key=lambda s: s.created_at, reverse=True)
        return subs[0]

    def get_subscription_status(self, obj) -> str | None:
        sub = self._latest_subscription(obj)
        return sub.status if sub else None

    def get_trial_end_date(self, obj):
        sub = self._latest_subscription(obj)
        return sub.trial_end_date if sub else None


# ---------------------------------------------------------------------------
# Tenant action input serializers
# ---------------------------------------------------------------------------

# Hard ceiling on trial extensions per call. Keeps a typo'd ``days`` value
# from accidentally extending a trial by years. Long extensions can still
# be issued by repeating the call, which leaves a trail of audit rows
# rather than one giant jump.
EXTEND_TRIAL_MAX_DAYS = 365


class ExtendTrialSerializer(serializers.Serializer):
    """Input for ``POST /tenants/{id}/extend-trial/``.

    ``days`` must be strictly positive — a zero/negative value would
    silently reverse or no-op the action, producing a misleading
    audit row. The upper bound is a defence against typos rather
    than a business rule (multi-year extensions can still be issued
    via repeated calls, each independently audited).
    """
    days = serializers.IntegerField(min_value=1, max_value=EXTEND_TRIAL_MAX_DAYS)


class ChangePlanSerializer(serializers.Serializer):
    """Input for ``POST /tenants/{id}/change-plan/``.

    ``plan_id`` must reference an *active* SubscriptionPlan. The
    view layer fetches the plan a second time inside a transaction
    so a concurrent deactivation between validation and use still
    raises — this validator catches the obvious bad-input case
    before we open the transaction.
    """
    plan_id = serializers.PrimaryKeyRelatedField(
        queryset=SubscriptionPlan.objects.filter(is_active=True),
    )


# ---------------------------------------------------------------------------
# Patient account serializer (platform-admin surface)
# ---------------------------------------------------------------------------

class PlatformPatientAccountSerializer(serializers.ModelSerializer):
    """Read-only projection of a global ``PatientAccount`` for platform admins.

    Strict allow-list of fields. The platform-admin surface is for
    account-level support (suspending abusive accounts, confirming
    that an email is registered) — NOT a clinical lookup tool. We
    deliberately do NOT expose:

      - ``password`` (hash) — never serialised on any surface.
      - ``first_name`` / ``last_name`` / ``date_of_birth`` / ``phone``
        from ``PatientProfile`` — these are clinical-adjacent PII.
      - Anything from ``PatientSharedResult`` rows beyond a count
        (no source name, no request reference, no file metadata).
      - Tokens (verification, JWT outstanding/blacklisted) — those
        are credentials, not metadata.

    ``cytova_patient_id`` IS included because it is the public-facing
    identifier patients quote to staff anyway, so a support operator
    needs it to correlate calls. It is not by itself sensitive.

    ``results_count`` is computed via a single ``annotate`` on the
    queryset (see the view's ``get_queryset``) so it costs one extra
    aggregate column rather than N individual COUNTs.
    """
    is_email_verified = serializers.SerializerMethodField()
    cytova_patient_id = serializers.SerializerMethodField()
    results_count = serializers.IntegerField(
        read_only=True,
        # Default for the detail path which doesn't use the
        # annotate-style queryset; the view falls back to a per-row
        # count there. Always present on the response.
        default=0,
    )

    class Meta:
        model = PatientAccount
        fields = [
            'id', 'email', 'is_active',
            'created_at', 'last_login',
            'is_email_verified', 'cytova_patient_id', 'results_count',
        ]
        read_only_fields = fields

    def get_is_email_verified(self, obj) -> bool:
        # Boolean derived from the timestamp — the timestamp itself
        # isn't surfaced (the operator doesn't need to know exactly
        # when the patient verified, just whether they did).
        return obj.email_verified_at is not None

    def get_cytova_patient_id(self, obj) -> str | None:
        # ``profile`` is a OneToOne reverse accessor — may not exist
        # for accounts that never completed signup. ``select_related``
        # at the queryset level keeps this from issuing per-row
        # SELECTs.
        profile = getattr(obj, 'profile', None)
        return profile.cytova_patient_id if profile else None


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class _DashboardTenantsSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    active = serializers.IntegerField()
    suspended = serializers.IntegerField()
    trial = serializers.IntegerField()


class _DashboardPatientsSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    active = serializers.IntegerField()
    new_last_30_days = serializers.IntegerField()


class _DashboardActivitySerializer(serializers.Serializer):
    results_shared_last_30_days = serializers.IntegerField()
    results_downloaded_last_30_days = serializers.IntegerField()
    emails_sent_last_30_days = serializers.IntegerField()


class PlatformDashboardSerializer(serializers.Serializer):
    """Top-level shape of the platform dashboard response.

    Strictly aggregated integers — no list of ids, no email, no
    subdomain string. The point is a forensic-safe overview that
    can be cached, screenshot-shared, or pasted into a status
    channel without leaking tenant or patient identity.

    The serializer is read-only by construction (``Serializer``
    base, no ``create``/``update``); the view never feeds it
    user-supplied data.
    """
    generated_at = serializers.DateTimeField()
    window_days = serializers.IntegerField()
    tenants = _DashboardTenantsSerializer()
    patients = _DashboardPatientsSerializer()
    activity = _DashboardActivitySerializer()
