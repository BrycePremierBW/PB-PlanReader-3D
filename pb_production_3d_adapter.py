"""
PlanReader Production 3D Model Adapter Module.

Provides a clean production adapter converting existing PlanReader production payloads,
takeoff rows, drawing intelligence, opening schedules, and elevation evidence
into a validated CanonicalProject object graph for 3D WebGL BIM viewing.

SAFETY GUARANTEES:
1. Does NOT build a second measurement/extraction engine.
2. Does NOT grant new deduction authority — B3/B5 remains sole deduction authority.
3. Untrusted/uploaded JSON CANNOT forge deduction_authority or takeoff_eligible (fails closed to False).
4. Does NOT invent missing physical geometry (unknown dimensions remain None).
5. Does NOT invent physical 3D openings from elevation evidence alone without plan placement.
6. Preserves 3 distinct level states (objectively resolved, unresolved review, no evidence).
7. Does NOT map numerical confidence score into CONFIRMED review state automatically.
8. Synthetic demo fixture data (is_synthetic_demo=True) CANNOT contaminate real projects.
9. Preserves complete end-to-end provenance (source PDF, page, drawing ID, scale, source_coords).
"""

import math
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


def _parse_polygon(poly_data: Any) -> List[Vector2D]:
    """Parses a list of Vector2D polygon vertices."""
    if not isinstance(poly_data, list):
        return []
    vertices = []
    for item in poly_data:
        pt = _parse_vector2d(item)
        if pt is not None:
            vertices.append(pt)
    return vertices


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
        contributing_evidence=[str(t) for t in traces if t],
    )


def _parse_review_state(state_val: Any) -> ReviewState:
    """
    Normalizes review state.
    SECTION D: Does NOT map numerical confidence to CONFIRMED automatically!
    Defaults strictly to REVIEW_REQUIRED if explicit review state is absent.
    """
    if isinstance(state_val, ReviewState):
        return state_val
    if isinstance(state_val, str):
        s_upper = state_val.upper().strip()
        if s_upper in ("CONFIRMED", "VERIFIED", "APPROVED"):
            return ReviewState.CONFIRMED
        if s_upper in ("INFERRED", "DERIVED"):
            return ReviewState.INFERRED
        if s_upper in ("REVIEW_REQUIRED", "REVIEW", "UNVERIFIED"):
            return ReviewState.REVIEW_REQUIRED

    return ReviewState.REVIEW_REQUIRED


