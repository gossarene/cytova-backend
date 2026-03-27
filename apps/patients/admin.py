from django.contrib import admin
from .models import Patient, PatientPortalAccount


class PortalAccountInline(admin.TabularInline):
    model = PatientPortalAccount
    extra = 0
    fields = ('email', 'is_active', 'created_at', 'last_login')
    readonly_fields = ('email', 'is_active', 'created_at', 'last_login')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = (
        'national_id', 'last_name', 'first_name',
        'date_of_birth', 'gender', 'is_active', 'created_at',
    )
    list_filter = ('gender', 'is_active')
    search_fields = ('national_id', 'first_name', 'last_name', 'email', 'phone')
    readonly_fields = ('id', 'created_by', 'created_at', 'updated_at')
    inlines = [PortalAccountInline]

    fieldsets = (
        ('Identity', {
            'fields': ('id', 'national_id', 'first_name', 'last_name', 'date_of_birth', 'gender'),
        }),
        ('Contact', {
            'fields': ('phone', 'email', 'address'),
        }),
        ('Billing', {
            'fields': ('insurance_number',),
        }),
        ('Status', {
            'fields': ('is_active', 'created_by', 'created_at', 'updated_at'),
        }),
    )

    def has_delete_permission(self, request, obj=None):
        # Enforce BR-P2: no hard delete via admin either
        return False


@admin.register(PatientPortalAccount)
class PatientPortalAccountAdmin(admin.ModelAdmin):
    list_display = ('email', 'patient', 'is_active', 'created_at', 'last_login')
    list_filter = ('is_active',)
    readonly_fields = ('id', 'patient', 'email', 'created_by', 'created_at', 'updated_at', 'last_login')
    search_fields = ('email', 'patient__first_name', 'patient__last_name', 'patient__national_id')

    def has_add_permission(self, request):
        return False
