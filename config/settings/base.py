"""
Cytova — Base Django Settings
Shared across all environments. Never used directly; import dev.py or prod.py.
"""
from pathlib import Path
from datetime import timedelta
from decouple import config, Csv

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
SECRET_KEY = config('SECRET_KEY')
DEBUG = config('DEBUG', default=False, cast=bool)
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1', cast=Csv())

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ---------------------------------------------------------------------------
# Applications — django-tenants requires SHARED_APPS + TENANT_APPS structure
# ---------------------------------------------------------------------------

# SHARED_APPS: installed in the public (global) schema only.
SHARED_APPS = [
    # django-tenants must be first
    'django_tenants',

    # Tenant registry lives in the public schema
    'apps.tenants',

    # Platform-managed label print presets (shared catalog)
    'apps.labels',

    # Standard Django shared apps
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'django.contrib.staticfiles',
]

# TENANT_APPS: installed in every tenant's private schema.
TENANT_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'django.contrib.admin',
    'django.contrib.sessions',
    'django.contrib.messages',

    # Domain apps (per-tenant)
    'apps.authentication',
    'apps.users',
    'apps.patients',
    'apps.catalog',
    'apps.requests',
    'apps.results',
    'apps.stock',
    'apps.suppliers',
    'apps.procurement',  # Thin routing app — no models, re-exports suppliers views
    'apps.partners',
    'apps.invoicing',
    'apps.alerts',
    'apps.dashboard',
    'apps.audit',
    'apps.lab_settings',

    # JWT token blacklist is per-tenant (tokens are tenant-scoped)
    'rest_framework_simplejwt.token_blacklist',

    # Infrastructure apps (no per-tenant DB models, but listed here
    # so they are available in tenant request context)
    'rest_framework',
    'corsheaders',
    'drf_spectacular',
    'django_filters',
]

# INSTALLED_APPS is the union; shared apps take precedence for routing.
INSTALLED_APPS = list(SHARED_APPS) + [
    app for app in TENANT_APPS if app not in SHARED_APPS
]

TENANT_MODEL = 'tenants.Tenant'
TENANT_DOMAIN_MODEL = 'tenants.Domain'

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
MIDDLEWARE = [
    # Tenant resolution must be the very first middleware.
    'common.middleware.CytovaTenantMiddleware',
    # Subscription check must be immediately after tenant resolution.
    'common.middleware.SubscriptionEnforcementMiddleware',

    'django.middleware.security.SecurityMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',

    # Captures IP, user agent, and request ID for audit logging.
    'common.middleware.AuditContextMiddleware',
]

# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------
# Tenant subdomains use the standard URL conf.
ROOT_URLCONF = 'config.urls'
# The public schema (admin.cytova.io) uses its own URL conf.
PUBLIC_SCHEMA_URLCONF = 'config.urls_public'

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

# ---------------------------------------------------------------------------
# Database — django-tenants requires its own PostgreSQL backend
# ---------------------------------------------------------------------------
DATABASES = {
    'default': {
        'ENGINE': 'django_tenants.postgresql_backend',
        'NAME': config('DB_NAME', default='cytova'),
        'USER': config('DB_USER', default='cytova_user'),
        'PASSWORD': config('DB_PASSWORD', default=''),
        'HOST': config('DB_HOST', default='localhost'),
        'PORT': config('DB_PORT', default='5432'),
        'CONN_MAX_AGE': config('DB_CONN_MAX_AGE', default=60, cast=int),
    }
}

DATABASE_ROUTERS = ['django_tenants.routers.TenantSyncRouter']