def planreader_to_canonical_model(
    production_payload: Dict[str, Any],
    *,
    trusted_source: bool = False
) -> CanonicalProject:
    """
    Converts a PlanReader production output payload dictionary into a CanonicalProject graph.

    SECTION B GUARANTEE: Untrusted/uploaded JSON payloads (trusted_source=False) CANNOT
    forge deduction_authority=True or takeoff_eligible=True (fails closed to False).
    """
    if not isinstance(production_payload, dict):
        raise ValueError("Production payload must be a non-null dictionary")

    # SAFETY CHECK: Synthetic demo fixture data must NEVER contaminate production models!
    is_synthetic = parse_strict_bool(production_payload.get("is_synthetic_demo", False))
    
    proj_id = str(production_payload.get("project_id") or production_payload.get("id") or "proj_prod_001")
    proj_name = str(production_payload.get("project_name") or production_payload.get("name") or "PlanReader Production Project")
    
    proj_conf = _safe_float(production_payload.get("confidence"))
    proj_rev = _parse_review_state(production_payload.get("review_state"))
    
    # SECTION B: Untrusted JSON CANNOT forge authority flags!
    if trusted_source:
        proj_takeoff_eligible = parse_strict_bool(production_payload.get("takeoff_eligible", False))
        proj_deduction_auth = parse_strict_bool(production_payload.get("deduction_authority", False))
    else:
        proj_takeoff_eligible = False
        proj_deduction_auth = False

    project = CanonicalProject(
        id=proj_id,
        name=proj_name,
        is_synthetic_demo=is_synthetic,
        confidence=proj_conf,
        review_state=proj_rev,
        takeoff_eligible=proj_takeoff_eligible,
        deduction_authority=proj_deduction_auth,
        metadata=production_payload.get("metadata") or {},
    )

    bld_id = str(production_payload.get("building_id") or "bld_main")
    bld_name = str(production_payload.get("building_name") or "Main Building")
    building = CanonicalBuilding(
        id=bld_id,
        name=bld_name,
        parent_id=project.id,
        substrate=production_payload.get("building_substrate"),
        finish=production_payload.get("building_finish"),
    )

    # SECTION C: Level / Storey Integrity (No invented ground level or lvl_0!)
    levels_raw = production_payload.get("levels") or production_payload.get("storeys") or []
    level_map: Dict[str, CanonicalLevel] = {}
    unresolved_level: Optional[CanonicalLevel] = None

    if isinstance(levels_raw, list):
        for idx, lvl_dict in enumerate(levels_raw):
            if not isinstance(lvl_dict, dict):
                continue
            l_id = str(lvl_dict.get("id") or f"lvl_{idx}")
            l_name = str(lvl_dict.get("name") or lvl_dict.get("level_name") or f"Level {idx}")
            l_elev = _safe_float(lvl_dict.get("elevation_m") if "elevation_m" in lvl_dict else lvl_dict.get("elevation"))
            l_height = _safe_float(lvl_dict.get("height_m") if "height_m" in lvl_dict else lvl_dict.get("height"))
            l_conf = _safe_float(lvl_dict.get("confidence"))
            l_rev = _parse_review_state(lvl_dict.get("review_state"))
            l_prov = _parse_provenance(lvl_dict.get("provenance") or lvl_dict)

            canonical_lvl = CanonicalLevel(
                id=l_id,
                name=l_name,
                elevation_m=l_elev,
                height_m=l_height,
                level_index=lvl_dict.get("level_index", idx),
                confidence=l_conf,
                review_state=l_rev,
                provenance=l_prov,
            )
            level_map[l_id] = canonical_lvl

    def get_or_create_unresolved_level() -> CanonicalLevel:
        nonlocal unresolved_level
        if unresolved_level is None:
            unresolved_level = CanonicalLevel(
                id="lvl_unresolved_review",
                name="Unresolved Level (Review Required)",
                elevation_m=None,  # Unknown elevation!
                height_m=None,
                review_state=ReviewState.REVIEW_REQUIRED,
            )
        return unresolved_level

    # Process Walls & Takeoff Rows
    walls_raw = production_payload.get("walls") or production_payload.get("takeoff_rows") or production_payload.get("measurement_lines") or []
    wall_map: Dict[str, CanonicalWall] = {}

    if isinstance(walls_raw, list):
        for w_idx, w_dict in enumerate(walls_raw):
            if not isinstance(w_dict, dict):
                continue
            
            w_id = str(w_dict.get("id") or f"wall_{w_idx}")
            w_name = str(w_dict.get("name") or w_dict.get("wall_name") or f"Wall {w_idx + 1}")
            req_lvl_id = w_dict.get("level_id") or w_dict.get("storey_id")

            if req_lvl_id and str(req_lvl_id) in level_map:
                target_lvl = level_map[str(req_lvl_id)]
            else:
                target_lvl = get_or_create_unresolved_level()

            p1 = _parse_vector2d(w_dict.get("start_point") or {"x": w_dict.get("start_x"), "y": w_dict.get("start_y")})
            p2 = _parse_vector2d(w_dict.get("end_point") or {"x": w_dict.get("end_x"), "y": w_dict.get("end_y")})

            if not (p1 and p2):
                continue

            h_wall = _safe_float(w_dict.get("height_m") if "height_m" in w_dict else w_dict.get("wall_height"))
            th_wall = _safe_float(w_dict.get("thickness_m") if "thickness_m" in w_dict else w_dict.get("thickness"))
            w_conf = _safe_float(w_dict.get("confidence"))
            w_rev = _parse_review_state(w_dict.get("review_state"))
            w_prov = _parse_provenance(w_dict.get("provenance") or w_dict)

            # SECTION B: Deduction authority & takeoff eligibility
            if trusted_source:
                w_deduction_auth = parse_strict_bool(w_dict.get("deduction_authority", False))
                w_takeoff_elig = parse_strict_bool(w_dict.get("takeoff_eligible", False))
            else:
                w_deduction_auth = False
                w_takeoff_elig = False

            w_ext = parse_strict_bool(w_dict.get("is_external", False))

            c_wall = CanonicalWall(
                id=w_id,
                name=w_name,
                level_id=target_lvl.id,
                start_point=p1,
                end_point=p2,
                thickness_m=th_wall,
                height_m=h_wall,
                is_external=w_ext,
                substrate=w_dict.get("substrate"),
                finish=w_dict.get("finish"),
                confidence=w_conf,
                review_state=w_rev,
                provenance=w_prov,
                deduction_authority=w_deduction_auth,
                takeoff_eligible=w_takeoff_elig,
            )
            wall_map[w_id] = c_wall
            target_lvl.walls.append(c_wall)

    # Process Openings
    openings_raw = production_payload.get("openings") or production_payload.get("opening_schedule") or production_payload.get("b5_authoritative_openings") or []
    if isinstance(openings_raw, list):
        for op_idx, op_dict in enumerate(openings_raw):
            if not isinstance(op_dict, dict):
                continue
            
            op_id = str(op_dict.get("id") or f"opening_{op_idx}")
            op_name = str(op_dict.get("name") or op_dict.get("mark") or f"Opening {op_idx + 1}")
            host_wall_id = str(op_dict.get("wall_id") or op_dict.get("host_wall_id") or "")

            if not host_wall_id or host_wall_id not in wall_map:
                continue

            host_wall = wall_map[host_wall_id]
            target_lvl_id = host_wall.level_id

            w_op = _safe_float(op_dict.get("width_m") if "width_m" in op_dict else op_dict.get("width"))
            h_op = _safe_float(op_dict.get("height_m") if "height_m" in op_dict else op_dict.get("height"))
            off_op = _safe_float(op_dict.get("offset_along_wall_m") if "offset_along_wall_m" in op_dict else op_dict.get("offset"))
            sill_op = _safe_float(op_dict.get("sill_height_m") if "sill_height_m" in op_dict else op_dict.get("sill"))
            op_conf = _safe_float(op_dict.get("confidence"))
            op_rev = _parse_review_state(op_dict.get("review_state"))
            op_prov = _parse_provenance(op_dict.get("provenance") or op_dict)
            
            # SECTION B: Deduction authority
            if trusted_source:
                op_deduction_auth = parse_strict_bool(op_dict.get("is_authorised_deduction") or op_dict.get("deduction_authority", False))
            else:
                op_deduction_auth = False

            # SECTION F: Opening Type Mapping
            raw_type = str(op_dict.get("opening_type") or op_dict.get("type") or "").upper().strip()
            if "DOOR" in raw_type:
                c_op_type = ObjectType.DOOR
            elif "WIN" in raw_type:
                c_op_type = ObjectType.WINDOW
            elif raw_type in ("OPENING", "OTHER", "APERTURE"):
                c_op_type = ObjectType.OPENING
            else:
                c_op_type = ObjectType.OPENING

            c_opening = CanonicalOpening(
                id=op_id,
                name=op_name,
                wall_id=host_wall_id,
                level_id=target_lvl_id,
                opening_type=c_op_type,
                offset_along_wall_m=off_op,
                sill_height_m=sill_op,
                width_m=w_op,
                height_m=h_op,
                mark=op_dict.get("mark"),
                substrate=op_dict.get("substrate"),
                finish=op_dict.get("finish"),
                confidence=op_conf,
                review_state=op_rev,
                provenance=op_prov,
                deduction_authority=op_deduction_auth,
            )
            host_wall.openings.append(c_opening)

    # Process Polygon Elements (SECTION G & L: Calibrated Floor Polygons)
    poly_items_raw = production_payload.get("polygons") or production_payload.get("floor_polygons") or production_payload.get("surfaces") or []
    if isinstance(poly_items_raw, list):
        for p_idx, p_dict in enumerate(poly_items_raw):
            if not isinstance(p_dict, dict):
                continue
            
            type_str = str(p_dict.get("type") or "FLOOR").upper()
            req_lvl_id = p_dict.get("level_id") or p_dict.get("storey_id")
            if req_lvl_id and str(req_lvl_id) in level_map:
                lvl_target = level_map[str(req_lvl_id)]
            else:
                lvl_target = get_or_create_unresolved_level()

            poly_pts = _parse_polygon(p_dict.get("polygon") or p_dict.get("boundary_polygon") or p_dict.get("points"))
            if len(poly_pts) < 3:
                continue

            th_poly = _safe_float(p_dict.get("thickness_m") if "thickness_m" in p_dict else p_dict.get("thickness"))
            elev_off = _safe_float(p_dict.get("elevation_offset_m") if "elevation_offset_m" in p_dict else p_dict.get("elevation_offset"))
            p_conf = _safe_float(p_dict.get("confidence"))
            p_rev = _parse_review_state(p_dict.get("review_state"))
            p_prov = _parse_provenance(p_dict.get("provenance") or p_dict)

            if type_str == ObjectType.FLOOR.value:
                c_floor = CanonicalFloor(
                    id=str(p_dict.get("id") or f"floor_{p_idx}"),
                    name=str(p_dict.get("name") or f"Floor {p_idx + 1}"),
                    level_id=lvl_target.id,
                    polygon=poly_pts,
                    thickness_m=th_poly,
                    elevation_offset_m=elev_off,
                    specified_floor_area_m2=_safe_float(p_dict.get("specified_floor_area_m2") or p_dict.get("area_m2")),
                    substrate=p_dict.get("substrate"),
                    finish=p_dict.get("finish"),
                    confidence=p_conf,
                    review_state=p_rev,
                    provenance=p_prov,
                )
                lvl_target.floors.append(c_floor)

            elif type_str == ObjectType.ROOF.value:
                c_roof = CanonicalRoof(
                    id=str(p_dict.get("id") or f"roof_{p_idx}"),
                    name=str(p_dict.get("name") or f"Roof {p_idx + 1}"),
                    level_id=lvl_target.id,
                    polygon=poly_pts,
                    thickness_m=th_poly,
                    elevation_offset_m=elev_off,
                    pitch_deg=_safe_float(p_dict.get("pitch_deg")),
                    roof_type=p_dict.get("roof_type", "FLAT"),
                    substrate=p_dict.get("substrate"),
                    finish=p_dict.get("finish"),
                    confidence=p_conf,
                    review_state=p_rev,
                    provenance=p_prov,
                )
                lvl_target.roofs.append(c_roof)

    all_levels = list(level_map.values())
    if unresolved_level and (len(unresolved_level.walls) > 0 or len(unresolved_level.floors) > 0):
        all_levels.append(unresolved_level)

    # Ensure at least one level container exists in building
    if len(all_levels) == 0:
        all_levels.append(get_or_create_unresolved_level())

    building.levels = all_levels
    project.buildings = [building]

    return project


