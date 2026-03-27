from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    StockCategoryViewSet,
    StockItemViewSet,
    StockLotViewSet,
    StockMovementReportViewSet,
    StockMovementViewSet,
)

router = DefaultRouter()
router.register(r'categories', StockCategoryViewSet, basename='stockcategory')
router.register(r'items', StockItemViewSet, basename='stockitem')
router.register(r'movements', StockMovementReportViewSet, basename='stockmovement-report')

urlpatterns = router.urls + [

    # Lots nested under items
    path(
        'items/<uuid:item_pk>/lots/',
        StockLotViewSet.as_view({'get': 'list', 'post': 'create'}),
        name='stocklot-list',
    ),
    path(
        'items/<uuid:item_pk>/lots/<uuid:pk>/',
        StockLotViewSet.as_view({'get': 'retrieve'}),
        name='stocklot-detail',
    ),

    # Movements nested under lots
    path(
        'lots/<uuid:lot_pk>/movements/',
        StockMovementViewSet.as_view({'get': 'list', 'post': 'create'}),
        name='stockmovement-list',
    ),
    path(
        'lots/<uuid:lot_pk>/movements/<uuid:pk>/',
        StockMovementViewSet.as_view({'get': 'retrieve'}),
        name='stockmovement-detail',
    ),
]
