"""
Cytova — Procurement Domain URL Configuration

Exposes supplier, purchase-order, and reception endpoints under a unified
/api/v1/procurement/ prefix. Views, serializers, services, and models
remain in apps.suppliers — this module provides clean domain-aligned routing.

Routes:
    /procurement/suppliers/                                     — Supplier CRUD
    /procurement/purchase-orders/                               — PO CRUD + actions
    /procurement/purchase-orders/{id}/items/                    — PO items
    /procurement/purchase-orders/{id}/receptions/               — Delivery receptions
"""
from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.suppliers.views import (
    SupplierViewSet,
    PurchaseOrderViewSet,
    PurchaseOrderItemViewSet,
    ReceptionViewSet,
)

router = DefaultRouter()
router.register(r'suppliers', SupplierViewSet, basename='procurement-supplier')
router.register(r'purchase-orders', PurchaseOrderViewSet, basename='procurement-purchaseorder')

urlpatterns = router.urls + [

    # Items nested under purchase orders
    path(
        'purchase-orders/<uuid:order_pk>/items/',
        PurchaseOrderItemViewSet.as_view({'get': 'list', 'post': 'create'}),
        name='procurement-purchaseorderitem-list',
    ),
    path(
        'purchase-orders/<uuid:order_pk>/items/<uuid:pk>/',
        PurchaseOrderItemViewSet.as_view({'get': 'retrieve', 'delete': 'destroy'}),
        name='procurement-purchaseorderitem-detail',
    ),

    # Receptions nested under purchase orders
    path(
        'purchase-orders/<uuid:order_pk>/receptions/',
        ReceptionViewSet.as_view({'get': 'list', 'post': 'create'}),
        name='procurement-reception-list',
    ),
    path(
        'purchase-orders/<uuid:order_pk>/receptions/<uuid:pk>/',
        ReceptionViewSet.as_view({'get': 'retrieve'}),
        name='procurement-reception-detail',
    ),
]
