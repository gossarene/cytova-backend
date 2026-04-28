"""
Cytova — Analysis Request Models

AnalysisRequest
    A lab work order for a single patient. Lives in DRAFT until confirmed by
    reception. Confirmation locks the item list.
    Hard delete is blocked — requests are medical records.

AnalysisRequestItem
    One exam line within a request. Carries execution metadata (mode, partner,
    rejection reason) and pricing (unit_price snapshotted from exam definition,
    billed_price resolved from pricing rule or manual override, price_source
    for traceability). Pricing is set at item creation, not confirmation.
    Hard delete is blocked at the model level; service layer removes items from
    DRAFT requests via queryset.delete().

ExamTraceability
    Mandatory companion to every AnalysisRequestItem (created in the same
    transaction). Tracks sample receipt, processing start, and completion,
    together with who performed the analysis.
"""
import uuid

from django.db import models
from django.utils import timezone

from common.models import BaseModel


# ---------------------------------------------------------------------------
# Choice enumerations
# ---------------------------------------------------------------------------

class RequestStatus(models.TextChoices):
    """Laboratory workflow status — the medical/operational state of the
    request as it moves through collection, analysis, and validation.

    DELIVERED and ARCHIVED used to live here in an earlier design but were
    extracted to ``ClosureStatus`` below so post-processing closure does
    not contaminate billing or the state-machine guards.
    """
    DRAFT                   = 'DRAFT',                   'Draft'
    CONFIRMED               = 'CONFIRMED',               'Confirmed'
    COLLECTION_IN_PROGRESS  = 'COLLECTION_IN_PROGRESS',  'Collection In Progress'
    IN_ANALYSIS             = 'IN_ANALYSIS',             'In Analysis'
    AWAITING_REVIEW         = 'AWAITING_REVIEW',         'Awaiting Review'
    RETEST_REQUIRED         = 'RETEST_REQUIRED',         'Retest Required'
    READY_FOR_RELEASE       = 'READY_FOR_RELEASE',       'Ready For Release'
    VALIDATED               = 'VALIDATED',               'Validated'
    IN_PROGRESS             = 'IN_PROGRESS',             'In Progress'
    COMPLETED               = 'COMPLETED',               'Completed'
    CANCELLED               = 'CANCELLED',               'Cancelled'


class ClosureStatus(models.TextChoices):
    """Post-processing closure state, orthogonal to workflow ``status``.

    OPEN      — request is in the active worklist (default)
    DELIVERED — patient has been notified / handed the result
    ARCHIVED  — manually closed; hidden from the active list

    Workflow status (DRAFT...VALIDATED/COMPLETED/CANCELLED) is unaffected by
    closure transitions, which is what keeps billing queries that look at
    ``status=VALIDATED`` correct after a request is delivered/archived.
    """
    OPEN      = 'OPEN',      'Open'
    DELIVERED = 'DELIVERED', 'Delivered'
    ARCHIVED  = 'ARCHIVED',  'Archived'


# Closure values that are hidden from the default request list. The
# lifecycle filter (``?lifecycle=delivered|archived|all``) opts them back
# in. ``OPEN`` is the only closure value visible by default.
ACTIVE_CLOSURE = ClosureStatus.OPEN


class ItemStatus(models.TextChoices):
    PENDING        = 'PENDING',        'Pending Collection'
    COLLECTED      = 'COLLECTED',      'Collected'
    RESULT_ENTERED = 'RESULT_ENTERED', 'Result Entered'
    UNDER_REVIEW   = 'UNDER_REVIEW',   'Under Review'
    VALIDATED      = 'VALIDATED',      'Validated'
    IN_PROGRESS    = 'IN_PROGRESS',    'In Progress'
    COMPLETED      = 'COMPLETED',      'Completed'
    REJECTED       = 'REJECTED',       'Rejected'


