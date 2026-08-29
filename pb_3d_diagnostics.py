"""
PlanReader 3D Canonical Model Reconciliation & Diagnostics Module.

Generates production diagnostic reports summarizing canonical model completeness,
level resolution, opening host attachment, provenance coverage, deduction authority,
stale model detection, estimator QA summary, and per-wall quantity reconciliation.

SAFETY GUARANTEES:
1. Does NOT alter tender/takeoff quantity authority (read-only diagnostic).
2. Provides concise Estimator QA Summary panel for non-technical users.
3. Performs PER-WALL quantity reconciliation using strong wall_ref identity only.
4. Handles duplicate candidate rows by marking status = 'ambiguous' (no last-write-wins).
5. Filters production takeoff units strictly to m² and wall role.
6. If canonical wall height/geometry is unresolved, status = 'canonical_geometry_unavailable'.
7. Separates physical opening placement from deduction authority.
"""

from typing import Dict, Any, List, Optional
from pb_canonical_building import CanonicalProject, ObjectType, ReviewState, parse_strict_bool
from pb_geometry_services import potential_net_wall_area


def _strict_row_quantity(row: Dict[str, Any]) -> Optional[float]:
    """
    Blocker #4: Reads a production row's numeric quantity STRICTLY.
    - Explicitly present `quantity` or `m2` is returned (including an explicit 0.0).
    - A MISSING/non-numeric quantity returns None — it is NEVER coerced to 0.0,
      which would fabricate a false `matched` reconciliation against a zeroed net
      area and hide evidence gaps.
    """
    for key in ("quantity", "m2"):
        raw = row.get(key)
        if raw is None:
            continue
        try:
            f = float(raw)
            if f != f or f in (float("inf"), float("-inf")):
                return None
            return f
        except (ValueError, TypeError):
            continue
    return None



