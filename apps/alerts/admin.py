from django.contrib import admin

from .models import InventoryAlert


@admin.register(InventoryAlert)
class InventoryAlertAdmin(admin.ModelAdmin):
    list_display = (
        'title', 'alert_type', 'severity', 'status',
        'stock_item', 'stock_lot', 'created_at',
    )
    list_filter = ('alert_type', 'severity', 'status')
    search_fields = (
        'title', 'stock_item__code', 'stock_item__name',
        'stock_lot__lot_number',
    )
    readonly_fields = (
        'id', 'alert_type', 'severity', 'status',
        'stock_item', 'stock_lot',
        'title', 'message', 'threshold_value', 'current_value',
        'acknowledged_at', 'acknowledged_by',
        'resolved_at', 'resolved_by',
        'created_at', 'updated_at',
    )
    ordering = ('-created_at',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