# ---------------------------------------------------------------------------
# Cache — Redis via django-redis
# ---------------------------------------------------------------------------
CACHES = {
    'default': {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': config('REDIS_URL', default='redis://localhost:6379/0'),
        'OPTIONS': {
            'CLIENT_CLASS': 'django_redis.client.DefaultClient',
            'SOCKET_CONNECT_TIMEOUT': 5,
            'SOCKET_TIMEOUT': 5,
            'CONNECTION_POOL_KWARGS': {'max_connections': 50},
            'IGNORE_EXCEPTIONS': True,  # Degrade gracefully if Redis is down
        },
        'KEY_PREFIX': 'cytova',
        'TIMEOUT': 300,
    }
}

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
AUTH_USER_MODEL = 'users.StaffUser'

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {'min_length': 12},
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# ---------------------------------------------------------------------------
# Internationalisation
# ---------------------------------------------------------------------------
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static & media files
# ---------------------------------------------------------------------------
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Media — overridden in production to use S3.
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

USE_S3 = config('USE_S3', default=False, cast=bool)

# ---------------------------------------------------------------------------
# File storage — result attachments (PDFs, scan images)
# In development: local FileSystemStorage (MEDIA_ROOT).
# In production:  set USE_S3=True and configure the variables below.
# ---------------------------------------------------------------------------
AWS_ACCESS_KEY_ID = config('AWS_ACCESS_KEY_ID', default='')
AWS_SECRET_ACCESS_KEY = config('AWS_SECRET_ACCESS_KEY', default='')
AWS_STORAGE_BUCKET_NAME = config('AWS_STORAGE_BUCKET_NAME', default='cytova-results')
AWS_S3_ENDPOINT_URL = config('AWS_S3_ENDPOINT_URL', default=None)  # MinIO endpoint
AWS_S3_REGION_NAME = config('AWS_S3_REGION_NAME', default='us-east-1')

# Signed URL TTL in seconds (15 minutes default)
RESULT_FILE_SIGNED_URL_EXPIRY = 900

# Upload constraints
RESULT_FILE_MAX_SIZE = 20 * 1024 * 1024  # 20 MB
RESULT_FILE_ALLOWED_MIME_TYPES = [
    'application/pdf',
    'image/jpeg',
    'image/png',
    'image/tiff',
]

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_RENDERER_CLASSES': [
        'common.renderers.CytovaJSONRenderer',
    ],
    'DEFAULT_PARSER_CLASSES': [
        'rest_framework.parsers.JSONParser',
        'rest_framework.parsers.MultiPartParser',
    ],
    'DEFAULT_PAGINATION_CLASS': 'common.pagination.CytovaCursorPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
    'EXCEPTION_HANDLER': 'common.exceptions.cytova_exception_handler',
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '50/hour',
        'user': '1000/hour',
        'auth_login': '5/minute',
        'auth_signup': '5/hour',
        'slug_check': '30/hour',
    },
}

# ---------------------------------------------------------------------------
# JWT — djangorestframework-simplejwt
# Algorithm is HS256 by default (overridden to RS256 in prod.py).
# ---------------------------------------------------------------------------
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=15),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=30),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': True,

    'ALGORITHM': config('JWT_ALGORITHM', default='HS256'),
    'SIGNING_KEY': config('JWT_SIGNING_KEY', default=SECRET_KEY),
    'VERIFYING_KEY': config('JWT_PUBLIC_KEY', default=None),

    'AUDIENCE': 'cytova-api',
    'ISSUER': 'cytova',

    'AUTH_HEADER_TYPES': ('Bearer',),
    'AUTH_HEADER_NAME': 'HTTP_AUTHORIZATION',

    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'sub',
    'TOKEN_TYPE_CLAIM': 'token_type',
    'JTI_CLAIM': 'jti',

    'TOKEN_OBTAIN_SERIALIZER': 'rest_framework_simplejwt.serializers.TokenObtainPairSerializer',
    'TOKEN_REFRESH_SERIALIZER': 'rest_framework_simplejwt.serializers.TokenRefreshSerializer',
    'TOKEN_BLACKLIST_SERIALIZER': 'rest_framework_simplejwt.serializers.TokenBlacklistSerializer',

    # Use CytovaAccessToken so role + email claims are included in all issued tokens.
    'AUTH_TOKEN_CLASSES': ('apps.authentication.tokens.CytovaAccessToken',),
}

