from django.contrib import admin
from .models import Tenant, Domain, PlatformAdmin


class DomainInline(admin.TabularInline):
    model = Domain
    extra = 1
    fields = ('domain', 'is_primary')


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ('name', 'subdomain', 'schema_name', 'plan', 'is_active', 'created_at')
    list_filter = ('plan', 'is_active')
    search_fields = ('name', 'subdomain', 'schema_name')
    readonly_fields = ('schema_name', 'created_at')
    inlines = [DomainInline]

    fieldsets = (
        (None, {
            'fields': ('name', 'subdomain', 'schema_name', 'plan', 'is_active'),
        }),
        ('Lifecycle', {
            'fields': ('created_at', 'activated_at', 'suspended_at'),
        }),
    )


@admin.register(PlatformAdmin)
class PlatformAdminAdmin(admin.ModelAdmin):
    list_display = ('email', 'is_active', 'created_at')
    search_fields = ('email',)
    readonly_fields = ('id', 'created_at', 'last_login')
    # No add form — platform admins are created via management commands or shell only.
