"""
Cytova — Cursor-Based Pagination

All list endpoints use cursor pagination. Rationale:
- Stable under concurrent inserts (no page-drift when new records arrive)
- Efficient at any depth (no OFFSET scan)
- Mobile-friendly: clients follow next_cursor, no arithmetic needed
"""
from rest_framework.pagination import CursorPagination
from rest_framework.response import Response


class CytovaCursorPagination(CursorPagination):
    page_size = 20
    max_page_size = 100
    page_size_query_param = 'page_size'
    ordering = '-created_at'
    cursor_query_param = 'cursor'

    def get_paginated_response(self, data):
        return Response({
            'data': data,
            'meta': {
                'pagination': {
                    'count': len(data),
                    'next_cursor': self.get_next_link(),
                    'previous_cursor': self.get_previous_link(),
                    'has_next': self.get_next_link() is not None,
                    'has_previous': self.get_previous_link() is not None,
                }
            },
            'errors': [],
        })

    def get_paginated_response_schema(self, schema):
        """OpenAPI schema for paginated responses."""
        return {
            'type': 'object',
            'required': ['data', 'meta', 'errors'],
            'properties': {
                'data': schema,
                'meta': {
                    'type': 'object',
                    'properties': {
                        'pagination': {
                            'type': 'object',
                            'properties': {
                                'count': {'type': 'integer'},
                                'next_cursor': {'type': 'string', 'nullable': True},
                                'previous_cursor': {'type': 'string', 'nullable': True},
                                'has_next': {'type': 'boolean'},
                                'has_previous': {'type': 'boolean'},
                            },
                        }
                    },
                },
                'errors': {'type': 'array', 'items': {}},
            },
        }
