"""
PlanReader Production 3D Model Adapter Module (Phase 5F Final Production Contract).

Provides a clean production adapter converting existing PlanReader production payloads,
documents, pages, takeoff rows, drawing intelligence, opening schedules, and elevation evidence
into a validated CanonicalProject object graph for 3D WebGL BIM viewing.

SAFETY GUARANTEES:
1. Does NOT build a second measurement/extraction engine.
2. Does NOT grant new deduction authority — B5/v175 is sole deduction authority.
3. Untrusted/uploaded JSON CANNOT forge deduction_authority or takeoff_eligible.
4. Fails closed if workspace ID is missing or invalid in database (require_workspace_id).
5. All app.lquery() calls consume List[Dict[str, Any]] (dictionary row access, page_no schema).
6. Snapshot v3 collects documents, pages, registered walls, mapper settings, roof evidence, takeoff rows.
7. Level resolution creates explicit unresolved containers (elevation_m=None) if level is unknown.
8. Re-validates every opening through is_authorised_deduction(op). Stale deduction booleans are rejected.
9. Rejects legacy 2.7m wall height fallbacks as physical truth.
10. Calibrated floor mapper geometry percentage-to-metric conversion via px_per_m.
"""

import math
import sqlite3
import json
import hashlib
from typing import Dict, Any, List, Optional, Tuple, Union, NamedTuple

from pb_canonical_building import (
    CanonicalProject,
    CanonicalBuilding,
    CanonicalLevel,
    CanonicalWall,
    CanonicalOpening,
    CanonicalFloor,
    CanonicalCeiling,
    CanonicalRoof,
    CanonicalSoffit,
    CanonicalBalcony,
    CanonicalParapet,
    CanonicalColumn,
    CanonicalBalustrade,
    CanonicalScreen,
    CanonicalSpace,
    CanonicalFinishSurface,
    Vector2D,
    Vector3D,
    Provenance,
    ReviewState,
    ObjectType,
    parse_strict_bool,
)
from pb_3d_diagnostics import generate_production_diagnostics_report


class WorkspaceCanonicalResult(NamedTuple):
    """SECTION H: Encapsulates the complete authoritative workspace conversion result."""
    project: CanonicalProject
    snapshot: Dict[str, Any]
    snapshot_fingerprint: str
    diagnostics: Dict[str, Any]
    skipped_items: List[Dict[str, Any]]


def require_workspace_id(workspace_id: Any) -> int:
    """
    SECTION 3 & C: Validates workspace identity strictly.
    
    Accepts ONLY a valid positive integer (or integer string) belonging to the selected workspace.
    Rejects booleans, zero, negative numbers, non-integral floats (e.g. 101.5), malformed strings, or invalid types.
    GUARANTEE: No implicit workspace fallbacks!
    """
    if workspace_id is None or isinstance(workspace_id, bool):
        raise ValueError(f"Invalid workspace ID: {workspace_id} (must be a positive integer)")

    # Reject non-integral floats such as 101.5
    if isinstance(workspace_id, float):
        if not workspace_id.is_integer():
            raise ValueError(f"Invalid workspace ID: {workspace_id} (non-integral float rejected)")
        wid = int(workspace_id)
    elif isinstance(workspace_id, int):
        wid = workspace_id
    elif isinstance(workspace_id, str):
        str_val = workspace_id.strip()
        if not str_val.isdigit():
            raise ValueError(f"Invalid workspace ID format: '{workspace_id}' (cannot parse as integer)")
        wid = int(str_val)
    else:
        raise ValueError(f"Invalid workspace ID type: {type(workspace_id)}")

    if wid <= 0:
        raise ValueError(f"Invalid workspace ID: {wid} (must be positive)")
    return wid


def _safe_float(val: Any) -> Optional[float]:
    """Parses a float cleanly, returning None if missing, nan, or inf."""
    if val is None or isinstance(val, bool):
        return None
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (ValueError, TypeError):
        return None


