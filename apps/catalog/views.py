"""
Cytova — Catalog Views

ExamCategoryViewSet   — list, create, retrieve, partial_update, deactivate
ExamDefinitionViewSet — list, create, retrieve, partial_update, deactivate, settings (GET/PUT)
PricingRuleViewSet    — list, create, retrieve, close (nested under exam)
"""
import logging

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status
from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from common.permissions import IsLabAdmin, IsAnyStaff
from .filters import (
    ExamCategoryFilter, ExamDefinitionFilter, ExamFamilyFilter,
    ExamSubFamilyFilter, TubeTypeFilter, ExamTechniqueFilter,
)
from .models import (
    ExamCategory, ExamFamily, ExamSubFamily, TubeType, ExamTechnique,
    ExamDefinition, ExamParameter, PricingRule, SampleType, ResultStructure,
)
from .serializers import (
    ExamCategoryCreateSerializer,
    ExamCategoryDetailSerializer,
    ExamCategoryListSerializer,
    ExamCategoryUpdateSerializer,
    ExamDefinitionCreateSerializer,
    ExamDefinitionDetailSerializer,
    ExamDefinitionListSerializer,
    ExamDefinitionUpdateSerializer,
    ExamFamilyListSerializer,
    ExamFamilyDetailSerializer,
    ExamFamilyCreateSerializer,
    ExamFamilyUpdateSerializer,
    ExamSubFamilyListSerializer,
    ExamSubFamilyDetailSerializer,
    ExamSubFamilyCreateSerializer,
    ExamSubFamilyUpdateSerializer,
    TubeTypeListSerializer,
    TubeTypeCreateSerializer,
    TubeTypeUpdateSerializer,
    ExamTechniqueListSerializer,
    ExamTechniqueCreateSerializer,
    ExamTechniqueUpdateSerializer,
    SampleTypeSerializer,
    LabExamSettingsSerializer,
    LabExamSettingsWriteSerializer,
    ExamDefinitionStructureChangeSerializer,
    ExamParameterSerializer,
    ExamParameterWriteSerializer,
    ExamParameterUpdateSerializer,
    PricingRuleCreateSerializer,
    PricingRuleSerializer,
    PricingRuleUpdateSerializer,
)
from .services import (
    ExamCategoryService, ExamDefinitionService, ExamParameterService,
    PricingRuleService,
    ExamFamilyService, ExamSubFamilyService, TubeTypeService, ExamTechniqueService,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ExamCategory
# ---------------------------------------------------------------------------

class ExamCategoryViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    queryset = ExamCategory.objects.all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = ExamCategoryFilter
    search_fields = ['name']
    ordering_fields = ['display_order', 'name', 'created_at']
    ordering = ['display_order', 'name']

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAnyStaff()]
        return [IsLabAdmin()]

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ExamCategoryDetailSerializer
        if self.action == 'create':
            return ExamCategoryCreateSerializer
        if self.action == 'partial_update':
            return ExamCategoryUpdateSerializer
        return ExamCategoryListSerializer

    def create(self, request, *args, **kwargs):
        serializer = ExamCategoryCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        category = ExamCategoryService.create(
            validated_data=serializer.validated_data,
            created_by=request.user,
            request=request,
        )
        return Response(
            ExamCategoryDetailSerializer(category).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        category = self.get_object()
        serializer = ExamCategoryUpdateSerializer(
            data=request.data,
            context={'instance': category},
        )
        serializer.is_valid(raise_exception=True)
        if not serializer.validated_data:
            return Response(
                ExamCategoryDetailSerializer(category).data,
                status=status.HTTP_200_OK,
            )
        category = ExamCategoryService.update(
            category=category,
            validated_data=serializer.validated_data,
            updated_by=request.user,
            request=request,
        )
        return Response(ExamCategoryDetailSerializer(category).data)

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, pk=None):
        category = self.get_object()
        category = ExamCategoryService.deactivate(
            category=category,
            deactivated_by=request.user,
            request=request,
        )
        return Response(ExamCategoryDetailSerializer(category).data)


