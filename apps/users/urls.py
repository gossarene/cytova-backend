from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import UserViewSet

router = DefaultRouter(trailing_slash=True)
router.register('', UserViewSet, basename='users')

urlpatterns = [
    path('', include(router.urls)),
]
