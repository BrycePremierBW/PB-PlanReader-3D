"""Phase 5M production diagnostics facade.

Preserves the previous implementation in ``pb_3d_diagnostics_legacy.py`` and
recomputes per-wall reconciliation with the actual v139 producer contract.
Dedicated v139 rows are trusted as wall rows through their exact producer
prefix and therefore do not require a generic ``row_role`` field.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import pb_3d_diagnostics_legacy as _legacy

for _name in dir(_legacy):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_legacy, _name)

from pb_canonical_building import CanonicalProject  # noqa: E402
from pb_geometry_services import potential_net_wall_area  # noqa: E402

_V139_PREFIX = "PB Unified Building v1.3.9 · "
_WALL_UNITS = {"m2", "m²", "sqm", "sq m"}


def _strict_quantity(row: Dict[str, Any]) -> Optional[float]:
    for key in ("quantity", "net_m2", "m2"):
        if key not in row:
            continue
        raw = row.get(key)
        if raw is None or isinstance(raw, bool):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None
    return None


def _registered_refs(project: CanonicalProject) -> set[str]:
    return {
        str(w.provenance.wall_ref).strip()
        for bld in project.buildings
        for lvl in bld.levels
        for w in lvl.walls
        if w.provenance and w.provenance.wall_ref and str(w.provenance.wall_ref).strip()
    }


def _row_wall_identity(row: Dict[str, Any], registered: set[str]) -> Tuple[Optional[str], str]:
    unit = str(row.get("unit") or "").strip().lower()
    if unit not in _WALL_UNITS:
        return None, "non_wall_area_unit"

    source_reference = str(row.get("source_reference") or "")
    if source_reference.startswith(_V139_PREFIX):
        ref = source_reference[len(_V139_PREFIX):].strip()
        if ref and ref in registered:
            return ref, "dedicated_v139"
        return None, "v139_unknown_wall_ref"

    # Generic/non-v139 rows require their own explicit wall role AND explicit
    # wall_ref.  Location text and database row ids are never identity.
    row_role = str(row.get("row_role") or "").strip().lower()
    ref = str(row.get("wall_ref") or "").strip()
    if row_role == "wall" and ref and ref in registered:
        return ref, "generic_explicit_wall"
    return None, "weak_or_missing_identity"


def _candidate_map(project: CanonicalProject, workspace_data: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    registered = _registered_refs(project)
    candidates: Dict[str, List[Dict[str, Any]]] = {}
    for row in workspace_data.get("takeoff_rows") or []:
        if not isinstance(row, dict):
            continue
        ref, identity_source = _row_wall_identity(row, registered)
        if ref is None:
            continue
        normalized = dict(row)
        normalized["_phase5m_identity_source"] = identity_source
        candidates.setdefault(ref, []).append(normalized)
    return candidates


def _reconciliation(project: CanonicalProject, workspace_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    by_ref = _candidate_map(project, workspace_data)
    out: List[Dict[str, Any]] = []

    for building in project.buildings:
        for level in building.levels:
            for wall in level.walls:
                ref = str((wall.provenance.wall_ref if wall.provenance else None) or wall.id)
                rows = by_ref.get(ref, [])
                areas = potential_net_wall_area(wall)
                gross = areas["gross_wall_area_m2"]
                observed = areas["observed_opening_area_m2"]
                deduction = areas["authorized_opening_deduction_area_m2"]
                net = areas["authorized_net_area_m2"]

                base = {
                    "canonical_wall_id": wall.id,
                    "wall_ref": ref,
                    "canonical_gross_m2": gross if wall.height_m is not None and wall.height_m > 0 else None,
                    "observed_opening_m2": observed if wall.height_m is not None and wall.height_m > 0 else None,
                    "authorized_deduction_m2": deduction if wall.height_m is not None and wall.height_m > 0 else None,
                    "authorized_net_m2": net if wall.height_m is not None and wall.height_m > 0 else None,
                    "matched_production_row_id": None,
                    "production_quantity": None,
                    "unit": None,
                    "variance_m2": None,
                }

                if wall.height_m is None or wall.height_m <= 0:
                    out.append({**base, "reconciliation_status": "canonical_geometry_unavailable", "explanation": "Canonical wall height is unresolved/missing."})
                    continue
                if not rows:
                    out.append({**base, "reconciliation_status": "unresolved", "explanation": "No production wall-area row with strong identity matched this registered wall."})
                    continue
                if len(rows) > 1:
                    out.append({**base, "unit": "m²", "reconciliation_status": "ambiguous", "explanation": f"Multiple ({len(rows)}) strong production candidates match wall_ref '{ref}'."})
                    continue

                row = rows[0]
                qty = _strict_quantity(row)
                row_id = str(row.get("id") or row.get("source_reference") or ref)
                unit = str(row.get("unit") or "")
                if qty is None:
                    out.append({
                        **base,
                        "matched_production_row_id": row_id,
                        "unit": unit or "m²",
                        "reconciliation_status": "production_quantity_invalid",
                        "explanation": "Matched row has missing, malformed, NaN or infinite production quantity.",
                    })
                    continue

                variance = net - qty
                status = "matched" if abs(variance) <= 1e-2 else "variance_detected"
                explanation = (
                    "Canonical net area matches production takeoff quantity exactly."
                    if status == "matched"
                    else f"Area variance of {variance:+.2f} m² between canonical model and production row."
                )
                out.append({
                    **base,
                    "matched_production_row_id": row_id,
                    "production_quantity": qty,
                    "unit": unit or "m²",
                    "variance_m2": variance,
                    "reconciliation_status": status,
                    "explanation": explanation,
                    "identity_source": row.get("_phase5m_identity_source"),
                })
    return out


def generate_production_diagnostics_report(
    project: CanonicalProject,
    workspace_data: Optional[Dict[str, Any]] = None,
    skipped_items: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    workspace_data = workspace_data or {}
    report = _legacy.generate_production_diagnostics_report(
        project,
        workspace_data=workspace_data,
        skipped_items=skipped_items,
    )

    recs = _reconciliation(project, workspace_data)
    report["per_wall_quantity_reconciliation"] = recs
    qa = report.setdefault("estimator_qa_summary", {})
    qa["matched_reconciliations"] = sum(r["reconciliation_status"] == "matched" for r in recs)
    qa["unresolved_reconciliations"] = sum(r["reconciliation_status"] == "unresolved" for r in recs)
    qa["ambiguous_reconciliations"] = sum(r["reconciliation_status"] == "ambiguous" for r in recs)
    qa["variances_detected"] = sum(r["reconciliation_status"] == "variance_detected" for r in recs)
    qa["canonical_geometry_unavailable"] = sum(r["reconciliation_status"] == "canonical_geometry_unavailable" for r in recs)
    qa["production_quantity_invalid"] = sum(r["reconciliation_status"] == "production_quantity_invalid" for r in recs)
    return report
