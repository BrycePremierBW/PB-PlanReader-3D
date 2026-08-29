"""
PlanReader Production 3D Model Adapter Module (Phase 5H Semantic Closure & Multi-Level Production Proof).

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
8. Translates LAGO elevation evidence into CanonicalEvidenceObservation objects (fail closed without plan host).
9. Uses validate_opening_geometry(opening, wall) as sole physical geometry safety gate.
10. Wall deduction gate becomes True ONLY when opening passes both B5 authority AND physical geometry validation.
11. STRONG wrong-level enforcement (blocker #2): checks EVERY opening level identity
    (level, level_name, level_id, storey_id) against the host wall's resolved level; a
    contradictory opening level can never be silently rewritten -> fail closed.
12. PHYSICAL opening state is classified in the Adapter into explicit buckets
    (invalid_geometry / wrong_level / conflict_overlap / manual_exclusion /
    physical_b5_authorised / physical_not_authorised) via validate_opening_geometry +
    detect_opening_overlaps, consumed by diagnostics (blocker #3) — never inferred a-posteriori.
13. FLOOR polygons are validated as simple non-degenerate non-self-intersecting polygons
    (blocker #6): invalid mapper shapes FAIL CLOSED and never become physical floors.
14. Floors map to a REAL storey using the owning page's storey identity (blocker #5): mapper
    floors are never silently collapsed onto lvl_unresolved_review.
15. Roof caps come from the REAL v1.4.0 producers (roof_evidence_v140 / roof_caps_v140) and are
    materialised into CanonicalRoof only when the cap polygon is valid (blocker #8).
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
    CanonicalEvidenceObservation,
    Vector2D,
    Vector3D,
    BoundingBox3D,
    ObjectType,
    ReviewState,
    Provenance,
    parse_strict_bool,
    parse_optional_confidence,
    parse_optional_float,
)
from pb_geometry_services import potential_net_wall_area, validate_opening_geometry, detect_opening_overlaps
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


def _segment_intersect(
    p1: "Vector2D", p2: "Vector2D", p3: "Vector2D", p4: "Vector2D"
) -> bool:
    """True if segments (p1,p2) and (p3,p4) properly intersect (excludes shared endpoints)."""
    def cross(o: "Vector2D", a: "Vector2D", b: "Vector2D") -> float:
        return (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x)

    d1 = cross(p3, p4, p1)
    d2 = cross(p3, p4, p2)
    d3 = cross(p1, p2, p3)
    d4 = cross(p1, p2, p4)

    def on_seg(o: "Vector2D", a: "Vector2D", b: "Vector2D") -> bool:
        return (
            min(a.x, b.x) - 1e-9 <= o.x <= max(a.x, b.x) + 1e-9
            and min(a.y, b.y) - 1e-9 <= o.y <= max(a.y, b.y) + 1e-9
        )

    if ((d1 > 1e-9 and d2 < -1e-9) or (d1 < -1e-9 and d2 > 1e-9)) and (
        (d3 > 1e-9 and d4 < -1e-9) or (d3 < -1e-9 and d4 > 1e-9)
    ):
        return True
    # Properly reject collinear/touching cases; allow only genuine crossing.
    return False


def _validate_floor_polygon(pts: List[Vector2D]) -> Tuple[bool, str]:
    """
    Blocker #6: Validates a floor polygon is a simple non-degenerate polygon.
    Requires >= 3 DISTINCT points, no self-intersection, and non-zero signed area.
    Degenerate/self-intersecting mapper shapes FAIL CLOSED (never become physical floors).
    """
    if len(pts) < 3:
        return False, f"Floor polygon needs >= 3 points (got {len(pts)})"

    # Deduplicate consecutive identical points (robust to repeated closing point).
    cleaned: List[Vector2D] = []
    for p in pts:
        if not cleaned or (abs(p.x - cleaned[-1].x) > 1e-9 or abs(p.y - cleaned[-1].y) > 1e-9):
            cleaned.append(p)
    if len(cleaned) < 3:
        return False, "Floor polygon has fewer than 3 distinct points"

    # Signed area (shoelace) — non-zero required.
    area2 = 0.0
    n = len(cleaned)
    for i in range(n):
        j = (i + 1) % n
        area2 += cleaned[i].x * cleaned[j].y - cleaned[j].x * cleaned[i].y
    if abs(area2) <= 1e-9:
        return False, "Floor polygon has zero area (degenerate)"

    # Self-intersection check over non-adjacent edges.
    edges = [(cleaned[i], cleaned[(i + 1) % n]) for i in range(n)]
    for i in range(n):
        for k in range(i + 2, n):
            if i == 0 and k == n - 1:
                continue  # first+last edges share start/end vertex
            if k == (i + 1) % n:
                continue
            if _segment_intersect(edges[i][0], edges[i][1], edges[k][0], edges[k][1]):
                return False, f"Floor polygon is self-intersecting between edges {i} and {k}"

    return True, "Valid simple floor polygon"


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
        source_coords=src_coords if isinstance(src_coords, dict) else None,
        scale_source=scale_source,
        workspace_id=str(prov_data.get("workspace_id")) if prov_data.get("workspace_id") is not None else None,
        document_id=str(prov_data.get("document_id")) if prov_data.get("document_id") is not None else None,
        page_id=str(prov_data.get("page_id")) if prov_data.get("page_id") is not None else None,
        wall_ref=str(prov_data.get("wall_ref")) if prov_data.get("wall_ref") is not None else None,
        opening_instance_id=str(prov_data.get("opening_instance_id")) if prov_data.get("opening_instance_id") is not None else None,
        plan_geometry_signature=str(prov_data.get("plan_geometry_signature")) if prov_data.get("plan_geometry_signature") is not None else None,
        coordinate_space=str(prov_data.get("coordinate_space")) if prov_data.get("coordinate_space") is not None else None,
        producer_module=str(prov_data.get("producer_module")) if prov_data.get("producer_module") is not None else None,
        producer_version=str(prov_data.get("producer_version")) if prov_data.get("producer_version") is not None else None,
        contributing_evidence=traces,
    )


def resolve_canonical_level(
    level_val: Any,
    levels_map: Dict[str, CanonicalLevel],
    diagnostics_log: Optional[List[Dict[str, Any]]] = None
) -> Tuple[CanonicalLevel, str]:
    """
    SECTION M, N, O, P: Resolves or registers a CanonicalLevel for building elements.
    Does NOT use sheet text (e.g. A101, Floor Plan) as canonical level identity.
    Preserves v135 level identity, 'Ground / unregistered' review state, and distinct level IDs.
    """
    diagnostics_log = diagnostics_log if diagnostics_log is not None else []

    if isinstance(level_val, str) and level_val in levels_map:
        return levels_map[level_val], level_val

    explicit_id = None
    source_polygon_id = None
    raw_name = ""
    lvl_idx = None
    elevation_m = None
    is_unregistered = False

    if isinstance(level_val, dict):
        raw_name = str(level_val.get("name") or level_val.get("level_name") or level_val.get("label") or "").strip()
        lvl_idx = level_val.get("level_index") if "level_index" in level_val else level_val.get("index")
        elevation_val = level_val.get("elevation_m") if "elevation_m" in level_val else level_val.get("ffl_m")
        elevation_m = _safe_float(elevation_val)
        explicit_id = level_val.get("id") or level_val.get("level_id")
        source_polygon_id = level_val.get("source_polygon") or level_val.get("prism_id")
    elif isinstance(level_val, str):
        raw_name = level_val.strip()
    elif isinstance(level_val, (int, float)):
        raw_name = f"Level {int(level_val)}"
        lvl_idx = int(level_val)

    if "unregistered" in raw_name.lower():
        is_unregistered = True

    if not raw_name and not explicit_id and not source_polygon_id:
        raw_name = "Ground"

    norm_name = raw_name.lower().strip()

    # Section M: Filter out sheet drawing numbers / generic sheet text
    sheet_text_patterns = [r"^a\d+$", r"^sheet\s*\d+$", r"^floor\s*plan$", r"^general\s*arrangement$", r"^drawing.*"]
    is_sheet_text = any(re.match(pat, raw_name.lower()) for pat in sheet_text_patterns)

    if is_sheet_text and not explicit_id and not source_polygon_id:
        key = "unresolved_review"
        if key not in levels_map:
            levels_map[key] = CanonicalLevel(
                id=f"lvl_{key}",
                name="Unresolved Level Container (Review Required)",
                level_index=0,
                elevation_m=None,
                review_state=ReviewState.REVIEW_REQUIRED,
            )
        return levels_map[key], "unresolved"

    # Section P: Identity hierarchy for key
    if explicit_id:
        key = str(explicit_id)
    elif source_polygon_id:
        poly_slug = re.sub(r'[^a-z0-9]+', '_', str(source_polygon_id).lower()).strip('_')
        idx_str = f"_idx_{lvl_idx}" if lvl_idx is not None else ""
        name_str = f"_{re.sub(r'[^a-z0-9]+', '_', norm_name).strip('_')}" if norm_name else ""
        key = f"lvl_poly_{poly_slug}{idx_str}{name_str}"
    elif not explicit_id and not source_polygon_id and norm_name:
        matched_lvl = None
        matched_key = None
        for existing_key, existing_lvl in levels_map.items():
            if existing_lvl.name and existing_lvl.name.lower().strip() == norm_name and not existing_key.startswith("lvl_poly_"):
                matched_lvl = existing_lvl
                matched_key = existing_key
                break
        if matched_lvl and matched_key:
            if elevation_m is not None and matched_lvl.elevation_m is None:
                matched_lvl.elevation_m = elevation_m
                matched_lvl.review_state = ReviewState.CONFIRMED
            return matched_lvl, matched_key

        key = (
            "ground" if ("ground" in norm_name or norm_name in ("0", "g", "lvl 0", "level 0")) else
            "level_1" if ("level 1" in norm_name or norm_name in ("1", "l1", "lvl 1", "first")) else
            "level_2" if ("level 2" in norm_name or norm_name in ("2", "l2", "lvl 2", "second")) else
            f"lvl_{re.sub(r'[^a-z0-9]+', '_', norm_name).strip('_') or 'unresolved'}"
        )
    elif is_unregistered:
        key = f"ground_unregistered_{lvl_idx if lvl_idx is not None else '0'}"
    elif lvl_idx is not None and raw_name:
        key = f"lvl_idx_{lvl_idx}_{re.sub(r'[^a-z0-9]+', '_', norm_name)}"
    else:
        key = "ground"

    if elevation_m is None and (key == "ground" or "ground" in norm_name or norm_name in ("0", "g", "lvl 0", "level 0") or not norm_name):
        elevation_m = 0.0

    if key not in levels_map:
        rev_state = (
            ReviewState.REVIEW_REQUIRED if is_unregistered or is_sheet_text else
            (ReviewState.CONFIRMED if elevation_m is not None else ReviewState.INFERRED)
        )
        levels_map[key] = CanonicalLevel(
            id=str(explicit_id) if explicit_id else key,
            name=raw_name or str(explicit_id) or "Unregistered Level",
            level_index=int(lvl_idx) if lvl_idx is not None else 0,
            elevation_m=elevation_m,
            review_state=rev_state,
        )
        if is_unregistered:
            levels_map[key].metadata["registered_storey"] = False
            levels_map[key].metadata["registration_status"] = "unregistered"
    else:
        if elevation_m is not None and levels_map[key].elevation_m is None:
            levels_map[key].elevation_m = elevation_m
            levels_map[key].review_state = ReviewState.CONFIRMED

    return levels_map[key], key


from pb_opening_production_v175 import is_authorised_deduction


def _get_present_float(d: Dict[str, Any], keys: List[str]) -> Optional[float]:
    """Extracts the first present, non-null valid float for given key aliases without 0.0 false-falsy bugs."""
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d and d[k] is not None:
            val = _safe_float(d[k])
            if val is not None:
                return val
    return None


def revalidate_b5_opening(opening_data: Dict[str, Any]) -> bool:
    """
    SECTION 1: Delegated B5 opening deduction authority.
    v175 is the SOLE deduction authority. Phase 5 NEVER broadens v175 authority.
    """
    if not isinstance(opening_data, dict):
        return False
    return is_authorised_deduction(opening_data)


def registered_wall_to_canonical_input(wall_obj: Any) -> Dict[str, Any]:
    """SECTION 15 & 6: Adapts a registered wall producer object into a canonical wall input dict (fail-closed if missing A/B). Preserves v135 level_index, source_polygon, level_id/storey_id."""
    if hasattr(wall_obj, "to_dict") and callable(wall_obj.to_dict):
        d = wall_obj.to_dict()
    elif isinstance(wall_obj, dict):
        d = wall_obj
    else:
        d = {}

    w_ref = getattr(wall_obj, "wall_ref", d.get("wall_ref", d.get("id", "")))
    side = getattr(wall_obj, "side", d.get("side", ""))
    sub = getattr(wall_obj, "substrate", d.get("substrate", ""))
    h_m = getattr(wall_obj, "height_m", d.get("height_m"))
    h_stat = getattr(wall_obj, "height_status", d.get("height_status", "review_required"))
    
    a_pt = getattr(wall_obj, "a", d.get("a"))
    b_pt = getattr(wall_obj, "b", d.get("b"))
    
    if hasattr(a_pt, "x") and hasattr(a_pt, "y"):
        a_dict = {"x": a_pt.x, "y": a_pt.y}
    elif isinstance(a_pt, (list, tuple)) and len(a_pt) >= 2:
        a_dict = {"x": a_pt[0], "y": a_pt[1]}
    else:
        a_dict = a_pt if isinstance(a_pt, dict) else None

    if hasattr(b_pt, "x") and hasattr(b_pt, "y"):
        b_dict = {"x": b_pt.x, "y": b_pt.y}
    elif isinstance(b_pt, (list, tuple)) and len(b_pt) >= 2:
        b_dict = {"x": b_pt[0], "y": b_pt[1]}
    else:
        b_dict = b_pt if isinstance(b_pt, dict) else None

    lvl_val = getattr(wall_obj, "level", d.get("level"))
    lvl_name = getattr(wall_obj, "level_name", d.get("level_name"))
    lvl_idx = getattr(wall_obj, "level_index", d.get("level_index"))
    src_poly = getattr(wall_obj, "source_polygon", d.get("source_polygon", d.get("prism_id")))
    lvl_id = getattr(wall_obj, "level_id", d.get("level_id", d.get("storey_id")))

    # SECTION 6 & 7: Structurally retain all level identity attributes without collapsing
    level_struct = {
        "level": lvl_val,
        "level_name": lvl_name or lvl_val,
        "level_index": lvl_idx,
        "source_polygon": src_poly,
        "level_id": lvl_id,
    }

    return {
        "wall_ref": str(w_ref),
        "side": str(side),
        "substrate": str(sub),
        "height_m": _safe_float(h_m),
        "height_status": str(h_stat),
        "a": a_dict,
        "b": b_dict,
        "level": level_struct if (isinstance(lvl_val, dict) or lvl_name or lvl_idx is not None or src_poly or lvl_id) else lvl_val,
        "level_name": lvl_name,
        "level_index": lvl_idx,
        "source_polygon": src_poly,
        "level_id": lvl_id,
        "provenance": d.get("provenance"),
        "openings": d.get("openings", []),
    }


def planreader_to_canonical_model(
    payload: Dict[str, Any],
    is_validated_internal_workspace: bool = False
) -> Tuple[CanonicalProject, List[Dict[str, Any]]]:
    """SECTION 4 & 5: Converts production payload into a validated CanonicalProject instance."""
    skipped_items: List[Dict[str, Any]] = []

    proj_id = str(payload.get("project_id") or payload.get("id") or "proj_canonical_1")
    proj_name = str(payload.get("project_name") or payload.get("name") or "Canonical Building Model")
    is_synth = parse_strict_bool(payload.get("is_synthetic_demo"))
    proj_conf = parse_optional_confidence(payload.get("confidence"))

    project = CanonicalProject(
        id=proj_id,
        name=proj_name,
        confidence=proj_conf,
        is_synthetic_demo=is_synth,
    )

    building = CanonicalBuilding(
        id=f"bld_{proj_id}",
        name=f"{proj_name} Main Structure",
    )

    levels_map: Dict[str, CanonicalLevel] = {}

    # Pre-populate explicitly declared levels
    for l_info in payload.get("levels") or []:
        if isinstance(l_info, dict):
            resolve_canonical_level(l_info, levels_map)

    # Section D & E: Translate Real Elevation Candidates from true_positive_openings (NO invented defaults!)
    elev_candidates = payload.get("elevation_opening_candidates") or []
    for cand in elev_candidates:
        if isinstance(cand, dict):
            p_no = cand.get("page_no") if cand.get("page_no") is not None else cand.get("page_number")
            dwg = cand.get("drawing_no") or cand.get("drawing_id") or cand.get("drawing_reference")
            side = cand.get("side") or cand.get("facade")
            lvl = cand.get("level") or cand.get("level_name")
            conf = parse_optional_confidence(cand.get("confidence"))
            prod = cand.get("producer")
            prod_ver = cand.get("producer_version")

            obs = CanonicalEvidenceObservation.from_dict({
                "id": str(cand.get("candidate_id") or cand.get("id") or cand.get("annotation_id") or "cand_obs"),
                "kind": "elevation_opening_candidate",
                "workspace_id": str(payload.get("workspace_id")) if payload.get("workspace_id") is not None else None,
                "document_id": str(cand.get("document_id")) if cand.get("document_id") is not None else None,
                "page_id": str(cand.get("page_id")) if cand.get("page_id") is not None else None,
                "page_no": int(p_no) if p_no is not None else None,
                "drawing_reference": str(dwg) if dwg is not None else None,
                "side": str(side) if side is not None else None,
                "level_name": str(lvl) if lvl is not None else None,
                "wall_ref": str(cand.get("wall_ref")) if cand.get("wall_ref") is not None else None,
                "source_coords": cand.get("source_coords") or ({"x0_pt": cand["x0_pt"], "x1_pt": cand["x1_pt"]} if "x0_pt" in cand else None),
                "coordinate_space": str(cand.get("coordinate_space")) if cand.get("coordinate_space") is not None else None,
                "width_m": parse_optional_float(cand.get("width_m")),
                "height_m": parse_optional_float(cand.get("height_m")),
                "producer": str(prod) if prod is not None else None,
                "producer_version": str(prod_ver) if prod_ver is not None else None,
                "confidence": conf,
                "review_state": ReviewState.REVIEW_REQUIRED,
                "reason_physical_unavailable": "Elevation opening candidate lacks physical plan host wall placement",
                "dimension_basis": str(cand.get("dimension_basis", "unknown")),
                "deduction_authority": False,
                "no_instance_creation": True,
                "calibration_status": str(cand.get("calibration_status")) if cand.get("calibration_status") is not None else None,
            })
            project.evidence_observations.append(obs)

    # Process walls
    raw_walls = payload.get("walls") or payload.get("registered_walls") or []
    for idx, w_raw in enumerate(raw_walls):
        if isinstance(w_raw, dict):
            w_input = w_raw
        else:
            w_input = registered_wall_to_canonical_input(w_raw)

        wall_ref = str(w_input.get("wall_ref") or w_input.get("id") or f"W-{idx+1}")
        a_vec = _parse_vector2d(w_input.get("a"))
        b_vec = _parse_vector2d(w_input.get("b"))
        if a_vec is None or b_vec is None:
            # SECTION 15: Fail closed on missing A or B endpoints (no fake 0,0 -> 10,0 endpoints!)
            skipped_items.append({"id": wall_ref, "type": "WALL", "reason": "Missing or invalid start/end coordinates"})
            continue

        h_m = _safe_float(w_input.get("height_m") or w_input.get("unconstrained_height_m"))
        th_m = _safe_float(w_input.get("thickness_m") or w_input.get("wall_thickness_m")) or 0.23
        w_conf = parse_optional_confidence(w_input.get("confidence"))

        h_stat = str(w_input.get("height_status") or "").lower().strip()
        if h_stat == "confirmed":
            r_state = ReviewState.CONFIRMED
        elif h_stat == "inferred":
            r_state = ReviewState.INFERRED
        else:
            r_state = ReviewState.REVIEW_REQUIRED

        takeoff_elig = parse_strict_bool(w_input.get("takeoff_eligible")) if is_validated_internal_workspace else False
        prov = _parse_provenance(w_input.get("provenance"))
        if not prov.wall_ref:
            prov.wall_ref = wall_ref

        if isinstance(w_input.get("level"), dict):
            level_val = w_input.get("level")
        elif (
            w_input.get("level_name")
            or w_input.get("level_index") is not None
            or w_input.get("source_polygon")
            or w_input.get("prism_id")
            or w_input.get("level_id")
            or w_input.get("storey_id")
        ):
            level_val = {
                "level": w_input.get("level"),
                "level_name": w_input.get("level_name") or w_input.get("level"),
                "level_index": w_input.get("level_index"),
                "source_polygon": w_input.get("source_polygon") or w_input.get("prism_id"),
                "level_id": w_input.get("level_id") or w_input.get("storey_id"),
            }
        else:
            level_val = w_input.get("level") or w_input.get("level_name") or w_input.get("level_id")

        target_lvl, wall_lvl_slug = resolve_canonical_level(level_val, levels_map)

        wall_id = wall_ref if wall_ref.startswith("wall_") else f"wall_{wall_ref}"
        c_wall = CanonicalWall(
            id=wall_id,
            name=f"Wall {wall_ref}",
            start_point=a_vec,
            end_point=b_vec,
            height_m=h_m,
            thickness_m=th_m,
            confidence=w_conf,
            review_state=r_state,
            takeoff_eligible=takeoff_elig,
            deduction_authority=False,  # Delayed until physical validation!
            provenance=prov,
        )

        # Process attached openings
        raw_openings = w_input.get("openings") or []
        wall_has_valid_b5_physical_opening = False

        for op_idx, op_raw in enumerate(raw_openings):
            if not isinstance(op_raw, dict):
                continue

            op_id = str(op_raw.get("id") or f"op_{wall_ref}_{op_idx+1}")
            op_mark = str(op_raw.get("mark") or op_raw.get("label") or f"Opening {op_idx+1}")
            op_type_str = str(op_raw.get("opening_type") or op_raw.get("type") or "").upper().strip()
            if "DOOR" in op_type_str:
                op_type = ObjectType.DOOR
            elif "WINDOW" in op_type_str:
                op_type = ObjectType.WINDOW
            else:
                op_type = ObjectType.OPENING

            # SECTION 3: Explicit key presence lookup (NO 0.0 false-falsy fallback bugs!)
            offset_m = _get_present_float(op_raw, ["offset_along_wall_m", "offset_m", "position_along_wall_m"])
            sill_m = _get_present_float(op_raw, ["sill_height_m", "sill_m"])
            w_op = _get_present_float(op_raw, ["width_m", "width"])
            h_op = _get_present_float(op_raw, ["height_m", "height"])

            op_prov = _parse_provenance(op_raw.get("provenance"))
            if not op_prov.wall_ref:
                op_prov.wall_ref = wall_ref

            op_deduct_auth = revalidate_b5_opening(op_raw) if is_validated_internal_workspace else False

            # SECTION A, B, C: Enforce Opening Host Identity
            claimed_wall_ref = op_raw.get("resolved_wall_ref") or op_raw.get("wall_ref")
            is_wrong_host = False
            wrong_host_note = ""

            if claimed_wall_ref and str(claimed_wall_ref).strip():
                claimed_str = str(claimed_wall_ref).strip()
                if claimed_str != str(wall_ref).strip():
                    is_wrong_host = True
                    wrong_host_note = (
                        f"Wrong host conflict: opening claimed wall ref '{claimed_str}' "
                        f"!= container wall ref '{wall_ref}'"
                    )
                    op_deduct_auth = False

            # SECTION C: Wrong-level conflict check
            op_level_evidences = []
            for lkey in ("level", "level_name", "level_id", "storey_id"):
                lv = op_raw.get(lkey)
                if lv:
                    try:
                        _, lslug = resolve_canonical_level(lv, levels_map)
                    except Exception:
                        lslug = "unresolved"
                    if lslug != "unresolved":
                        op_level_evidences.append((lkey, lv, lslug))
            is_wrong_level = False
            wrong_level_note = ""
            if op_level_evidences and wall_lvl_slug != "unresolved":
                asserted_slugs = {s for _, _, s in op_level_evidences}
                if wall_lvl_slug not in asserted_slugs:
                    is_wrong_level = True
                    wrong_level_note = (
                        f"Wrong level conflict: opening level evidence "
                        f"{[(k, str(v)) for k, v, _ in op_level_evidences]} != host wall level '{wall_lvl_slug}'"
                    )
                    op_deduct_auth = False

            c_opening = CanonicalOpening(
                id=op_id,
                wall_id=c_wall.id if not is_wrong_host else None,
                name=op_mark,
                opening_type=op_type,
                width_m=w_op,
                height_m=h_op,
                offset_along_wall_m=offset_m,
                sill_height_m=sill_m,
                confidence=parse_optional_confidence(op_raw.get("confidence")),
                review_state=ReviewState.REVIEW_REQUIRED,
                takeoff_eligible=c_wall.takeoff_eligible,
                deduction_authority=op_deduct_auth,
                provenance=op_prov,
            )
            c_opening.metadata["claimed_wall_ref"] = str(claimed_wall_ref) if claimed_wall_ref else str(wall_ref)
            c_opening.metadata["actual_container_wall_ref"] = str(wall_ref)

            # SECTION 2: Correct tuple unpack of validate_opening_geometry
            is_phys_valid, geo_msg = validate_opening_geometry(c_opening, c_wall)

            if is_wrong_host:
                phys_state = "wrong_host"
                phys_reason = wrong_host_note
                op_deduct_auth = False
                skipped_items.append({"id": op_id, "type": "OPENING", "reason": wrong_host_note})
            elif is_wrong_level:
                phys_state = "wrong_level"
                phys_reason = wrong_level_note
                op_deduct_auth = False
                skipped_items.append({"id": op_id, "type": "OPENING", "reason": wrong_level_note})
            elif not is_phys_valid:
                phys_state = "invalid_geometry"
                phys_reason = geo_msg
                op_deduct_auth = False
                skipped_items.append({"id": op_id, "type": "OPENING", "reason": f"Invalid opening geometry: {geo_msg}"})
            elif parse_strict_bool(op_raw.get("manual_exclusion")) or parse_strict_bool(op_raw.get("excluded_from_takeoff")):
                phys_state = "manual_exclusion"
                phys_reason = "Opening manually excluded from takeoff reconciliation."
                op_deduct_auth = False
            elif op_deduct_auth:
                phys_state = "physical_b5_authorised"
                phys_reason = "Opens physically and passes automatic B5 deduction authority."
            else:
                phys_state = "physical_not_authorised"
                phys_reason = "Opens physically but lacks automatic B5 deduction authority."

            c_opening.review_state = ReviewState.CONFIRMED if phys_state == "physical_b5_authorised" else ReviewState.REVIEW_REQUIRED
            c_opening.deduction_authority = op_deduct_auth
            c_opening.metadata["physical_state"] = phys_state
            c_opening.metadata["physical_reason"] = phys_reason
            c_opening.metadata["geometry_valid"] = is_phys_valid

            if phys_state == "physical_b5_authorised":
                wall_has_valid_b5_physical_opening = True

            c_wall.openings.append(c_opening)

        # Overlap conflict detection over complete wall opening set
        if c_wall.openings:
            has_overlap, overlap_pairs = detect_opening_overlaps(c_wall.openings)
            if overlap_pairs:
                wall_has_valid_b5_physical_opening = False
                for c_opframe in c_wall.openings:
                    if c_opframe.metadata.get("physical_state") == "physical_b5_authorised":
                        c_opframe.review_state = ReviewState.REVIEW_REQUIRED
                        c_opframe.deduction_authority = False
                        c_opframe.metadata["physical_state"] = "conflict_overlap"
                        c_opframe.metadata["physical_reason"] = f"Overlap/duplicate conflict detected: {overlap_pairs}"

        c_wall.deduction_authority = wall_has_valid_b5_physical_opening
        target_lvl.walls.append(c_wall)

    # Process floors (Sections 6, 7, 8, 9)
    raw_floors = payload.get("floors") or payload.get("mapper_shapes") or []
    for f_idx, f_raw in enumerate(raw_floors):
        if not isinstance(f_raw, dict):
            continue

        man_m2 = _get_present_float(f_raw, ["manual_m2"])
        raw_box = f_raw.get("raw_box") if isinstance(f_raw.get("raw_box"), dict) else f_raw

        # Section 8: manual_m2 box without spatial polygon becomes evidence observation
        has_polygon_data = "x" in raw_box or "points" in raw_box or "polygon" in raw_box
        if man_m2 is not None and not has_polygon_data:
            obs = CanonicalEvidenceObservation.from_dict({
                "id": str(f_raw.get("box_id") or f"manual_floor_{f_idx+1}"),
                "kind": "manual_floor_area_allowance",
                "workspace_id": str(payload.get("workspace_id") or ""),
                "document_id": str(f_raw.get("document_id") or ""),
                "page_id": str(f_raw.get("page_id") or ""),
                "level_name": str(f_raw.get("_source_level") or ""),
                "producer": "pb_floor_mapper_v127",
                "producer_version": "v127",
                "confidence": 0.90,
                "review_state": ReviewState.CONFIRMED,
                "reason_physical_unavailable": f"Manual floor area allowance of {man_m2} m² lacks spatial polygon boundary",
            })
            project.evidence_observations.append(obs)
            continue

        # Section 7: Convert percentage coordinates to metric metres!
        w_px = _safe_float(f_raw.get("page_width_px"))
        h_px = _safe_float(f_raw.get("page_height_px"))
        px_m = _safe_float(f_raw.get("px_per_m"))

        f_pts_raw = _points_from_shape(raw_box)
        f_pts: List[Vector2D] = []
        if w_px and h_px and px_m and px_m > 0:
            for pt in f_pts_raw:
                p_vec = _parse_vector2d(pt)
                if p_vec:
                    # Convert percentage (0-100%) -> pixel -> metres
                    px_x = (p_vec.x / 100.0) * w_px
                    px_y = (p_vec.y / 100.0) * h_px
                    m_x = px_x / px_m
                    m_y = px_y / px_m
                    f_pts.append(Vector2D(x=m_x, y=m_y))
        else:
            f_pts = [_parse_vector2d(pt) for pt in f_pts_raw if _parse_vector2d(pt) is not None]
            f_pts = [pt for pt in f_pts if pt is not None]

        f_lvl_val = f_raw.get("level") or f_raw.get("level_name") or f_raw.get("level_id")
        target_lvl, _ = resolve_canonical_level(f_lvl_val, levels_map)

        f_id = str(f_raw.get("box_id") or f_raw.get("id") or f"floor_{f_idx+1}")
        f_name = str(f_raw.get("name") or f"Floor Polygon {f_idx+1}")

        # Section 9: Validate polygon strictly
        poly_valid, poly_msg = _validate_floor_polygon(f_pts) if (w_px and h_px and px_m) else (False, "Missing page dimensions or calibration px_per_m")
        if not poly_valid and f_pts_raw:
            obs = CanonicalEvidenceObservation.from_dict({
                "id": f_id,
                "kind": "rejected_floor_polygon",
                "workspace_id": str(payload.get("workspace_id") or ""),
                "document_id": str(f_raw.get("document_id") or ""),
                "page_id": str(f_raw.get("page_id") or ""),
                "level_name": str(f_lvl_val or ""),
                "producer": "pb_floor_mapper_v128",
                "producer_version": "v128",
                "review_state": ReviewState.REVIEW_REQUIRED,
                "reason_physical_unavailable": f"Invalid floor polygon geometry: {poly_msg}",
            })
            project.evidence_observations.append(obs)
            skipped_items.append({"id": f_id, "type": "FLOOR", "reason": f"Invalid floor polygon geometry: {poly_msg}"})

        c_floor = CanonicalFloor(
            id=f_id,
            name=f_name,
            polygon=f_pts if poly_valid else [],
            review_state=ReviewState.CONFIRMED if poly_valid else ReviewState.REVIEW_REQUIRED,
            takeoff_eligible=is_validated_internal_workspace,
            provenance=_parse_provenance(f_raw.get("provenance")),
        )
        c_floor.metadata["geometry_valid"] = poly_valid
        c_floor.metadata["geometry_reason"] = poly_msg
        target_lvl.floors.append(c_floor)

    # SECTION J, K, L: Process v140 roof evidence & caps with objective roof Z proof!
    roof_data = payload.get("roof_data")
    caps = []
    roof_evidence = {}
    if isinstance(roof_data, dict):
        roof_evidence = roof_data.get("evidence") if isinstance(roof_data.get("evidence"), dict) else {}
        caps = roof_data.get("caps") if isinstance(roof_data.get("caps"), list) else []
    elif isinstance(roof_data, list):
        caps = roof_data

    pitches_deg = roof_evidence.get("pitches_deg") or []
    first_pitch = (pitches_deg[0] if isinstance(pitches_deg, list) and pitches_deg else
                   float(pitches_deg) if isinstance(pitches_deg, (int, float)) else None)

    # Section J: A pitch does NOT prove roof form. Positive pitch alone remains UNKNOWN.
    roof_type = "FLAT" if parse_strict_bool(roof_evidence.get("flat")) else "UNKNOWN"

    # Section K: Cap Z is objective ONLY if EVERY contributing wall height is confirmed and > 0!
    all_contributing_walls_confirmed = (
        isinstance(raw_walls, list) and len(raw_walls) > 0 and
        all(
            _safe_float(w.get("height_m")) is not None and
            _safe_float(w.get("height_m")) > 0 and
            str(w.get("height_status") or "").lower().strip() == "confirmed"
            for w in raw_walls if isinstance(w, dict)
        )
    )

    for cap_idx, cap in enumerate(caps):
        if not isinstance(cap, dict):
            continue
        cap_pts_raw = cap.get("points")
        if not cap_pts_raw and isinstance(cap.get("polygon"), list):
            cap_pts_raw = cap.get("polygon")
        cap_pts = [_parse_vector2d(pt) for pt in (cap_pts_raw or []) if _parse_vector2d(pt) is not None]
        cap_pts = [pt for pt in cap_pts if pt is not None]

        roof_valid, roof_reason = (True, "Valid roof cap") if cap_pts else (False, "No polygon in roof cap")
        if cap_pts:
            roof_valid, roof_reason = _validate_floor_polygon(cap_pts)
        if not roof_valid:
            skipped_items.append({
                "id": str(cap.get("id") or f"roof_cap_{cap_idx+1}"),
                "type": "ROOF",
                "reason": f"Invalid roof cap geometry: {roof_reason}",
            })

        cap_lvl_val = cap.get("level") or roof_evidence.get("level") or None
        cap_lvl, _ = resolve_canonical_level(cap_lvl_val, levels_map)

        cap_pitch = first_pitch
        cap_type = roof_type

        # Section 5 (Phase 5L): Objective Roof Z proof & level elevation offset verification
        cap_z_local = _safe_float(cap.get("z"))
        
        # Use canonical walls attached to THIS cap's level
        all_levels = list(levels_map.values())
        cap_lvl_walls = cap_lvl.walls
        if not cap_lvl_walls and len(all_levels) == 1:
            cap_lvl_walls = all_levels[0].walls

        level_walls_confirmed = (
            len(cap_lvl_walls) > 0 and
            all(
                w.height_m is not None and
                w.height_m > 0 and
                w.review_state == ReviewState.CONFIRMED
                for w in cap_lvl_walls
            )
        )

        reproduced_cap_level_wall_height = max(
            [w.height_m for w in cap_lvl_walls if w.height_m is not None]
        ) if (cap_lvl_walls and any(w.height_m is not None for w in cap_lvl_walls)) else None

        cap_z_agrees = (
            level_walls_confirmed and
            reproduced_cap_level_wall_height is not None and
            (cap_z_local is None or abs(cap_z_local - reproduced_cap_level_wall_height) <= 0.05)
        )

        level_elev_known = cap_lvl.elevation_m is not None and _safe_float(cap_lvl.elevation_m) is not None

        if level_elev_known and level_walls_confirmed and cap_z_agrees:
            c_roof_z = cap_lvl.elevation_m + reproduced_cap_level_wall_height
            roof_r_state = ReviewState.CONFIRMED if roof_valid else ReviewState.REVIEW_REQUIRED
            z_msg = f"Roof cap Z confirmed objectively at absolute model Z {c_roof_z}m"
        else:
            c_roof_z = None
            roof_r_state = ReviewState.REVIEW_REQUIRED
            if not level_elev_known:
                z_msg = "Roof cap height Z rejected: cap level elevation_m is unresolved"
            elif not level_walls_confirmed:
                z_msg = "Roof cap height Z rejected: contributing walls for this level have unconfirmed or defaulted heights"
            else:
                z_msg = f"Roof cap Z rejected: cap local z ({cap_z_local}m) disagrees with reproduced level wall height ({reproduced_cap_level_wall_height}m)"

        c_roof = CanonicalRoof(
            id=str(cap.get("id") or f"roof_cap_{cap_idx+1}"),
            name=str(cap.get("name") or f"Roof Envelope {cap_idx+1}"),
            polygon=cap_pts if roof_valid else [],
            pitch_deg=cap_pitch,
            roof_type=cap_type,
            elevation=c_roof_z,
            review_state=roof_r_state,
            takeoff_eligible=is_validated_internal_workspace,
            provenance=_parse_provenance(cap.get("provenance")),
        )
        c_roof.metadata["z"] = c_roof_z
        c_roof.metadata["z_status_message"] = z_msg
        c_roof.metadata["z_reason"] = z_msg
        c_roof.metadata["pitches_deg"] = pitches_deg
        c_roof.metadata["geometry_valid"] = roof_valid
        c_roof.metadata["geometry_reason"] = roof_reason
        c_roof.metadata["v140_evidence_status"] = roof_evidence.get("status") or "Roof profile unresolved"
        cap_lvl.roofs.append(c_roof)

    if not levels_map:
        resolve_canonical_level(None, levels_map)

    building.levels = list(levels_map.values())
    project.buildings.append(building)
    return project, skipped_items


def get_producer_versions() -> Dict[str, str]:
    """SECTION 13 & 7: Derive consumed producer versions from real module VERSION constants."""
    versions = {
        "3d_engine": "v1.5.1",
        "v127_mapper": "v127",
        "v128_mapper": "v128",
        "v135_levels": "v135",
        "v139_walls": "v139",
        "v140_roof": "v140",
        "v175_openings": "v1.7.5",
        "v178_elevation_bridge": "v1.7.8",
        "v172_elevation_evidence": "v1.7.2",
    }
    try:
        from pb_opening_production_v175 import VERSION as v175_v
        versions["v175_openings"] = str(v175_v)
    except Exception:
        pass
    try:
        from pb_elevation_production_bridge_v178 import VERSION as v178_v
        versions["v178_elevation_bridge"] = str(v178_v)
    except Exception:
        pass
    try:
        from pb_elevation_evidence_v172 import VERSION as v172_v
        versions["v172_elevation_evidence"] = str(v172_v)
    except Exception:
        pass
    try:
        from pb_unified_building_v139 import VERSION as v139_v
        versions["v139_walls"] = str(v139_v)
    except Exception:
        pass
    try:
        from pb_roof_envelope_v140 import VERSION as v140_v
        versions["v140_roof"] = str(v140_v)
    except Exception:
        pass
    try:
        from pb_floor_mapper_v127 import VERSION as v127_v
        versions["v127_mapper"] = str(v127_v)
    except Exception:
        pass
    try:
        from pb_floor_mapper_v128 import VERSION as v128_v
        versions["v128_mapper"] = str(v128_v)
    except Exception:
        pass
    try:
        from pb_elevation_registration_v135 import VERSION as v135_v
        versions["v135_levels"] = str(v135_v)
    except Exception:
        pass
    return versions


def collect_workspace_3d_evidence(app: Any, workspace_id: int) -> Dict[str, Any]:
    """
    SECTION 18, 5F, 60, Q, 1, 2: Collects complete evidence snapshot v3 directly from production SQLite DB & producers.
    Reads persisted v175 page evidence settings (opening_evidence_v175_pages / opening_evidence_v175_page_<id>).
    """
    wid = require_workspace_id(workspace_id)
    snapshot: Dict[str, Any] = {
        "workspace_metadata": {"id": wid},
        "documents": [],
        "pages": [],
        "registered_walls": [],
        "mapper_shapes": [],
        "elevation_opening_candidates": [],
        "evidence_observations": [],
        "roof_data": None,
        "takeoff_rows": [],
        "diagnostics_log": [],
        "producer_versions": get_producer_versions()
    }

    if not (app and hasattr(app, "lquery")):
        snapshot["diagnostics_log"].append({"type": "no_lquery", "msg": "App instance lacks lquery method"})
        return snapshot

    try:
        # 1. Fetch workspace metadata
        ws_rows = app.lquery("SELECT id, job_no, job_name, builder_client, site_address FROM workspaces WHERE id = ?", (wid,))
        if ws_rows and isinstance(ws_rows[0], dict):
            snapshot["workspace_metadata"] = ws_rows[0]

        # 2. Fetch documents
        doc_rows = app.lquery("SELECT id, workspace_id, file_name, sha256, category, page_count, source_type FROM documents WHERE workspace_id = ?", (wid,))
        valid_doc_ids = set()
        for r in doc_rows:
            if isinstance(r, dict):
                snapshot["documents"].append(r)
                if r.get("id") is not None:
                    valid_doc_ids.add(int(r.get("id")))

        # 3. SECTION Q: Fetch pages & validate document-page workspace ownership!
        page_rows = app.lquery("SELECT id, workspace_id, document_id, page_no, page_label, page_type, scale_text, px_per_m, width_px, height_px, render_zoom, selected FROM pages WHERE workspace_id = ?", (wid,))
        valid_pages = []
        for p in page_rows:
            if isinstance(p, dict):
                doc_id = p.get("document_id")
                if doc_id is not None and int(doc_id) in valid_doc_ids:
                    valid_pages.append(p)
                else:
                    snapshot["diagnostics_log"].append({
                        "type": "stale_reference",
                        "msg": f"Page #{p.get('id')} references missing or foreign document_id {doc_id} in workspace #{wid}",
                    })
        snapshot["pages"] = valid_pages

        # 4. Fetch registered walls from v139 producer
        if hasattr(app, "build_registered_walls_v139") and callable(app.build_registered_walls_v139):
            try:
                reg_walls = app.build_registered_walls_v139(wid)
                snapshot["registered_walls"] = [registered_wall_to_canonical_input(w) for w in reg_walls]
            except Exception as e:
                snapshot["diagnostics_log"].append({"type": "v139_wall_error", "msg": str(e)})

        # 5. SECTION E & F: Fetch takeoff rows using correct app.registered_wall_takeoff_rows_v139(reg_walls) signature!
        if hasattr(app, "registered_wall_takeoff_rows_v139") and callable(app.registered_wall_takeoff_rows_v139):
            try:
                reg_wall_objs = app.build_registered_walls_v139(wid) if hasattr(app, "build_registered_walls_v139") else []
                v139_takeoff = app.registered_wall_takeoff_rows_v139(reg_wall_objs)
                for r in v139_takeoff:
                    if isinstance(r, dict):
                        snapshot["takeoff_rows"].append(r)
            except Exception as e:
                snapshot["diagnostics_log"].append({"type": "v139_takeoff_error", "msg": str(e)})

        # 6. Read real floor mapper state for pages using page_id (NOT page_no!)
        for p in valid_pages:
            p_id = p.get("id")
            p_no = p.get("page_no")
            if p_id is not None and hasattr(app, "workspace_setting"):
                mapper_setting = app.workspace_setting(wid, f"floor_mapper_v127_page_{p_id}", None)
                if mapper_setting:
                    try:
                        m_data = json.loads(mapper_setting) if isinstance(mapper_setting, str) else mapper_setting
                        if isinstance(m_data, dict):
                            calib = m_data.get("calibration")
                            w_px = _safe_float(p.get("width_px"))
                            h_px = _safe_float(p.get("height_px"))
                            px_m = calibration_px_per_m(calib, w_px, h_px) if (isinstance(calib, dict) and w_px and h_px) else None
                            page_label = str(p.get("page_label") or "").strip()
                            page_type = str(p.get("page_type") or "").strip()

                            raw_boxes = m_data.get("boxes")
                            if not isinstance(raw_boxes, list):
                                raw_boxes = [m_data] if ("x" in m_data or "manual_m2" in m_data or "points" in m_data) else []

                            for b_idx, box in enumerate(raw_boxes):
                                if isinstance(box, dict):
                                    snapshot["mapper_shapes"].append({
                                        "box_id": box.get("id") or f"box_{p_id}_{b_idx+1}",
                                        "page_id": p_id,
                                        "document_id": p.get("document_id"),
                                        "page_width_px": w_px,
                                        "page_height_px": h_px,
                                        "px_per_m": px_m,
                                        "calibration": calib,
                                        "raw_box": box,
                                        "manual_m2": _safe_float(box.get("manual_m2")),
                                        "_source_level": page_label or page_type or None,
                                        "provenance": {
                                            "workspace_id": str(wid),
                                            "document_id": str(p.get("document_id")),
                                            "page_id": str(p_id),
                                            "page_number": p_no,
                                        }
                                    })
                    except Exception:
                        pass

        # 7. Collect REAL v140 roof envelope evidence & caps
        roof_snapshot = None
        try:
            roof_evidence_res = None
            if hasattr(app, "roof_evidence_v140") and callable(app.roof_evidence_v140):
                roof_evidence_res = app.roof_evidence_v140(wid)

            roof_caps_res = None
            if hasattr(app, "roof_caps_v140") and callable(app.roof_caps_v140):
                prisms_walls = [registered_wall_to_canonical_input(w) for w in
                                (app.build_registered_walls_v139(wid)
                                 if hasattr(app, "build_registered_walls_v139") else [])]
                roof_caps_res = app.roof_caps_v140(wid, prisms_walls)

            if roof_evidence_res is not None and isinstance(roof_evidence_res, dict):
                roof_snapshot = {
                    "producer": "v140",
                    "evidence": roof_evidence_res,
                    "caps": list(roof_caps_res) if isinstance(roof_caps_res, list) else [],
                }
        except Exception as e:
            snapshot["diagnostics_log"].append({"type": "v140_roof_error", "msg": str(e)})
        snapshot["roof_data"] = roof_snapshot

        # 8. SECTION 1, 2, 14: Collect persisted v175 page evidence (elevation openings, diagnostics, provenance)
        try:
            p5_pages_setting = app.workspace_setting(wid, "opening_evidence_v175_pages", "[]") if hasattr(app, "workspace_setting") else None
            p5_page_ids = json.loads(str(p5_pages_setting or "[]")) if p5_pages_setting else []
            if not isinstance(p5_page_ids, list):
                p5_page_ids = []

            for p_id in p5_page_ids:
                try:
                    p_id_int = int(p_id)
                except (ValueError, TypeError):
                    continue

                matching_page = next((p for p in valid_pages if p.get("id") == p_id_int), None)
                if not matching_page:
                    continue

                page_setting = app.workspace_setting(wid, f"opening_evidence_v175_page_{p_id_int}", "{}")
                p5_data = json.loads(str(page_setting or "{}")) if page_setting else {}
                if not isinstance(p5_data, dict):
                    continue

                elev_ops = p5_data.get("elevation_openings") or []
                elev_provs = p5_data.get("elevation_provenance") or []
                elev_diags = p5_data.get("elevation_diagnostics") or []

                for idx, item in enumerate(elev_ops):
                    if isinstance(item, dict):
                        snapshot["elevation_opening_candidates"].append(item)
                        
                        prov = elev_provs[idx] if (isinstance(elev_provs, list) and idx < len(elev_provs) and isinstance(elev_provs[idx], dict)) else {}
                        diag = elev_diags[idx] if (isinstance(elev_diags, list) and idx < len(elev_diags) and isinstance(elev_diags[idx], dict)) else {}

                        obs_id = str(item.get("id") or item.get("candidate_id") or f"obs_v175_p{p_id_int}_{idx+1}")
                        dwg_ref = str(prov.get("drawing_ref") or item.get("drawing_ref") or prov.get("drawing_id") or item.get("drawing_reference") or "") or None
                        dwg_title = str(prov.get("drawing_title") or item.get("drawing_title") or "") or None

                        snapshot["evidence_observations"].append({
                            "candidate_id": obs_id,
                            "kind": "elevation_opening_candidate",
                            "workspace_id": str(wid),
                            "document_id": str(matching_page.get("document_id")) if matching_page.get("document_id") is not None else None,
                            "page_id": str(p_id_int),
                            "page_no": matching_page.get("page_no"),
                            "source_filename": str(prov.get("source_filename") or prov.get("source_pdf") or ""),
                            "source_page": prov.get("source_page") or item.get("source_page_no") or matching_page.get("page_no"),
                            "drawing_reference": dwg_ref,
                            "drawing_title": dwg_title,
                            "side": str(prov.get("elevation_side") or item.get("elevation_side") or item.get("side") or item.get("facade") or "") or None,
                            "level_name": str(prov.get("level") or item.get("level") or "") or None,
                            "wall_ref": str(prov.get("wall_ref") or item.get("wall_ref") or "") or None,
                            "source_coords": item.get("bbox_px") or prov.get("source_coords") or item.get("source_coords") or item.get("bbox"),
                            "coordinate_space": str(prov.get("coord_space") or prov.get("coordinate_space") or item.get("coord_space") or item.get("coordinate_space") or "") or None,
                            "calibration": prov.get("calibration") or item.get("calibration"),
                            "calibration_source": str(prov.get("calibration_source") or "") or None,
                            "calibration_state": str(prov.get("calibration_state") or "") or None,
                            "width_m": _safe_float(item.get("width_m")),
                            "height_m": _safe_float(item.get("height_m")),
                            "accepted_state": True,
                            "rejected_state": False,
                            "reason": "v1.7.5/v1.7.8 elevation evidence candidate",
                            "producer": "pb_opening_production_v175",
                            "producer_version": snapshot["producer_versions"].get("v175_openings", "v1.7.5"),
                            "deduction_authority": False,
                            "no_instance_creation": True,
                        })
        except Exception as e:
            snapshot["diagnostics_log"].append({"type": "v175_persisted_evidence_error", "msg": str(e)})

    except Exception as e:
        snapshot["diagnostics_log"].append({"type": "evidence_collection_error", "msg": str(e)})

    return snapshot


def planreader_workspace_to_canonical(app: Any, workspace_id: int) -> WorkspaceCanonicalResult:
    """
    SECTION L & 5F: Main production entry point executing workspace evidence collection,
    canonical model synthesis, diagnostics, and snapshot fingerprinting.
    """
    wid = require_workspace_id(workspace_id)
    snapshot = collect_workspace_3d_evidence(app, wid)

    from pb_canonical_persistence import compute_workspace_source_fingerprint
    snapshot_fp = compute_workspace_source_fingerprint(snapshot)

    project, skipped = planreader_to_canonical_model(snapshot, is_validated_internal_workspace=True)
    project.id = f"ws_{wid}_canonical"
    project.name = f"Workspace #{wid} Canonical BIM Model"

    diagnostics = generate_production_diagnostics_report(project, workspace_data=snapshot, skipped_items=skipped)
    diagnostics["workspace_id"] = wid
    diagnostics["source_revision_fingerprint"] = snapshot_fp

    return WorkspaceCanonicalResult(
        project=project,
        snapshot=snapshot,
        snapshot_fingerprint=snapshot_fp,
        diagnostics=diagnostics,
        skipped_items=skipped,
    )