class ExecutionMode(models.TextChoices):
    INTERNAL      = 'INTERNAL',      'Internal'
    SUBCONTRACTED = 'SUBCONTRACTED', 'Subcontracted'
    REJECTED      = 'REJECTED',      'Rejected'


class SourceType(models.TextChoices):
    DIRECT_PATIENT       = 'DIRECT_PATIENT',       'Direct Patient'
    PARTNER_ORGANIZATION = 'PARTNER_ORGANIZATION', 'Partner Organization'


class BillingMode(models.TextChoices):
    DIRECT_PAYMENT  = 'DIRECT_PAYMENT',  'Direct Payment'
    PARTNER_BILLING = 'PARTNER_BILLING', 'Partner Billing'


class PriceSource(models.TextChoices):
    DEFAULT_PRICE        = 'DEFAULT_PRICE',        'Default Price'
    PARTNER_AGREED_PRICE = 'PARTNER_AGREED_PRICE', 'Partner Agreed Price'
    PRICING_RULE         = 'PRICING_RULE',         'Pricing Rule'
    MANUAL_OVERRIDE      = 'MANUAL_OVERRIDE',      'Manual Override'


# ---------------------------------------------------------------------------
# AnalysisRequest
# ---------------------------------------------------------------------------

class AnalysisRequest(BaseModel):
    """
    A lab work order: one patient → one or more exam items.

    Lifecycle: DRAFT → CONFIRMED → IN_PROGRESS → COMPLETED
                     └─────────────────────────→ CANCELLED

    request_number is auto-generated by the service after the first save
    (format: REQ-{YYYY}-{8-char UUID prefix}).
    """
    request_number = models.CharField(
        max_length=30,
        unique=True,
        db_index=True,
        blank=True,
        default='',
        help_text='Internal/system identifier (e.g. REQ-2026-A2AF70DE). '
                  'Used for audit logs and backend debugging.',
    )
    # A dedicated patient-facing reference keeps internal identifiers out of
    # printable documents. The two are separate on purpose: the internal
    # ``request_number`` can stay stable even if the public layout changes,
    # and the public reference can be regenerated or reformatted in the
    # future without rewriting audit history.
    public_reference = models.CharField(
        max_length=20,
        unique=True,
        blank=True,
        default='',
        help_text='Clean patient-facing reference (YYYYMMDD-NNNNNN). '
                  'Used on final reports and other external documents.',
    )
    patient = models.ForeignKey(
        'patients.Patient',
        on_delete=models.PROTECT,
        related_name='analysis_requests',
    )
    status = models.CharField(
        max_length=25,
        choices=RequestStatus.choices,
        default=RequestStatus.DRAFT,
        db_index=True,
    )
    notes = models.TextField(blank=True, default='')

    # ---- Source tracking ----
    source_type = models.CharField(
        max_length=25,
        choices=SourceType.choices,
        default=SourceType.DIRECT_PATIENT,
        db_index=True,
        help_text='Where this request originated from.',
    )
    partner_organization = models.ForeignKey(
        'partners.PartnerOrganization',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='analysis_requests',
        help_text='Required when source_type is PARTNER_ORGANIZATION.',
    )
    external_reference = models.CharField(
        max_length=100,
        blank=True,
        default='',
        help_text='Reference number from the partner (e.g. their internal order ID).',
    )
    billing_mode = models.CharField(
        max_length=20,
        choices=BillingMode.choices,
        default=BillingMode.DIRECT_PAYMENT,
        db_index=True,
        help_text='How this request will be billed.',
    )
    source_notes = models.TextField(
        blank=True,
        default='',
        help_text='Free-text notes related to the request source or billing.',
    )

    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='confirmed_requests',
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cancelled_requests',
    )
    final_conclusion = models.TextField(
        blank=True,
        default='',
        help_text='Biologist conclusion included on the final patient report.',
    )
    created_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_analysis_requests',
    )

    # ---- Patient notification tracking ----
    # Lightweight per-request counters so the UI can render "Patient notified
    # by email <when>" badges and warn before re-notifying. Detailed per-attempt
    # records live in AuditLog (entity_type='PatientResultNotification').
    notified_by_email_at = models.DateTimeField(null=True, blank=True)
    notified_by_email_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
        help_text='Operator who sent the most recent successful email notification.',
    )
    notification_count = models.PositiveSmallIntegerField(
        default=0,
        help_text='How many successful patient notifications have been sent across all channels.',
    )
    last_patient_notification_channel = models.CharField(
        max_length=20,
        blank=True, default='',
        help_text='Channel used for the most recent successful notification (e.g. "EMAIL").',
    )

    # ---- Closure lifecycle (orthogonal to workflow ``status``) ----
    # Tracks the post-processing state without touching the medical/operational
    # workflow status. Billing queries continue to filter on ``status`` only,
    # so a request that has been delivered or archived still bills correctly.
    closure_status = models.CharField(
        max_length=15,
        choices=ClosureStatus.choices,
        default=ClosureStatus.OPEN,
        db_index=True,
    )

    # ---- Lifecycle marker stamps (closure transitions) ----
    delivered_at = models.DateTimeField(null=True, blank=True)
    delivered_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )
    archived_at = models.DateTimeField(null=True, blank=True)
    archived_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='+',
    )

    class Meta:
        verbose_name = 'Analysis Request'
        verbose_name_plural = 'Analysis Requests'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['patient', 'status']),
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['source_type', 'partner_organization']),
        ]

    def __str__(self):
        return f'{self.request_number} — {self.patient} [{self.status}]'

    def delete(self, *args, **kwargs):
        """Analysis requests are medical records and cannot be hard-deleted."""
        raise PermissionError(
            'Analysis requests cannot be deleted. Cancel instead.'
        )


