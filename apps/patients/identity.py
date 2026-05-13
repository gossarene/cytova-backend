"""
Patient identity reliability predicate.

Cytova surfaces a patient's previous lab value on result reports
("Previous: 95 mg/dL · 2026-04-12") so a biologist can spot trends
at a glance. That convenience becomes a clinical risk if the
patient's identity on file is incomplete: the "same patient" join
falls back to whatever document number / DOB the lab has on hand,
and an auto-generated placeholder ID or an unknown DOB can match
a different real person — surfacing the wrong patient's history
on a report.

This module owns the single boolean answer: "do we trust this
patient's identity enough to attach historical context to their
report?". The check is pure Python — it does not touch the
database, and is safe to call from any context that already has
the Patient instance in hand.

Rules
-----
A patient is eligible for previous-value surfacing only when ALL
of the following hold:

  - ``date_of_birth_unknown`` is False
  - ``date_of_birth`` is not None
  - ``document_type`` is not ``UNKNOWN``
  - ``identity_number_auto_generated`` is False
  - ``document_number`` is non-empty (after strip)

``DocumentType.OTHER`` is accepted PROVIDED the document number
exists and is not auto-generated — "Other" means "real document
that doesn't fit our other categories", not "no document". The
combined check above already enforces that.

The default safe answer is ``False``. New / unfamiliar shapes
(missing attributes, unexpected sentinel values) all collapse to
"unreliable" — the cost of a false negative (no previous value
shown for a real patient) is a UX inconvenience; the cost of a
false positive (wrong patient's history attached) is a clinical
incident.
"""
from __future__ import annotations

from .models import DocumentType, Patient


SKIP_REASON_INCOMPLETE_IDENTITY = 'INCOMPLETE_PATIENT_IDENTITY'


def is_patient_identity_reliable_for_history(patient: Patient) -> bool:
    """Return True when the patient's identity is reliable enough to
    cross-link to historical lab values."""
    if patient is None:
        return False

    # DOB must be on file and not flagged unknown. Both checks are
    # required — ``date_of_birth_unknown=False`` with ``date_of_birth=None``
    # would be a data-integrity bug, but we treat it as unreliable
    # anyway rather than dereferencing a null timestamp.
    if getattr(patient, 'date_of_birth_unknown', False):
        return False
    if getattr(patient, 'date_of_birth', None) is None:
        return False

    # ``DocumentType.UNKNOWN`` is the explicit "no document on file"
    # marker. The model auto-generates a placeholder identifier in
    # that case (``AUTO-PT-YYYYMMDD-XXXXXX``); using it as a real
    # ID would conflate unrelated patients.
    doc_type = getattr(patient, 'document_type', None)
    if doc_type == DocumentType.UNKNOWN:
        return False

    if getattr(patient, 'identity_number_auto_generated', False):
        return False

    doc_number = getattr(patient, 'document_number', '') or ''
    if not doc_number.strip():
        return False

    return True
