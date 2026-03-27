from rest_framework.routers import DefaultRouter

from .views import InventoryAlertViewSet

router = DefaultRouter()
router.register(r'', InventoryAlertViewSet, basename='inventoryalert')

urlpatterns = router.urls
