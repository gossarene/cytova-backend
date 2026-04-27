"""
Cytova — Production Settings
Requires all environment variables to be explicitly set.
"""
from .base import *  # noqa: F401, F403
from decouple import config, Csv

DEBUG = False

ALLOWED_HOSTS = config('ALLOWED_HOSTS', cast=Csv())

# ---------------------------------------------------------------------------
# Security hardening
# ---------------------------------------------------------------------------
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000          # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_BROWSER_XSS_FILTER = True
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Strict'
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = True

# ---------------------------------------------------------------------------
# CORS — production
#
# Two lists working together:
#
#   1. CORS_ALLOWED_ORIGINS — explicit, env-driven. Use for custom domains or
#      enterprise white-label hosts that don't live under cytova.io.
#
#   2. CORS_ALLOWED_ORIGIN_REGEXES — pinned to HTTPS direct subdomains of
#      cytova.io. New tenants are reachable as soon as DNS is configured —
#      no settings update or redeploy required. The regex deliberately:
#        * requires https (no plain http in prod)
#        * forbids ports (production traffic is on 443, behind the proxy)
#        * forbids multi-level subdomains (only `<slug>.cytova.io`)
#        * uses DNS-label characters only ([a-z0-9-])
#        * does NOT match the apex domain `cytova.io` itself
#
# Wildcard origins (CORS_ALLOW_ALL_ORIGINS) are never set in production.
# When the frontend and API share an origin behind the reverse proxy, CORS
# is moot and these settings are a no-op — leaving them on is harmless and
# covers cross-origin tooling (admin portal, embedded results viewer, etc.).
# ---------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = config('CORS_ALLOWED_ORIGINS', cast=Csv(), default='')
CORS_ALLOWED_ORIGIN_REGEXES = [
    r'^https://[a-z0-9-]+\.cytova\.io$',
]

# ---------------------------------------------------------------------------
# JWT — RS256 asymmetric signing in production
# Store private/public keys in secrets manager; inject as env vars.
# Use \n in env var value to represent newlines.
# ---------------------------------------------------------------------------
SIMPLE_JWT = {
    **SIMPLE_JWT,  # noqa: F405
    'ALGORITHM': 'RS256',
    'SIGNING_KEY': config('JWT_PRIVATE_KEY').replace('\\n', '\n'),
    'VERIFYING_KEY': config('JWT_PUBLIC_KEY').replace('\\n', '\n'),
}

# ---------------------------------------------------------------------------
# File storage — private S3 / MinIO bucket
# ---------------------------------------------------------------------------
if USE_S3:  # noqa: F405
    DEFAULT_FILE_STORAGE = 'storages.backends.s3boto3.S3Boto3Storage'
    AWS_STORAGE_BUCKET_NAME = config('STORAGE_BUCKET_NAME')
    AWS_ACCESS_KEY_ID = config('STORAGE_ACCESS_KEY')
    AWS_SECRET_ACCESS_KEY = config('STORAGE_SECRET_KEY')
    AWS_S3_REGION_NAME = config('STORAGE_REGION', default='eu-west-1')
    AWS_DEFAULT_ACL = 'private'             # No public access ever
    AWS_QUERYSTRING_AUTH = True             # Signed URLs required
    AWS_QUERYSTRING_EXPIRE = 900            # 15 minutes
    AWS_S3_FILE_OVERWRITE = False           # Never silently overwrite
    AWS_S3_CUSTOM_DOMAIN = None             # No CDN for private files
    AWS_S3_ENDPOINT_URL = config('STORAGE_ENDPOINT_URL', default=None)  # MinIO
    AWS_S3_OBJECT_PARAMETERS = {
        'ServerSideEncryption': 'AES256',
    }

# ---------------------------------------------------------------------------
# Static files — WhiteNoise for efficient serving
# ---------------------------------------------------------------------------
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# ---------------------------------------------------------------------------
# Email — SMTP
# ---------------------------------------------------------------------------
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = config('EMAIL_HOST')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_HOST_USER = config('EMAIL_HOST_USER')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD')
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)

# ---------------------------------------------------------------------------
# Logging — structured JSON for log aggregation (Datadog, CloudWatch, etc.)
# ---------------------------------------------------------------------------
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'json': {
            'format': (
                '{"time": "%(asctime)s", "level": "%(levelname)s", '
                '"logger": "%(name)s", "message": "%(message)s"}'
            ),
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'json',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
        'django.security': {
            'handlers': ['console'],
            'level': 'ERROR',
            'propagate': False,
        },
        'celery': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
}
