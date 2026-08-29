"""
PlanReader Geometry Services Module.

Performs clean metric geometric calculations strictly on supplied canonical model data.

IMPORTANT GUARANTEES:
1. Operates ONLY on pre-supplied canonical geometry data.
2. Does NOT create a duplicate PDF measurement/extraction engine.
3. Does NOT automatically grant deduction authority for calculated net wall areas.
4. Separates authorized vs unauthorized opening deduction areas cleanly (no blanket authorization).
5. Detects overlapping/duplicate openings and invalid/unresolved geometry:
   - If a conflict exists (overlapping, duplicate, or invalid openings), authorized deduction area FAILS CLOSED to 0.0,
     and authorized net area equals gross wall area (no deductions applied!).
6. level_extents / model_bounds fail closed in Z when level elevation is unknown (no ground level 0.0 assumption).
   Unknown level height derives Z top from explicit element heights if available.
7. Opening position validation requires explicit sill_height_m and offset_along_wall_m (no 0.0 fallbacks!).
"""

import math
from enum import Enum
from typing import Dict, Any, List, Optional, Tuple
from pb_canonical_building import (
    CanonicalProject,
    CanonicalBuilding,
    CanonicalLevel,
    CanonicalWall,
    CanonicalOpening,
    CanonicalElement,
    CanonicalSpace,
    Vector2D,
    Vector3D,
    BoundingBox3D,
    ReviewState,
    parse_strict_bool,
)


def _is_valid_float(val: Any) -> bool:
    if val is None:
        return False
    try:
        f = float(val)
        return not (math.isnan(f) or math.isinf(f))
    except (ValueError, TypeError):
        return False


def validate_wall_geometry(wall: CanonicalWall) -> Tuple[bool, str]:
    """Validates wall baseline, height, and thickness parameters."""
    if not (wall.start_point and wall.start_point.is_valid()):
        return False, "Invalid or missing wall start_point coordinates"
    if not (wall.end_point and wall.end_point.is_valid()):
        return False, "Invalid or missing wall end_point coordinates"
    if not _is_valid_float(wall.height_m) or wall.height_m <= 1e-4:
        return False, f"Invalid or missing wall height_m: {wall.height_m}"
    if wall.thickness_m is not None and (not _is_valid_float(wall.thickness_m) or wall.thickness_m < 0.0):
        return False, f"Invalid wall thickness_m: {wall.thickness_m}"

    w_len = wall_length(wall)
    if w_len <= 1e-4:
        return False, f"Wall length is zero or negative: {w_len}"

    return True, "Valid Wall Geometry"


def validate_opening_geometry(opening: CanonicalOpening, wall: Optional[CanonicalWall] = None) -> Tuple[bool, str]:
    """
    Validates opening dimensions, sill height, offset, and wall bounds.
    Requires explicit non-null width_m, height_m, offset_along_wall_m, and sill_height_m.
    No 0.0 fallback substitutions allowed!
    """
    if not _is_valid_float(opening.width_m) or opening.width_m <= 0.0:
        return False, f"Invalid or missing opening width_m: {opening.width_m}"
    if not _is_valid_float(opening.height_m) or opening.height_m <= 0.0:
        return False, f"Invalid or missing opening height_m: {opening.height_m}"
    if not _is_valid_float(opening.offset_along_wall_m) or opening.offset_along_wall_m < 0.0:
        return False, f"Invalid or missing opening offset_along_wall_m: {opening.offset_along_wall_m}"
    if not _is_valid_float(opening.sill_height_m) or opening.sill_height_m < 0.0:
        return False, f"Invalid or missing opening sill_height_m: {opening.sill_height_m}"

    if wall is not None:
        valid_w, msg_w = validate_wall_geometry(wall)
        if not valid_w:
            return False, f"Host wall invalid: {msg_w}"

        w_len = wall_length(wall)
        if opening.offset_along_wall_m + opening.width_m > w_len + 1e-3:
            return False, f"Opening width ({opening.width_m}m at offset {opening.offset_along_wall_m}m) exceeds wall length ({w_len:.2f}m)"

        if wall.height_m is not None and opening.sill_height_m + opening.height_m > wall.height_m + 1e-3:
            return False, f"Opening top ({opening.sill_height_m + opening.height_m}m) exceeds wall height ({wall.height_m:.2f}m)"

    return True, "Valid Opening Geometry"