# ---------------------------------------------------------------------------
# Reference-data viewsets
#
# These four viewsets (Family / SubFamily / TubeType / Technique) are
# deliberately shaped identically to ExamCategoryViewSet: list + retrieve from
# mixins, write actions hand-rolled so every create/update/deactivate goes
# through a service that writes an AuditLog. The small amount of repetition
# is a feature — it keeps audit semantics, permission wiring and response
# shape trivially auditable. A shared ``BaseRefViewSet`` would save a dozen
# lines but hide exactly the pieces (permissions, service routing, read
# serializer after write) that a reviewer needs to verify quickly.
# ---------------------------------------------------------------------------


class ExamFamilyViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    queryset = ExamFamily.objects.all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = ExamFamilyFilter
    search_fields = ['name']
    ordering_fields = ['display_order', 'name', 'created_at']
    ordering = ['display_order', 'name']

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAnyStaff()]
        return [IsLabAdmin()]

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ExamFamilyDetailSerializer
        if self.action == 'create':
            return ExamFamilyCreateSerializer
        if self.action == 'partial_update':
            return ExamFamilyUpdateSerializer
        return ExamFamilyListSerializer

    def create(self, request, *args, **kwargs):
        serializer = ExamFamilyCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        family = ExamFamilyService.create(
            validated_data=serializer.validated_data,
            created_by=request.user,
            request=request,
        )
        return Response(
            ExamFamilyDetailSerializer(family).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        family = self.get_object()
        serializer = ExamFamilyUpdateSerializer(
            data=request.data,
            context={'instance': family},
        )
        serializer.is_valid(raise_exception=True)
        family = ExamFamilyService.update(
            family=family,
            validated_data=serializer.validated_data,
            updated_by=request.user,
            request=request,
        )
        return Response(ExamFamilyDetailSerializer(family).data)

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, pk=None):
        family = self.get_object()
        family = ExamFamilyService.deactivate(
            family=family,
            deactivated_by=request.user,
            request=request,
        )
        return Response(ExamFamilyDetailSerializer(family).data)

    @action(detail=True, methods=['post'], url_path='reactivate')
    def reactivate(self, request, pk=None):
        family = self.get_object()
        family = ExamFamilyService.reactivate(
            family=family,
            reactivated_by=request.user,
            request=request,
        )
        return Response(ExamFamilyDetailSerializer(family).data)


class ExamSubFamilyViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = ExamSubFamilyFilter
    search_fields = ['name']
    ordering_fields = ['name', 'created_at']
    ordering = ['name']

    def get_queryset(self):
        # select_related keeps the dropdown list path free of N+1 when the
        # family_name is denormalised into the list serializer.
        return ExamSubFamily.objects.select_related('family').all()

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAnyStaff()]
        return [IsLabAdmin()]

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ExamSubFamilyDetailSerializer
        if self.action == 'create':
            return ExamSubFamilyCreateSerializer
        if self.action == 'partial_update':
            return ExamSubFamilyUpdateSerializer
        return ExamSubFamilyListSerializer

    def create(self, request, *args, **kwargs):
        serializer = ExamSubFamilyCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        sub = ExamSubFamilyService.create(
            validated_data=serializer.validated_data,
            created_by=request.user,
            request=request,
        )
        sub = ExamSubFamily.objects.select_related('family').get(pk=sub.pk)
        return Response(
            ExamSubFamilyDetailSerializer(sub).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        sub = self.get_object()
        serializer = ExamSubFamilyUpdateSerializer(
            data=request.data,
            context={'instance': sub},
        )
        serializer.is_valid(raise_exception=True)
        sub = ExamSubFamilyService.update(
            sub=sub,
            validated_data=serializer.validated_data,
            updated_by=request.user,
            request=request,
        )
        sub = ExamSubFamily.objects.select_related('family').get(pk=sub.pk)
        return Response(ExamSubFamilyDetailSerializer(sub).data)

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, pk=None):
        sub = self.get_object()
        sub = ExamSubFamilyService.deactivate(
            sub=sub,
            deactivated_by=request.user,
            request=request,
        )
        return Response(ExamSubFamilyDetailSerializer(sub).data)

    @action(detail=True, methods=['post'], url_path='reactivate')
    def reactivate(self, request, pk=None):
        sub = self.get_object()
        sub = ExamSubFamilyService.reactivate(
            sub=sub,
            reactivated_by=request.user,
            request=request,
        )
        sub = ExamSubFamily.objects.select_related('family').get(pk=sub.pk)
        return Response(ExamSubFamilyDetailSerializer(sub).data)


class TubeTypeViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    queryset = TubeType.objects.all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = TubeTypeFilter
    search_fields = ['name']
    ordering_fields = ['name', 'created_at']
    ordering = ['name']

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAnyStaff()]
        return [IsLabAdmin()]

    def get_serializer_class(self):
        if self.action == 'create':
            return TubeTypeCreateSerializer
        if self.action == 'partial_update':
            return TubeTypeUpdateSerializer
        return TubeTypeListSerializer

    def create(self, request, *args, **kwargs):
        serializer = TubeTypeCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tube = TubeTypeService.create(
            validated_data=serializer.validated_data,
            created_by=request.user,
            request=request,
        )
        return Response(
            TubeTypeListSerializer(tube).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        tube = self.get_object()
        serializer = TubeTypeUpdateSerializer(
            data=request.data,
            context={'instance': tube},
        )
        serializer.is_valid(raise_exception=True)
        tube = TubeTypeService.update(
            tube=tube,
            validated_data=serializer.validated_data,
            updated_by=request.user,
            request=request,
        )
        return Response(TubeTypeListSerializer(tube).data)

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, pk=None):
        tube = self.get_object()
        tube = TubeTypeService.deactivate(
            tube=tube,
            deactivated_by=request.user,
            request=request,
        )
        return Response(TubeTypeListSerializer(tube).data)

    @action(detail=True, methods=['post'], url_path='reactivate')
    def reactivate(self, request, pk=None):
        tube = self.get_object()
        tube = TubeTypeService.reactivate(
            tube=tube,
            reactivated_by=request.user,
            request=request,
        )
        return Response(TubeTypeListSerializer(tube).data)


class ExamTechniqueViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    queryset = ExamTechnique.objects.all()
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = ExamTechniqueFilter
    search_fields = ['name']
    ordering_fields = ['name', 'created_at']
    ordering = ['name']

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAnyStaff()]
        return [IsLabAdmin()]

    def get_serializer_class(self):
        if self.action == 'create':
            return ExamTechniqueCreateSerializer
        if self.action == 'partial_update':
            return ExamTechniqueUpdateSerializer
        return ExamTechniqueListSerializer

    def create(self, request, *args, **kwargs):
        serializer = ExamTechniqueCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tech = ExamTechniqueService.create(
            validated_data=serializer.validated_data,
            created_by=request.user,
            request=request,
        )
        return Response(
            ExamTechniqueListSerializer(tech).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        tech = self.get_object()
        serializer = ExamTechniqueUpdateSerializer(
            data=request.data,
            context={'instance': tech},
        )
        serializer.is_valid(raise_exception=True)
        tech = ExamTechniqueService.update(
            tech=tech,
            validated_data=serializer.validated_data,
            updated_by=request.user,
            request=request,
        )
        return Response(ExamTechniqueListSerializer(tech).data)

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, pk=None):
        tech = self.get_object()
        tech = ExamTechniqueService.deactivate(
            tech=tech,
            deactivated_by=request.user,
            request=request,
        )
        return Response(ExamTechniqueListSerializer(tech).data)

    @action(detail=True, methods=['post'], url_path='reactivate')
    def reactivate(self, request, pk=None):
        tech = self.get_object()
        tech = ExamTechniqueService.reactivate(
            tech=tech,
            reactivated_by=request.user,
            request=request,
        )
        return Response(ExamTechniqueListSerializer(tech).data)


class SampleTypeViewSet(GenericViewSet):
    """
    Read-only reference listing of the SampleType taxonomy.

    Sample types are a fixed clinical enumeration — not free-form lab metadata
    like tube types. They live in ``SampleType.choices`` so the model-level
    constraint, the ORM filters, and the OpenAPI schema stay in sync. This
    endpoint exposes them as a flat ``[{value, label}]`` list so the frontend
    can populate dropdowns consistently with the other reference endpoints,
    without having to hardcode the enum itself.

    Changing this taxonomy is intentionally a code change: it must go through
    migration review because it affects exam definitions, traceability, and
    reporting. Hence no write endpoints.
    """
    permission_classes = [IsAnyStaff]
    serializer_class = SampleTypeSerializer

    def get_queryset(self):
        return []

    def list(self, request, *args, **kwargs):
        payload = [{'value': v, 'label': l} for v, l in SampleType.choices]
        return Response(SampleTypeSerializer(payload, many=True).data)


