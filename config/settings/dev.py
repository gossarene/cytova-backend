"""
Cytova — Development Settings
Local development only. Never use in production.
"""
from .base import *  # noqa: F401, F403

DEBUG = True

ALLOWED_HOSTS = ['*']

# ---------------------------------------------------------------------------
# CORS — allow all local origins in development
# ---------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = [
    'http://localhost:5173',   # Vite default
    'http://localhost:3000',
    'http://localhost:8080',
]
CORS_ALLOWED_ORIGIN_REGEXES = [
    r'^http://.*\.localhost(:\d+)?$',  # any *.localhost subdomain
]

# ---------------------------------------------------------------------------
# JWT — HS256 for local development (no RSA key pair needed)
# ---------------------------------------------------------------------------
SIMPLE_JWT = {
    **SIMPLE_JWT,  # noqa: F405
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': SECRET_KEY,  # noqa: F405
    'VERIFYING_KEY': None,
}

# ---------------------------------------------------------------------------
# Throttling — relaxed for development
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    **REST_FRAMEWORK,  # noqa: F405
    'DEFAULT_THROTTLE_RATES': {
        'anon': '1000/hour',
        'user': '10000/hour',
        'auth_login': '100/15min',
    },
}

# ---------------------------------------------------------------------------
# File storage — local filesystem in development
# ---------------------------------------------------------------------------
DEFAULT_FILE_STORAGE = 'django.core.files.storage.FileSystemStorage'

# ---------------------------------------------------------------------------
# Email — print to console
# ---------------------------------------------------------------------------
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# ---------------------------------------------------------------------------
# Logging — verbose console output
# ---------------------------------------------------------------------------
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[{levelname}] {asctime} {name} — {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'DEBUG',
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        'django.db.backends': {
            'handlers': ['console'],
            'level': 'WARNING',  # Set to DEBUG to log all SQL queries
            'propagate': False,
        },
        'django_tenants': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
        'celery': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}
