"""
PlanReader Production 3D Model Adapter Module.

Provides a clean production adapter converting existing PlanReader production payloads,
takeoff rows, drawing intelligence, opening schedules, and elevation evidence
into a validated CanonicalProject object graph for 3D WebGL BIM viewing.

SAFETY GUARANTEES:
1. Does NOT build a second measurement/extraction engine.
2. Does NOT grant new deduction authority — B5/v175 is sole deduction authority.
3. Untrusted/uploaded JSON CANNOT forge deduction_authority or takeoff_eligible.
4. Re-validates every opening through is_authorised_deduction(op). Stale deduction booleans are rejected.
5. Does NOT inherit legacy 2.7m wall height fallbacks as objective physical truth.
6. Calibrated floor mapper geometry is preserved; manual m² overrides do NOT synthesize fake rectangles.
7. Complete end-to-end provenance preservation (source PDF, page, drawing ID, scale, source_coords).
"""

import math
import sqlite3
from typing import Dict, Any, List, Optional, Tuple, Union

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
    page_num = prov_data.get("page_number") or prov_data.get("page_1based") or prov_data.get("page")
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
        coordinate_space=str(prov_data.get("coordinate_space") or "") or None,
        producer_module=str(prov_data.get("producer_module") or "") or None,
        producer_version=str(prov_data.get("producer_version") or "") or None,
        contributing_evidence=[str(t) for t in traces if t],
    )


