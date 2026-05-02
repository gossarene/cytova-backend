"""
Cytova — Analysis Request Views

AnalysisRequestViewSet
    list, retrieve, create, partial_update, confirm, cancel

AnalysisRequestItemViewSet  (nested under requests)
    list, retrieve, create (add item), partial_update (update metadata),
    destroy (remove from draft), start, complete, reject
"""
import logging

from django.core.files.storage import default_storage
from django.http import FileResponse
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.viewsets import GenericViewSet

from common.permissions import (
    IsAnyStaff,
    IsBiologistOrAbove,
    IsLabAdmin,
    IsReceptionistOrLabAdmin,
    IsTechnicianOrAbove,
)

from apps.audit.models import ActorType, AuditAction, AuditLog
from .filters import AnalysisRequestFilter, AnalysisRequestItemFilter
from .models import AnalysisRequest, AnalysisRequestItem, AnalysisRequestReport
from .serializers import (
    AnalysisRequestCreateSerializer,
    AnalysisRequestDetailSerializer,
    AnalysisRequestItemCreateSerializer,
    AnalysisRequestItemSerializer,
    AnalysisRequestItemUpdateSerializer,
    AnalysisRequestListSerializer,
    AnalysisRequestUpdateSerializer,
    ItemMarkCollectedSerializer,
    ItemRejectSerializer,
    PricingPreviewRequestSerializer,
    RequestLabelBatchSerializer,
    ResolvedItemPriceSerializer,
)
from .services import AnalysisRequestItemService, AnalysisRequestService
from .label_service import RequestLabelService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class NotifyCytovaThrottle(SimpleRateThrottle):
    """Per-user throttle for ``POST .../notify-cytova/``. Reads its
    rate from ``DEFAULT_THROTTLE_RATES['notify_cytova']``. A small
    dedicated subclass keeps the per-action throttle declaration
    declarative — DRF's ``ScopedRateThrottle`` resolves its scope
    from a view-level attribute that ``@action`` cannot supply.
    """
    scope = 'notify_cytova'

    def get_cache_key(self, request, view):
        ident = (
            str(request.user.pk) if request.user.is_authenticated
            else self.get_ident(request)
        )
        return self.cache_format % {'scope': self.scope, 'ident': ident}


def _get_request_or_404(pk) -> AnalysisRequest:
    try:
        return AnalysisRequest.objects.get(pk=pk)
    except AnalysisRequest.DoesNotExist:
        raise NotFound('Analysis request not found.')


def _serialize_report(ar: AnalysisRequest, report) -> dict:
    """Shared envelope for report endpoints (generate / regenerate / GET)."""
    return {
        'id': str(report.id),
        'version_number': report.version_number,
        'is_current': report.is_current,
        'generated_at': report.generated_at.isoformat(),
        'generated_by_email': report.generated_by.email if report.generated_by else None,
        'pdf_url': f'/requests/{ar.id}/report/download/',
    }


def _serialize_access_token(token, request) -> dict:
    from django.conf import settings as dj_settings
    from django.utils import timezone as tz
    fe_base = getattr(dj_settings, 'CYTOVA_FRONTEND_BASE_URL', '')
    if not fe_base:
        scheme = 'https' if request.is_secure() else 'http'
        fe_base = f'{scheme}://{request.get_host()}'
    scheme = 'https' if request.is_secure() else 'http'
    api_base = f'{scheme}://{request.get_host()}'
    expired = token.expires_at <= tz.now()
    return {
        'status': 'expired' if expired else ('active' if token.is_active else 'revoked'),
        'token': token.token,
        'expires_at': token.expires_at.isoformat(),
        'access_url': f'{fe_base}/results/access/{token.token}',
        'download_url': f'{api_base}/r/{token.token}/download/',
    }


def _get_item_or_404(request_pk, pk) -> AnalysisRequestItem:
    try:
        return (
            AnalysisRequestItem.objects
            .select_related('analysis_request', 'traceability')
            .get(pk=pk, analysis_request_id=request_pk)
        )
    except AnalysisRequestItem.DoesNotExist:
        raise NotFound('Analysis request item not found.')


# ---------------------------------------------------------------------------
# AnalysisRequestViewSet
# ---------------------------------------------------------------------------

class AnalysisRequestViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = AnalysisRequestFilter
    search_fields = ['request_number', 'patient__first_name', 'patient__last_name',
                     'patient__document_number', 'external_reference',
                     'partner_organization__code', 'partner_organization__name']
    ordering_fields = ['created_at', 'status', 'request_number']
    ordering = ['-created_at']

    def get_queryset(self):
        from django.db.models import Prefetch
        from apps.requests.models import AnalysisRequestItem

        # Prefetch items WITH their nested relations to avoid N+1 queries
        # when the serializer accesses item.exam_definition.code/name.
        items_qs = (
            AnalysisRequestItem.objects
            .select_related('exam_definition', 'pricing_rule')
            .order_by('created_at')
        )
        return (
            AnalysisRequest.objects
            .select_related(
                'patient', 'partner_organization',
                'created_by', 'confirmed_by', 'cancelled_by',
            )
            .prefetch_related(Prefetch('items', queryset=items_qs))
        )

    def filter_queryset(self, queryset):
        """Skip the FilterSet for non-list actions.

        ``AnalysisRequestFilter`` excludes DELIVERED + ARCHIVED rows by
        default to keep the front-desk list focused on active work. Without
        this guard, that exclusion also fires on ``retrieve`` (and any
        other action that goes through ``get_object()``) — which would
        return 404 the moment a request transitions to ARCHIVED, even
        though the row exists and the user has explicit access to it
        (just clicked "Archive" or opened the link from search/filters).

        Detail actions on this ViewSet that don't go through
        ``get_object()`` (the ones using ``_get_request_or_404`` directly:
        archive, mark_delivered, notify_patient, etc.) are unaffected
        either way — but routing them through the unfiltered queryset
        when they DO hit get_object() keeps the rule consistent.
        """
        if self.action != 'list':
            return queryset
        return super().filter_queryset(queryset)

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAnyStaff()]
        if self.action == 'cancel':
            return [IsLabAdmin()]
        if self.action == 'finalize_validation':
            return [IsBiologistOrAbove()]
        if self.action == 'mark_delivered':
            # Same gate as notify_patient — both are patient-comms surfaces.
            return [IsReceptionistOrLabAdmin()]
        if self.action == 'archive':
            # Archival is an oversight action, not part of daily operations.
            return [IsLabAdmin()]
        if self.action == 'labels':
            # GET can be read by any staff (so viewers can see metadata
            # and the signed download URL). POST (generation) is gated
            # at the same level as ``confirm`` because producing labels
            # is part of the post-confirmation reception workflow.
            if self.request.method == 'POST':
                return [IsReceptionistOrLabAdmin()]
            return [IsAnyStaff()]
        if self.action == 'labels_download':
            # Protected PDF download endpoint — any authenticated staff
            # within the tenant can read the sensitive document. Tenant
            # isolation is already enforced by CytovaTenantMiddleware,
            # so a cross-tenant caller cannot even resolve to the right
            # schema. Unauthenticated callers hit DRF's 401 path before
            # reaching the view.
            return [IsAnyStaff()]
        if self.action == 'report_generate':
            # Generating the final patient report is a biologist-level
            # responsibility — the same user who finalizes the request.
            return [IsBiologistOrAbove()]
        if self.action in (
            'report_download', 'report_versions', 'report_version_download',
            'report_history',
        ):
            return [IsAnyStaff()]
        if self.action in ('create_access_token', 'regenerate_access_token'):
            return [IsAnyStaff()]
        if self.action == 'notify_cytova':
            # Sharing a result with a global patient portal account is
            # a patient-comms action — but unlike ``notify_patient``
            # (email blast) the lab user doing it might be the
            # technician who just finished the run. Per spec, gate at
            # technician-or-above so receptionists, techs, biologists,
            # and lab admins can all share; viewers and inventory
            # managers cannot.
            return [IsTechnicianOrAbove()]
        if self.action == 'cytova_share_status':
            # Read-only badge for the request detail page — any
            # authenticated staff who can already see the request can
            # see whether it has been shared with the patient portal.
            return [IsAnyStaff()]
        if self.action == 'revoke_cytova_share':
            # Revocation is a stricter gate than sharing: it removes a
            # patient's existing access. Restrict to receptionist + lab
            # admin (same as ``notify_patient``) so a technician who
            # could share it can't unilaterally yank it back.
            return [IsReceptionistOrLabAdmin()]
        if self.action == 'reopen_result':
            # Walking an issued result back to VALIDATED is the most
            # consequential action on the request — the new report
            # version will overwrite what the patient currently sees.
            # Restrict to biologists + lab admins.
            return [IsBiologistOrAbove()]
        if self.action == 'notify_patient':
            # Patient-facing communication is a front-desk responsibility
            # (receptionists handle patient outreach) plus lab admins for
            # oversight. Same gate as ``confirm`` and ``create``, which are
            # the other patient-touching operations on this ViewSet.
            #
            # Tightened from IsAnyStaff: technicians and inventory managers
            # should not be able to email a patient on behalf of the lab,
            # even though they can generate a secure link for internal use.
            #
            # TODO(rbac): introduce a fine-grained ``requests.notify_patient``
            # permission in common/permissions_registry.py and switch to
            # ``RequiresPermission`` once the dashboard exposes per-role
            # editing for patient-communication permissions.
            return [IsReceptionistOrLabAdmin()]
        # create, partial_update, confirm, preview_pricing
        return [IsReceptionistOrLabAdmin()]

    def get_serializer_class(self):
        if self.action == 'list':
            return AnalysisRequestListSerializer
        return AnalysisRequestDetailSerializer

    def create(self, request, *args, **kwargs):
        serializer = AnalysisRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated = dict(serializer.validated_data)
        # ``confirm`` drives whether the new request should be immediately
        # transitioned to CONFIRMED (used by the 3-step creation wizard).
        # Pop it off the payload so it does not reach ``AnalysisRequest``
        # as an unknown kwarg.
        confirm_after = bool(validated.pop('confirm', False))
        ar = AnalysisRequestService.create(
            validated_data=validated,
            created_by=request.user,
            request=request,
            confirm_after=confirm_after,
        )
        ar = (
            AnalysisRequest.objects
            .select_related(
                'patient', 'partner_organization',
                'created_by', 'confirmed_by', 'cancelled_by',
            )
            .prefetch_related('items')
            .get(id=ar.id)
        )
        return Response(
            AnalysisRequestDetailSerializer(ar).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        ar = _get_request_or_404(kwargs['pk'])
        serializer = AnalysisRequestUpdateSerializer(
            data=request.data,
            context={'instance': ar},
        )
        serializer.is_valid(raise_exception=True)
        ar = AnalysisRequestService.update(
            analysis_request=ar,
            validated_data=serializer.validated_data,
            updated_by=request.user,
            request=request,
        )
        return Response(AnalysisRequestDetailSerializer(ar).data)

    @action(detail=True, methods=['post'], url_path='confirm')
    def confirm(self, request, pk=None):
        ar = _get_request_or_404(pk)
        ar = AnalysisRequestService.confirm(
            analysis_request=ar,
            confirmed_by=request.user,
            request=request,
        )
        ar = (
            AnalysisRequest.objects
            .select_related(
                'patient', 'partner_organization',
                'created_by', 'confirmed_by', 'cancelled_by',
            )
            .prefetch_related('items')
            .get(id=ar.id)
        )
        return Response(AnalysisRequestDetailSerializer(ar).data)

    @action(detail=True, methods=['post'], url_path='cancel')
    def cancel(self, request, pk=None):
        ar = _get_request_or_404(pk)
        ar = AnalysisRequestService.cancel(
            analysis_request=ar,
            cancelled_by=request.user,
            request=request,
        )
        return Response(AnalysisRequestDetailSerializer(ar).data)

    @action(detail=True, methods=['post'], url_path='finalize-validation')
    def finalize_validation(self, request, pk=None):
        ar = _get_request_or_404(pk)
        ar = AnalysisRequestService.finalize_validation(
            analysis_request=ar,
            finalized_by=request.user,
            request=request,
        )
        return Response(AnalysisRequestDetailSerializer(ar).data)

    @action(detail=True, methods=['post'], url_path='mark-delivered')
    def mark_delivered(self, request, pk=None):
        """Manually mark a request as DELIVERED.

        Useful when the patient was reached through a non-tracked channel
        (printed copy, in-person handover, WhatsApp manual share). The
        notification flow auto-promotes VALIDATED → DELIVERED on email
        success — this endpoint is for everything else.
        """
        from .services import AnalysisRequestService
        ar = _get_request_or_404(pk)
        ar = AnalysisRequestService.mark_delivered(
            analysis_request=ar,
            actor=request.user,
            request=request,
        )
        return Response(AnalysisRequestDetailSerializer(ar).data)

    @action(detail=True, methods=['post'], url_path='archive')
    def archive(self, request, pk=None):
        """Archive a request — hides it from the default list view.
        Permitted from terminal states (DELIVERED / COMPLETED / CANCELLED /
        VALIDATED); the state machine guards illegal transitions."""
        from .services import AnalysisRequestService
        ar = _get_request_or_404(pk)
        ar = AnalysisRequestService.archive(
            analysis_request=ar,
            actor=request.user,
            request=request,
        )
        return Response(AnalysisRequestDetailSerializer(ar).data)

    @action(detail=True, methods=['get', 'post'], url_path='labels')
    def labels(self, request, pk=None):
        """
        Label batch endpoint — nested under a request.

        GET
            Return the existing label batch metadata + the protected
            download path for the PDF. 404 if the request has not had
            labels generated yet.

        POST
            Idempotent generate-or-get. If a batch already exists, it
            is returned verbatim (no new barcodes, no new PDF, no new
            audit). Otherwise a new batch is created, labels are
            allocated, the PDF is rendered and stored, and one CREATE
            audit entry is written.
        """
        ar = _get_request_or_404(pk)
        if request.method == 'POST':
            batch = RequestLabelService.generate_or_get(
                analysis_request=ar,
                generated_by=request.user,
                request=request,
            )
            return Response(RequestLabelBatchSerializer(batch).data)

        # GET
        batch = getattr(ar, 'label_batch', None)
        if batch is None:
            return Response(
                {'detail': 'No labels have been generated for this request yet.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(RequestLabelBatchSerializer(batch).data)

    @action(detail=True, methods=['get'], url_path='labels/download')
    def labels_download(self, request, pk=None):
        """
        Protected PDF download for a request's generated labels.

        Every byte flows through this authenticated endpoint — the raw
        storage URL (``/media/...`` in dev, or a direct S3 URL in
        production) is never handed to clients. A user who tries to
        guess the media path sees a 404 in production (no direct
        public access) and a JWT-less 401 here. Because the tenant is
        resolved by ``CytovaTenantMiddleware`` on every request, a
        caller with a valid JWT for tenant A cannot reach tenant B's
        labels: the schema switch happens before the view even runs.

        Always streams via ``FileResponse`` rather than redirecting to
        a pre-signed storage URL. The tiny bandwidth cost buys
        continuous access control — a copied link never works because
        the backend mediates every download. For small label PDFs this
        is the right tradeoff.
        """
        from .models import ItemStatus
        from apps.users.models import Role

        ar = _get_request_or_404(pk)
        batch = getattr(ar, 'label_batch', None)
        if batch is None or not batch.pdf_file_key:
            raise NotFound('No labels have been generated for this request yet.')

        is_admin = getattr(request.user, 'role', None) == Role.LAB_ADMIN
        if not is_admin:
            active_items = ar.items.exclude(status=ItemStatus.REJECTED)
            all_collected = active_items.exists() and not active_items.filter(
                status=ItemStatus.PENDING,
            ).exists()
            if all_collected:
                from rest_framework.exceptions import PermissionDenied
                raise PermissionDenied(
                    'Labels cannot be downloaded after all specimens are collected.'
                )

        file_obj = default_storage.open(batch.pdf_file_key, 'rb')
        return FileResponse(
            file_obj,
            content_type='application/pdf',
            as_attachment=True,
            filename=f'labels_{ar.request_number}.pdf',
        )

    @action(detail=True, methods=['get', 'post'], url_path='report')
    def report(self, request, pk=None):
        """
        GET  — return current report metadata if present, 404 otherwise.
        POST — generate version 1 if none exists, idempotent thereafter.
        """
        from .report_service import RequestReportService
        ar = _get_request_or_404(pk)

        if request.method == 'GET':
            current = RequestReportService.get_current(ar)
            if current is None:
                raise NotFound('No report has been generated for this request yet.')
            return Response(_serialize_report(ar, current))

        report = RequestReportService.generate_or_get(
            analysis_request=ar,
            generated_by=request.user,
            request=request,
        )
        return Response(_serialize_report(ar, report))

    @action(detail=True, methods=['post'], url_path='report/regenerate')
    def report_regenerate(self, request, pk=None):
        """
        Create the next report version and switch the "current" pointer.

        Requires a VALIDATED request and at least one previous version.
        Historical versions are preserved untouched. An explicit audit
        entry distinguishes this from the initial generation.
        """
        from .report_service import RequestReportService
        ar = _get_request_or_404(pk)
        report = RequestReportService.regenerate(
            analysis_request=ar,
            generated_by=request.user,
            request=request,
        )
        return Response(_serialize_report(ar, report))

    @action(detail=True, methods=['get'], url_path='report/versions')
    def report_versions(self, request, pk=None):
        """
        List every report version generated for this request, newest
        version first. Exposed so the UI can render a history panel
        and let operators download any past version.
        """
        ar = _get_request_or_404(pk)
        versions = (
            AnalysisRequestReport.objects
            .filter(analysis_request=ar)
            .select_related('generated_by')
            .order_by('-version_number')
        )
        return Response({
            'results': [
                {
                    'id': str(v.id),
                    'version_number': v.version_number,
                    'is_current': v.is_current,
                    'generated_at': v.generated_at.isoformat(),
                    'generated_by_email': (
                        v.generated_by.email if v.generated_by else None
                    ),
                    'downloadable': bool(v.pdf_file_key),
                    'pdf_url': (
                        f'/requests/{ar.id}/report/versions/{v.id}/download/'
                    ),
                }
                for v in versions
            ],
        })

    @action(
        detail=True, methods=['get'],
        url_path=r'report/versions/(?P<report_id>[0-9a-f-]+)/download',
    )
    def report_version_download(self, request, pk=None, report_id=None):
        """
        Stream a specific (historical or current) report version.

        Security:
        - The version must belong to the request in the URL — a stray
          report_id from another request resolves to 404, never leaks.
        - Tenant isolation is enforced by middleware: a record created in
          another tenant's schema is invisible to this query.
        - Raw storage keys are never exposed; the file is streamed via
          ``FileResponse`` through the authenticated endpoint only.
        """
        ar = _get_request_or_404(pk)
        try:
            report = AnalysisRequestReport.objects.get(
                pk=report_id, analysis_request=ar,
            )
        except AnalysisRequestReport.DoesNotExist:
            raise NotFound('Report version not found for this request.')

        if not report.pdf_file_key:
            raise NotFound('Report version has no stored PDF.')

        file_obj = default_storage.open(report.pdf_file_key, 'rb')
        return FileResponse(
            file_obj,
            content_type='application/pdf',
            as_attachment=True,
            filename=f'report_{ar.public_reference or ar.request_number}_v{report.version_number}.pdf',
        )

    @action(detail=True, methods=['get'], url_path='report-history')
    def report_history(self, request, pk=None):
        """``GET /requests/{id}/report-history/`` — staff-only
        traceability that joins the lab-internal ``AnalysisRequestReport``
        version line with the patient-portal ``PatientSharedResult``
        rows that referenced each version. Lets the lab UI render a
        single panel answering: "for this request, what versions did we
        generate, which were shared with the patient, through which
        channel, and when?"

        Distinct from ``report/versions`` (which is purely the lab
        version list) by adding the patient-side cross-reference, the
        issuance lifecycle snapshot, and a ``channels_used`` aggregate.

        Cross-schema lookup
        -------------------
        Lab versions live in the tenant schema (resolved by
        ``CytovaTenantMiddleware``). Patient share rows live in the
        public schema. The join is performed in two queries — never via
        a cross-schema FK — and is scoped on
        ``(source_tenant_schema, source_request_id)`` so a UUID
        collision across two tenants could never bleed history across
        labs.

        Privacy contract
        ----------------
        Internal storage paths (``pdf_file_key``, ``storage_key``,
        ``patient_storage_key``) are never serialised. The download
        path returned per version reuses the existing authenticated
        ``report/versions/{id}/download`` endpoint — same access-control
        surface as ``report_versions``.

        Patient-side history is intentionally NOT filtered to ACTIVE
        rows — staff should be able to see the full lifecycle including
        revoked / hidden shares. The patient-portal view (Phase 2) hides
        those; the lab view does not.
        """
        from django.db import connection as _conn
        from apps.patient_portal.models import PatientSharedResult

        ar = _get_request_or_404(pk)

        lab_versions = list(
            AnalysisRequestReport.objects
            .filter(analysis_request=ar)
            .select_related('generated_by')
            .order_by('-version_number')
        )

        # Pull every share row this request ever had — including
        # revoked + hidden — so the lab traceability surface mirrors
        # what actually happened. The Phase-2 patient-portal endpoint
        # filters those out; the lab one must not.
        tenant_schema = getattr(_conn, 'schema_name', '') or ''
        share_events = list(
            PatientSharedResult.objects
            .filter(
                source_tenant_schema=tenant_schema,
                source_request_id=ar.id,
            )
            .order_by('-shared_at', '-created_at')
        )

        # Bucket share events by the lab version_number they referenced.
        # Pre-Phase-1 share rows may carry ``report_version_number=None``
        # — those land in an "unversioned" bucket, surfaced separately
        # so they aren't silently dropped.
        by_version: dict[int, list] = {}
        unversioned_shares: list = []
        for evt in share_events:
            if evt.report_version_number is None:
                unversioned_shares.append(evt)
            else:
                by_version.setdefault(evt.report_version_number, []).append(evt)

        def _serialize_share_event(evt) -> dict:
            return {
                'shared_result_id': str(evt.id),
                'shared_at': (
                    evt.shared_at.isoformat() if evt.shared_at
                    else evt.created_at.isoformat()
                ),
                'shared_channel': evt.shared_channel or '',
                'share_status': evt.status,
                'is_current_for_patient': evt.is_current_for_patient,
                'patient_account_id': str(evt.patient_account_id),
            }

        return Response({
            'data': {
                'request_id': str(ar.id),
                'request_number': ar.request_number,
                'request_status': ar.status,
                'issued_at': ar.issued_at.isoformat() if ar.issued_at else None,
                'issued_by_email': (
                    ar.issued_by.email if ar.issued_by_id else None
                ),
                'reopened_at': (
                    ar.reopened_at.isoformat() if ar.reopened_at else None
                ),
                'reopened_by_email': (
                    ar.reopened_by.email if ar.reopened_by_id else None
                ),
                'reopen_reason': ar.reopen_reason or '',
                'lab_versions': [
                    {
                        'id': str(v.id),
                        'version_number': v.version_number,
                        'is_current': v.is_current,
                        'generated_at': v.generated_at.isoformat(),
                        'generated_by_email': (
                            v.generated_by.email if v.generated_by else None
                        ),
                        'downloadable': bool(v.pdf_file_key),
                        'pdf_url': (
                            f'/api/v1/requests/{ar.id}/report/versions/'
                            f'{v.id}/download/'
                        ),
                        'shared_with_patient': [
                            _serialize_share_event(evt)
                            for evt in by_version.get(v.version_number, [])
                        ],
                    }
                    for v in lab_versions
                ],
                'unversioned_shares': [
                    _serialize_share_event(evt) for evt in unversioned_shares
                ],
                # Distinct channel set across patient-facing shares.
                # Sorted for stable response shape — frontend renders a
                # comma-separated badge from this without re-deriving.
                'channels_used': sorted({
                    evt.shared_channel for evt in share_events
                    if evt.shared_channel
                }),
            },
            'meta': None,
            'errors': [],
        })

    @action(detail=True, methods=['get'], url_path='report/download')
    def report_download(self, request, pk=None):
        """
        Stream the CURRENT report PDF version.

        Access is gated by the same tenant isolation and authentication
        used by labels_download. The raw storage key is never exposed —
        every byte flows through this authenticated endpoint.
        """
        from .report_service import RequestReportService
        ar = _get_request_or_404(pk)
        report = RequestReportService.get_current(ar)
        if report is None or not report.pdf_file_key:
            raise NotFound('No report has been generated for this request yet.')

        file_obj = default_storage.open(report.pdf_file_key, 'rb')
        return FileResponse(
            file_obj,
            content_type='application/pdf',
            as_attachment=True,
            filename=f'report_{ar.public_reference or ar.request_number}_v{report.version_number}.pdf',
        )

    @action(detail=True, methods=['get', 'post'], url_path='access-token')
    def create_access_token(self, request, pk=None):
        """
        GET  — return the current active token state (or null).
        POST — get-or-create a token (idempotent).

        On the first POST that actually creates a token (no active
        token previously), the request transitions to RESULT_ISSUED.
        Subsequent POSTs that just return the existing active token
        do NOT re-trigger issuance; they're idempotent reads.
        """
        from .patient_access import ResultAccessService
        from .issuance import CHANNEL_SHARE_LINK, mark_request_issued
        ar = _get_request_or_404(pk)

        if request.method == 'GET':
            token = ResultAccessService.get_active_token(ar)
            if token is None:
                return Response({'status': 'not_generated'})
            return Response(_serialize_access_token(token, request))

        had_active_token = ResultAccessService.get_active_token(ar) is not None
        token = ResultAccessService.get_or_create_token(ar)
        if not had_active_token:
            # First time we mint a patient-facing link → issuance fires.
            mark_request_issued(
                analysis_request=ar, channel=CHANNEL_SHARE_LINK,
                actor=request.user, request=request,
            )
        return Response(_serialize_access_token(token, request))

    @action(detail=True, methods=['post'], url_path='access-token/regenerate')
    def regenerate_access_token(self, request, pk=None):
        """Force-create a new token, deactivating the previous one.

        Once the request has been issued, regeneration is a deliberate
        re-emission and requires ``force_resend=true`` in the body.
        Without it the endpoint returns an ALREADY_ISSUED error so the
        frontend can surface a confirmation modal.
        """
        from .patient_access import ResultAccessService
        from .issuance import (
            AlreadyIssued, CHANNEL_SHARE_LINK,
            enforce_resend_gate, mark_request_issued,
        )
        ar = _get_request_or_404(pk)
        force_resend = bool(request.data.get('force_resend')) if hasattr(request, 'data') else False
        try:
            enforce_resend_gate(
                analysis_request=ar, channel=CHANNEL_SHARE_LINK,
                force_resend=force_resend,
                actor=request.user, request=request,
            )
        except AlreadyIssued as exc:
            return Response(
                {
                    'data': None,
                    'meta': None,
                    'errors': [{
                        'code': exc.code,
                        'message': exc.detail.get('message') if hasattr(exc, 'detail') else str(exc),
                        'field': None,
                        'detail': {},
                    }],
                },
                status=status.HTTP_409_CONFLICT,
            )
        token = ResultAccessService.create_token(ar)
        # Pre-issuance regen still flips the request to issued.
        mark_request_issued(
            analysis_request=ar, channel=CHANNEL_SHARE_LINK,
            actor=request.user, request=request,
        )
        return Response(_serialize_access_token(token, request))

    @action(detail=True, methods=['post'], url_path='notify-patient')
    def notify_patient(self, request, pk=None):
        """Send patient result-ready notifications over the channels enabled
        in lab settings. V1 supports email only — WhatsApp share remains a
        manual frontend action and SMS is not implemented yet.

        Always-on contract:
          - active access token is reused; only created if none exists
          - secure link is built from the request host (tenant-aware)
          - email body never contains medical data
          - response shape exposes which channels succeeded/failed so the
            frontend can render per-channel feedback
        """
        from .notification_service import (
            ChannelOutcome, EmailChannelDisabled, NoChannelsRequested,
            PatientEmailMissing, RequestNotificationService,
        )
        from .issuance import (
            AlreadyIssued, CHANNEL_EMAIL,
            enforce_resend_gate, mark_request_issued,
        )

        ar = _get_request_or_404(pk)
        force_resend = bool(request.data.get('force_resend')) if hasattr(request, 'data') else False

        # Enforce the issuance gate BEFORE we hit the email provider so
        # an already-issued request doesn't burn a delivery quota only
        # to be silently ignored.
        try:
            enforce_resend_gate(
                analysis_request=ar, channel=CHANNEL_EMAIL,
                force_resend=force_resend,
                actor=request.user, request=request,
            )
        except AlreadyIssued as exc:
            return Response(
                {
                    'data': None,
                    'meta': None,
                    'errors': [{
                        'code': exc.code,
                        'message': exc.detail.get('message') if hasattr(exc, 'detail') else str(exc),
                        'field': None,
                        'detail': {},
                    }],
                },
                status=status.HTTP_409_CONFLICT,
            )

        try:
            outcome = RequestNotificationService.notify_patient(ar, request)
        except (PatientEmailMissing, EmailChannelDisabled, NoChannelsRequested) as exc:
            return Response(
                {
                    'data': None,
                    'meta': None,
                    'errors': [{
                        'code': exc.code,
                        'message': exc.message,
                        'field': None,
                        'detail': {},
                    }],
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Issuance fires only when at least one channel actually
        # succeeded. A 0-success outcome (provider down, etc.) leaves
        # the request in VALIDATED so the lab can retry without
        # resend-gate friction.
        if outcome.channels_succeeded:
            mark_request_issued(
                analysis_request=ar, channel=CHANNEL_EMAIL,
                actor=request.user, request=request,
            )

        return Response({
            'secure_link': outcome.secure_link,
            'expires_at': outcome.expires_at,
            'channels_attempted': outcome.channels_attempted,
            'channels_succeeded': outcome.channels_succeeded,
            'channels_failed': [
                {
                    'channel': c.channel,
                    'status': c.status,
                    'provider': c.provider,
                    'error': c.error,
                }
                for c in outcome.channels_failed
            ],
        })

    @action(
        detail=True, methods=['post'], url_path='notify-cytova',
        throttle_classes=[NotifyCytovaThrottle],
    )
    def notify_cytova(self, request, pk=None):
        """Share the current report with a global Cytova patient
        account after verifying identity. See
        ``apps/requests/notify_cytova_service.py`` for the snapshot
        rationale + identity-verification policy.
        """
        from .notify_cytova_service import (
            CytovaChannelDisabled,
            IdentityVerificationFailed,
            MissingIdentity,
            NotifyCytovaError,
            notify_cytova as notify_cytova_service,
        )
        from .serializers import NotifyCytovaSerializer
        from .issuance import CHANNEL_CYTOVA, mark_request_issued
        from apps.lab_settings.models import LabSettings
        from apps.patient_portal.models import (
            PatientSharedResult, SharedResultStatus,
        )
        from apps.users.models import Role
        from django.db import connection as _conn

        ar = _get_request_or_404(pk)

        # Lab-level kill switch. Pre-check before serializer
        # validation so a disabled tenant gets a clear error code
        # instead of a generic 400 about missing fields. Mirrors the
        # ``EmailChannelDisabled`` shape used by notify-by-email so
        # the frontend handles both channels with the same branch.
        if not LabSettings.get_solo().notification_enable_cytova:
            exc = CytovaChannelDisabled()
            return Response(
                {
                    'data': None,
                    'meta': None,
                    'errors': [{
                        'code': exc.code,
                        'message': exc.message,
                        'field': None,
                        'detail': {},
                    }],
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = NotifyCytovaSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        force_share = serializer.validated_data.pop('force_share', False)

        # One-shot guard: if a non-revoked share already exists for
        # this lab request, refuse unless the caller is a privileged
        # role AND explicitly passed ``force_share=true``. Revoked
        # shares are excluded — once a previous share was actively
        # withdrawn, re-sharing is a normal lab decision (still gated
        # by the standard role + identity flow that follows).
        existing = (
            PatientSharedResult.objects
            .filter(
                source_tenant_schema=getattr(_conn, 'schema_name', '') or '',
                source_request_id=ar.id,
            )
            .exclude(status=SharedResultStatus.REVOKED)
            .order_by('-created_at')
            .first()
        )
        if existing is not None:
            actor_role = getattr(request.user, 'role', None)
            privileged = actor_role in {Role.LAB_ADMIN, Role.BIOLOGIST}
            if not (force_share and privileged):
                return Response(
                    {
                        'data': None,
                        'meta': None,
                        'errors': [{
                            'code': 'CYTOVA_ALREADY_SHARED',
                            'message': 'This result has already been shared '
                                       'with the patient via Cytova.',
                            'field': None,
                            'detail': {
                                'shared_result_id': str(existing.id),
                                'requires_role': ['LAB_ADMIN', 'BIOLOGIST'],
                            },
                        }],
                    },
                    status=status.HTTP_409_CONFLICT,
                )

        try:
            # Identity fields are optional after Phase D — the linked
            # path lets the operator submit an empty body when the
            # patient has been previously linked. ``.get(...)`` with
            # safe defaults preserves that contract; the service
            # decides which path to use.
            shared, email_status = notify_cytova_service(
                analysis_request=ar,
                cytova_patient_id=serializer.validated_data.get('cytova_patient_id', '') or '',
                first_name=serializer.validated_data.get('first_name', '') or '',
                last_name=serializer.validated_data.get('last_name', '') or '',
                date_of_birth=serializer.validated_data.get('date_of_birth'),
                actor=request.user,
                request=request,
            )
        except MissingIdentity as exc:
            # Caller bypassed the UX hint and tried to share an
            # unlinked patient with no identity payload. Surface a
            # distinct code so the frontend can tell the operator
            # what to do (link first).
            return Response(
                {
                    'data': None,
                    'meta': None,
                    'errors': [{
                        'code': exc.code,
                        'message': exc.message,
                        'field': None,
                        'detail': {},
                    }],
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except IdentityVerificationFailed as exc:
            # Single non-distinguishing failure — never tell the lab
            # user which field went wrong. The service has already
            # written an audit row.
            return Response(
                {
                    'data': None,
                    'meta': None,
                    'errors': [{
                        'code': exc.code,
                        'message': exc.message,
                        'field': None,
                        'detail': {},
                    }],
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        except NotifyCytovaError as exc:
            return Response(
                {
                    'data': None,
                    'meta': None,
                    'errors': [{
                        'code': exc.code,
                        'message': exc.message,
                        'field': None,
                        'detail': {},
                    }],
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # First successful Cytova share also flips the request to
        # RESULT_ISSUED. Subsequent (force-share) shares stay in
        # RESULT_ISSUED — mark_request_issued is idempotent.
        mark_request_issued(
            analysis_request=ar, channel=CHANNEL_CYTOVA,
            actor=request.user, request=request,
        )

        return Response({
            'data': {
                'shared_result_id': str(shared.id),
                'email_notification': email_status,  # 'SENT' | 'FAILED'
                'message': "Result successfully shared with patient.",
            },
            'meta': None,
            'errors': [],
        })

    @action(detail=True, methods=['get'], url_path='cytova-share')
    def cytova_share_status(self, request, pk=None):
        """``GET /requests/{id}/cytova-share/`` — lab-side lookup that
        powers the "Shared with Cytova patient" badge on the request
        detail page.

        Reads the public-schema ``PatientSharedResult`` table scoped to
        the current tenant + this request's UUID. Returns the latest
        share's status (or ``null`` when no share exists). Patient PII
        is never exposed: just status, IDs, and timestamps.
        """
        from apps.patient_portal.models import PatientSharedResult
        from django.db import connection as _conn

        ar = _get_request_or_404(pk)
        share = (
            PatientSharedResult.objects
            .filter(
                source_tenant_schema=getattr(_conn, 'schema_name', '') or '',
                source_request_id=ar.id,
            )
            .order_by('-created_at')
            .first()
        )
        if share is None:
            return Response({
                'data': {'status': None, 'shared_result_id': None},
                'meta': None, 'errors': [],
            })
        return Response({
            'data': {
                'status': share.status,
                'shared_result_id': str(share.id),
                'created_at': share.created_at.isoformat(),
                'revoked_at': share.revoked_at.isoformat() if share.revoked_at else None,
                'email_notification_status': share.email_notification_status or None,
            },
            'meta': None, 'errors': [],
        })

    @action(detail=True, methods=['post'], url_path='revoke-cytova-share')
    def revoke_cytova_share(self, request, pk=None):
        """``POST /requests/{id}/revoke-cytova-share/`` — flip every
        active patient share spawned from this lab request to
        ``REVOKED``.

        The lab-side audit row is written here; the patient-side audit
        row(s) are written by the service for each affected share.
        Idempotent — repeated calls after the first revoke return
        ``revoked_count=0``. Lab data (``AnalysisRequest``,
        ``AnalysisRequestReport``, the PDF blob on storage) is
        untouched.
        """
        from apps.patient_portal.services import revoke_shares_for_lab_request
        from django.db import connection as _conn

        ar = _get_request_or_404(pk)
        actor_email = getattr(request.user, 'email', '') or ''
        revoked_count = revoke_shares_for_lab_request(
            tenant_schema=getattr(_conn, 'schema_name', '') or '',
            source_request_id=ar.id,
            revoked_by_lab=actor_email or 'lab',
            request=request,
        )

        # Tenant audit — mirrors the share-side AuditLog row written by
        # ``notify_cytova_service`` so the lab's audit history shows
        # the full lifecycle.
        AuditLog.objects.create(
            actor_type=ActorType.STAFF_USER,
            actor_id=getattr(request.user, 'id', None),
            actor_email=actor_email,
            action=AuditAction.CYTOVA_SHARE_REVOKED,
            entity_type='AnalysisRequest',
            entity_id=ar.id,
            diff={'after': {
                'notify_cytova_outcome': 'REVOKED',
                'revoked_count': revoked_count,
                'request_number': ar.request_number,
            }},
            ip_address=getattr(request, 'audit_ip', None),
            user_agent=getattr(request, 'audit_user_agent', ''),
        )

        return Response({
            'data': {
                'revoked_count': revoked_count,
                'message': 'Cytova sharing has been revoked for this request.',
            },
            'meta': None, 'errors': [],
        })

    @action(detail=True, methods=['post'], url_path='reopen-result')
    def reopen_result(self, request, pk=None):
        """``POST /requests/{id}/reopen-result/`` — controlled
        correction flow.

        Walks an issued request back to ``VALIDATED`` so the lab can
        produce a new report version. Marks the current report as
        no-longer-current (preserved as history) and resets the
        Cytova-share single-shot lock indirectly by recording the
        reopen — the lab still has to share again deliberately.

        Permissions are stricter than notify: BIOLOGIST or LAB_ADMIN
        only. The endpoint refuses unless the request is currently
        ``RESULT_ISSUED`` — reopen is meaningless before issuance.
        Audit row uses the dedicated ``RESULT_REOPENED`` action so an
        audit reader can spot the correction trail.
        """
        from .serializers import ReopenResultSerializer
        from .state_machine import RequestStateMachine
        from .models import AnalysisRequestReport, RequestStatus

        ar = _get_request_or_404(pk)
        if ar.status != RequestStatus.RESULT_ISSUED:
            return Response(
                {
                    'data': None,
                    'meta': None,
                    'errors': [{
                        'code': 'NOT_ISSUED',
                        'message': 'Only issued requests can be reopened.',
                        'field': None,
                        'detail': {'current_status': ar.status},
                    }],
                },
                status=status.HTTP_409_CONFLICT,
            )

        serializer = ReopenResultSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data['reason'].strip()

        from django.db import transaction as _tx
        from django.utils import timezone as _tz
        with _tx.atomic():
            RequestStateMachine.transition(ar, RequestStatus.VALIDATED)
            ar.reopened_at = _tz.now()
            ar.reopened_by = request.user
            ar.reopen_reason = reason
            ar.save(update_fields=[
                'status', 'reopened_at', 'reopened_by', 'reopen_reason',
                'updated_at',
            ])

            # Mark the current report version as not-current so the
            # lab can regenerate. The row is preserved (versions are
            # history); a new version will be produced on next
            # ``RequestReportService.regenerate``.
            superseded_count = AnalysisRequestReport.objects.filter(
                analysis_request=ar, is_current=True,
            ).update(is_current=False, updated_at=_tz.now())

            AuditLog.objects.create(
                actor_type=ActorType.STAFF_USER,
                actor_id=getattr(request.user, 'id', None),
                actor_email=getattr(request.user, 'email', '') or '',
                action=AuditAction.RESULT_REOPENED,
                entity_type='AnalysisRequest',
                entity_id=ar.id,
                # ``reason`` IS the correction context the regulator
                # cares about — this is the one place we deliberately
                # store free-text from the lab user. Truncated as a
                # belt-and-braces against accidentally pasted PII or
                # giant blobs.
                diff={'after': {
                    'request_number': ar.request_number,
                    'reason': reason[:2000],
                    'superseded_report_versions': superseded_count,
                }},
                ip_address=getattr(request, 'audit_ip', None),
                user_agent=getattr(request, 'audit_user_agent', ''),
            )

        return Response({
            'data': {
                'status': ar.status,
                'reopened_at': ar.reopened_at.isoformat(),
                'superseded_report_versions': superseded_count,
                'message': 'Result reopened. Generate a new report version when ready.',
            },
            'meta': None, 'errors': [],
        })

    @action(detail=False, methods=['post'], url_path='preview-pricing')
    def preview_pricing(self, request):
        """
        Resolve pricing for a tentative (source, partner, exams) tuple
        WITHOUT persisting anything. Used by the Step 3 recap of the
        request creation wizard.

        The returned list uses the exact same ``RequestPricingResolver``
        that the final ``create`` path calls, so "preview matches final"
        is a structural guarantee — not a promise repeated in two places.
        """
        from apps.partners.models import PartnerOrganization
        serializer = PricingPreviewRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        partner = None
        partner_id = data.get('partner_organization_id')
        if partner_id is not None:
            partner = PartnerOrganization.objects.get(id=partner_id)

        resolved = AnalysisRequestService.preview_pricing(
            source_type=data['source_type'],
            partner=partner,
            exam_ids=data['exam_definition_ids'],
        )
        return Response({
            'items': ResolvedItemPriceSerializer(resolved, many=True).data,
        })


# ---------------------------------------------------------------------------
# AnalysisRequestItemViewSet  (nested under /requests/{request_pk}/items/)
# ---------------------------------------------------------------------------

class AnalysisRequestItemViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_class = AnalysisRequestItemFilter
    ordering_fields = ['created_at', 'status']
    ordering = ['created_at']

    def get_queryset(self):
        request_pk = self.kwargs['request_pk']
        return (
            AnalysisRequestItem.objects
            .filter(analysis_request_id=request_pk)
            .select_related(
                'exam_definition', 'pricing_rule',
                'traceability__sample_received_by',
                'traceability__performed_by',
            )
        )

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAnyStaff()]
        # Conceptual permission ``requests.collection_mark`` —
        # implemented via ``IsTechnicianOrAbove`` because specimen
        # collection is a hands-on lab action performed by technicians
        # and above, matching the existing gate for ``start`` and
        # ``complete``.
        if self.action in ('start', 'complete', 'mark_collected'):
            return [IsTechnicianOrAbove()]
        if self.action == 'reject':
            return [IsBiologistOrAbove()]
        # create, partial_update, destroy
        return [IsReceptionistOrLabAdmin()]

    def get_serializer_class(self):
        if self.action == 'create':
            return AnalysisRequestItemCreateSerializer
        if self.action == 'partial_update':
            return AnalysisRequestItemUpdateSerializer
        return AnalysisRequestItemSerializer

    def _get_parent(self):
        return _get_request_or_404(self.kwargs['request_pk'])

    def list(self, request, *args, **kwargs):
        self._get_parent()  # 404 if parent request not found
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        self._get_parent()
        return super().retrieve(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        ar = self._get_parent()
        serializer = AnalysisRequestItemCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = AnalysisRequestService.add_item(
            analysis_request=ar,
            validated_data=serializer.validated_data,
            added_by=request.user,
            request=request,
        )
        item = (
            AnalysisRequestItem.objects
            .select_related(
                'exam_definition', 'pricing_rule',
                'traceability__sample_received_by',
                'traceability__performed_by',
            )
            .get(id=item.id)
        )
        return Response(
            AnalysisRequestItemSerializer(item).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        item = _get_item_or_404(self.kwargs['request_pk'], kwargs['pk'])
        serializer = AnalysisRequestItemUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = AnalysisRequestItemService.update(
            item=item,
            validated_data=serializer.validated_data,
            updated_by=request.user,
            request=request,
        )
        return Response(AnalysisRequestItemSerializer(item).data)

    def destroy(self, request, *args, **kwargs):
        ar = self._get_parent()
        item = _get_item_or_404(self.kwargs['request_pk'], kwargs['pk'])
        AnalysisRequestService.remove_item(
            analysis_request=ar,
            item=item,
            removed_by=request.user,
            request=request,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post'], url_path='start')
    def start(self, request, request_pk=None, pk=None):
        item = _get_item_or_404(request_pk, pk)
        item = AnalysisRequestItemService.start(
            item=item,
            started_by=request.user,
            request=request,
        )
        return Response(AnalysisRequestItemSerializer(item).data)

    @action(detail=True, methods=['post'], url_path='complete')
    def complete(self, request, request_pk=None, pk=None):
        item = _get_item_or_404(request_pk, pk)
        item = AnalysisRequestItemService.complete(
            item=item,
            completed_by=request.user,
            request=request,
        )
        return Response(AnalysisRequestItemSerializer(item).data)

    @action(detail=True, methods=['post'], url_path='reject')
    def reject(self, request, request_pk=None, pk=None):
        item = _get_item_or_404(request_pk, pk)
        serializer = ItemRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = AnalysisRequestItemService.reject(
            item=item,
            rejection_reason=serializer.validated_data['rejection_reason'],
            rejected_by=request.user,
            request=request,
        )
        return Response(AnalysisRequestItemSerializer(item).data)

    @action(detail=True, methods=['post'], url_path='mark-collected')
    def mark_collected(self, request, request_pk=None, pk=None):
        """
        Mark an analysis request item as collected (specimen drawn).

        Conceptual permission: ``requests.collection_mark`` — enforced
        at the class level via ``IsTechnicianOrAbove``. The service
        layer re-validates state-machine legality and handles the
        single point of request-level status derivation.

        Idempotent — re-posting for an already-collected item returns
        the current state without mutating the original traceability
        record or writing a duplicate audit entry.
        """
        item = _get_item_or_404(request_pk, pk)
        serializer = ItemMarkCollectedSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = AnalysisRequestItemService.mark_collected(
            item=item,
            collected_by=request.user,
            request=request,
            collection_notes=serializer.validated_data.get('collection_notes', ''),
        )
        return Response(AnalysisRequestItemSerializer(item).data)
