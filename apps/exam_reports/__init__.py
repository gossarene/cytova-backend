"""
Cytova — Exam operational reports.

Distinct from ``apps.financial_reports`` (which builds a money-side
simulation) and from ``apps.invoicing`` (which mints Invoice rows).
This app exists to answer operational questions like "how many
exams did each partner generate for us in this period?" without
touching any monetary state.

Read-only surface — no models, no audit-writing schema, no
billing semantics. Aggregates run in the active tenant schema so
multi-tenant isolation is enforced by the request middleware, not
by a query-level filter.
"""

default_app_config = 'apps.exam_reports.apps.ExamReportsConfig'