# ---------------------------------------------------------------------------
# AnalysisRequestItem
# ---------------------------------------------------------------------------

class AnalysisRequestItem(BaseModel):
    """
    One exam line within an analysis request.

    Pricing is resolved at item creation time:
    - ``unit_price`` is snapshotted from the exam definition's reference price.
    - ``billed_price`` is computed from an applicable pricing rule, or defaults
      to ``unit_price``, or can be overridden manually.
    - ``price_source`` records how the billed price was determined.

    Items with execution_mode=REJECTED receive zero prices.

    Note: although SUBCONTRACTED is a valid execution_mode for record-keeping,
    there is no active inter-laboratory workflow — all processing is internal.
    """
    analysis_request = models.ForeignKey(
        AnalysisRequest,
        on_delete=models.CASCADE,
        related_name='items',
    )
    exam_definition = models.ForeignKey(
        'catalog.ExamDefinition',
        on_delete=models.PROTECT,
        related_name='request_items',
    )
    status = models.CharField(
        max_length=20,
        choices=ItemStatus.choices,
        default=ItemStatus.PENDING,
        db_index=True,
    )
    execution_mode = models.CharField(
        max_length=20,
        choices=ExecutionMode.choices,
        default=ExecutionMode.INTERNAL,
        db_index=True,
    )
    rejection_reason = models.TextField(blank=True, default='')
    external_partner_name = models.CharField(max_length=255, blank=True, default='')
    notes = models.TextField(blank=True, default='')

    # Pricing — set at item creation, frozen at confirmation
    unit_price = models.DecimalField(
        max_digits=12, decimal_places=4, default=0,
        help_text='Reference price snapshotted from ExamDefinition.unit_price at item creation.',
    )
    billed_price = models.DecimalField(
        max_digits=12, decimal_places=4, default=0,
        help_text='Actual price charged. May differ from unit_price due to rule or manual override.',
    )
    pricing_rule = models.ForeignKey(
        'catalog.PricingRule',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='item_snapshots',
        help_text='The pricing rule that was applied, if any. Kept for traceability.',
    )
    price_source = models.CharField(
        max_length=20,
        choices=PriceSource.choices,
        default=PriceSource.DEFAULT_PRICE,
        help_text='How the billed price was determined.',
    )

    # Specimen collection — populated when a technician marks the item
    # as collected. These fields live on the item (not the traceability
    # row) because collection is a per-item operational milestone and
    # the traceability model is reserved for sample receipt / analysis
    # completion timestamps. The request-level status is derived from
    # these values rather than stored redundantly.
    collected_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text='When the specimen for this item was collected.',
    )
    collected_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='collected_items',
        help_text='Staff user who marked the item as collected.',
    )
    collection_notes = models.TextField(
        blank=True,
        default='',
        help_text='Optional operational notes captured at collection time.',
    )

    class Meta:
        verbose_name = 'Analysis Request Item'
        verbose_name_plural = 'Analysis Request Items'
        ordering = ['created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['analysis_request', 'exam_definition'],
                name='unique_exam_per_request',
            )
        ]

    def __str__(self):
        return (
            f'{self.analysis_request.request_number} / '
            f'{self.exam_definition.code} [{self.status}]'
        )

    def delete(self, *args, **kwargs):
        """Hard delete blocked. Service layer uses queryset.delete() for DRAFT removal."""
        raise PermissionError(
            'Analysis request items cannot be hard-deleted directly. '
            'Remove them from the draft request via the service layer.'
        )


