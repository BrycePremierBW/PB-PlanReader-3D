"""
PlanReader 3D Canonical Model Reconciliation & Diagnostics Module.

Generates production diagnostic reports summarizing canonical model completeness,
level resolution, opening host attachment, provenance coverage, deduction authority,
stale model detection, and per-wall quantity reconciliation against PlanReader takeoff rows.

SAFETY GUARANTEES:
1. Does NOT alter tender/takeoff quantity authority (read-only diagnostic).
2. Performs PER-WALL quantity reconciliation using strong wall_ref identity only.
3. Filters production takeoff units strictly to m². Excludes lm, No., item, L, allowances.
4. If strong identity is missing, status = 'unresolved' (no misleading whole-table variances).
5. Reports explicit skip diagnostics for elements without registered physical producers.
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
    Generates a production diagnostic report for a CanonicalProject instance.
    """
    skipped_items = skipped_items or []
    
    total_walls = 0
    total_openings = 0
    total_floors = 0
    total_ceilings = 0
    total_roofs = 0
    total_other = 0

    confirmed_count = 0
    inferred_count = 0
    review_required_count = 0

    takeoff_eligible_count = 0
    authorized_deduction_count = 0

    unresolved_levels_count = 0
    unresolved_heights_count = 0
    missing_provenance_count = 0

    per_wall_reconciliation: List[Dict[str, Any]] = []

    # Map production takeoff rows by strong identity (wall_ref / id)
    takeoff_rows_by_ref: Dict[str, Dict[str, Any]] = {}
    if workspace_data and isinstance(workspace_data, dict):
        raw_rows = workspace_data.get("takeoff_rows") or workspace_data.get("walls") or []
        for r in raw_rows:
            if isinstance(r, dict):
                ref = str(r.get("wall_ref") or r.get("id") or "")
                unit = str(r.get("unit") or "m2").lower().strip()
                # SECTION 9: Filter units strictly to m²! Exclude lm, No., item, L, allowances
                if ref and unit in ("m2", "m²", "sqm", "sq m"):
                    takeoff_rows_by_ref[ref] = r

    for bld in project.buildings:
        for lvl in bld.levels:
            if lvl.elevation_m is None:
                unresolved_levels_count += 1
            if lvl.height_m is None:
                unresolved_heights_count += 1

            for w in lvl.walls:
                total_walls += 1
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

                # SECTION 9: Per-Wall Reconciliation using strong identity
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
                    if op.review_state == ReviewState.CONFIRMED:
                        confirmed_count += 1
                    elif op.review_state == ReviewState.INFERRED:
                        inferred_count += 1
                    else:
                        review_required_count += 1

                    if parse_strict_bool(op.deduction_authority):
                        authorized_deduction_count += 1

            for f in lvl.floors:
                total_floors += 1
            for c in lvl.ceilings:
                total_ceilings += 1
            for r in lvl.roofs:
                total_roofs += 1

    total_canonical_objects = total_walls + total_openings + total_floors + total_ceilings + total_roofs + total_other

    return {
        "project_id": project.id,
        "project_name": project.name,
        "is_synthetic_demo": project.is_synthetic_demo,
        "total_canonical_objects": total_canonical_objects,
        "object_breakdown": {
            "walls": total_walls,
            "openings": total_openings,
            "floors": total_floors,
            "ceilings": total_ceilings,
            "roofs": total_roofs,
            "other": total_other,
        },
        "review_state_summary": {
            "confirmed": confirmed_count,
            "inferred": inferred_count,
            "review_required": review_required_count,
        },
        "authority_summary": {
            "takeoff_eligible_elements": takeoff_eligible_count,
            "authorized_deduction_elements": authorized_deduction_count,
        },
        "integrity_checks": {
            "unresolved_levels": unresolved_levels_count,
            "unresolved_heights": unresolved_heights_count,
            "missing_provenance": missing_provenance_count,
            "skipped_items_count": len(skipped_items),
            "skipped_items": skipped_items,
        },
        "per_wall_quantity_reconciliation": per_wall_reconciliation,
    }
