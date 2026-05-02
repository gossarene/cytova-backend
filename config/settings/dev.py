"""
Cytova — Development Settings
Local development only. Never use in production.
"""
from .base import *  # noqa: F401, F403

DEBUG = True

ALLOWED_HOSTS = ['*']

# ---------------------------------------------------------------------------
# CORS — local development origins.
#
# Wildcard origins (CORS_ALLOW_ALL_ORIGINS) are intentionally NOT used. Even
# in dev, the allowlist is restricted to known dev hostnames so that a
# rogue page on the network can't drive the dev backend.
# ---------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = [
    'http://localhost:5173',   # Vite default
    'http://localhost:3000',
    'http://localhost:8080',
]
CORS_ALLOWED_ORIGIN_REGEXES = [
    # Direct localhost — platform/signup dev.
    r'^http://localhost(:\d+)?$',
    r'^http://127\.0\.0\.1(:\d+)?$',

    # *.localhost — legacy dev tenant hosts.
    r'^http://[a-z0-9-]+\.localhost(:\d+)?$',

    # *.cytova.io — local multi-tenant dev.
    r'^http://[a-z0-9-]+\.cytova\.io(:\d+)?$',
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
# Throttling — disabled in development (avoids Redis dependency)
# The default cache backend uses Redis which may not be running locally.
# Each throttle check attempts a Redis connection with a 5-second timeout,
# causing ~8-10 second delays per request when Redis is unreachable.
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    **REST_FRAMEWORK,  # noqa: F405
    'DEFAULT_THROTTLE_CLASSES': [],  # Disable throttling entirely in dev
    'DEFAULT_THROTTLE_RATES': {
        'anon': '1000/hour',
        'user': '10000/hour',
        'auth_login': '100/minute',
        'auth_signup': '5/hour',
        'slug_check': '30/hour',
        # Dev/tests use a generous cap; the prod default is in base.py.
        'notify_cytova': '1000/hour',
        'link_cytova_identity': '1000/hour',
    },
}

# ---------------------------------------------------------------------------
# Cache — use in-memory cache for development (no Redis dependency)
# ---------------------------------------------------------------------------
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'cytova-dev',
    }
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
