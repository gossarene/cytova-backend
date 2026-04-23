from django.contrib import admin

from .models import LabelPrintPreset


@admin.register(LabelPrintPreset)
class LabelPrintPresetAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'print_mode', 'is_active', 'is_system')
    list_filter = ('print_mode', 'is_active', 'is_system')
    search_fields = ('name', 'code')
    readonly_fields = ('created_at', 'updated_at')
