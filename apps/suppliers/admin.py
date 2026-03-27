from django.contrib import admin

from .models import (
    PurchaseOrder,
    PurchaseOrderItem,
    Reception,
    ReceptionItem,
    Supplier,
)


# ---------------------------------------------------------------------------
# Supplier
# ---------------------------------------------------------------------------

@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ('name', 'contact_name', 'email', 'phone', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'contact_name', 'email')
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('name',)

    def has_delete_permission(self, request, obj=None):
        return False


# ---------------------------------------------------------------------------
# PurchaseOrder
# ---------------------------------------------------------------------------

class PurchaseOrderItemInline(admin.TabularInline):
    model = PurchaseOrderItem
    extra = 0
    fields = (
        'stock_item', 'ordered_quantity', 'received_quantity',
        'unit_price', 'notes',
    )
    readonly_fields = (
        'stock_item', 'ordered_quantity', 'received_quantity',
        'unit_price', 'notes',
    )
    show_change_link = True
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = (
        'order_number', 'supplier', 'status',
        'expected_delivery_date', 'created_at',
    )
    list_filter = ('status', 'supplier')
    search_fields = ('order_number', 'supplier__name')
    readonly_fields = (
        'id', 'order_number', 'status',
        'confirmed_at', 'confirmed_by',
        'cancelled_at', 'cancelled_by',
        'closed_at', 'closed_by',
        'created_by', 'created_at', 'updated_at',
    )
    ordering = ('-created_at',)
    inlines = [PurchaseOrderItemInline]

    def has_delete_permission(self, request, obj=None):
        return False


# ---------------------------------------------------------------------------
# Reception
# ---------------------------------------------------------------------------

class ReceptionItemInline(admin.TabularInline):
    model = ReceptionItem
    extra = 0
    fields = (
        'order_item', 'received_quantity', 'lot_number',
        'expiry_date', 'unit_cost', 'discrepancy_quantity', 'stock_lot',
    )
    readonly_fields = (
        'order_item', 'received_quantity', 'lot_number',
        'expiry_date', 'unit_cost', 'discrepancy_quantity', 'stock_lot',
    )
    can_delete = False
    max_num = 0

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Reception)
class ReceptionAdmin(admin.ModelAdmin):
    list_display = (
        '__str__', 'order', 'received_at', 'received_by',
        'has_discrepancy', 'created_at',
    )
    list_filter = ('has_discrepancy',)
    search_fields = ('order__order_number',)
    readonly_fields = (
        'id', 'order', 'received_at', 'received_by',
        'notes', 'has_discrepancy', 'created_at', 'updated_at',
    )
    ordering = ('-received_at',)
    inlines = [ReceptionItemInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
