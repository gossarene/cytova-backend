from django.contrib import admin

from .models import StockCategory, StockItem, StockLot, StockMovement


class StockItemInline(admin.TabularInline):
    model = StockItem
    extra = 0
    fields = ('code', 'name', 'unit', 'minimum_threshold', 'is_active')
    readonly_fields = ('code', 'name', 'unit', 'minimum_threshold', 'is_active')
    show_change_link = True
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(StockCategory)
class StockCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'display_order', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name',)
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('display_order', 'name')
    inlines = [StockItemInline]

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StockItem)
class StockItemAdmin(admin.ModelAdmin):
    list_display = (
        'code', 'name', 'category', 'unit',
        'minimum_threshold', 'main_supplier_name', 'is_active', 'created_at',
    )
    list_filter = ('category', 'is_active')
    search_fields = ('code', 'name', 'main_supplier_name')
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('category__display_order', 'name')

    def has_delete_permission(self, request, obj=None):
        return False


class StockMovementInline(admin.TabularInline):
    model = StockMovement
    extra = 0
    fields = (
        'movement_type', 'quantity', 'quantity_before', 'quantity_after',
        'reason', 'reference', 'performed_by', 'performed_at',
    )
    readonly_fields = (
        'movement_type', 'quantity', 'quantity_before', 'quantity_after',
        'reason', 'reference', 'performed_by', 'performed_at',
    )
    ordering = ('-performed_at',)
    can_delete = False
    show_change_link = True
    max_num = 0

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(StockLot)
class StockLotAdmin(admin.ModelAdmin):
    list_display = (
        'lot_number', 'item', 'initial_quantity', 'current_quantity',
        'is_exhausted', 'expiry_date', 'received_at',
    )
    list_filter = ('is_exhausted', 'item__category')
    search_fields = ('lot_number', 'item__code', 'item__name')
    readonly_fields = (
        'id', 'item', 'lot_number', 'initial_quantity', 'current_quantity',
        'is_exhausted', 'created_at', 'updated_at',
    )
    ordering = ('-received_at',)
    inlines = [StockMovementInline]

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = (
        'movement_type', 'lot', 'quantity',
        'quantity_before', 'quantity_after', 'performed_by', 'performed_at',
    )
    list_filter = ('movement_type',)
    search_fields = ('lot__lot_number', 'lot__item__code', 'reference')
    readonly_fields = (
        'id', 'lot', 'movement_type', 'quantity',
        'quantity_before', 'quantity_after',
        'reason', 'reference', 'reference_type',
        'performed_by', 'performed_at',
    )
    ordering = ('-performed_at',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
