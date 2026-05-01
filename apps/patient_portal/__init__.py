"""
Cytova — Patient Portal (global patient identity)

Why patient accounts are *global*
---------------------------------
A Cytova patient is a person, not an artefact of a particular
laboratory. The same human routinely interacts with multiple labs across
their life — referrals, second opinions, moving cities — and the portal
must let them log in once, see all their results across labs, and prove
identity using a single stable Cytova Patient ID
(``CV-XXXX-XXXX``). Per-tenant patient login would require a separate
account per lab, which collapses the value proposition of the portal.

This is intentionally distinct from ``apps.patients.Patient``, which
records a *patient's relationship with one specific lab* — their
medical record, document numbers, requests, and results — and lives in
each lab's private tenant schema. The two models are joined by the
human, not by a database FK: the lab-side ``Patient`` may reference a
portal account (or not, for walk-ins).

Schema strategy (and why it's the public schema for now)
--------------------------------------------------------
Strict requirement: portal account tables MUST NOT live inside any
lab tenant schema. ``django-tenants`` offers two native registration
points: ``SHARED_APPS`` (public schema only) and ``TENANT_APPS`` (every
tenant schema). Adding the portal app to ``TENANT_APPS`` would create
the same tables in every lab schema — the exact thing we forbid.

A *third*, dedicated ``patients`` schema (matching the Cytova
deployment narrative) requires either a custom ``DATABASE_ROUTERS``
entry that swaps ``search_path`` for portal models, or a separate
``migrate_schemas``-style management command that runs the portal
migrations against a fixed ``set search_path = patients``. Both are
buildable but invasive: they touch the test conftest, the CI migrate
step, and the runtime middleware. Per the strict rules
("if too risky in this step, implement in public schema temporarily
with clear TODO comments"), the foundation lives in ``SHARED_APPS``
today — tables in ``public`` only, never in lab tenants.

The migration to a dedicated ``patients`` schema is mechanical when we
choose to do it:

  1. Add a ``DATABASE_ROUTERS`` entry that returns ``False`` for
     ``allow_migrate(db, app_label='patient_portal')`` outside the
     ``patients`` schema.
  2. Add a one-shot management command that wraps the existing
     ``migrate`` flow in a ``set search_path = patients,public`` cursor.
  3. Update ``conftest.py`` to create the ``patients`` schema once per
     session, alongside the lab tenant schema.
  4. Move ``apps.patient_portal`` out of ``SHARED_APPS`` (no longer
     attached to ``public``).

None of the model code changes — only the schema each row lives in.
That is why we keep the app self-contained, with explicit ``db_table``
names and no FKs into ``apps.tenants`` or ``apps.users``.

Auth interaction
----------------
``PatientAccount`` is intentionally NOT registered as
``AUTH_USER_MODEL`` — that role belongs to ``apps.users.StaffUser``.
Patient accounts extend ``AbstractBaseUser`` (for password hashing and
``last_login``) without ``PermissionsMixin`` (patients don't carry
Django auth groups/permissions; that machinery would also try to create
M2M tables tied to the ``auth_*`` shared models, which we don't want
in a future dedicated schema). Patient-portal authentication will be a
separate auth backend keyed on ``PatientAccount.email``; existing lab
JWT auth continues to work unchanged.
"""
