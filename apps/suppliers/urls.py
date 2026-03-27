from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    PurchaseOrderItemViewSet,
    PurchaseOrderViewSet,
    ReceptionViewSet,
    SupplierViewSet,
)

router = DefaultRouter()
router.register(r'suppliers', SupplierViewSet, basename='supplier')
router.register(r'purchase-orders', PurchaseOrderViewSet, basename='purchaseorder')

urlpatterns = router.urls + [

    # Items nested under purchase orders
    path(
        'purchase-orders/<uuid:order_pk>/items/',
        PurchaseOrderItemViewSet.as_view({'get': 'list', 'post': 'create'}),
        name='purchaseorderitem-list',
    ),
    path(
        'purchase-orders/<uuid:order_pk>/items/<uuid:pk>/',
        PurchaseOrderItemViewSet.as_view({'get': 'retrieve', 'delete': 'destroy'}),
        name='purchaseorderitem-detail',
    ),

    # Receptions nested under purchase orders
    path(
        'purchase-orders/<uuid:order_pk>/receptions/',
        ReceptionViewSet.as_view({'get': 'list', 'post': 'create'}),
        name='reception-list',
    ),
    path(
        'purchase-orders/<uuid:order_pk>/receptions/<uuid:pk>/',
        ReceptionViewSet.as_view({'get': 'retrieve'}),
        name='reception-detail',
    ),
]
