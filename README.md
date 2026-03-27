# Cytova Backend

Django + DRF REST API for the Cytova medical laboratory SaaS platform.

---

## Prerequisites

- Python 3.11+
- PostgreSQL 15+
- Redis 7+

---

## 1. Python Environment

```bash
cd backend

# Create virtual environment
python -m venv .venv

# Activate — Linux / macOS
source .venv/bin/activate

# Activate — Windows (PowerShell)
.venv\Scripts\Activate.ps1

# Activate — Windows (CMD)
.venv\Scripts\activate.bat

# Install dependencies
pip install -r requirements.txt
```

---

## 2. Environment Variables

```bash
cp .env.example .env
# Edit .env with your local values
```

Key variables to set for local development:

| Variable | Description | Default |
|---|---|---|
| `SECRET_KEY` | Django secret key | — (required) |
| `DB_NAME` | PostgreSQL database name | `cytova` |
| `DB_USER` | PostgreSQL user | `cytova_user` |
| `DB_PASSWORD` | PostgreSQL password | — (required) |
| `DB_HOST` | PostgreSQL host | `localhost` |
| `REDIS_URL` | Redis connection URL | `redis://localhost:6379/0` |

---

## 3. PostgreSQL Setup

```sql
-- Run as postgres superuser
CREATE USER cytova_user WITH PASSWORD 'cytova_password';
CREATE DATABASE cytova OWNER cytova_user;
GRANT ALL PRIVILEGES ON DATABASE cytova TO cytova_user;
```

The `cytova_user` must have permission to create schemas (required by django-tenants):

```sql
ALTER USER cytova_user CREATEDB;
```

---

## 4. Database Migrations

> **Important:** This project uses `django-tenants`. Standard `migrate` commands
> are replaced by `migrate_schemas`.

```bash
# Step 1: Generate migrations for all apps
python manage.py makemigrations tenants users audit

# Step 2: Run shared (public schema) migrations first
python manage.py migrate_schemas --shared

# Step 3: Create your first test tenant
python manage.py shell
```

```python
# In the Django shell — create a tenant for local development
from apps.tenants.models import Tenant, Domain

tenant = Tenant(
    schema_name='schema_laba',
    name='Lab A',
    subdomain='laba',
)
tenant.save()  # auto_create_schema=True creates the schema

Domain.objects.create(
    domain='laba.localhost',
    tenant=tenant,
    is_primary=True,
)
```

```bash
# Step 4: Run tenant migrations (applies to all existing tenant schemas)
python manage.py migrate_schemas
```

### Local DNS for Subdomains

Add entries to your hosts file so subdomains resolve locally:

**Linux / macOS** — `/etc/hosts`:
```
127.0.0.1  laba.localhost
127.0.0.1  labb.localhost
```

**Windows** — `C:\Windows\System32\drivers\etc\hosts`:
```
127.0.0.1  laba.localhost
127.0.0.1  labb.localhost
```

---

## 5. Running the Development Server

```bash
python manage.py runserver
```

Access the API at `http://laba.localhost:8000/api/v1/`

| URL | Description |
|---|---|
| `http://laba.localhost:8000/health/` | Health check |
| `http://laba.localhost:8000/api/v1/docs/` | Swagger UI |
| `http://laba.localhost:8000/api/v1/schema/` | OpenAPI schema (JSON) |
| `http://laba.localhost:8000/api/v1/auth/login/` | Obtain JWT tokens |
| `http://laba.localhost:8000/admin/` | Django admin |

---

## 6. Create a Superuser (for Django Admin)

```bash
python manage.py createsuperuser
```

Run this command **from a tenant context** by first switching the shell to the
correct schema, or use the Django shell:

```python
from django_tenants.utils import schema_context
from apps.users.models import StaffUser

with schema_context('schema_laba'):
    StaffUser.objects.create_superuser(
        email='admin@laba.io',
        password='SecurePass123!',
        first_name='Admin',
        last_name='User',
        role='LAB_ADMIN',
    )
```

---

## 7. Redis

```bash
# Start Redis locally (if not using Docker)
redis-server

# Or with Docker
docker run -d -p 6379:6379 redis:7-alpine
```

---

## 8. Celery Worker

Open a second terminal with the virtual environment activated:

```bash
# Start the Celery worker
celery -A config worker --loglevel=info

# Start Celery Beat (scheduled tasks — alerts, etc.)
celery -A config beat --loglevel=info --scheduler django_celery_beat.schedulers:DatabaseScheduler
```

Both the worker and beat scheduler must be running for alerts and background
tasks to function.

---

## 9. Docker Compose (Full Stack)

```bash
# Start everything (Postgres, Redis, API, Celery worker, Celery Beat)
docker compose up

# Rebuild after dependency changes
docker compose up --build

# Run in background
docker compose up -d
```

---

## 10. Running Tests

```bash
# All tests
pytest

# Single test file
pytest apps/users/tests/test_models.py

# Single test by name
pytest apps/users/tests/test_models.py::TestStaffUser::test_create_user

# With coverage
pytest --cov=apps --cov-report=term-missing
```

---

## 11. Code Quality

```bash
# Lint
flake8 .

# Format check
black --check .

# Apply formatting
black .

# Type check (optional)
mypy .
```

---

## Project Structure

```
backend/
├── config/
│   ├── settings/
│   │   ├── base.py       ← shared settings
│   │   ├── dev.py        ← development overrides
│   │   └── prod.py       ← production overrides
│   ├── urls.py           ← tenant URL conf
│   ├── urls_public.py    ← public schema URL conf (admin.cytova.io)
│   ├── celery.py         ← Celery app
│   ├── wsgi.py
│   └── asgi.py
├── apps/
│   ├── tenants/          ← Tenant + Domain models (public schema)
│   ├── users/            ← StaffUser model (per-tenant)
│   └── audit/            ← AuditLog model (per-tenant, append-only)
├── common/
│   ├── models.py         ← BaseModel, SoftDeleteModel
│   ├── pagination.py     ← CytovaCursorPagination
│   ├── exceptions.py     ← Custom DRF exception handler
│   ├── renderers.py      ← Envelope JSON renderer
│   ├── permissions.py    ← RBAC permission classes
│   ├── middleware.py     ← Tenant + Audit middleware
│   ├── urls.py           ← Health check
│   └── utils/
│       └── crypto.py     ← Token generation, hashing
├── manage.py
├── requirements.txt
└── .env.example
```

---

## Multi-Tenancy Notes

- Each laboratory is a **tenant** with its own isolated PostgreSQL schema.
- Tenant resolution happens via the `Host` header subdomain on every request.
- Use `migrate_schemas --shared` for shared (public) migrations and
  `migrate_schemas` for tenant migrations.
- Never use `migrate` directly — it will not apply tenant migrations correctly.
- When writing management commands or Celery tasks that span tenants, use
  `django_tenants.utils.schema_context(schema_name)` as a context manager.

---

## Environment Selection

The `DJANGO_SETTINGS_MODULE` environment variable selects the settings file:

| Value | Use |
|---|---|
| `config.settings.dev` | Local development (default) |
| `config.settings.prod` | Production |

Set it in your `.env` file or export it before running any command:

```bash
export DJANGO_SETTINGS_MODULE=config.settings.prod
```