# ---------------------------------------------------------------------------
# ExamDefinition
# ---------------------------------------------------------------------------

class ExamDefinitionViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = ExamDefinitionFilter
    search_fields = ['code', 'name']
    ordering_fields = ['code', 'name', 'created_at']

    def get_queryset(self):
        return ExamDefinition.objects.select_related(
            'category', 'family', 'sub_family', 'tube_type', 'technique', 'lab_settings',
        ).all()

    def get_permissions(self):
        if self.action in ('list', 'retrieve', 'exam_settings'):
            return [IsAnyStaff()]
        return [IsLabAdmin()]

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ExamDefinitionDetailSerializer
        if self.action == 'create':
            return ExamDefinitionCreateSerializer
        if self.action == 'partial_update':
            return ExamDefinitionUpdateSerializer
        return ExamDefinitionListSerializer

    def create(self, request, *args, **kwargs):
        serializer = ExamDefinitionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        exam = ExamDefinitionService.create(
            validated_data=serializer.validated_data,
            created_by=request.user,
            request=request,
        )
        exam = ExamDefinition.objects.select_related(
            'category', 'family', 'sub_family', 'tube_type', 'technique', 'lab_settings',
        ).get(id=exam.id)
        return Response(
            ExamDefinitionDetailSerializer(exam).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        exam = self.get_object()
        serializer = ExamDefinitionUpdateSerializer(
            data=request.data,
            context={'instance': exam},
        )
        serializer.is_valid(raise_exception=True)
        if not serializer.validated_data:
            return Response(ExamDefinitionDetailSerializer(exam).data)
        exam = ExamDefinitionService.update(
            exam=exam,
            validated_data=serializer.validated_data,
            updated_by=request.user,
            request=request,
        )
        exam = ExamDefinition.objects.select_related(
            'category', 'family', 'sub_family', 'tube_type', 'technique', 'lab_settings',
        ).get(id=exam.id)
        return Response(ExamDefinitionDetailSerializer(exam).data)

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, pk=None):
        exam = self.get_object()
        exam = ExamDefinitionService.deactivate(
            exam=exam,
            deactivated_by=request.user,
            request=request,
        )
        return Response(ExamDefinitionDetailSerializer(exam).data)

    @action(detail=True, methods=['post'], url_path='change-structure')
    def change_structure(self, request, pk=None):
        """``POST /exam-definitions/{id}/change-structure/``

        Switch the exam's ``result_structure`` between SINGLE_VALUE
        and MULTI_PARAMETER. This is the ONLY sanctioned write path
        for that field — the standard PATCH refuses it because a
        silent flip would change how every in-flight item is
        interpreted at result entry. In-flight items are protected
        upstream by the ``result_structure_snapshot`` on
        ``AnalysisRequestItem``; see the service docstring.
        """
        exam = self.get_object()
        serializer = ExamDefinitionStructureChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        exam = ExamDefinitionService.change_structure(
            exam=exam,
            new_structure=serializer.validated_data['result_structure'],
            parameters=serializer.validated_data.get('parameters') or [],
            updated_by=request.user,
            request=request,
        )
        exam = ExamDefinition.objects.select_related(
            'category', 'family', 'sub_family', 'tube_type', 'technique', 'lab_settings',
        ).prefetch_related('parameters').get(id=exam.id)
        return Response(ExamDefinitionDetailSerializer(exam).data)

    @action(detail=True, methods=['get', 'put'], url_path='settings')
    def exam_settings(self, request, pk=None):
        exam = self.get_object()

        if request.method == 'GET':
            try:
                lab_settings = exam.lab_settings
                return Response(LabExamSettingsSerializer(lab_settings).data)
            except Exception:
                return Response({})

        # PUT — upsert lab settings (IsLabAdmin enforced via get_permissions)
        serializer = LabExamSettingsWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        lab_settings = ExamDefinitionService.upsert_lab_settings(
            exam=exam,
            validated_data=serializer.validated_data,
            updated_by=request.user,
            request=request,
        )
        return Response(LabExamSettingsSerializer(lab_settings).data)