def _parse_vector2d(pt_data: Any) -> Optional[Vector2D]:
    """Parses a Vector2D from dict, tuple, or list."""
    if pt_data is None:
        return None
    if isinstance(pt_data, Vector2D):
        return pt_data if pt_data.is_valid() else None
    if isinstance(pt_data, dict):
        x = _safe_float(pt_data.get("x", pt_data.get("x0", pt_data.get("start_x"))))
        y = _safe_float(pt_data.get("y", pt_data.get("y0", pt_data.get("start_y"))))
        if x is not None and y is not None:
            return Vector2D(x=x, y=y)
    elif isinstance(pt_data, (list, tuple)) and len(pt_data) >= 2:
        x = _safe_float(pt_data[0])
        y = _safe_float(pt_data[1])
        if x is not None and y is not None:
            return Vector2D(x=x, y=y)
    return None


def _parse_provenance(prov_data: Any) -> Provenance:
    """Extracts end-to-end drawing provenance from a production dict or Provenance instance."""
    if isinstance(prov_data, Provenance):
        return prov_data
    if not isinstance(prov_data, dict):
        return Provenance()

    source_pdf = str(prov_data.get("source_pdf") or prov_data.get("pdf") or prov_data.get("document_name") or "")
    page_num = prov_data.get("page_no") or prov_data.get("page_number") or prov_data.get("page_1based") or prov_data.get("page")
    try:
        page_num = int(page_num) if page_num is not None else None
    except (ValueError, TypeError):
        page_num = None

    drawing_id = str(prov_data.get("drawing_id") or prov_data.get("drawing_no") or prov_data.get("sheet_id") or "")
    scale_source = str(prov_data.get("scale_source") or prov_data.get("stated_scale") or "")
    
    traces = prov_data.get("contributing_evidence") or prov_data.get("evidence_traces") or []
    if isinstance(traces, str):
        traces = [traces]
    elif not isinstance(traces, list):
        traces = []

    src_coords = prov_data.get("source_coords") or prov_data.get("bbox")

    return Provenance(
        source_pdf=source_pdf,
        page_number=page_num,
        drawing_id=drawing_id,
        scale_source=scale_source,
        source_coords=src_coords,
        workspace_id=str(prov_data.get("workspace_id") or "") or None,
        document_id=str(prov_data.get("document_id") or "") or None,
        page_id=str(prov_data.get("page_id") or "") or None,
        wall_ref=str(prov_data.get("wall_ref") or "") or None,
        opening_instance_id=str(prov_data.get("opening_instance_id") or "") or None,
        plan_geometry_signature=str(prov_data.get("plan_geometry_signature") or "") or None,
        coordinate_space=str(prov_data.get("coordinate_space") or "PLAN_2D"),
        producer_module=str(prov_data.get("producer_module") or "") or None,
        producer_version=str(prov_data.get("producer_version") or "") or None,
        contributing_evidence=[str(t) for t in traces if t],
    )


def resolve_canonical_level(
    level_claim: Optional[Union[str, int]],
    level_map: Dict[str, CanonicalLevel],
    unresolved_container: CanonicalLevel,
    skipped_items: List[Dict[str, Any]]
) -> Tuple[CanonicalLevel, str]:
    """
    SECTION B: Level Resolution Service.
    Preserves 5 explicit level states.
    """
    if level_claim is None:
        return unresolved_container, "no_level_evidence"

    claim_str = str(level_claim).strip()
    if not claim_str:
        return unresolved_container, "no_level_evidence"

    if claim_str in level_map:
        matched_lvl = level_map[claim_str]
        if matched_lvl.elevation_m is not None:
            return matched_lvl, "objectively_known_level"
        else:
            return matched_lvl, "unresolved_level_in_map"

    skipped_items.append({
        "item": f"level_claim_{claim_str}",
        "reason": f"wrong_known_level_conflict: level '{claim_str}' not found in known storeys"
    })
    return unresolved_container, "wrong_known_level_conflict"


