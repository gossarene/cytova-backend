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
# PricingRule  (nested under exam: /exams/{exam_pk}/pricing/)
# ---------------------------------------------------------------------------

class PricingRuleViewSet(ListModelMixin, RetrieveModelMixin, GenericViewSet):

    def get_permissions(self):
        if self.action in ('list', 'retrieve'):
            return [IsAnyStaff()]
        return [IsLabAdmin()]

    def _get_exam(self, exam_pk):
        try:
            return ExamDefinition.objects.get(pk=exam_pk)
        except ExamDefinition.DoesNotExist:
            from rest_framework.exceptions import NotFound
            raise NotFound('Exam definition not found.')

    def get_queryset(self):
        exam_pk = self.kwargs.get('exam_pk')
        qs = PricingRule.objects.select_related('created_by')
        if exam_pk:
            qs = qs.filter(exam_definition_id=exam_pk)
        return qs

    def list(self, request, *args, **kwargs):
        self._get_exam(self.kwargs['exam_pk'])  # 404 if exam not found
        return super().list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        self._get_exam(self.kwargs['exam_pk'])
        return super().retrieve(request, *args, **kwargs)

    def get_serializer_class(self):
        if self.action == 'create':
            return PricingRuleCreateSerializer
        return PricingRuleSerializer

    def create(self, request, *args, **kwargs):
        exam = self._get_exam(self.kwargs['exam_pk'])
        serializer = PricingRuleCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        rule = PricingRuleService.create(
            exam=exam,
            validated_data=serializer.validated_data,
            created_by=request.user,
            request=request,
        )
        return Response(
            PricingRuleSerializer(rule).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=['post'], url_path='close')
    def close(self, request, exam_pk=None, pk=None):
        self._get_exam(exam_pk)
        rule = self.get_object()

        effective_to = request.data.get('effective_to')
        if not effective_to:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({'effective_to': 'This field is required.'})

        from rest_framework import serializers as drf_serializers
        date_field = drf_serializers.DateField()
        try:
            effective_to = date_field.to_internal_value(effective_to)
        except Exception:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({'effective_to': 'Enter a valid date.'})

        rule = PricingRuleService.close(
            rule=rule,
            effective_to=effective_to,
            closed_by=request.user,
            request=request,
        )
        return Response(PricingRuleSerializer(rule).data)
