from django.contrib import admin
from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ('timestamp', 'action', 'entity_type', 'entity_id', 'actor_email', 'ip_address')
    list_filter = ('action', 'actor_type', 'entity_type')
    search_fields = ('actor_email', 'entity_type', 'entity_id')
    readonly_fields = [f.name for f in AuditLog._meta.get_fields()]
    ordering = ('-timestamp',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
