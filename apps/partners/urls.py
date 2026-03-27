from rest_framework.routers import DefaultRouter

from .views import PartnerOrganizationViewSet

router = DefaultRouter()
router.register(r'', PartnerOrganizationViewSet, basename='partnerorganization')

urlpatterns = router.urls