def registered_wall_to_canonical_input(
    wall_dict: Dict[str, Any],
    skipped_items: List[Dict[str, Any]],
    is_validated_internal_workspace: bool = False
) -> Tuple[Optional[CanonicalWall], List[Dict[str, Any]]]:
    """
    SECTION 2 & D & M: Adapts the EXACT real v139 wall contract.
    Preserves facade/side identity and wall_ref as strong ID.
    """
    if not isinstance(wall_dict, dict):
        return None, []

    wall_ref = str(wall_dict.get("wall_ref") or wall_dict.get("id") or "")
    if not wall_ref:
        skipped_items.append({"item": wall_dict, "reason": "Missing wall_ref/id"})
        return None, []

    p1 = _parse_vector2d(wall_dict.get("a") or wall_dict.get("start_point"))
    p2 = _parse_vector2d(wall_dict.get("b") or wall_dict.get("end_point"))

    if not (p1 and p2):
        skipped_items.append({"item": wall_ref, "reason": "insufficient_geometry_endpoints_missing"})
        return None, []

    computed_len = p1.distance_to(p2)
    reported_len = _safe_float(wall_dict.get("length_m"))
    if reported_len is not None and abs(computed_len - reported_len) > 0.05:
        skipped_items.append({
            "item": wall_ref,
            "reason": f"length_disagreement: computed {computed_len:.2f}m vs reported {reported_len:.2f}m"
        })

    height_status = str(wall_dict.get("height_status") or "").lower()
    height_conf = str(wall_dict.get("height_confidence") or "").lower()
    raw_height = _safe_float(wall_dict.get("height_m"))

    if height_status in ("provisional", "review", "default", "unverified") or height_conf in ("review", "default", "unverified"):
        c_height = None
        c_review = ReviewState.REVIEW_REQUIRED
        skipped_items.append({"item": wall_ref, "reason": f"height_provisional_rejected: {height_status}/{height_conf}"})
    elif raw_height is not None and raw_height > 0:
        c_height = raw_height
        c_review = ReviewState.CONFIRMED if height_status in ("confirmed", "verified") else ReviewState.INFERRED
    else:
        c_height = None
        c_review = ReviewState.REVIEW_REQUIRED

    th_wall = _safe_float(wall_dict.get("thickness_m") if "thickness_m" in wall_dict else wall_dict.get("thickness"))
    prov = _parse_provenance(wall_dict.get("provenance") or wall_dict)
    prov.wall_ref = wall_ref
    prov.producer_module = "pb_unified_building_v139"

    side_str = str(wall_dict.get("side") or "").upper().strip()

    wall_takeoff_eligible = is_validated_internal_workspace and (c_height is not None) and (c_review in (ReviewState.CONFIRMED, ReviewState.INFERRED))

    c_wall = CanonicalWall(
        id=wall_ref,
        name=f"Wall {wall_ref}",
        level_id="lvl_unresolved_review",
        start_point=p1,
        end_point=p2,
        thickness_m=th_wall,
        height_m=c_height,
        is_external=parse_strict_bool(wall_dict.get("is_external", True)),
        substrate=wall_dict.get("substrate"),
        finish=wall_dict.get("finish"),
        confidence=_safe_float(wall_dict.get("confidence")),
        review_state=c_review,
        provenance=prov,
        deduction_authority=False,  # Evaluated after opening revalidation
        takeoff_eligible=wall_takeoff_eligible,
        metadata={"side": side_str, "facade_id": wall_dict.get("facade_id")} if side_str else {}
    )

    nested_openings_raw = wall_dict.get("openings") or []
    return c_wall, nested_openings_raw


def revalidate_b5_opening(opening_dict: Dict[str, Any]) -> bool:
    """
    SECTION 3 & C: Dynamic B5 opening authority revalidation gate.
    Re-runs pb_opening_production_v175.is_authorised_deduction(opening_dict).
    """
    if not isinstance(opening_dict, dict):
        return False
    try:
        import pb_opening_production_v175 as op_prod
        return bool(op_prod.is_authorised_deduction(opening_dict))
    except Exception:
        return False