def generate_production_diagnostics_report(
    project: CanonicalProject,
    workspace_data: Optional[Dict[str, Any]] = None,
    skipped_items: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """Generates a production diagnostic report and Estimator QA summary for a CanonicalProject instance."""
    skipped_items = skipped_items or []
    workspace_data = workspace_data or {}
    
    total_walls = 0
    physical_walls_rendered = 0
    baseline_only_walls = 0
    walls_missing_heights = 0

    total_openings = 0
    physical_openings = 0
    evidence_only_openings = 0
    authorised_b5_opening_instances = 0  # SECTION L: Count authorised OPENING INSTANCES!

    total_floors = 0
    calibrated_floors = 0

    total_roofs = 0
    roof_geometry_rendered = 0

    confirmed_count = 0
    inferred_count = 0
    review_required_count = 0

    takeoff_eligible_count = 0
    rejected_deduction_claims = 0

    known_levels_count = 0
    unresolved_levels_count = 0
    missing_provenance_count = 0

    opening_state_counts = {
        "evidence_only": 0,
        "physical_not_authorised": 0,
        "physical_b5_authorised": 0,
        "invalid_geometry": 0,
        "wrong_host": 0,
        "wrong_level": 0,
        "conflict_overlap": 0,
        "manual_exclusion": 0,
    }

    per_wall_reconciliation: List[Dict[str, Any]] = []

    # SECTION G (blocker #4): Map production takeoff rows by wall_ref -> list[candidates].
    # STRONG IDENTITY ONLY: a row is matched ONLY via an explicit wall_ref, OR a
    # source_reference that parses to a real wall code. A bare table-row `id` or free-text
    # `location` is NEVER treated as a wall identity (prevents phantom matches / fabricated
    # reconciliation). If a row lacks a strong identity it simply cannot be a candidate.
    takeoff_candidates_by_ref: Dict[str, List[Dict[str, Any]]] = {}
    raw_rows = workspace_data.get("takeoff_rows") or []
    import re as _re
    for r in raw_rows:
        if isinstance(r, dict):
            unit = str(r.get("unit") or "").lower().strip()
            row_role = str(r.get("row_role") or "").lower().strip()
            if not (unit in ("m2", "m²", "sqm", "sq m") and row_role == "wall"):
                continue

            ref = ""
            # 1) Explicit strong wall_ref wins.
            wref = r.get("wall_ref")
            if wref and str(wref).strip():
                ref = str(wref).strip()
            else:
                # 2) Parse a real wall code out of source_reference ONLY.
                src = str(r.get("source_reference") or "")
                if "PB Unified Building" in src or "·" in src:
                    m_wall = _re.search(r"(?:W\d+|W-[A-Z0-9]+|\bW\d+\b)", src)
                    if m_wall:
                        ref = m_wall.group(0)
                else:
                    m_wall = _re.search(r"^W[-_]?[A-Z0-9]+(?=\s|$|\·|,|;)", src)
                    if m_wall:
                        ref = m_wall.group(0)

            if ref:
                takeoff_candidates_by_ref.setdefault(ref, []).append(r)

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

                prov = w.provenance
                if not (prov and (prov.source_pdf or prov.drawing_id or prov.wall_ref)):
                    missing_provenance_count += 1

                p_net = potential_net_wall_area(w)
                c_gross = p_net["gross_wall_area_m2"]
                c_obs = p_net["observed_opening_area_m2"]
                c_ded = p_net["authorized_opening_deduction_area_m2"]
                c_net = p_net["authorized_net_area_m2"]

                ref_key = str(prov.wall_ref or w.id)
                candidates = takeoff_candidates_by_ref.get(ref_key, [])

                # SECTION G: Multi-candidate ambiguity check (NO LAST-WRITE-WINS!)
                if w.height_m is None or w.height_m <= 0:
                    per_wall_reconciliation.append({
                        "canonical_wall_id": w.id,
                        "wall_ref": ref_key,
                        "canonical_gross_m2": None,
                        "observed_opening_m2": None,
                        "authorized_deduction_m2": None,
                        "authorized_net_m2": None,
                        "matched_production_row_id": str(candidates[0].get("id") or ref_key) if len(candidates) == 1 else None,
                        "production_quantity": _strict_row_quantity(candidates[0]) if len(candidates) == 1 else None,
                        "unit": "m²" if len(candidates) == 1 else None,
                        "variance_m2": None,
                        "reconciliation_status": "canonical_geometry_unavailable",
                        "explanation": "Canonical wall height is unresolved/missing.",
                    })
                elif len(candidates) > 1:
                    per_wall_reconciliation.append({
                        "canonical_wall_id": w.id,
                        "wall_ref": ref_key,
                        "canonical_gross_m2": c_gross,
                        "observed_opening_m2": c_obs,
                        "authorized_deduction_m2": c_ded,
                        "authorized_net_m2": c_net,
                        "matched_production_row_id": None,
                        "production_quantity": None,
                        "unit": "m²",
                        "variance_m2": None,
                        "reconciliation_status": "ambiguous",
                        "explanation": f"Multiple ({len(candidates)}) candidate production takeoff rows match wall_ref '{ref_key}'. Rejection of last-write-wins.",
                    })
                elif len(candidates) == 1:
                    matched_row = candidates[0]
                    unit = str(matched_row.get("unit") or "").lower().strip()
                    row_role = str(matched_row.get("row_role") or "").lower().strip()

                    if unit not in ("m2", "m²", "sqm", "sq m") or (row_role and row_role != "wall"):
                        per_wall_reconciliation.append({
                            "canonical_wall_id": w.id,
                            "wall_ref": ref_key,
                            "canonical_gross_m2": c_gross,
                            "observed_opening_m2": c_obs,
                            "authorized_deduction_m2": c_ded,
                            "authorized_net_m2": c_net,
                            "matched_production_row_id": str(matched_row.get("id") or ref_key),
                            "production_quantity": None,
                            "unit": unit,
                            "variance_m2": None,
                            "reconciliation_status": "production_row_not_comparable",
                            "explanation": f"Matched production row role '{row_role}' / unit '{unit}' is not comparable to vertical wall area m².",
                        })
                        continue

                    prod_qty = _strict_row_quantity(matched_row)
                    if prod_qty is None:
                        per_wall_reconciliation.append({
                            "canonical_wall_id": w.id,
                            "wall_ref": ref_key,
                            "canonical_gross_m2": c_gross,
                            "observed_opening_m2": c_obs,
                            "authorized_deduction_m2": c_ded,
                            "authorized_net_m2": c_net,
                            "matched_production_row_id": str(matched_row.get("id") or ref_key),
                            "production_quantity": None,
                            "unit": "m²",
                            "variance_m2": None,
                            "reconciliation_status": "production_quantity_invalid",
                            "explanation": "Matched row has missing, malformed, or NaN production quantity.",
                        })
                        continue

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
                    b5_auth = parse_strict_bool(op.deduction_authority)

                    # Blocker #3: The physical opening state bucket comes from the Adapter's
                    # explicit metadata['physical_state'] (invalid_geometry / wrong_level /
                    # conflict_overlap / manual_exclusion / physical_b5_authorised /
                    # physical_not_authorised). For models built without the adapter (or older
                    # payloads) fall back to strict placement inference.
                    phys_state = str(op.metadata.get("physical_state") or "").strip()
                    if not phys_state:
                        is_physically_placed = (
                            op.offset_along_wall_m is not None and
                            op.width_m is not None and op.width_m > 0 and
                            op.height_m is not None and op.height_m > 0
                        )
                        if is_physically_placed:
                            phys_state = "physical_b5_authorised" if b5_auth else "physical_not_authorised"
                        elif b5_auth:
                            phys_state = "invalid_geometry"
                        else:
                            phys_state = "evidence_only"

                    # SECTION L: Count authorised OPENING INSTANCES!
                    if b5_auth:
                        authorised_b5_opening_instances += 1

                    if phys_state == "physical_b5_authorised":
                        physical_openings += 1
                        opening_state_counts["physical_b5_authorised"] += 1
                    elif phys_state in ("physical_not_authorised", "invalid_geometry", "wrong_level",
                                        "conflict_overlap", "manual_exclusion", "wrong_host"):
                        evidence_only_openings += 1
                        opening_state_counts.setdefault(phys_state, 0)
                        opening_state_counts[phys_state] += 1
                        rejected_deduction_claims += 1
                    else:  # evidence_only or unknown -> treat as evidence only, fail closed
                        evidence_only_openings += 1
                        opening_state_counts["evidence_only"] += 1
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

    # SECTION N: Read-only LAGO elevation candidates observation
    elevation_opening_candidates = workspace_data.get("elevation_opening_candidates") or []

    # SECTION X: Expanded Estimator QA Summary Breakdown
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
        "authorised_b5_deductions": authorised_b5_opening_instances,  # SECTION L: OPENING INSTANCES
        "rejected_deduction_claims": rejected_deduction_claims,
        "elevation_opening_candidates_observed": len(elevation_opening_candidates),  # SECTION N
        "opening_state_counts": opening_state_counts,
        "roof_geometry_rendered": roof_geometry_rendered,
        "provenance_gaps": missing_provenance_count,
        "matched_reconciliations": len([r for r in per_wall_reconciliation if r["reconciliation_status"] == "matched"]),
        "unresolved_reconciliations": len([r for r in per_wall_reconciliation if r["reconciliation_status"] == "unresolved"]),
        "ambiguous_reconciliations": len([r for r in per_wall_reconciliation if r["reconciliation_status"] == "ambiguous"]),
        "variances_detected": len([r for r in per_wall_reconciliation if r["reconciliation_status"] == "variance_detected"]),
        "canonical_geometry_unavailable": len([r for r in per_wall_reconciliation if r["reconciliation_status"] == "canonical_geometry_unavailable"]),
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
