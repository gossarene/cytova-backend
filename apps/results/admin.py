from django.contrib import admin
from .models import ExamResult, ResultFile


class ResultFileInline(admin.TabularInline):
    model = ResultFile
    extra = 0
    # file_key is intentionally excluded from the admin UI
    fields = ('original_filename', 'mime_type', 'file_size', 'uploaded_by', 'created_at')
    readonly_fields = ('original_filename', 'mime_type', 'file_size', 'uploaded_by', 'created_at')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ExamResult)
class ExamResultAdmin(admin.ModelAdmin):
    list_display = (
        'item', 'status', 'is_abnormal',
        'result_value', 'validated_at', 'published_at', 'created_at',
    )
    list_filter = ('status', 'is_abnormal')
    search_fields = (
        'item__analysis_request__request_number',
        'item__exam_definition__code',
        'item__exam_definition__name',
    )
    readonly_fields = (
        'id', 'item',
        'status',
        'validated_by', 'validated_at',
        'published_by', 'published_at',
        'created_by', 'created_at', 'updated_at',
    )
    inlines = [ResultFileInline]
    fieldsets = (
        ('Identity', {
            'fields': ('id', 'item', 'status'),
        }),
        ('Result data', {
            'fields': (
                'result_value', 'result_unit', 'reference_range',
                'is_abnormal', 'comments', 'internal_notes',
            ),
        }),
        ('Validation', {
            'fields': ('validation_notes', 'validated_by', 'validated_at'),
        }),
        ('Publication', {
            'fields': ('published_by', 'published_at'),
        }),
        ('Audit', {
            'fields': ('created_by', 'created_at', 'updated_at'),
        }),
    )

    def has_delete_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False