def detect_opening_overlaps(openings: List[CanonicalOpening]) -> Tuple[bool, List[Tuple[str, str]]]:
    """
    Detects duplicate or partially overlapping openings along the wall.
    Returns (has_overlaps, list_of_overlapping_pairs).
    """
    overlaps = []
    n = len(openings)
    for i in range(n):
        op1 = openings[i]
        valid1, _ = validate_opening_geometry(op1)
        if not valid1:
            continue
        off1, w1 = op1.offset_along_wall_m, op1.width_m
        sill1, h1 = op1.sill_height_m, op1.height_m

        for j in range(i + 1, n):
            op2 = openings[j]
            valid2, _ = validate_opening_geometry(op2)
            if not valid2:
                continue
            off2, w2 = op2.offset_along_wall_m, op2.width_m
            sill2, h2 = op2.sill_height_m, op2.height_m

            # Check 1D interval overlap along length AND height
            horiz_overlap = max(0.0, min(off1 + w1, off2 + w2) - max(off1, off2))
            vert_overlap = max(0.0, min(sill1 + h1, sill2 + h2) - max(sill1, sill2))

            if horiz_overlap > 1e-3 and vert_overlap > 1e-3:
                overlaps.append((op1.id, op2.id))

    return len(overlaps) > 0, overlaps


def wall_length(wall: CanonicalWall) -> float:
    """Calculates Euclidean length of a wall in meters."""
    if not (wall.start_point and wall.start_point.is_valid() and wall.end_point and wall.end_point.is_valid()):
        return 0.0
    dx = wall.end_point.x - wall.start_point.x
    dy = wall.end_point.y - wall.start_point.y
    length = math.hypot(dx, dy)
    return 0.0 if math.isnan(length) or math.isinf(length) else float(length)


def wall_gross_area(wall: CanonicalWall) -> float:
    """Calculates gross vertical surface area of one side of a wall in m²."""
    w_len = wall_length(wall)
    h = wall.height_m if _is_valid_float(wall.height_m) else 0.0
    return float(w_len * max(0.0, h))


def gross_opening_area(opening: CanonicalOpening) -> float:
    """Calculates gross opening rectangle area in m²."""
    w = opening.width_m if _is_valid_float(opening.width_m) else 0.0
    h = opening.height_m if _is_valid_float(opening.height_m) else 0.0
    return float(max(0.0, w) * max(0.0, h))


def attached_opening_geometry(opening: CanonicalOpening, wall: CanonicalWall) -> Dict[str, Any]:
    """
    Computes 3D relative placement geometry and bounding box for an opening on a host wall.
    Operates strictly on pre-supplied opening offsets and dimensions.
    """
    is_valid, msg = validate_opening_geometry(opening, wall)
    if not is_valid:
        return {
            "opening_id": opening.id,
            "wall_id": wall.id,
            "is_valid": False,
            "validation_error": msg,
            "gross_area_m2": 0.0,
            "bounding_box_3d": None,
        }

    w_len = wall_length(wall)
    offset = opening.offset_along_wall_m
    w_width = opening.width_m
    w_height = opening.height_m
    sill = opening.sill_height_m

    if w_len > 1e-6:
        ux = (wall.end_point.x - wall.start_point.x) / w_len
        uy = (wall.end_point.y - wall.start_point.y) / w_len
    else:
        ux, uy = 1.0, 0.0

    x_start = wall.start_point.x + ux * offset
    y_start = wall.start_point.y + uy * offset
    x_end = wall.start_point.x + ux * (offset + w_width)
    y_end = wall.start_point.y + uy * (offset + w_width)

    z_min = sill
    z_max = sill + w_height

    min_pt = Vector3D(x=min(x_start, x_end), y=min(y_start, y_end), z=z_min)
    max_pt = Vector3D(x=max(x_start, x_end), y=max(y_start, y_end), z=z_max)

    return {
        "opening_id": opening.id,
        "wall_id": wall.id,
        "is_valid": True,
        "offset_along_wall_m": offset,
        "width_m": w_width,
        "height_m": w_height,
        "sill_height_m": sill,
        "gross_area_m2": gross_opening_area(opening),
        "bounding_box_3d": BoundingBox3D(min_point=min_pt, max_point=max_pt).to_dict(),
        "wall_baseline_start": {"x": x_start, "y": y_start},
        "wall_baseline_end": {"x": x_end, "y": y_end},
    }