def collect_workspace_3d_evidence(app: Any, workspace_id: Any) -> Dict[str, Any]:
    """
    SECTION 1, 2, 4, 5, 6: Reads-only Authoritative Workspace Evidence Snapshot v3.
    
    GUARANTEES:
    1. Obey require_workspace_id() (no fallback to workspace 1 or 101).
    2. All app.lquery() interactions consume List[Dict[str, Any]] (dictionary row access, page_no schema).
    3. Snapshot documents & pages carrying workspace ownership.
    4. Categorized producer diagnostics log (no silent passes).
    """
    wid_int = require_workspace_id(workspace_id)
    ws_meta = None
    diagnostics_log: List[Dict[str, Any]] = []

    # SECTION 1: Production app.lquery() returns List[Dict[str, Any]]
    if app and hasattr(app, "lquery"):
        try:
            rows = app.lquery("SELECT id, job_no, job_name, builder_client, site_address FROM workspaces WHERE id=?", (wid_int,))
            if rows and len(rows) > 0:
                row0 = rows[0]
                if isinstance(row0, dict):
                    ws_meta = {
                        "id": row0.get("id", wid_int),
                        "job_no": row0.get("job_no", ""),
                        "name": row0.get("job_name", f"Workspace #{wid_int}"),
                        "builder_client": row0.get("builder_client", ""),
                        "site_address": row0.get("site_address", ""),
                    }
                else:
                    diagnostics_log.append({"type": "schema_contract_mismatch", "error": "lquery returned non-dict row"})
        except Exception as e:
            diagnostics_log.append({"type": "producer_exception", "producer": "database_workspaces", "error": str(e)})

    if not ws_meta and app and hasattr(app, "workspaces") and isinstance(app.workspaces, dict):
        ws_raw = app.workspaces.get(wid_int)
        if isinstance(ws_raw, dict):
            ws_meta = ws_raw

    if not ws_meta:
        raise ValueError(f"Invalid or missing workspace ID #{wid_int} in database.")

    # SECTION 2: Query Documents carrying workspace ownership
    documents = []
    if app and hasattr(app, "lquery"):
        try:
            d_rows = app.lquery(
                "SELECT id, workspace_id, file_name, sha256, category, page_count, source_type "
                "FROM documents WHERE workspace_id=?", (wid_int,)
            )
            for dr in d_rows:
                if isinstance(dr, dict):
                    documents.append({
                        "workspace_id": wid_int,
                        "document_id": dr.get("id"),
                        "file_name": dr.get("file_name"),
                        "sha256": dr.get("sha256"),
                        "category": dr.get("category"),
                        "page_count": dr.get("page_count"),
                        "source_type": dr.get("source_type"),
                    })
        except Exception as e:
            diagnostics_log.append({"type": "producer_exception", "producer": "database_documents", "error": str(e)})

    # SECTION 1: Query Pages carrying workspace ownership using REAL schema (page_no, NOT page_number!)
    pages = []
    if app and hasattr(app, "lquery"):
        try:
            p_rows = app.lquery(
                "SELECT id, workspace_id, document_id, page_no, page_label, page_type, scale_text, px_per_m, width_px, height_px, render_zoom, selected "
                "FROM pages WHERE workspace_id=?", (wid_int,)
            )
            for pr in p_rows:
                if isinstance(pr, dict):
                    pages.append({
                        "workspace_id": wid_int,
                        "page_id": pr.get("id"),
                        "document_id": pr.get("document_id"),
                        "page_no": pr.get("page_no"),
                        "page_label": pr.get("page_label"),
                        "page_type": pr.get("page_type"),
                        "scale_text": pr.get("scale_text"),
                        "px_per_m": _safe_float(pr.get("px_per_m")),
                        "width_px": _safe_float(pr.get("width_px")),
                        "height_px": _safe_float(pr.get("height_px")),
                        "render_zoom": _safe_float(pr.get("render_zoom")),
                        "selected": pr.get("selected"),
                    })
        except Exception as e:
            diagnostics_log.append({"type": "producer_exception", "producer": "database_pages", "error": str(e)})

    # SECTION D: Query Registered Walls via v139
    reg_walls = []
    try:
        import pb_unified_building_v139 as ub
        if hasattr(ub, "build_registered_walls"):
            reg_walls = ub.build_registered_walls(app, wid_int)
    except Exception as e:
        diagnostics_log.append({"type": "producer_exception", "producer": "pb_unified_building_v139", "error": str(e)})

    # SECTION 7: Query page-scoped floor mapper saved state (v127/v128)
    mapper_shapes = []
    if app and hasattr(app, "workspace_setting"):
        target_pages = pages if pages else [{"page_id": 1}]
        for p in target_pages:
            p_id = p.get("page_id") or p.get("id", 1)
            setting_key = f"floor_mapper_v127_page_{p_id}"
            raw_setting = app.workspace_setting(wid_int, setting_key, None)
            
            if raw_setting:
                parsed_setting = None
                if isinstance(raw_setting, str):
                    try:
                        parsed_setting = json.loads(raw_setting)
                    except Exception as e:
                        diagnostics_log.append({"type": "schema_contract_mismatch", "producer": "floor_mapper", "key": setting_key, "error": str(e)})
                elif isinstance(raw_setting, dict):
                    parsed_setting = raw_setting

                if parsed_setting:
                    mapper_shapes.append({
                        "workspace_id": wid_int,
                        "page_id": p_id,
                        "setting_key": setting_key,
                        "width_px": p.get("width_px"),
                        "height_px": p.get("height_px"),
                        "mapper_setting": parsed_setting
                    })

    # SECTION 13: Query Roof Evidence & Caps via v140 as two separate contracts
    roof_data = {}
    try:
        import pb_roof_envelope_v140 as re
        if hasattr(re, "roof_evidence"):
            roof_data["roof_evidence"] = re.roof_evidence(app, wid_int)
        if hasattr(re, "roof_caps"):
            roof_data["roof_caps"] = re.roof_caps(app, wid_int, reg_walls)
    except Exception as e:
        diagnostics_log.append({"type": "producer_exception", "producer": "pb_roof_envelope_v140", "error": str(e)})

    # SECTION 24: Query Takeoff Rows using app.registered_wall_takeoff_rows_v139 or database
    takeoff_rows = []
    if app and hasattr(app, "registered_wall_takeoff_rows_v139"):
        try:
            t_rows = app.registered_wall_takeoff_rows_v139(wid_int)
            for tr in (t_rows or []):
                if isinstance(tr, dict):
                    takeoff_rows.append({
                        "workspace_id": wid_int,
                        "id": tr.get("id"),
                        "wall_ref": tr.get("source_reference") or tr.get("wall_ref"),
                        "quantity": _safe_float(tr.get("quantity") or tr.get("m2")),
                        "unit": str(tr.get("unit") or "").lower().strip(),
                        "row_role": str(tr.get("row_role") or "wall").lower().strip(),
                    })
        except Exception as e:
            diagnostics_log.append({"type": "producer_exception", "producer": "v139_wall_takeoff_rows", "error": str(e)})

    if len(takeoff_rows) == 0 and app and hasattr(app, "lquery"):
        try:
            t_rows = app.lquery(
                "SELECT id, workspace_id, section, element, location, quantity, unit, quantity_status, row_role "
                "FROM takeoff_rows WHERE workspace_id=?", (wid_int,)
            )
            for tr in t_rows:
                if isinstance(tr, dict):
                    takeoff_rows.append({
                        "workspace_id": wid_int,
                        "id": tr.get("id"),
                        "section": tr.get("section"),
                        "element": tr.get("element"),
                        "location": tr.get("location"),
                        "quantity": _safe_float(tr.get("quantity")),
                        "unit": str(tr.get("unit") or "").lower().strip(),
                        "quantity_status": tr.get("quantity_status"),
                        "row_role": str(tr.get("row_role") or "").lower().strip(),
                    })
        except Exception as e:
            diagnostics_log.append({"type": "producer_exception", "producer": "database_takeoff_rows", "error": str(e)})

    return {
        "workspace_metadata": ws_meta,
        "documents": documents,
        "pages": pages,
        "registered_walls": reg_walls,
        "mapper_shapes": mapper_shapes,
        "roof_data": roof_data,
        "takeoff_rows": takeoff_rows,
        "producer_versions": {
            "3d_engine": "v1.5.1",
            "v139_walls": "v139",
            "v175_openings": "v175",
            "v128_mapper": "v128",
            "v140_roof": "v140",
        },
        "diagnostics_log": diagnostics_log,
    }


