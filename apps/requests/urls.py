from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import AnalysisRequestViewSet, AnalysisRequestItemViewSet

router = DefaultRouter()
router.register(r'', AnalysisRequestViewSet, basename='analysisrequest')

# Explicit nested URL patterns — items are always scoped to their parent request
item_urls = [
    path(
        '<uuid:request_pk>/items/',
        AnalysisRequestItemViewSet.as_view({
            'get': 'list',
            'post': 'create',
        }),
        name='analysisrequestitem-list',
    ),
    path(
        '<uuid:request_pk>/items/<uuid:pk>/',
        AnalysisRequestItemViewSet.as_view({
            'get': 'retrieve',
            'patch': 'partial_update',
            'delete': 'destroy',
        }),
        name='analysisrequestitem-detail',
    ),
    path(
        '<uuid:request_pk>/items/<uuid:pk>/start/',
        AnalysisRequestItemViewSet.as_view({'post': 'start'}),
        name='analysisrequestitem-start',
    ),
    path(
        '<uuid:request_pk>/items/<uuid:pk>/complete/',
        AnalysisRequestItemViewSet.as_view({'post': 'complete'}),
        name='analysisrequestitem-complete',
    ),
    path(
        '<uuid:request_pk>/items/<uuid:pk>/reject/',
        AnalysisRequestItemViewSet.as_view({'post': 'reject'}),
        name='analysisrequestitem-reject',
    ),
    path(
        '<uuid:request_pk>/items/<uuid:pk>/mark-collected/',
        AnalysisRequestItemViewSet.as_view({'post': 'mark_collected'}),
        name='analysisrequestitem-mark-collected',
    ),
]

urlpatterns = router.urls + item_urls
