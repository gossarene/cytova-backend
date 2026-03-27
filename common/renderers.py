"""
Cytova — JSON Renderer

Wraps all success responses in the standard envelope:
    { "data": ..., "meta": null, "errors": [] }

Responses already in envelope format (from the paginator or views that
explicitly return the envelope) are passed through unchanged.
Error responses are already wrapped by the exception handler — not touched here.
"""
from rest_framework.renderers import JSONRenderer

_ENVELOPE_KEYS = frozenset({'data', 'meta', 'errors'})


class CytovaJSONRenderer(JSONRenderer):

    def render(self, data, accepted_media_type=None, renderer_context=None):
        response = renderer_context.get('response') if renderer_context else None

        # Error responses are already in envelope format (handled by the
        # exception handler). Pass through without re-wrapping.
        if response is not None and response.status_code >= 400:
            return super().render(data, accepted_media_type, renderer_context)

        # Paginated and explicitly-enveloped responses are already wrapped.
        if isinstance(data, dict) and data.keys() == _ENVELOPE_KEYS:
            return super().render(data, accepted_media_type, renderer_context)

        # Wrap single-resource and simple responses.
        wrapped = {
            'data': data,
            'meta': None,
            'errors': [],
        }
        return super().render(wrapped, accepted_media_type, renderer_context)
