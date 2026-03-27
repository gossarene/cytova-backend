from django.contrib import admin

from .models import PartnerOrganization


@admin.register(PartnerOrganization)
class PartnerOrganizationAdmin(admin.ModelAdmin):
    list_display = (
        'code', 'name', 'organization_type',
        'contact_person', 'email', 'is_active', 'created_at',
    )
    list_filter = ('organization_type', 'is_active')
    search_fields = ('code', 'name', 'contact_person', 'email')
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('name',)

    def has_delete_permission(self, request, obj=None):
        return False
