from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    ExamCategoryViewSet,
    ExamFamilyViewSet,
    ExamSubFamilyViewSet,
    TubeTypeViewSet,
    ExamTechniqueViewSet,
    SampleTypeViewSet,
    ExamDefinitionViewSet,
    ExamParameterViewSet,
    PricingRuleViewSet,
)

router = DefaultRouter()

# Structured reference data (new canonical routes)
router.register(r'families', ExamFamilyViewSet, basename='examfamily')
router.register(r'sub-families', ExamSubFamilyViewSet, basename='examsubfamily')
router.register(r'tube-types', TubeTypeViewSet, basename='tubetype')
router.register(r'techniques', ExamTechniqueViewSet, basename='examtechnique')
router.register(r'sample-types', SampleTypeViewSet, basename='sampletype')

# Legacy categories endpoint — kept for backward compatibility with the
# current frontend while the sidebar migrates to /families/. Both routes
# coexist until the frontend flip is complete; the deprecated ExamCategory
# model is still behind this route.
router.register(r'categories', ExamCategoryViewSet, basename='examcategory')

# Exam definitions + pricing rules
router.register(r'exams', ExamDefinitionViewSet, basename='examdefinition')
router.register(r'pricing-rules', PricingRuleViewSet, basename='pricingrule')

# Nested read-only list for pricing rules scoped to an exam
nested_pricing_urls = [
    path(
        'exams/<uuid:exam_pk>/pricing-rules/',
        PricingRuleViewSet.as_view({'get': 'list'}),
        name='pricingrule-by-exam',
    ),
]

nested_parameter_urls = [
    path(
        'exams/<uuid:exam_pk>/parameters/',
        ExamParameterViewSet.as_view({'get': 'list', 'post': 'create'}),
        name='examparameter-list',
    ),
    path(
        'exams/<uuid:exam_pk>/parameters/<uuid:pk>/',
        ExamParameterViewSet.as_view({'patch': 'partial_update'}),
        name='examparameter-detail',
    ),
    path(
        'exams/<uuid:exam_pk>/parameters/<uuid:pk>/deactivate/',
        ExamParameterViewSet.as_view({'post': 'deactivate'}),
        name='examparameter-deactivate',
    ),
]

urlpatterns = router.urls + nested_pricing_urls + nested_parameter_urls
