from django.contrib import admin
from .models import ExamCategory, ExamDefinition, LabExamSettings, PricingRule


class ExamDefinitionInline(admin.TabularInline):
    model = ExamDefinition
    extra = 0
    fields = ('code', 'name', 'sample_type', 'unit_price', 'is_active')
    readonly_fields = ('code', 'name', 'sample_type', 'unit_price', 'is_active')
    show_change_link = True
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ExamCategory)
class ExamCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'display_order', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name',)
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('display_order', 'name')
    inlines = [ExamDefinitionInline]

    def has_delete_permission(self, request, obj=None):
        return False


class LabExamSettingsInline(admin.StackedInline):
    model = LabExamSettings
    extra = 0
    fields = ('reference_range', 'turnaround_hours_override', 'is_enabled', 'internal_notes', 'updated_by', 'updated_at')
    readonly_fields = ('updated_at',)
    can_delete = False


class PricingRuleInline(admin.TabularInline):
    model = PricingRule
    extra = 0
    fields = ('pricing_type', 'value', 'partner_organization', 'source_type', 'priority', 'is_active', 'start_date', 'end_date')
    readonly_fields = ('pricing_type', 'value', 'partner_organization', 'source_type', 'priority', 'is_active', 'start_date', 'end_date')
    show_change_link = True
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ExamDefinition)
class ExamDefinitionAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'category', 'sample_type', 'unit_price', 'is_active', 'created_at')
    list_filter = ('category', 'sample_type', 'is_active')
    search_fields = ('code', 'name')
    readonly_fields = ('id', 'created_at', 'updated_at')
    inlines = [LabExamSettingsInline, PricingRuleInline]

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LabExamSettings)
class LabExamSettingsAdmin(admin.ModelAdmin):
    list_display = ('exam_definition', 'is_enabled', 'turnaround_hours_override', 'updated_at')
    list_filter = ('is_enabled',)
    readonly_fields = ('id', 'updated_at')


@admin.register(PricingRule)
class PricingRuleAdmin(admin.ModelAdmin):
    list_display = ('exam_definition', 'pricing_type', 'value', 'partner_organization', 'source_type', 'priority', 'is_active', 'created_at')
    list_filter = ('pricing_type', 'is_active', 'exam_definition__category')
    search_fields = ('exam_definition__code', 'exam_definition__name', 'notes')
    readonly_fields = ('id', 'created_by', 'created_at', 'updated_at')
