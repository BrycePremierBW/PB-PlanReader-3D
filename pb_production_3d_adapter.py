"""
PlanReader Production 3D Model Adapter Module (Phase 5G Production Contract Lock).

Provides a clean production adapter converting existing PlanReader production payloads,
documents, pages, takeoff rows, drawing intelligence, opening schedules, and elevation evidence
into a validated CanonicalProject object graph for 3D WebGL BIM viewing.

SAFETY GUARANTEES:
1. Does NOT build a second measurement/extraction engine.
2. Does NOT grant new deduction authority — B5/v175 is sole deduction authority.
3. Untrusted/uploaded JSON CANNOT forge deduction_authority or takeoff_eligible.
4. Fails closed if workspace ID is missing or invalid in database (require_workspace_id).
5. All app.lquery() calls consume List[Dict[str, Any]] (dictionary row access, page_no schema).
6. Uses REAL pb_floor_mapper_v127.calibration_px_per_m and pb_floor_mapper_v128._points_from_shape.
7. Calls app.registered_wall_takeoff_rows_v139(reg_walls) with wall iterable, NOT workspace_id.
8. Snapshot v3 validates document-page workspace ownership strictly.
"""

import math
import sqlite3
import json
import hashlib
import re
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
from pb_geometry_services import potential_net_wall_area, validate_opening_geometry
from pb_3d_diagnostics import generate_production_diagnostics_report
from pb_floor_mapper_v127 import calibration_px_per_m
from pb_floor_mapper_v128 import _points_from_shape


class WorkspaceCanonicalResult(NamedTuple):
    """Encapsulates the complete authoritative workspace conversion result."""
    project: CanonicalProject
    snapshot: Dict[str, Any]
    snapshot_fingerprint: str
    diagnostics: Dict[str, Any]
    skipped_items: List[Dict[str, Any]]


def require_workspace_id(workspace_id: Any) -> int:
    """
    SECTION 3 & C: Validates workspace identity strictly.
    Rejects booleans, zero, negative numbers, non-integral floats (e.g. 101.5), malformed strings, or invalid types.
    """
    if workspace_id is None or isinstance(workspace_id, bool):
        raise ValueError(f"Invalid workspace ID: {workspace_id} (must be a positive integer)")

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
    """SECTION B & H: Level Resolution Service."""
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

    # Create level on demand for valid named storey claims
    new_lvl = CanonicalLevel(
        id=f"lvl_{claim_str.lower().replace(' ', '_')}",
        name=claim_str,
        elevation_m=None,
        height_m=None,
        review_state=ReviewState.REVIEW_REQUIRED,
    )
    level_map[claim_str] = new_lvl
    return new_lvl, "created_named_storey"


def registered_wall_to_canonical_input(
    wall_dict: Dict[str, Any],
    skipped_items: List[Dict[str, Any]],
    is_validated_internal_workspace: bool = False
) -> Tuple[Optional[CanonicalWall], List[Dict[str, Any]]]:
    """SECTION D & M: Adapts real v139 wall contract."""
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
    """SECTION 3, C, M: Dynamic B5 opening authority revalidation gate."""
    if not isinstance(opening_dict, dict):
        return False
    try:
        import pb_opening_production_v175 as op_prod
        return bool(op_prod.is_authorised_deduction(opening_dict))
    except Exception:
        return False


