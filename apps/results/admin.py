from django.contrib import admin
from .models import ResultVersion, ResultFile


class ResultFileInline(admin.TabularInline):
    model = ResultFile
    extra = 0
    fields = ('original_filename', 'mime_type', 'file_size', 'uploaded_by', 'created_at')
    readonly_fields = ('original_filename', 'mime_type', 'file_size', 'uploaded_by', 'created_at')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ResultVersion)
class ResultVersionAdmin(admin.ModelAdmin):
    list_display = (
        'item', 'version_number', 'is_current', 'status', 'is_abnormal',
        'result_value', 'entered_at', 'submitted_at', 'validated_at',
        'published_at', 'created_at',
    )
    list_filter = ('status', 'is_current', 'is_abnormal')
    search_fields = (
        'item__analysis_request__request_number',
        'item__exam_definition__code',
        'item__exam_definition__name',
    )
    readonly_fields = (
        'id', 'item', 'version_number', 'is_current',
        'status',
        'entered_by', 'entered_at',
        'submitted_by', 'submitted_at',
        'validated_by', 'validated_at',
        'rejected_by', 'rejected_at',
        'published_by', 'published_at',
        'created_at', 'updated_at',
    )
    inlines = [ResultFileInline]
    fieldsets = (
        ('Identity', {
            'fields': ('id', 'item', 'version_number', 'is_current', 'status'),
        }),
        ('Result data', {
            'fields': (
                'result_value', 'result_unit', 'reference_range',
                'is_abnormal', 'comments', 'internal_notes', 'notes',
            ),
        }),
        ('Entry', {
            'fields': ('entered_by', 'entered_at'),
        }),
        ('Submission', {
            'fields': ('submitted_by', 'submitted_at'),
        }),
        ('Validation', {
            'fields': ('validation_notes', 'validated_by', 'validated_at'),
        }),
        ('Rejection', {
            'fields': ('rejection_notes', 'rejected_by', 'rejected_at'),
        }),
        ('Publication', {
            'fields': ('published_by', 'published_at'),
        }),
        ('Audit', {
            'fields': ('created_at', 'updated_at'),
        }),
    )

    def has_delete_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False