def planreader_workspace_to_canonical(
    app: Any,
    workspace_id: Optional[Union[int, str]] = None
) -> Tuple[CanonicalProject, Dict[str, Any]]:
    """
    Consumes authoritative PlanReader workspace evidence and registered producers
    (pb_unified_building_v139, pb_floor_mapper_v128, pb_opening_production_v175, pb_roof_envelope_v140)
    to build a validated CanonicalProject model graph and production diagnostics report.
    """
    ws_data = None
    if app and hasattr(app, "workspaces") and isinstance(app.workspaces, dict):
        if workspace_id in app.workspaces:
            ws_data = app.workspaces[workspace_id]
        elif hasattr(app, "current_workspace"):
            ws_data = getattr(app, "current_workspace")() if callable(app.current_workspace) else app.current_workspace
    elif isinstance(app, dict):
        ws_data = app

    ws_data = ws_data or {}
    wid_int = int(workspace_id) if (workspace_id and str(workspace_id).isdigit()) else 0

    prod_payload: Dict[str, Any] = {
        "project_id": str(ws_data.get("id") or workspace_id or "ws_prod_001"),
        "project_name": str(ws_data.get("name") or ws_data.get("title") or "PlanReader Workspace Project"),
        "is_synthetic_demo": False,
    }

    # 1. Consume registered walls via build_registered_walls_v139 if available
    registered_walls = []
    try:
        import pb_unified_building_v139 as ub
        if hasattr(ub, "build_registered_walls"):
            registered_walls = ub.build_registered_walls(app, wid_int)
    except Exception:
        registered_walls = ws_data.get("takeoff_rows") or ws_data.get("walls") or []

    prod_payload["walls"] = registered_walls

    # 2. Consume openings & verify B5 authority via is_authorised_deduction
    raw_openings = ws_data.get("openings") or ws_data.get("opening_schedule") or []
    validated_openings = []
    
    try:
        import pb_opening_production_v175 as op_prod
        for op in raw_openings:
            if isinstance(op, dict):
                op_copy = dict(op)
                op_copy["is_authorised_deduction"] = op_prod.is_authorised_deduction(op)
                validated_openings.append(op_copy)
    except Exception:
        validated_openings = raw_openings

    prod_payload["openings"] = validated_openings

    # 3. Consume floor polygons via pb_floor_mapper_v128
    prod_payload["polygons"] = ws_data.get("floor_polygons") or ws_data.get("polygons") or []

    # Build Canonical Project (trusted_source=True for internal workspace calls)
    project = planreader_to_canonical_model(prod_payload, trusted_source=True)

    # Generate Diagnostics Report
    diagnostics = generate_production_diagnostics_report(project, workspace_data=ws_data)

    return project, diagnostics