# ---------------------------------------------------------------------------
# CORS — django-cors-headers
# Specific origins configured in dev.py and prod.py.
# ---------------------------------------------------------------------------
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOWED_HEADERS = [
    'accept',
    'authorization',
    'content-type',
    'x-request-id',
    'accept-language',
]
CORS_EXPOSE_HEADERS = [
    'x-request-id',
    'x-ratelimit-limit',
    'x-ratelimit-remaining',
    'x-ratelimit-reset',
]

# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = config('CELERY_BROKER_URL', default='redis://localhost:6379/1')
CELERY_RESULT_BACKEND = config('CELERY_RESULT_BACKEND', default='redis://localhost:6379/2')
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'UTC'
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60        # Hard limit: 30 minutes
CELERY_TASK_SOFT_TIME_LIMIT = 25 * 60   # Soft limit: 25 minutes (raises SoftTimeLimitExceeded)
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'
CELERY_WORKER_PREFETCH_MULTIPLIER = 1   # Fair task distribution
CELERY_TASK_ACKS_LATE = True            # Re-queue on worker crash

# ---------------------------------------------------------------------------
# Security headers (reinforced in prod.py)
# ---------------------------------------------------------------------------
X_FRAME_OPTIONS = 'DENY'
SECURE_CONTENT_TYPE_NOSNIFF = True
REFERRER_POLICY = 'strict-origin-when-cross-origin'

# ---------------------------------------------------------------------------
# API documentation — drf-spectacular
# ---------------------------------------------------------------------------
SPECTACULAR_SETTINGS = {
    'TITLE': 'Cytova API',
    'DESCRIPTION': 'REST API for Cytova — Medical Laboratory SaaS Platform',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'SCHEMA_PATH_PREFIX': '/api/v1/',
    'COMPONENT_SPLIT_REQUEST': True,
    'ENUM_GENERATE_CHOICE_DESCRIPTION': False,
}

# ---------------------------------------------------------------------------
# Email (base defaults — overridden per environment)
# ---------------------------------------------------------------------------
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='noreply@cytova.io')
SERVER_EMAIL = config('SERVER_EMAIL', default='errors@cytova.io')

# ---------------------------------------------------------------------------
# Platform settings
# ---------------------------------------------------------------------------
CYTOVA_DOMAIN = config('CYTOVA_DOMAIN', default='cytova.io')

# Allow requests with no matching tenant to fall through to the public schema
# (useful in development). Set to False in production.
SHOW_PUBLIC_IF_NO_TENANT_FOUND = config(
    'SHOW_PUBLIC_IF_NO_TENANT_FOUND', default=True, cast=bool
)

# ---------------------------------------------------------------------------
# Inventory Alerts
# ---------------------------------------------------------------------------
ALERT_EXPIRY_WARNING_DAYS = config('ALERT_EXPIRY_WARNING_DAYS', default=30, cast=int)

# ---------------------------------------------------------------------------
# Platform / Dashboard
# ---------------------------------------------------------------------------
# Maximum rows returned by dashboard "top-N" queries (e.g. top partners).
DASHBOARD_TOP_N_LIMIT = 20

# Subscription enforcement — tenant paths exempt from subscription checks.
# Auth endpoints must remain accessible so tenants can obtain tokens even
# when their subscription is expired.
SUBSCRIPTION_EXEMPT_PATH_PREFIXES = [
    '/health/',
    '/api/v1/auth/',
]

# Maximum alert IDs accepted in a single bulk-acknowledge request.
ALERT_BULK_ACKNOWLEDGE_MAX = 200

# Platform dashboard: trial expiry warning window (days).
PLATFORM_TRIAL_WARNING_DAYS = 7