def potential_net_wall_area(wall: CanonicalWall) -> Dict[str, Any]:
    """
    Calculates wall gross area, observed opening areas, authorized vs unauthorized opening deductions,
    and potential net area.
    """
    wall_deduction_auth = parse_strict_bool(wall.deduction_authority)

    valid_w, msg_w = validate_wall_geometry(wall)
    if not valid_w:
        return {
            "wall_id": wall.id,
            "is_valid": False,
            "validation_error": msg_w,
            "gross_wall_area_m2": 0.0,
            "observed_opening_area_m2": 0.0,
            "potential_net_area_m2": 0.0,
            "authorized_opening_deduction_area_m2": 0.0,
            "authorized_net_area_m2": 0.0,
            "unauthorized_opening_area_m2": 0.0,
            "valid_opening_count": 0,
            "invalid_unresolved_opening_count": len(wall.openings),
            "authorized_opening_count": 0,
            "unauthorized_opening_count": len(wall.openings),
            "has_overlapping_openings": False,
            "all_deductions_authorized": False,
            "authority_note": f"Invalid Wall Geometry: {msg_w}",
        }

    gross = wall_gross_area(wall)
    observed_opening_area = 0.0
    raw_authorized_deduction_area = 0.0

    valid_count = 0
    invalid_count = 0
    auth_count = 0
    unauth_count = 0

    for op in wall.openings:
        valid_op, _ = validate_opening_geometry(op, wall)
        if not valid_op:
            invalid_count += 1
            continue

        valid_count += 1
        op_area = gross_opening_area(op)
        observed_opening_area += op_area

        op_deduction_auth = parse_strict_bool(op.deduction_authority)
        if wall_deduction_auth and op_deduction_auth:
            raw_authorized_deduction_area += op_area
            auth_count += 1
        else:
            unauth_count += 1

    has_overlaps, overlap_pairs = detect_opening_overlaps(wall.openings)
    potential_net = max(0.0, gross - observed_opening_area)

    # FAIL-CLOSED FOR AUTHORIZED NET AREA ON CONFLICT:
    has_conflict = has_overlaps or (invalid_count > 0) or not wall_deduction_auth

    if has_conflict:
        authorized_deduction_area = 0.0
        authorized_net = gross  # Fail closed to gross wall area
        effective_auth_count = 0
        all_authorized = False

        if has_overlaps:
            note = f"Conflict: Overlapping / Duplicate Openings ({len(overlap_pairs)} pair(s)) — Authorized Net Fails Closed to Gross Wall"
        elif invalid_count > 0:
            note = f"Conflict: {invalid_count} Invalid/Unresolved Opening(s) — Authorized Net Fails Closed to Gross Wall"
        else:
            note = "Deduction NOT Authorized — Potential Net Geometry Only"
    else:
        authorized_deduction_area = raw_authorized_deduction_area
        authorized_net = max(0.0, gross - authorized_deduction_area)
        effective_auth_count = auth_count
        all_authorized = (valid_count > 0 and unauth_count == 0)
        
        if all_authorized:
            note = "All Opening Deductions Authorized by Evidence"
        elif authorized_deduction_area > 0:
            note = f"Partial Deduction Authorized ({authorized_deduction_area:.2f} m² authorized)"
        else:
            note = "Deduction NOT Authorized — Potential Net Geometry Only"

    unauthorized_opening_area = max(0.0, observed_opening_area - authorized_deduction_area)

    return {
        "wall_id": wall.id,
        "is_valid": True,
        "gross_wall_area_m2": gross,
        "observed_opening_area_m2": observed_opening_area,
        "potential_net_area_m2": potential_net,
        "authorized_opening_deduction_area_m2": authorized_deduction_area,
        "authorized_net_area_m2": authorized_net,
        "unauthorized_opening_area_m2": unauthorized_opening_area,
        "valid_opening_count": valid_count,
        "invalid_unresolved_opening_count": invalid_count,
        "authorized_opening_count": effective_auth_count,
        "unauthorized_opening_count": unauth_count,
        "has_overlapping_openings": has_overlaps,
        "all_deductions_authorized": all_authorized,
        "authority_note": note,
    }


def space_floor_area(space: CanonicalSpace) -> float:
    """Calculates floor area of a room/space boundary polygon using shoelace formula in m²."""
    if space.specified_floor_area_m2 is not None and _is_valid_float(space.specified_floor_area_m2) and space.specified_floor_area_m2 > 0:
        return float(space.specified_floor_area_m2)

    poly = [pt for pt in space.boundary_polygon if pt and pt.is_valid()]
    if len(poly) < 3:
        return 0.0

    n = len(poly)
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += poly[i].x * poly[j].y
        area -= poly[j].x * poly[i].y

    return float(abs(area) * 0.5)


