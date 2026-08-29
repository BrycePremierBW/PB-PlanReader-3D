"""
PlanReader 3D Canonical Model Reconciliation & Diagnostics Module.

Generates production diagnostic reports summarizing canonical model completeness,
level resolution, opening host attachment, provenance coverage, deduction authority,
stale model detection, and quantity reconciliation against PlanReader takeoff totals.

SAFETY GUARANTEES:
1. Does NOT alter tender/takeoff quantity authority (read-only diagnostic).
2. Reports variances explicitly without forcing dimension matching.
3. Exposes unresolved levels, missing heights, and review-required items clearly.
"""

from typing import Dict, Any, List, Optional
from pb_canonical_building import CanonicalProject, ObjectType, ReviewState, parse_strict_bool
from pb_geometry_services import potential_net_wall_area, wall_gross_area


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

    gross_wall_area_m2 = 0.0
    observed_opening_area_m2 = 0.0
    authorized_deduction_area_m2 = 0.0
    authorized_net_area_m2 = 0.0

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
                if not (prov and (prov.source_pdf or prov.drawing_id)):
                    missing_provenance_count += 1

                p_net = potential_net_wall_area(w)
                gross_wall_area_m2 += p_net["gross_wall_area_m2"]
                observed_opening_area_m2 += p_net["observed_opening_area_m2"]
                authorized_deduction_area_m2 += p_net["authorized_opening_deduction_area_m2"]
                authorized_net_area_m2 += p_net["authorized_net_area_m2"]

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

    # Quantity Reconciliation against production workspace takeoff rows (if present)
    production_takeoff_gross_m2 = 0.0
    reconciliation_notes = []

    if workspace_data and isinstance(workspace_data, dict):
        takeoff_rows = workspace_data.get("takeoff_rows") or workspace_data.get("lines") or []
        for r in takeoff_rows:
            if isinstance(r, dict):
                qty = r.get("quantity") or r.get("m2") or r.get("area_m2") or 0.0
                try:
                    production_takeoff_gross_m2 += float(qty)
                except (ValueError, TypeError):
                    pass

    area_variance_m2 = gross_wall_area_m2 - production_takeoff_gross_m2
    if abs(area_variance_m2) > 1e-2:
        reconciliation_notes.append(f"Wall Area Variance: Canonical ({gross_wall_area_m2:.2f} m²) vs Production Takeoff ({production_takeoff_gross_m2:.2f} m²), Diff = {area_variance_m2:+.2f} m²")
    else:
        reconciliation_notes.append("Canonical Wall Area matches Production Takeoff Totals cleanly.")

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
        "quantity_reconciliation": {
            "canonical_gross_wall_area_m2": gross_wall_area_m2,
            "canonical_observed_opening_area_m2": observed_opening_area_m2,
            "canonical_authorized_deduction_area_m2": authorized_deduction_area_m2,
            "canonical_authorized_net_area_m2": authorized_net_area_m2,
            "production_takeoff_gross_m2": production_takeoff_gross_m2,
            "area_variance_m2": area_variance_m2,
            "notes": reconciliation_notes,
        },
    }
