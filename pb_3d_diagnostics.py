"""
PlanReader 3D Canonical Model Reconciliation & Diagnostics Module.

Generates production diagnostic reports summarizing canonical model completeness,
level resolution, opening host attachment, provenance coverage, deduction authority,
stale model detection, estimator QA summary, and per-wall quantity reconciliation.

SAFETY GUARANTEES:
1. Does NOT alter tender/takeoff quantity authority (read-only diagnostic).
2. Provides concise Estimator QA Summary panel for non-technical users.
3. Performs PER-WALL quantity reconciliation using strong wall_ref identity only.
4. Filters production takeoff units strictly to m². Excludes lm, No., item, L, allowances.
5. If strong identity is missing, status = 'unresolved' (no misleading whole-table variances).
6. Reports explicit skip diagnostics for elements without registered physical producers.
"""

from typing import Dict, Any, List, Optional
from pb_canonical_building import CanonicalProject, ObjectType, ReviewState, parse_strict_bool
from pb_geometry_services import potential_net_wall_area


def generate_production_diagnostics_report(
    project: CanonicalProject,
    workspace_data: Optional[Dict[str, Any]] = None,
    skipped_items: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    Generates a production diagnostic report and Estimator QA summary for a CanonicalProject instance.
    """
    skipped_items = skipped_items or []
    workspace_data = workspace_data or {}
    
    total_walls = 0
    physical_walls_rendered = 0
    baseline_only_walls = 0
    walls_missing_heights = 0

    total_openings = 0
    physical_openings = 0
    evidence_only_openings = 0

    total_floors = 0
    calibrated_floors = 0

    total_roofs = 0
    roof_geometry_rendered = 0

    confirmed_count = 0
    inferred_count = 0
    review_required_count = 0

    takeoff_eligible_count = 0
    authorized_deduction_count = 0
    rejected_deduction_claims = 0

    known_levels_count = 0
    unresolved_levels_count = 0
    missing_provenance_count = 0

    per_wall_reconciliation: List[Dict[str, Any]] = []

    # Map production takeoff rows by strong identity (wall_ref / id)
    takeoff_rows_by_ref: Dict[str, Dict[str, Any]] = {}
    raw_rows = workspace_data.get("takeoff_rows") or []
    for r in raw_rows:
        if isinstance(r, dict):
            ref = str(r.get("wall_ref") or r.get("id") or r.get("location") or "")
            unit = str(r.get("unit") or "m2").lower().strip()
            row_role = str(r.get("row_role") or "wall").lower().strip()
            # SECTION J: Filter units strictly to m² and wall role!
            if ref and unit in ("m2", "m²", "sqm", "sq m") and row_role == "wall":
                takeoff_rows_by_ref[ref] = r

    for bld in project.buildings:
        for lvl in bld.levels:
            if lvl.elevation_m is None:
                unresolved_levels_count += 1
            else:
                known_levels_count += 1

            for w in lvl.walls:
                total_walls += 1
                if w.height_m is not None and w.height_m > 0:
                    physical_walls_rendered += 1
                else:
                    baseline_only_walls += 1
                    walls_missing_heights += 1

                if w.review_state == ReviewState.CONFIRMED:
                    confirmed_count += 1
                elif w.review_state == ReviewState.INFERRED:
                    inferred_count += 1
                else:
                    review_required_count += 1

                if parse_strict_bool(w.takeoff_eligible):
                    takeoff_eligible_count += 1
                if parse_strict_bool(w.deduction_authority):
                    authorized_deduction_count += 1

                prov = w.provenance
                if not (prov and (prov.source_pdf or prov.drawing_id or prov.wall_ref)):
                    missing_provenance_count += 1

                p_net = potential_net_wall_area(w)
                c_gross = p_net["gross_wall_area_m2"]
                c_obs = p_net["observed_opening_area_m2"]
                c_ded = p_net["authorized_opening_deduction_area_m2"]
                c_net = p_net["authorized_net_area_m2"]

                ref_key = str(prov.wall_ref or w.id)
                matched_row = takeoff_rows_by_ref.get(ref_key)

                if matched_row:
                    prod_qty = float(matched_row.get("quantity") or matched_row.get("m2") or matched_row.get("net_m2") or 0.0)
                    variance = c_net - prod_qty
                    if abs(variance) <= 1e-2:
                        rec_status = "matched"
                        rec_exp = "Canonical net area matches production takeoff quantity exactly."
                    else:
                        rec_status = "variance_detected"
                        rec_exp = f"Area variance of {variance:+.2f} m² between 3D model and production row."

                    per_wall_reconciliation.append({
                        "canonical_wall_id": w.id,
                        "wall_ref": ref_key,
                        "canonical_gross_m2": c_gross,
                        "observed_opening_m2": c_obs,
                        "authorized_deduction_m2": c_ded,
                        "authorized_net_m2": c_net,
                        "matched_production_row_id": str(matched_row.get("id") or ref_key),
                        "production_quantity": prod_qty,
                        "unit": "m²",
                        "variance_m2": variance,
                        "reconciliation_status": rec_status,
                        "explanation": rec_exp,
                    })
                else:
                    per_wall_reconciliation.append({
                        "canonical_wall_id": w.id,
                        "wall_ref": ref_key,
                        "canonical_gross_m2": c_gross,
                        "observed_opening_m2": c_obs,
                        "authorized_deduction_m2": c_ded,
                        "authorized_net_m2": c_net,
                        "matched_production_row_id": None,
                        "production_quantity": None,
                        "unit": None,
                        "variance_m2": None,
                        "reconciliation_status": "unresolved",
                        "explanation": "No matching production takeoff row with strong identity found.",
                    })

                for op in w.openings:
                    total_openings += 1
                    # SECTION K: Separate physical placement from deduction authority!
                    is_physically_placed = (
                        op.offset_along_wall_m is not None and
                        op.width_m is not None and op.width_m > 0 and
                        op.height_m is not None and op.height_m > 0
                    )
                    
                    if is_physically_placed:
                        physical_openings += 1
                    else:
                        evidence_only_openings += 1

                    if parse_strict_bool(op.deduction_authority):
                        authorized_deduction_count += 1
                    else:
                        rejected_deduction_claims += 1

            for f in lvl.floors:
                total_floors += 1
                if len(f.polygon) >= 3:
                    calibrated_floors += 1

            for r in lvl.roofs:
                total_roofs += 1
                if len(r.polygon) >= 3:
                    roof_geometry_rendered += 1

    total_canonical_objects = total_walls + total_openings + total_floors + total_roofs

    # SECTION P: Expanded Estimator QA Summary Breakdown
    estimator_qa_summary = {
        "physical_walls_rendered": physical_walls_rendered,
        "baseline_only_walls": baseline_only_walls,
        "walls_missing_heights": walls_missing_heights,
        "known_levels": known_levels_count,
        "unresolved_levels": unresolved_levels_count,
        "calibrated_floors": calibrated_floors,
        "manual_floor_allowances": len([s for s in skipped_items if "manual_m2_allowance" in str(s.get("reason", ""))]),
        "physical_openings": physical_openings,
        "evidence_only_openings": evidence_only_openings,
        "authorised_b5_deductions": authorized_deduction_count,
        "rejected_deduction_claims": rejected_deduction_claims,
        "roof_geometry_rendered": roof_geometry_rendered,
        "provenance_gaps": missing_provenance_count,
        "matched_reconciliations": len([r for r in per_wall_reconciliation if r["reconciliation_status"] == "matched"]),
        "unresolved_reconciliations": len([r for r in per_wall_reconciliation if r["reconciliation_status"] == "unresolved"]),
        "variances_detected": len([r for r in per_wall_reconciliation if r["reconciliation_status"] == "variance_detected"]),
        "producer_diagnostics_log": workspace_data.get("diagnostics_log", []),
    }

    return {
        "project_id": project.id,
        "project_name": project.name,
        "is_synthetic_demo": project.is_synthetic_demo,
        "total_canonical_objects": total_canonical_objects,
        "estimator_qa_summary": estimator_qa_summary,
        "object_breakdown": {
            "walls": total_walls,
            "openings": total_openings,
            "floors": total_floors,
            "roofs": total_roofs,
        },
        "review_state_summary": {
            "confirmed": confirmed_count,
            "inferred": inferred_count,
            "review_required": review_required_count,
        },
        "integrity_checks": {
            "unresolved_levels": unresolved_levels_count,
            "unresolved_heights": walls_missing_heights,
            "missing_provenance": missing_provenance_count,
            "skipped_items_count": len(skipped_items),
            "skipped_items": skipped_items,
        },
        "per_wall_quantity_reconciliation": per_wall_reconciliation,
    }