def level_extents(level: CanonicalLevel) -> Optional[BoundingBox3D]:
    """
    Calculates total 3D bounding box for all elements on a specific level.
    ROUND 4 GATE 1: Returns None if level.elevation_m is unknown.
    Derives Z top from explicit element heights where level.height_m is missing.
    """
    if not _is_valid_float(level.elevation_m):
        return None

    min_x, max_x = float("inf"), float("-inf")
    min_y, max_y = float("inf"), float("-inf")

    z_min = float(level.elevation_m)
    
    # Derive max Z height from explicit level height or element heights
    max_elem_h = 0.0
    if _is_valid_float(level.height_m) and level.height_m > 0:
        max_elem_h = level.height_m
    else:
        for w in level.walls:
            if _is_valid_float(w.height_m):
                max_elem_h = max(max_elem_h, w.height_m)
        for col in level.columns:
            if _is_valid_float(col.height_m):
                max_elem_h = max(max_elem_h, col.height_m)
    for r in level.roofs:
        if _is_valid_float(r.elevation):
            max_elem_h = max(max_elem_h, r.elevation - z_min)
            z_min = min(z_min, r.elevation)

    z_max = z_min + max_elem_h

    def include_pt(x: Optional[float], y: Optional[float]):
        nonlocal min_x, max_x, min_y, max_y
        if _is_valid_float(x) and _is_valid_float(y):
            min_x = min(min_x, x)
            max_x = max(max_x, x)
            min_y = min(min_y, y)
            max_y = max(max_y, y)

    for w in level.walls:
        if w.start_point: include_pt(w.start_point.x, w.start_point.y)
        if w.end_point: include_pt(w.end_point.x, w.end_point.y)

    for group in [level.floors, level.ceilings, level.roofs, level.soffits, level.balconies]:
        for item in group:
            for pt in getattr(item, "polygon", []):
                include_pt(pt.x, pt.y)

    for col in level.columns:
        if col.center and col.width_m and col.depth_m:
            w2, d2 = col.width_m / 2.0, col.depth_m / 2.0
            include_pt(col.center.x - w2, col.center.y - d2)
            include_pt(col.center.x + w2, col.center.y + d2)

    if min_x == float("inf"):
        return None

    return BoundingBox3D(
        min_point=Vector3D(x=min_x, y=min_y, z=z_min),
        max_point=Vector3D(x=max_x, y=max_y, z=z_max),
    )


def model_bounds(project: CanonicalProject) -> Tuple[bool, Optional[BoundingBox3D]]:
    """
    Calculates global 3D bounding box across all buildings and levels in a project.
    SECTION 12: Includes objective roof elevation in bounds calculation.
    Fails closed: Returns (False, None) if project is empty or bounds cannot be derived.
    """
    min_x, max_x = float("inf"), float("-inf")
    min_y, max_y = float("inf"), float("-inf")
    min_z, max_z = float("inf"), float("-inf")

    found_elements = False

    for b in project.buildings:
        for lvl in b.levels:
            l_bounds = level_extents(lvl)
            if l_bounds is not None:
                found_elements = True
                min_x = min(min_x, l_bounds.min_point.x)
                max_x = max(max_x, l_bounds.max_point.x)
                min_y = min(min_y, l_bounds.min_point.y)
                max_y = max(max_y, l_bounds.max_point.y)
                min_z = min(min_z, l_bounds.min_point.z)
                max_z = max(max_z, l_bounds.max_point.z)

            for r in lvl.roofs:
                if _is_valid_float(r.elevation):
                    found_elements = True
                    max_z = max(max_z, r.elevation)
                    min_z = min(min_z, r.elevation)

    if not found_elements:
        return False, None

    return True, BoundingBox3D(
        min_point=Vector3D(x=min_x, y=min_y, z=min_z),
        max_point=Vector3D(x=max_x, y=max_y, z=max_z),
    )


def surface_metadata(element: CanonicalElement) -> Dict[str, Any]:
    """Returns detailed surface metadata, provenance, substrate, and review details for an element."""
    prov = element.provenance.to_dict() if element.provenance else {}
    has_explicit_provenance = bool(prov.get("source_pdf") or prov.get("drawing_id"))

    return {
        "id": element.id,
        "name": element.name,
        "type": element.object_type.value if isinstance(element.object_type, Enum) else str(element.object_type),
        "substrate": element.substrate or "Not Specified",
        "finish": element.finish or "Not Specified",
        "confidence": element.confidence,
        "review_state": element.review_state.value if isinstance(element.review_state, Enum) else str(element.review_state),
        "provenance": prov,
        "has_explicit_provenance": has_explicit_provenance,
        "takeoff_eligible": parse_strict_bool(element.takeoff_eligible),
        "deduction_authority": parse_strict_bool(element.deduction_authority),
        "metadata": element.metadata,
    }
