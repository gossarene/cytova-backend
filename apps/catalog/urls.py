from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import ExamCategoryViewSet, ExamDefinitionViewSet, PricingRuleViewSet

router = DefaultRouter()
router.register(r'categories', ExamCategoryViewSet, basename='examcategory')
router.register(r'exams', ExamDefinitionViewSet, basename='examdefinition')

# Explicit nested URLs for pricing rules scoped to an exam
pricing_urls = [
    path(
        'exams/<uuid:exam_pk>/pricing/',
        PricingRuleViewSet.as_view({'get': 'list', 'post': 'create'}),
        name='pricingrule-list',
    ),
    path(
        'exams/<uuid:exam_pk>/pricing/<uuid:pk>/',
        PricingRuleViewSet.as_view({'get': 'retrieve'}),
        name='pricingrule-detail',
    ),
    path(
        'exams/<uuid:exam_pk>/pricing/<uuid:pk>/close/',
        PricingRuleViewSet.as_view({'post': 'close'}),
        name='pricingrule-close',
    ),
]

urlpatterns = router.urls + pricing_urls