def planreader_to_canonical_model(
    production_payload: Dict[str, Any],
    *,
    is_validated_internal_workspace: bool = False
) -> Tuple[CanonicalProject, List[Dict[str, Any]]]:
    """
    Converts a PlanReader production output payload into a CanonicalProject graph.
    Returns (canonical_project, skipped_items_diagnostics).
    """
    if not isinstance(production_payload, dict):
        raise ValueError("Production payload must be a non-null dictionary")

    skipped_items: List[Dict[str, Any]] = []
    is_synthetic = parse_strict_bool(production_payload.get("is_synthetic_demo", False))

    ws_meta = production_payload.get("workspace_metadata") or {}
    proj_id = str(production_payload.get("project_id") or ws_meta.get("id") or "proj_prod_001")
    proj_name = str(production_payload.get("project_name") or ws_meta.get("name") or ws_meta.get("job_name") or "PlanReader Production Project")

    project = CanonicalProject(
        id=proj_id,
        name=proj_name,
        is_synthetic_demo=is_synthetic,
        confidence=_safe_float(production_payload.get("confidence")),
        review_state=ReviewState.REVIEW_REQUIRED,
        takeoff_eligible=False,
        deduction_authority=False,
    )

    bld = CanonicalBuilding(id="bld_main", name="Main Building", parent_id=project.id)
    level_map: Dict[str, CanonicalLevel] = {}

    unresolved_level_container = CanonicalLevel(
        id="lvl_unresolved_review",
        name="Unresolved Level (Review Required)",
        elevation_m=None,
        height_m=None,
        review_state=ReviewState.REVIEW_REQUIRED
    )

    levels_raw = production_payload.get("levels") or production_payload.get("storeys") or []
    if isinstance(levels_raw, list):
        for idx, l_dict in enumerate(levels_raw):
            if not isinstance(l_dict, dict):
                continue
            l_id = str(l_dict.get("id") or f"lvl_{idx}")
            l_name = str(l_dict.get("name") or f"Level {idx}")
            l_elev = _safe_float(l_dict.get("elevation_m") if "elevation_m" in l_dict else l_dict.get("elevation"))
            
            c_lvl = CanonicalLevel(
                id=l_id,
                name=l_name,
                elevation_m=l_elev,
                height_m=_safe_float(l_dict.get("height_m")),
                review_state=ReviewState.CONFIRMED if l_elev is not None else ReviewState.REVIEW_REQUIRED,
                provenance=_parse_provenance(l_dict),
            )
            level_map[l_id] = c_lvl

    # Process Walls
    walls_raw = production_payload.get("walls") or production_payload.get("registered_walls") or []
    if isinstance(walls_raw, list):
        for w_dict in walls_raw:
            if not isinstance(w_dict, dict):
                continue

            c_wall, nested_ops = registered_wall_to_canonical_input(
                w_dict, skipped_items, is_validated_internal_workspace=is_validated_internal_workspace
            )
            if c_wall is None:
                continue

            claimed_lvl = w_dict.get("level_id") or w_dict.get("storey_id")
            target_lvl, res_reason = resolve_canonical_level(claimed_lvl, level_map, unresolved_level_container, skipped_items)
            c_wall.level_id = target_lvl.id
            target_lvl.walls.append(c_wall)

            # Process nested openings attached to host wall
            for op_dict in nested_ops:
                if not isinstance(op_dict, dict):
                    continue

                # SECTION 20: Verify nested host identity matches host wall!
                resolved_host = op_dict.get("resolved_wall_ref") or op_dict.get("wall_ref")
                if resolved_host and str(resolved_host) != c_wall.id:
                    skipped_items.append({"item": op_dict, "reason": f"wrong_host_conflict: opening claims host '{resolved_host}' but nested on '{c_wall.id}'"})
                    continue

                op_id = str(op_dict.get("id") or op_dict.get("opening_instance_id") or f"op_{len(c_wall.openings) + 1}")
                op_mark = str(op_dict.get("mark") or op_dict.get("name") or "OP")
                
                b5_authorized = revalidate_b5_opening(op_dict)
                
                # SECTION 18: If opening is B5 authorized, grant wall deduction gate!
                if b5_authorized:
                    c_wall.deduction_authority = True

                w_op = _safe_float(op_dict.get("width_m") if "width_m" in op_dict else op_dict.get("width"))
                h_op = _safe_float(op_dict.get("height_m") if "height_m" in op_dict else op_dict.get("height"))
                off_op = _safe_float(op_dict.get("offset_m") if "offset_m" in op_dict else (op_dict.get("offset_along_wall_m") if "offset_along_wall_m" in op_dict else op_dict.get("offset")))
                sill_op = _safe_float(op_dict.get("sill_m") if "sill_m" in op_dict else (op_dict.get("sill_height_m") if "sill_height_m" in op_dict else op_dict.get("sill")))
                
                raw_type = str(op_dict.get("opening_type") or op_dict.get("type") or "").upper().strip()
                c_op_type = ObjectType.DOOR if "DOOR" in raw_type else (ObjectType.WINDOW if "WIN" in raw_type else ObjectType.OPENING)

                op_prov = _parse_provenance(op_dict.get("provenance") or op_dict)
                op_prov.opening_instance_id = str(op_dict.get("opening_instance_id") or op_id)
                op_prov.plan_geometry_signature = str(op_dict.get("plan_geometry_signature") or "")

                c_op = CanonicalOpening(
                    id=op_id,
                    name=op_mark,
                    wall_id=c_wall.id,
                    level_id=target_lvl.id,
                    opening_type=c_op_type,
                    offset_along_wall_m=off_op,
                    sill_height_m=sill_op,
                    width_m=w_op,
                    height_m=h_op,
                    mark=op_mark,
                    confidence=_safe_float(op_dict.get("confidence")),
                    review_state=ReviewState.REVIEW_REQUIRED,
                    provenance=op_prov,
                    deduction_authority=b5_authorized,
                )
                c_wall.openings.append(c_op)

    # SECTION 7, 8, 9, 10, 11: Real v127/v128 Floor Mapper Metric Conversion
    mapper_shapes_raw = production_payload.get("mapper_shapes") or production_payload.get("polygons") or []
    if isinstance(mapper_shapes_raw, list):
        for item in mapper_shapes_raw:
            if not isinstance(item, dict):
                continue
            
            item_wid = item.get("workspace_id")
            if item_wid and str(item_wid) != str(proj_id):
                skipped_items.append({"item": item, "reason": "foreign_workspace_evidence_rejected"})
                continue

            setting = item.get("mapper_setting") or item
            shapes = setting.get("boxes") or setting.get("shapes") or [setting]
            
            # SECTION 7: Calculate px_per_m from calibration line
            calib_dict = setting.get("calibration") or {}
            w_px = _safe_float(item.get("width_px") or setting.get("width_px"))
            h_px = _safe_float(item.get("height_px") or setting.get("height_px"))
            
            # SECTION 9: Delete 1000x1000 page dimension fallbacks! If missing, attempt calculation or skip
            px_per_m = _safe_float(calib_dict.get("px_per_m"))
            if not px_per_m and calib_dict.get("len_m") and calib_dict.get("x1") is not None:
                dx = float(calib_dict["x2"]) - float(calib_dict["x1"])
                dy = float(calib_dict["y2"]) - float(calib_dict["y1"])
                dist_px = math.hypot(dx, dy)
                if dist_px > 0 and float(calib_dict["len_m"]) > 0:
                    px_per_m = dist_px / float(calib_dict["len_m"])

            for p_idx, p_dict in enumerate(shapes if isinstance(shapes, list) else [shapes]):
                if not isinstance(p_dict, dict):
                    continue

                pts = p_dict.get("polygon") or p_dict.get("points") or []
                area_m2 = _safe_float(p_dict.get("specified_floor_area_m2") or p_dict.get("area_m2") or p_dict.get("area"))

                # SECTION 8 & 9: Requires valid page dimensions and calibration!
                if isinstance(pts, list) and len(pts) >= 3 and px_per_m and px_per_m > 0 and w_px and h_px:
                    vertices: List[Vector2D] = []
                    for pt in pts:
                        p_vec = _parse_vector2d(pt)
                        if p_vec:
                            # SECTION 8: Percentage coordinates 0-100 to page pixels -> plan metres
                            px_x = (p_vec.x / 100.0) * w_px
                            px_y = (p_vec.y / 100.0) * h_px
                            m_x = px_x / px_per_m
                            m_y = px_y / px_per_m
                            vertices.append(Vector2D(x=m_x, y=m_y))

                    if len(vertices) >= 3:
                        # SECTION 11: Floor on unresolved level stays REVIEW_REQUIRED
                        c_floor = CanonicalFloor(
                            id=str(p_dict.get("id") or f"floor_{p_idx}"),
                            name=str(p_dict.get("name") or f"Floor {p_idx + 1}"),
                            level_id=unresolved_level_container.id,
                            polygon=vertices,
                            specified_floor_area_m2=area_m2,
                            review_state=ReviewState.REVIEW_REQUIRED,  # SECTION 11
                        )
                        unresolved_level_container.floors.append(c_floor)
                elif area_m2 is not None and area_m2 > 0:
                    skipped_items.append({
                        "item": f"floor_manual_allowance_{p_idx}",
                        "reason": f"manual_m2_allowance_no_physical_polygon: {area_m2:.2f} m²"
                    })

    # SECTION 13, 14, 15, 16: Exact v140 Roof Evidence & Caps Integration
    roof_data = production_payload.get("roof_data") or {}
    if isinstance(roof_data, dict) and roof_data:
        roof_caps = roof_data.get("roof_caps") or roof_data.get("caps") or []
        for r_idx, r_dict in enumerate(roof_caps if isinstance(roof_caps, list) else []):
            if isinstance(r_dict, dict):
                r_poly = [_parse_vector2d(pt) for pt in (r_dict.get("polygon") or r_dict.get("points") or []) if _parse_vector2d(pt) is not None]
                raw_type = r_dict.get("roof_type")
                r_type_str = str(raw_type) if raw_type is not None else None
                
                c_roof = CanonicalRoof(
                    id=str(r_dict.get("id") or f"roof_{r_idx}"),
                    name=str(r_dict.get("name") or f"Roof Envelope {r_idx + 1}"),
                    level_id=unresolved_level_container.id,
                    polygon=r_poly,
                    pitch_deg=_safe_float(r_dict.get("pitch_deg") if "pitch_deg" in r_dict else r_dict.get("pitches_deg")),
                    roof_type=r_type_str,  # SECTION 16: Unknown stays None (never fallback to FLAT)
                    review_state=ReviewState.REVIEW_REQUIRED,  # SECTION 15: Fences legacy 2.7m fallbacks
                )
                unresolved_level_container.roofs.append(c_roof)

    for unhandled in ["CEILING", "SOFFIT", "BALCONY", "PARAPET", "COLUMN", "BALUSTRADE", "SCREEN", "SPACE", "SURFACE"]:
        skipped_items.append({
            "item": unhandled,
            "reason": f"producer_not_available: No active producer registered for {unhandled}"
        })

    all_levels = list(level_map.values())
    if len(unresolved_level_container.walls) > 0 or len(unresolved_level_container.floors) > 0 or len(unresolved_level_container.roofs) > 0:
        all_levels.append(unresolved_level_container)

    if len(all_levels) == 0:
        all_levels.append(unresolved_level_container)

    bld.levels = all_levels
    project.buildings = [bld]

    return project, skipped_items


def planreader_workspace_to_canonical(
    app: Any,
    workspace_id: Any
) -> WorkspaceCanonicalResult:
    """
    SECTION A, C, E, H: Builds canonical 3D model from workspace DB and evidence snapshot v3.
    Returns WorkspaceCanonicalResult namedtuple.
    Fails closed if workspace ID is invalid or missing in database!
    """
    wid_int = require_workspace_id(workspace_id)

    # SECTION D: Collect authoritative workspace evidence snapshot v3
    snapshot = collect_workspace_3d_evidence(app, wid_int)

    # Compute deterministic fingerprint
    from pb_canonical_persistence import compute_workspace_source_fingerprint
    snapshot_fp = compute_workspace_source_fingerprint(snapshot)

    # Convert snapshot into Canonical Project
    project, skipped_items = planreader_to_canonical_model(snapshot, is_validated_internal_workspace=True)
    diagnostics = generate_production_diagnostics_report(project, workspace_data=snapshot, skipped_items=skipped_items)

    return WorkspaceCanonicalResult(
        project=project,
        snapshot=snapshot,
        snapshot_fingerprint=snapshot_fp,
        diagnostics=diagnostics,
        skipped_items=skipped_items
    )
