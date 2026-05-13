"""
Helpers that resolve the result structure + parameter set for a
specific ``AnalysisRequestItem``.

Why this module exists
----------------------
``ExamDefinition.result_structure`` and the active-parameter list
on the catalog can change AFTER a request was created (a lab admin
correcting a mistyped definition: SINGLE_VALUE → MULTI_PARAMETER,
or adding / deactivating parameters). When that happens, in-flight
items MUST keep behaving as they were at creation time — flipping
mid-request would break result entry, validation, and report
rendering.

The item snapshots (``result_structure_snapshot`` +
``parameter_ids_snapshot``) freeze those two pieces of information
at item creation. The two helpers in this module are the ONLY
sanctioned readers — every other module reads from here so the
fallback policy stays consistent:

  - snapshot present → use the snapshot verbatim
  - snapshot empty (legacy row pre-dating the field) → fall back to
    the live ``exam_definition`` so existing rows keep working

A future cleanup migration can back-fill the snapshots on legacy
items; until then the fallback path preserves behaviour.
"""
from __future__ import annotations

from apps.catalog.models import ExamParameter


def effective_result_structure(item) -> str:
    """Return the result structure that should drive entry / rendering
    for ``item``. Snapshot wins; live value is the fallback."""
    snapshot = getattr(item, 'result_structure_snapshot', '') or ''
    if snapshot:
        return snapshot
    return item.exam_definition.result_structure


def effective_active_parameter_ids(item) -> list[str]:
    """Return the list of ExamParameter UUIDs (as strings) that are
    valid for THIS item's result entry.

    Resolution policy:

      - If the item has a snapshot, return the snapshot as-is. This
        is the contract: in-flight items never gain a parameter
        that was added after their creation, and never lose one
        that was deactivated after creation. Whether each snapshotted
        parameter is *still* active on the catalog is irrelevant to
        the item — the snapshot is the source of truth.

      - Otherwise (legacy row pre-dating the snapshot field), fall
        back to the currently-active parameters on the exam
        definition. This matches pre-snapshot behaviour.

    Returned ids are normalised to strings so callers can compare
    against ``request.data['parameter_id']`` (also string) without
    extra coercion at the call site.
    """
    snapshot = getattr(item, 'parameter_ids_snapshot', None)
    if snapshot:
        return [str(pid) for pid in snapshot]

    return [
        str(pid) for pid in
        ExamParameter.objects
        .filter(exam_definition=item.exam_definition, is_active=True)
        .values_list('id', flat=True)
    ]