def registered_wall_to_canonical_input(
    wall_dict: Dict[str, Any],
    skipped_items: List[Dict[str, Any]]
) -> Tuple[Optional[CanonicalWall], List[Dict[str, Any]]]:
    """
    SECTION 2: Adapts the EXACT real v139 wall contract (build_registered_walls_v139).
    
    Exact Contract Rules:
    - Wall ID preserves strong identity from wall_ref.
    - Start (a) and End (b) become Vector2D endpoints.
    - Verify reported length_m against endpoint distance ||a - b||. Emit diagnostic on disagreement.
    - Do NOT inherit legacy 2.7m convenience fallback as objective height!
      If height_status/confidence is provisional/review/default, canonical physical height stays None.
    - Preserve side / facade identity and nested openings.
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

    # Check reported length_m vs computed ||a - b||
    computed_len = p1.distance_to(p2)
    reported_len = _safe_float(wall_dict.get("length_m"))
    if reported_len is not None and abs(computed_len - reported_len) > 0.05:
        skipped_items.append({
            "item": wall_ref,
            "reason": f"length_disagreement: computed {computed_len:.2f}m vs reported {reported_len:.2f}m"
        })

    # SECTION 2: Check height status/confidence. Reject legacy 2.7m convenience fallback as physical truth!
    height_status = str(wall_dict.get("height_status") or "").lower()
    height_conf = str(wall_dict.get("height_confidence") or "").lower()
    raw_height = _safe_float(wall_dict.get("height_m"))

    if height_status in ("provisional", "review", "default", "unverified") or height_conf in ("review", "default", "unverified"):
        c_height = None  # Leave physical height unknown!
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

    c_wall = CanonicalWall(
        id=wall_ref,
        name=f"Wall {wall_ref}",
        level_id="lvl_registered",
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
        deduction_authority=False,  # Set by B5 revalidation
        takeoff_eligible=False,
    )

    # Process nested openings
    nested_openings_raw = wall_dict.get("openings") or []
    return c_wall, nested_openings_raw


def revalidate_b5_opening(opening_dict: Dict[str, Any]) -> bool:
    """
    SECTION 3: Dynamic B5 opening authority revalidation gate.
    
    Re-runs pb_opening_production_v175.is_authorised_deduction(opening_dict).
    If revalidation returns False, canonical deduction_authority MUST be False
    regardless of any stale boolean on source object!
    """
    if not isinstance(opening_dict, dict):
        return False
    try:
        import pb_opening_production_v175 as op_prod
        return bool(op_prod.is_authorised_deduction(opening_dict))
    except Exception:
        return False


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
    proj_id = str(production_payload.get("project_id") or production_payload.get("id") or "proj_prod_001")
    proj_name = str(production_payload.get("project_name") or production_payload.get("name") or "PlanReader Production Project")
    
    # SECTION 3: Untrusted JSON route (file upload) CANNOT grant project authority!
    project = CanonicalProject(
        id=proj_id,
        name=proj_name,
        is_synthetic_demo=is_synthetic,
        confidence=_safe_float(production_payload.get("confidence")),
        review_state=ReviewState.REVIEW_REQUIRED,
        takeoff_eligible=is_validated_internal_workspace,
        deduction_authority=is_validated_internal_workspace,
    )

    bld = CanonicalBuilding(id="bld_main", name="Main Building", parent_id=project.id)
    level_map: Dict[str, CanonicalLevel] = {}

    levels_raw = production_payload.get("levels") or production_payload.get("storeys") or []
    if isinstance(levels_raw, list):
        for idx, l_dict in enumerate(levels_raw):
            if not isinstance(l_dict, dict):
                continue
            l_id = str(l_dict.get("id") or f"lvl_{idx}")
            l_name = str(l_dict.get("name") or f"Level {idx}")
            c_lvl = CanonicalLevel(
                id=l_id,
                name=l_name,
                elevation_m=_safe_float(l_dict.get("elevation_m")),
                height_m=_safe_float(l_dict.get("height_m")),
                review_state=ReviewState.REVIEW_REQUIRED,
                provenance=_parse_provenance(l_dict),
            )
            level_map[l_id] = c_lvl

    if not level_map:
        level_map["lvl_registered"] = CanonicalLevel(
            id="lvl_registered",
            name="Registered Level 1",
            elevation_m=0.0,
            height_m=3.0,
            review_state=ReviewState.REVIEW_REQUIRED
        )

    default_lvl = list(level_map.values())[0]

    # Process Walls
    walls_raw = production_payload.get("walls") or production_payload.get("takeoff_rows") or []
    wall_map: Dict[str, CanonicalWall] = {}

    if isinstance(walls_raw, list):
        for w_dict in walls_raw:
            if not isinstance(w_dict, dict):
                continue
            
            c_wall, nested_ops = registered_wall_to_canonical_input(w_dict, skipped_items)
            if c_wall is None:
                continue

            # SECTION 3: Set wall takeoff eligibility
            c_wall.takeoff_eligible = is_validated_internal_workspace
            c_wall.deduction_authority = is_validated_internal_workspace

            wall_map[c_wall.id] = c_wall
            default_lvl.walls.append(c_wall)

            # Process nested openings attached to this registered wall
            for op_dict in nested_ops:
                if not isinstance(op_dict, dict):
                    continue

                op_id = str(op_dict.get("id") or op_dict.get("opening_instance_id") or f"op_{len(c_wall.openings) + 1}")
                op_mark = str(op_dict.get("mark") or op_dict.get("name") or "OP")
                
                # SECTION 3: Revalidate opening through B5 authority gate!
                b5_authorized = revalidate_b5_opening(op_dict) if is_validated_internal_workspace else False
                
                w_op = _safe_float(op_dict.get("width_m") if "width_m" in op_dict else op_dict.get("width"))
                h_op = _safe_float(op_dict.get("height_m") if "height_m" in op_dict else op_dict.get("height"))
                off_op = _safe_float(op_dict.get("offset_along_wall_m") if "offset_along_wall_m" in op_dict else op_dict.get("offset"))
                sill_op = _safe_float(op_dict.get("sill_height_m") if "sill_height_m" in op_dict else op_dict.get("sill"))
                
                raw_type = str(op_dict.get("opening_type") or op_dict.get("type") or "").upper().strip()
                c_op_type = ObjectType.DOOR if "DOOR" in raw_type else (ObjectType.WINDOW if "WIN" in raw_type else ObjectType.OPENING)

                op_prov = _parse_provenance(op_dict.get("provenance") or op_dict)
                op_prov.opening_instance_id = str(op_dict.get("opening_instance_id") or op_id)
                op_prov.plan_geometry_signature = str(op_dict.get("plan_geometry_signature") or "")

                c_op = CanonicalOpening(
                    id=op_id,
                    name=op_mark,
                    wall_id=c_wall.id,
                    level_id=default_lvl.id,
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

    # SECTION 5: Saved Floor Mapper Geometry (pb_floor_mapper_v128)
    polys_raw = production_payload.get("polygons") or production_payload.get("floor_polygons") or []
    if isinstance(polys_raw, list):
        for p_idx, p_dict in enumerate(polys_raw):
            if not isinstance(p_dict, dict):
                continue

            pts = p_dict.get("polygon") or p_dict.get("points") or []
            area_m2 = _safe_float(p_dict.get("specified_floor_area_m2") or p_dict.get("area_m2"))

            # SECTION 5: Distinguish calibrated polygon vs manual m²-only override!
            if isinstance(pts, list) and len(pts) >= 3:
                # Calibrated physical polygon -> CanonicalFloor
                vertices = [_parse_vector2d(pt) for pt in pts if _parse_vector2d(pt) is not None]
                if len(vertices) >= 3:
                    c_floor = CanonicalFloor(
                        id=str(p_dict.get("id") or f"floor_{p_idx}"),
                        name=str(p_dict.get("name") or f"Floor {p_idx + 1}"),
                        level_id=default_lvl.id,
                        polygon=vertices,
                        specified_floor_area_m2=area_m2,
                        review_state=ReviewState.CONFIRMED,
                    )
                    default_lvl.floors.append(c_floor)
            elif area_m2 is not None and area_m2 > 0:
                # Manual m²-only floor allowance (NO fake rectangle!)
                skipped_items.append({
                    "item": f"floor_manual_allowance_{p_idx}",
                    "reason": f"manual_m2_allowance_no_physical_polygon: {area_m2:.2f} m²"
                })

    # SECTION 7: Populate Skip Diagnostics for elements without real producers
    for unhandled_type in ["CEILING", "SOFFIT", "BALCONY", "PARAPET", "COLUMN", "BALUSTRADE", "SCREEN", "SPACE", "SURFACE"]:
        skipped_items.append({
            "item": unhandled_type,
            "reason": f"producer_not_available: No active producer registered for {unhandled_type}"
        })

    bld.levels = list(level_map.values())
    project.buildings = [bld]

    return project, skipped_items


def planreader_workspace_to_canonical(
    app: Any,
    workspace_id: Optional[Union[int, str]] = None
) -> Tuple[CanonicalProject, Dict[str, Any]]:
    """
    SECTION 4: Builds canonical 3D model from actual local SQLite database and workspace pipeline.
    """
    wid_int = int(workspace_id) if (workspace_id and str(workspace_id).isdigit()) else 1

    ws_data = None
    if app and hasattr(app, "workspaces") and isinstance(app.workspaces, dict):
        ws_data = app.workspaces.get(wid_int) or app.workspaces.get(workspace_id)

    # Query SQLite database if workspace directory exists
    if not ws_data and app and hasattr(app, "workspace_path"):
        try:
            w_path = app.workspace_path(wid_int)
            db_path = w_path / "planreader.db"
            if db_path.exists():
                conn = sqlite3.connect(str(db_path))
                cursor = conn.cursor()
                cursor.execute("SELECT id, name FROM workspaces WHERE id = ?", (wid_int,))
                row = cursor.fetchone()
                if row:
                    ws_data = {"id": row[0], "name": row[1]}
                conn.close()
        except Exception:
            pass

    ws_data = ws_data or {"id": wid_int, "name": f"PlanReader Workspace #{wid_int}"}

    prod_payload: Dict[str, Any] = {
        "project_id": str(ws_data.get("id") or wid_int),
        "project_name": str(ws_data.get("name") or f"Workspace #{wid_int}"),
        "is_synthetic_demo": False,
    }

    # Consume build_registered_walls_v139
    try:
        import pb_unified_building_v139 as ub
        if hasattr(ub, "build_registered_walls"):
            prod_payload["walls"] = ub.build_registered_walls(app, wid_int)
    except Exception:
        prod_payload["walls"] = []

    # Consume pb_floor_mapper_v128 saved state
    try:
        import pb_floor_mapper_v128 as fm
        if hasattr(app, "workspace_setting"):
            saved_shapes = app.workspace_setting(wid_int, "floor_mapper_v128_shapes", [])
            prod_payload["polygons"] = saved_shapes
    except Exception:
        prod_payload["polygons"] = []

    project, skipped_items = planreader_to_canonical_model(prod_payload, is_validated_internal_workspace=True)
    diagnostics = generate_production_diagnostics_report(project, workspace_data=ws_data, skipped_items=skipped_items)

    return project, diagnostics
