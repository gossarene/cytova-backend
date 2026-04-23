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
from rest_framework.viewsets import GenericViewSet

from common.permissions import (
    IsAnyStaff,
    IsBiologistOrAbove,
    IsLabAdmin,
    IsReceptionistOrLabAdmin,
    IsTechnicianOrAbove,
)
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

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAnyStaff()]
        if self.action == 'cancel':
            return [IsLabAdmin()]
        if self.action == 'finalize_validation':
            return [IsBiologistOrAbove()]
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
        if self.action in ('report_download', 'report_versions', 'report_version_download'):
            # Any authenticated staff in the tenant can read the history
            # and stream any stored version. Tenant isolation is enforced
            # upstream by CytovaTenantMiddleware, and the view itself
            # scopes the lookup to the parent request.
            return [IsAnyStaff()]
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
