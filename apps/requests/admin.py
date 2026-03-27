from django.contrib import admin
from .models import AnalysisRequest, AnalysisRequestItem, ExamTraceability


class AnalysisRequestItemInline(admin.TabularInline):
    model = AnalysisRequestItem
    extra = 0
    fields = (
        'exam_definition', 'status', 'execution_mode',
        'unit_price', 'billed_price', 'rejection_reason',
    )
    readonly_fields = (
        'exam_definition', 'status', 'execution_mode',
        'unit_price', 'billed_price',
    )
    show_change_link = True
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(AnalysisRequest)
class AnalysisRequestAdmin(admin.ModelAdmin):
    list_display = (
        'request_number', 'patient', 'status',
        'confirmed_at', 'created_by', 'created_at',
    )
    list_filter = ('status',)
    search_fields = ('request_number', 'patient__first_name', 'patient__last_name',
                     'patient__national_id')
    readonly_fields = (
        'id', 'request_number', 'patient', 'status',
        'confirmed_at', 'confirmed_by',
        'cancelled_at', 'cancelled_by',
        'created_by', 'created_at', 'updated_at',
    )
    inlines = [AnalysisRequestItemInline]

    def has_delete_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False


class ExamTraceabilityInline(admin.StackedInline):
    model = ExamTraceability
    extra = 0
    fields = (
        'sample_received_at', 'sample_received_by',
        'analysis_completed_at', 'performed_by',
    )
    readonly_fields = (
        'sample_received_at', 'sample_received_by',
        'analysis_completed_at', 'performed_by',
    )
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(AnalysisRequestItem)
class AnalysisRequestItemAdmin(admin.ModelAdmin):
    list_display = (
        'analysis_request', 'exam_definition', 'status',
        'execution_mode', 'unit_price', 'billed_price',
    )
    list_filter = ('status', 'execution_mode')
    search_fields = (
        'analysis_request__request_number',
        'exam_definition__code', 'exam_definition__name',
    )
    readonly_fields = (
        'id', 'analysis_request', 'exam_definition',
        'unit_price', 'billed_price', 'pricing_rule',
        'created_at', 'updated_at',
    )
    inlines = [ExamTraceabilityInline]

    def has_delete_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False