# ---------------------------------------------------------------------------
# ExamTraceability
# ---------------------------------------------------------------------------

class ExamTraceability(models.Model):
    """
    Mandatory per-item traceability record.

    Created in the same transaction as the AnalysisRequestItem (CLAUDE.md
    constraint). All timestamps are NULL until the corresponding processing
    step occurs. Provides a complete audit trail for each exam performed.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    item = models.OneToOneField(
        AnalysisRequestItem,
        on_delete=models.CASCADE,
        related_name='traceability',
    )

    # Populated when item transitions PENDING → IN_PROGRESS
    sample_received_at = models.DateTimeField(null=True, blank=True)
    sample_received_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='received_samples',
    )

    # Populated when item transitions IN_PROGRESS → COMPLETED
    analysis_completed_at = models.DateTimeField(null=True, blank=True)
    performed_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='performed_analyses',
    )

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = 'Exam Traceability'
        verbose_name_plural = 'Exam Traceability Records'

    def __str__(self):
        return f'Traceability for {self.item}'


# ---------------------------------------------------------------------------
# Request Labels
# ---------------------------------------------------------------------------

class RequestLabelBatch(BaseModel):
    """
    One-per-request printable label generation batch.

    Current lifecycle rule: **generate once and reuse.** The
    ``OneToOneField`` to ``AnalysisRequest`` enforces this at the DB
    level — once a batch exists for a request, calling the label
    generation endpoint returns the same batch verbatim. This is
    deliberately the safest professional default: once physical labels
    have been printed and stuck onto specimen tubes, producing new
    barcodes would open a traceability gap. A future "force regenerate"
    capability can be added as a distinct action when the operational
    need is real, with its own audit trail.

    Hard delete is blocked — label batches are traceability records.
    """
    analysis_request = models.OneToOneField(
        AnalysisRequest,
        on_delete=models.PROTECT,
        related_name='label_batch',
    )
    generated_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        related_name='generated_label_batches',
    )
    generated_at = models.DateTimeField(default=timezone.now, db_index=True)
    label_count = models.PositiveSmallIntegerField(
        help_text='Total labels in this batch (distinct families + fixed extras).',
    )
    family_count = models.PositiveSmallIntegerField(
        help_text='Distinct exam families counted at generation time (audit).',
    )
    pdf_file_key = models.CharField(
        max_length=500,
        blank=True,
        default='',
        help_text='Internal storage key for the rendered PDF. Never exposed to clients as-is.',
    )

    class Meta:
        verbose_name = 'Request Label Batch'
        verbose_name_plural = 'Request Label Batches'
        ordering = ['-generated_at']

    def __str__(self):
        return f'{self.analysis_request.request_number} \u2192 {self.label_count} labels'

    def delete(self, *args, **kwargs):
        raise PermissionError(
            'Request label batches cannot be deleted — they are traceability records.'
        )


class RequestLabel(BaseModel):
    """
    One label row inside a batch.

    ``barcode_value`` is the canonical, **system-wide unique** identifier
    printed on the physical label and scanned throughout downstream
    workflow — the unique + indexed constraint means any scan in any
    module can resolve back in O(1) to the label, the batch, and the
    parent analysis request, preserving full traceability.

    Hard delete is blocked — labels are traceability records.
    """
    batch = models.ForeignKey(
        RequestLabelBatch,
        on_delete=models.CASCADE,
        related_name='labels',
    )
    barcode_value = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text='System-wide unique identifier printed on the label and scanned in workflow.',
    )
    label_index = models.PositiveSmallIntegerField(
        help_text='1-based position of this label within the batch.',
    )
    family_name = models.CharField(
        max_length=150,
        blank=True,
        default='',
        help_text='Exam family this label is pinned to; empty for the fixed extras.',
    )

    class Meta:
        verbose_name = 'Request Label'
        verbose_name_plural = 'Request Labels'
        ordering = ['batch', 'label_index']
        constraints = [
            models.UniqueConstraint(
                fields=['batch', 'label_index'],
                name='unique_label_index_per_batch',
            ),
        ]
        indexes = [
            models.Index(fields=['batch', 'label_index']),
        ]

    def __str__(self):
        return f'{self.barcode_value} ({self.label_index}/{self.batch.label_count})'

    def delete(self, *args, **kwargs):
        raise PermissionError(
            'Request labels cannot be deleted — they are traceability records.'
        )


class RequestReferenceSequence(models.Model):
    """
    Per-tenant daily counter used by the public_reference allocator.

    One row per calendar ``date`` within this tenant's schema. Overlapping
    creates serialise on the row via ``select_for_update`` inside the
    allocator's ``transaction.atomic`` block. The resulting reference is
    composed as ``YYYYMMDD-<6-digit-sequence>``.
    """
    date = models.DateField(unique=True)
    last_value = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Request Reference Sequence'
        verbose_name_plural = 'Request Reference Sequences'

    def __str__(self):
        return f'RequestReferenceSequence({self.date.isoformat()} @ {self.last_value})'


class LabelSequence(models.Model):
    """
    Per-tenant monthly counter used by the numeric label code allocator.

    One row per ``(year, month)`` within this tenant's schema. Because
    the table lives in the tenant schema, the ``(year, month)`` unique
    constraint is already tenant-scoped — no ``tenant_code`` column is
    required on the row itself. The allocator composes the final label
    code by prefixing the tenant's public-schema numeric_code.

    Access path for concurrency safety: callers wrap ``get_or_create``
    + ``select_for_update`` + increment in a single ``transaction.atomic``
    block so overlapping label generations serialise on this row.
    """
    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField()
    last_value = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Label Sequence'
        verbose_name_plural = 'Label Sequences'
        constraints = [
            models.UniqueConstraint(
                fields=['year', 'month'],
                name='unique_label_sequence_year_month',
            ),
        ]
        indexes = [
            models.Index(fields=['year', 'month']),
        ]

    def __str__(self):
        return f'LabelSequence({self.year:04d}-{self.month:02d} @ {self.last_value})'


# ---------------------------------------------------------------------------
# Patient result access tokens
# ---------------------------------------------------------------------------

class ResultAccessToken(BaseModel):
    """
    A short-lived, non-guessable token that grants a patient (or their
    delegate) access to download a specific result PDF without
    authentication.

    Security model:
    - Token is a 64-char hex string (256 bits of entropy).
    - Expires after a configurable duration (default 48h).
    - ``is_active`` can be revoked manually before expiry.
    - The token resolves to a specific report version's ``pdf_file_key``
      — the file is streamed via ``FileResponse``, never exposed as a
      raw URL.
    - Tokens are tenant-scoped: the access endpoint sets the search
      path from the token's tenant context.
    """
    token = models.CharField(
        max_length=64, unique=True, db_index=True, editable=False,
    )
    analysis_request = models.ForeignKey(
        AnalysisRequest,
        on_delete=models.CASCADE,
        related_name='access_tokens',
    )
    patient = models.ForeignKey(
        'patients.Patient',
        on_delete=models.CASCADE,
        related_name='result_access_tokens',
    )
    report_file_key = models.CharField(
        max_length=500,
        help_text='Snapshot of the pdf_file_key at token creation time.',
    )
    expires_at = models.DateTimeField(db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Result Access Token'
        verbose_name_plural = 'Result Access Tokens'
        ordering = ['-created_at']

    def __str__(self):
        return f'Token for {self.analysis_request} (expires {self.expires_at})'

    @property
    def is_valid(self) -> bool:
        return self.is_active and self.expires_at > timezone.now()

    @property
    def is_locked(self) -> bool:
        return self.locked_until is not None and self.locked_until > timezone.now()


# ---------------------------------------------------------------------------
# Final patient report
# ---------------------------------------------------------------------------

class AnalysisRequestReport(BaseModel):
    """
    One generated version of the final patient report for an analysis
    request. A request accumulates one or more versions over time —
    the first is produced by ``generate_or_get``, each subsequent one
    by an explicit ``regenerate`` action.

    Invariants (enforced by DB constraints):
    - ``version_number`` is unique within a request.
    - At most one row per request has ``is_current=True``.

    The "current" version is what the download endpoint streams and
    what the detail payload exposes to the UI. Older versions are
    retained untouched for traceability and are never deleted.

    Security: ``pdf_file_key`` is an internal storage key, never
    exposed to clients. Downloads are streamed through the
    authenticated ``report/download/`` endpoint only.
    """
    analysis_request = models.ForeignKey(
        AnalysisRequest,
        on_delete=models.PROTECT,
        related_name='reports',
    )
    version_number = models.PositiveIntegerField(
        help_text='1-indexed version number within this request. '
                  'Increments on every regenerate action.',
    )
    is_current = models.BooleanField(
        default=True,
        db_index=True,
        help_text='True for the version that download and UI use. '
                  'Exactly one current version per request.',
    )
    generated_by = models.ForeignKey(
        'users.StaffUser',
        on_delete=models.SET_NULL,
        null=True,
        related_name='generated_reports',
    )
    generated_at = models.DateTimeField(default=timezone.now, db_index=True)
    pdf_file_key = models.CharField(
        max_length=500,
        blank=True,
        default='',
        help_text='Internal storage key for the rendered PDF. Never exposed to clients.',
    )

    class Meta:
        verbose_name = 'Analysis Request Report'
        verbose_name_plural = 'Analysis Request Reports'
        ordering = ['-generated_at']
        constraints = [
            models.UniqueConstraint(
                fields=['analysis_request', 'version_number'],
                name='unique_report_version_per_request',
            ),
            models.UniqueConstraint(
                fields=['analysis_request'],
                condition=models.Q(is_current=True),
                name='unique_current_report_per_request',
            ),
        ]
        indexes = [
            models.Index(fields=['analysis_request', '-version_number']),
        ]

    def __str__(self):
        return (
            f'Report v{self.version_number} for '
            f'{self.analysis_request.request_number}'
        )

    def delete(self, *args, **kwargs):
        raise PermissionError(
            'Request reports cannot be deleted — they are medical records.'
        )
