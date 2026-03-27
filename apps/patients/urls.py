from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import PatientViewSet

router = DefaultRouter(trailing_slash=True)
router.register('', PatientViewSet, basename='patients')

urlpatterns = [
    path('', include(router.urls)),
]
