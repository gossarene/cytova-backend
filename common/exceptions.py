"""
Cytova — Custom DRF Exception Handler

Transforms all error responses into the standard Cytova envelope:
    { "data": null, "meta": null, "errors": [...] }

Each error object:
    { "code": "REQUIRED", "message": "...", "field": "email", "detail": {} }
"""
import logging
from rest_framework.views import exception_handler as drf_exception_handler
from rest_framework.exceptions import (
    ValidationError,
    AuthenticationFailed,
    NotAuthenticated,
    PermissionDenied,
    NotFound,
    MethodNotAllowed,
    Throttled,
    UnsupportedMediaType,
    ParseError,
)

logger = logging.getLogger(__name__)

# Maps DRF exception types to our machine-readable error codes.
_EXCEPTION_CODE_MAP = {
    AuthenticationFailed: 'AUTHENTICATION_FAILED',
    NotAuthenticated: 'TOKEN_INVALID',
    PermissionDenied: 'PERMISSION_DENIED',
    NotFound: 'NOT_FOUND',
    MethodNotAllowed: 'METHOD_NOT_ALLOWED',
    Throttled: 'RATE_LIMITED',
    UnsupportedMediaType: 'INVALID_FORMAT',
    ParseError: 'INVALID_FORMAT',
}

# Maps DRF validation error codes to our uppercase conventions.
_CODE_NORMALISATION = {
    'required': 'REQUIRED',
    'blank': 'REQUIRED',
    'null': 'REQUIRED',
    'invalid': 'INVALID_FORMAT',
    'invalid_choice': 'INVALID_VALUE',
    'unique': 'DUPLICATE',
    'unique_for_date': 'DUPLICATE',
    'does_not_exist': 'NOT_FOUND',
    'incorrect_type': 'INVALID_FORMAT',
    'max_length': 'INVALID_VALUE',
    'min_length': 'INVALID_VALUE',
    'max_value': 'INVALID_VALUE',
    'min_value': 'INVALID_VALUE',
    'max_decimal_places': 'INVALID_VALUE',
    'max_digits': 'INVALID_VALUE',
    'date': 'INVALID_FORMAT',
    'datetime': 'INVALID_FORMAT',
    'invalid_image': 'INVALID_FORMAT',
}


def _normalise_code(raw_code) -> str:
    code_str = str(raw_code) if raw_code else ''
    return _CODE_NORMALISATION.get(code_str, code_str.upper() or 'VALIDATION_ERROR')


def _parse_validation_detail(detail, field_prefix: str = '') -> list:
    """
    Recursively flatten DRF's nested validation error detail into a list of
    flat error objects compatible with the Cytova error format.
    """
    errors = []

    if isinstance(detail, list):
        for item in detail:
            if isinstance(item, dict):
                errors.extend(_parse_validation_detail(item, field_prefix))
            else:
                errors.append({
                    'code': _normalise_code(getattr(item, 'code', None)),
                    'message': str(item),
                    'field': field_prefix or None,
                    'detail': {},
                })

    elif isinstance(detail, dict):
        for field, messages in detail.items():
            # 'non_field_errors' maps to top-level (no field)
            if field == 'non_field_errors':
                prefix = field_prefix or None
            else:
                prefix = f'{field_prefix}.{field}' if field_prefix else field
            errors.extend(_parse_validation_detail(messages, prefix or ''))

    else:
        errors.append({
            'code': _normalise_code(getattr(detail, 'code', None)),
            'message': str(detail),
            'field': field_prefix or None,
            'detail': {},
        })

    return errors


def cytova_exception_handler(exc, context):
    """
    Central exception handler registered in REST_FRAMEWORK settings.
    Wraps every error response in the Cytova envelope format.
    """
    response = drf_exception_handler(exc, context)

    if response is None:
        # Unhandled exception — Django will return a 500. Log it.
        logger.exception('Unhandled exception', exc_info=exc)
        return None

    errors = []

    if isinstance(exc, ValidationError):
        errors = _parse_validation_detail(exc.detail)
    else:
        code = _EXCEPTION_CODE_MAP.get(type(exc), 'ERROR')
        if hasattr(exc, 'detail'):
            message = (
                str(exc.detail)
                if not isinstance(exc.detail, (list, dict))
                else str(exc.default_detail)
            )
        else:
            message = str(exc)
        errors = [{'code': code, 'message': message, 'field': None, 'detail': {}}]

    response.data = {
        'data': None,
        'meta': None,
        'errors': errors,
    }

    return response
