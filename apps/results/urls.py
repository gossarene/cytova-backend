from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import ResultVersionViewSet, ResultFileViewSet

router = DefaultRouter()
router.register(r'', ResultVersionViewSet, basename='resultversion')

# Explicit nested URLs for files — kept separate for clean access control
file_urls = [
    path(
        '<uuid:result_pk>/files/',
        ResultFileViewSet.as_view({'get': 'list', 'post': 'upload'}),
        name='resultfile-list',
    ),
    path(
        '<uuid:result_pk>/files/<uuid:pk>/download/',
        ResultFileViewSet.as_view({'get': 'download'}),
        name='resultfile-download',
    ),
    path(
        '<uuid:result_pk>/files/<uuid:pk>/',
        ResultFileViewSet.as_view({'delete': 'delete'}),
        name='resultfile-delete',
    ),
]

urlpatterns = router.urls + file_urls
