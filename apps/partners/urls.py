from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import PartnerExamPriceViewSet, PartnerOrganizationViewSet

router = DefaultRouter()
router.register(r'', PartnerOrganizationViewSet, basename='partnerorganization')

# Nested routes for partner-scoped agreed pricing. Kept as explicit paths
# (rather than pulling in drf-nested-routers) to match the same idiom
# already used by the catalog app for its nested pricing-rules endpoint.
_nested_exam_price_urls = [
    path(
        '<uuid:partner_pk>/exam-prices/',
        PartnerExamPriceViewSet.as_view({'get': 'list', 'post': 'create'}),
        name='partnerexamprice-list',
    ),
    path(
        '<uuid:partner_pk>/exam-prices/<uuid:pk>/',
        PartnerExamPriceViewSet.as_view({
            'get': 'retrieve',
            'patch': 'partial_update',
        }),
        name='partnerexamprice-detail',
    ),
    path(
        '<uuid:partner_pk>/exam-prices/<uuid:pk>/deactivate/',
        PartnerExamPriceViewSet.as_view({'post': 'deactivate'}),
        name='partnerexamprice-deactivate',
    ),
    path(
        '<uuid:partner_pk>/exam-prices/<uuid:pk>/reactivate/',
        PartnerExamPriceViewSet.as_view({'post': 'reactivate'}),
        name='partnerexamprice-reactivate',
    ),
]

# Nested paths first — otherwise the router's catch-all for the root
# partner endpoint would swallow ``<partner_pk>/exam-prices/``.
urlpatterns = _nested_exam_price_urls + router.urls
