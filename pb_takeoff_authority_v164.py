"""Shared commercial-authority policy for PlanReader take-off rows.

The legacy take-off table contains rows from several producers.  Ordinary
estimator-authored rows retain their existing behaviour, but 3D model-surface
rows are derived geometry and therefore require explicit, attributable review
before they can enter pricing, quotation, or JobHub publication paths.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
from typing import Any, Dict, Mapping, Tuple


MODEL_SURFACE_ROLE = "model_surface"
MODEL_SURFACE_SOURCE_PREFIX = "pb 3d surface editor "
FLOOR_REFERENCE_ROLE = "floor_area"

AUTHORITY_REVIEW_REQUIRED = "REVIEW_REQUIRED"
AUTHORITY_APPROVED = "APPROVED"

AUTHORITY_STATUS_FIELD = "commercial_authority_status"
AUTHORITY_SOURCE_FIELD = "commercial_authority_source"
AUTHORITY_REVIEWED_BY_FIELD = "commercial_authority_reviewed_by"
AUTHORITY_REVIEWED_AT_FIELD = "commercial_authority_reviewed_at"
AUTHORITY_FINGERPRINT_FIELD = "commercial_authority_fingerprint"

_EXCLUDED_SCOPE_VALUES = {"exclude", "excluded", "exclusion"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalised(value: Any) -> str:
    return _text(value).lower().replace("-", "_").replace(" ", "_")


def is_model_surface_row(row: Mapping[str, Any]) -> bool:
    role_matches = _normalised(row.get("row_role")) == MODEL_SURFACE_ROLE
    source_matches = _text(row.get("source_reference")).lower().startswith(
        MODEL_SURFACE_SOURCE_PREFIX
    )
    return role_matches or source_matches


def is_floor_reference_row(row: Mapping[str, Any]) -> bool:
    return _normalised(row.get("row_role")) == FLOOR_REFERENCE_ROLE


def is_excluded_takeoff_row(row: Mapping[str, Any]) -> bool:
    """Recognise the legacy inclusion spellings without substring ambiguity."""
    return _normalised(row.get("inclusion_status")) in _EXCLUDED_SCOPE_VALUES


_AUTHORITY_BOUND_FIELDS = (
    "workspace_id",
    "section",
    "element",
    "location",
    "substrate",
    "finish_system",
    "quantity",
    "unit",
    "quantity_status",
    "source_page",
    "source_reference",
    "inclusion_status",
    "coats",
    "coverage_m2_per_litre",
    "productivity_m2_per_hour",
    "rate_per_unit",
    "confidence",
    "notes",
    "row_role",
    AUTHORITY_SOURCE_FIELD,
    AUTHORITY_REVIEWED_BY_FIELD,
    AUTHORITY_REVIEWED_AT_FIELD,
)


def _canonical_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    return _text(value)


def compute_model_surface_authority_fingerprint(row: Mapping[str, Any]) -> str:
    """Bind a model-surface approval to every consequential row field."""
    payload = {
        field: _canonical_value(row.get(field)) for field in _AUTHORITY_BOUND_FIELDS
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def approve_model_surface_row(
    row: Mapping[str, Any], *, source: Any, reviewed_by: Any, reviewed_at: Any
) -> Dict[str, Any]:
    """Create a complete approval record for the current immutable row state."""
    if not is_model_surface_row(row):
        raise ValueError("Only a 3D model-surface row can receive model authority.")
    workspace_id = row.get("workspace_id")
    if isinstance(workspace_id, bool):
        raise ValueError("A positive workspace identity is required for model authority.")
    try:
        valid_workspace_id = int(workspace_id) > 0
    except (TypeError, ValueError, OverflowError):
        valid_workspace_id = False
    if not valid_workspace_id:
        raise ValueError("A positive workspace identity is required for model authority.")
    source_text = _text(source)
    reviewer_text = _text(reviewed_by)
    reviewed_at_text = _text(reviewed_at)
    if not source_text or not reviewer_text or not reviewed_at_text:
        raise ValueError("Source evidence, reviewer identity, and review timestamp are required.")
    approved = dict(row)
    approved["workspace_id"] = int(workspace_id)
    approved[AUTHORITY_STATUS_FIELD] = AUTHORITY_APPROVED
    approved[AUTHORITY_SOURCE_FIELD] = source_text
    approved[AUTHORITY_REVIEWED_BY_FIELD] = reviewer_text
    approved[AUTHORITY_REVIEWED_AT_FIELD] = reviewed_at_text
    approved[AUTHORITY_FINGERPRINT_FIELD] = compute_model_surface_authority_fingerprint(
        approved
    )
    return approved


def model_surface_authority(row: Mapping[str, Any]) -> Tuple[bool, str]:
    """Return whether a model surface has a complete row-level approval record.

    An approval label on its own is not authority.  The source evidence,
    reviewer identity, and review timestamp must all be present as well.
    """
    if not is_model_surface_row(row):
        return True, "NOT_MODEL_SURFACE"
    if _normalised(row.get(AUTHORITY_STATUS_FIELD)) != AUTHORITY_APPROVED.lower():
        return False, "3D model surface has not received commercial approval"
    if not _text(row.get(AUTHORITY_SOURCE_FIELD)):
        return False, "3D model surface approval has no source evidence reference"
    if not _text(row.get(AUTHORITY_REVIEWED_BY_FIELD)):
        return False, "3D model surface approval has no reviewer identity"
    if not _text(row.get(AUTHORITY_REVIEWED_AT_FIELD)):
        return False, "3D model surface approval has no review timestamp"
    fingerprint = _text(row.get(AUTHORITY_FINGERPRINT_FIELD))
    expected = compute_model_surface_authority_fingerprint(row)
    if not fingerprint or not hmac.compare_digest(fingerprint, expected):
        return False, "3D model surface approval no longer matches the current row"
    return True, "APPROVED"


def takeoff_row_publishability(row: Mapping[str, Any]) -> Tuple[bool, str]:
    """Single policy used by preflight, pricing, exports, and JobHub delivery."""
    if is_floor_reference_row(row):
        return False, "FLOOR_REFERENCE"
    if is_excluded_takeoff_row(row):
        return False, "EXCLUDED"
    approved, reason = model_surface_authority(row)
    if not approved:
        return False, reason
    return True, "PUBLISHABLE"
