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
from .filters import ExamCategoryFilter, ExamDefinitionFilter
from .models import ExamCategory, ExamDefinition, PricingRule
from .serializers import (
    ExamCategoryCreateSerializer,
    ExamCategoryDetailSerializer,
    ExamCategoryListSerializer,
    ExamCategoryUpdateSerializer,
    ExamDefinitionCreateSerializer,
    ExamDefinitionDetailSerializer,
    ExamDefinitionListSerializer,
    ExamDefinitionUpdateSerializer,
    LabExamSettingsSerializer,
    LabExamSettingsWriteSerializer,
    PricingRuleCreateSerializer,
    PricingRuleSerializer,
    PricingRuleUpdateSerializer,
)
from .services import ExamCategoryService, ExamDefinitionService, PricingRuleService

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
# ExamDefinition
# ---------------------------------------------------------------------------

class ExamDefinitionViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = ExamDefinitionFilter
    search_fields = ['code', 'name']
    ordering_fields = ['code', 'name', 'created_at']

    def get_queryset(self):
        return ExamDefinition.objects.select_related('category', 'lab_settings').all()

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
        exam = ExamDefinition.objects.select_related('category', 'lab_settings').get(id=exam.id)
        return Response(
            ExamDefinitionDetailSerializer(exam).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        exam = self.get_object()
        serializer = ExamDefinitionUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not serializer.validated_data:
            return Response(ExamDefinitionDetailSerializer(exam).data)
        exam = ExamDefinitionService.update(
            exam=exam,
            validated_data=serializer.validated_data,
            updated_by=request.user,
            request=request,
        )
        exam = ExamDefinition.objects.select_related('category', 'lab_settings').get(id=exam.id)
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