# ---------------------------------------------------------------------------
# ExamParameter (nested under /exams/{exam_pk}/parameters/)
# ---------------------------------------------------------------------------

class ExamParameterViewSet(GenericViewSet):
    """Manage parameters for MULTI_PARAMETER exam definitions."""

    def get_permissions(self):
        if self.action == 'list':
            return [IsAnyStaff()]
        return [IsLabAdmin()]

    def _get_exam(self):
        from rest_framework.exceptions import NotFound
        try:
            return ExamDefinition.objects.get(pk=self.kwargs['exam_pk'])
        except ExamDefinition.DoesNotExist:
            raise NotFound('Exam definition not found.')

    def _get_param(self):
        from rest_framework.exceptions import NotFound
        try:
            return ExamParameter.objects.get(
                pk=self.kwargs['pk'],
                exam_definition_id=self.kwargs['exam_pk'],
            )
        except ExamParameter.DoesNotExist:
            raise NotFound('Exam parameter not found.')

    def list(self, request, exam_pk=None, *args, **kwargs):
        self._get_exam()
        params = (
            ExamParameter.objects
            .filter(exam_definition_id=exam_pk)
            .order_by('display_order', 'name')
        )
        return Response(ExamParameterSerializer(params, many=True).data)

    def create(self, request, exam_pk=None, *args, **kwargs):
        exam = self._get_exam()
        serializer = ExamParameterWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        param = ExamParameterService.create(
            exam=exam,
            validated_data=serializer.validated_data,
            created_by=request.user,
            request=request,
        )
        return Response(
            ExamParameterSerializer(param).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, exam_pk=None, pk=None, *args, **kwargs):
        param = self._get_param()
        serializer = ExamParameterUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        param = ExamParameterService.update(
            param=param,
            validated_data=serializer.validated_data,
            updated_by=request.user,
            request=request,
        )
        return Response(ExamParameterSerializer(param).data)

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, exam_pk=None, pk=None):
        param = self._get_param()
        param = ExamParameterService.deactivate(
            param=param,
            deactivated_by=request.user,
            request=request,
        )
        return Response(ExamParameterSerializer(param).data)


# ---------------------------------------------------------------------------
# PricingRule
# ---------------------------------------------------------------------------

class PricingRuleViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    """
    Top-level CRUD for pricing rules.

    Also available nested under an exam at /exams/{exam_pk}/pricing-rules/
    (read-only list, filtered by exam).
    """
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    ordering_fields = ['priority', 'created_at']
    ordering = ['-priority', '-created_at']

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAnyStaff()]
        return [IsLabAdmin()]

    def get_queryset(self):
        qs = PricingRule.objects.select_related(
            'exam_definition', 'partner_organization', 'created_by',
        )
        # Support nested route: /exams/{exam_pk}/pricing-rules/
        exam_pk = self.kwargs.get('exam_pk')
        if exam_pk:
            qs = qs.filter(exam_definition_id=exam_pk)
        return qs

    def get_serializer_class(self):
        if self.action == 'create':
            return PricingRuleCreateSerializer
        if self.action == 'partial_update':
            return PricingRuleUpdateSerializer
        return PricingRuleSerializer

    def create(self, request, *args, **kwargs):
        serializer = PricingRuleCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        rule = PricingRuleService.create(
            validated_data=serializer.validated_data,
            created_by=request.user,
            request=request,
        )
        rule = PricingRule.objects.select_related(
            'exam_definition', 'partner_organization', 'created_by',
        ).get(id=rule.id)
        return Response(
            PricingRuleSerializer(rule).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        rule = self.get_object()
        serializer = PricingRuleUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        rule = PricingRuleService.update(
            rule=rule,
            validated_data=serializer.validated_data,
            updated_by=request.user,
            request=request,
        )
        return Response(PricingRuleSerializer(rule).data)

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, pk=None, **kwargs):
        rule = self.get_object()
        rule = PricingRuleService.deactivate(
            rule=rule,
            deactivated_by=request.user,
            request=request,
        )
        return Response(PricingRuleSerializer(rule).data)
