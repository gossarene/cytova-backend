from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import ExamCategoryViewSet, ExamDefinitionViewSet, PricingRuleViewSet

router = DefaultRouter()
router.register(r'categories', ExamCategoryViewSet, basename='examcategory')
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

urlpatterns = router.urls + nested_pricing_urls