def collect_workspace_3d_evidence(app: Any, workspace_id: Any) -> Dict[str, Any]:
    """
    SECTION 1, 2, 4, 5, 6, Q: Reads-only Authoritative Workspace Evidence Snapshot v3.
    """
    wid_int = require_workspace_id(workspace_id)
    ws_meta = None
    diagnostics_log: List[Dict[str, Any]] = []

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
    doc_by_id = {}
    if app and hasattr(app, "lquery"):
        try:
            d_rows = app.lquery(
                "SELECT id, workspace_id, file_name, sha256, category, page_count, source_type "
                "FROM documents WHERE workspace_id=?", (wid_int,)
            )
            for dr in d_rows:
                if isinstance(dr, dict):
                    d_obj = {
                        "workspace_id": wid_int,
                        "document_id": dr.get("id"),
                        "file_name": dr.get("file_name"),
                        "sha256": dr.get("sha256"),
                        "category": dr.get("category"),
                        "page_count": dr.get("page_count"),
                        "source_type": dr.get("source_type"),
                    }
                    documents.append(d_obj)
                    if dr.get("id") is not None:
                        doc_by_id[dr.get("id")] = d_obj
        except Exception as e:
            diagnostics_log.append({"type": "producer_exception", "producer": "database_documents", "error": str(e)})

    # SECTION 1 & Q: Query Pages and validate document-page ownership!
    pages = []
    if app and hasattr(app, "lquery"):
        try:
            p_rows = app.lquery(
                "SELECT id, workspace_id, document_id, page_no, page_label, page_type, scale_text, px_per_m, width_px, height_px, render_zoom, selected "
                "FROM pages WHERE workspace_id=?", (wid_int,)
            )
            for pr in p_rows:
                if isinstance(pr, dict):
                    doc_id = pr.get("document_id")
                    # SECTION Q: Validate page belongs to document in same workspace!
                    if documents and doc_id not in doc_by_id:
                        diagnostics_log.append({
                            "type": "stale_reference",
                            "page_id": pr.get("id"),
                            "error": f"Page {pr.get('id')} references missing/foreign document {doc_id}"
                        })
                        continue

                    pages.append({
                        "workspace_id": wid_int,
                        "page_id": pr.get("id"),
                        "document_id": doc_id,
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

    # SECTION C: NO PAGE FALLBACK! Only query mapper settings for actual workspace pages
    mapper_shapes = []
    if pages and app and hasattr(app, "workspace_setting"):
        for p in pages:
            p_id = p.get("page_id")
            if p_id is None:
                continue
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
                        "page_px_per_m": p.get("px_per_m"),
                        "mapper_setting": parsed_setting
                    })

    # SECTION 13 & P: Query Roof Evidence & Caps via v140
    roof_data = {}
    try:
        import pb_roof_envelope_v140 as re
        if hasattr(re, "roof_evidence"):
            roof_data["roof_evidence"] = re.roof_evidence(app, wid_int)
        if hasattr(re, "roof_caps"):
            roof_data["roof_caps"] = re.roof_caps(app, wid_int, reg_walls)
    except Exception as e:
        diagnostics_log.append({"type": "producer_exception", "producer": "pb_roof_envelope_v140", "error": str(e)})

    # SECTION E: Correct call signature for v139 takeoff rows producer -> app.registered_wall_takeoff_rows_v139(reg_walls)
    takeoff_rows = []
    if app and hasattr(app, "registered_wall_takeoff_rows_v139"):
        try:
            # Pass reg_walls iterable, NOT workspace_id integer!
            t_rows = app.registered_wall_takeoff_rows_v139(reg_walls)
            for tr in (t_rows or []):
                if isinstance(tr, dict):
                    src_ref = tr.get("source_reference") or tr.get("wall_ref") or ""
                    # SECTION F: Extract wall_ref identity from 'PB Unified Building v1.3.9 · W01'
                    m_wall = re.search(r"(?:W\d+|W-[A-Z0-9]+|\bW\d+\b)", src_ref)
                    derived_wall_ref = m_wall.group(0) if m_wall else src_ref

                    takeoff_rows.append({
                        "workspace_id": wid_int,
                        "id": tr.get("id"),
                        "wall_ref": derived_wall_ref,
                        "source_reference": src_ref,
                        "quantity": _safe_float(tr.get("quantity") or tr.get("net_m2") or tr.get("m2")),
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
                    loc_str = str(tr.get("location") or "")
                    m_wall = re.search(r"(?:W\d+|W-[A-Z0-9]+|\bW\d+\b)", loc_str)
                    derived_ref = m_wall.group(0) if m_wall else None

                    takeoff_rows.append({
                        "workspace_id": wid_int,
                        "id": tr.get("id"),
                        "wall_ref": derived_ref,
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
    """Converts a PlanReader production output payload into a CanonicalProject graph."""
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

            claimed_lvl = w_dict.get("level") or w_dict.get("level_name") or w_dict.get("level_id") or w_dict.get("storey_id")
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

    # SECTION A & B: Real v127/v128 Floor Mapper Metric Conversion using calibration_px_per_m & _points_from_shape
    mapper_shapes_raw = production_payload.get("mapper_shapes") or production_payload.get("polygons") or []
    if isinstance(mapper_shapes_raw, list):
        for item in mapper_shapes_raw:
            if not isinstance(item, dict):
                continue
            
            setting = item.get("mapper_setting") or item
            w_px = _safe_float(item.get("width_px") or setting.get("width_px"))
            h_px = _safe_float(item.get("height_px") or setting.get("height_px"))
            
            # SECTION A: Use REAL pb_floor_mapper_v127.calibration_px_per_m()
            calib_dict = setting.get("calibration") or {}
            px_per_m = calibration_px_per_m(calib_dict, w_px or 0.0, h_px or 0.0) if (w_px and h_px) else 0.0
            if px_per_m <= 0:
                px_per_m = _safe_float(item.get("page_px_per_m") or calib_dict.get("px_per_m")) or 0.0

            shapes = setting.get("boxes") or setting.get("shapes") or [setting]

            for p_idx, p_dict in enumerate(shapes if isinstance(shapes, list) else [shapes]):
                if not isinstance(p_dict, dict):
                    continue

                # SECTION B: Use REAL pb_floor_mapper_v128._points_from_shape() (supports irregular polygons and v127 rectangles)
                pts = _points_from_shape(p_dict)
                manual_m2 = _safe_float(p_dict.get("manual_m2") if "manual_m2" in p_dict else p_dict.get("specified_floor_area_m2"))

                # Require valid page dimensions and calibration for physical floor!
                if len(pts) >= 3 and px_per_m > 0 and w_px and h_px:
                    vertices: List[Vector2D] = []
                    for pt in pts:
                        px_x = (pt["x"] / 100.0) * w_px
                        px_y = (pt["y"] / 100.0) * h_px
                        m_x = px_x / px_per_m
                        m_y = px_y / px_per_m
                        vertices.append(Vector2D(x=m_x, y=m_y))

                    if len(vertices) >= 3:
                        c_floor = CanonicalFloor(
                            id=str(p_dict.get("id") or f"floor_{p_idx}"),
                            name=str(p_dict.get("name") or f"Floor {p_idx + 1}"),
                            level_id=unresolved_level_container.id,
                            polygon=vertices,
                            specified_floor_area_m2=manual_m2,
                            review_state=ReviewState.REVIEW_REQUIRED,
                        )
                        unresolved_level_container.floors.append(c_floor)
                elif manual_m2 is not None and manual_m2 > 0:
                    skipped_items.append({
                        "item": f"floor_manual_allowance_{p_idx}",
                        "reason": f"manual_m2_allowance_no_physical_polygon: {manual_m2:.2f} m²"
                    })

    # SECTION P: Real v140 Roof Evidence & Caps Integration
    roof_data = production_payload.get("roof_data") or {}
    if isinstance(roof_data, dict) and roof_data:
        roof_caps = roof_data.get("roof_caps") or roof_data.get("caps") or []
        for r_idx, r_dict in enumerate(roof_caps if isinstance(roof_caps, list) else []):
            if isinstance(r_dict, dict):
                r_pts = r_dict.get("points") or r_dict.get("polygon") or []
                r_poly = [_parse_vector2d(pt) for pt in r_pts if _parse_vector2d(pt) is not None]
                raw_type = r_dict.get("roof_type")
                r_type_str = str(raw_type) if raw_type is not None else None
                
                # SECTION P: pitches_deg is a LIST; do NOT pass list to _safe_float()!
                raw_pitches = r_dict.get("pitches_deg") or r_dict.get("pitch_deg")
                if isinstance(raw_pitches, list):
                    pitch_val = _safe_float(raw_pitches[0]) if len(raw_pitches) == 1 else None
                else:
                    pitch_val = _safe_float(raw_pitches)

                c_roof = CanonicalRoof(
                    id=str(r_dict.get("id") or f"roof_{r_idx}"),
                    name=str(r_dict.get("name") or f"Roof Envelope {r_idx + 1}"),
                    level_id=unresolved_level_container.id,
                    polygon=r_poly,
                    pitch_deg=pitch_val,
                    roof_type=r_type_str,
                    review_state=ReviewState.REVIEW_REQUIRED,
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
    """Builds canonical 3D model from workspace DB and evidence snapshot v3."""
    wid_int = require_workspace_id(workspace_id)
    snapshot = collect_workspace_3d_evidence(app, wid_int)

    from pb_canonical_persistence import compute_workspace_source_fingerprint
    snapshot_fp = compute_workspace_source_fingerprint(snapshot)

    project, skipped_items = planreader_to_canonical_model(snapshot, is_validated_internal_workspace=True)
    diagnostics = generate_production_diagnostics_report(project, workspace_data=snapshot, skipped_items=skipped_items)

    return WorkspaceCanonicalResult(
        project=project,
        snapshot=snapshot,
        snapshot_fingerprint=snapshot_fp,
        diagnostics=diagnostics,
        skipped_items=skipped_items
    )
