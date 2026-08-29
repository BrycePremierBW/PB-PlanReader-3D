"""
PlanReader Geometry Services Module.

Performs clean metric geometric calculations strictly on supplied canonical model data.

IMPORTANT GUARANTEES:
1. Operates ONLY on pre-supplied canonical geometry data.
2. Does NOT create a duplicate PDF measurement/extraction engine.
3. Does NOT automatically grant deduction authority for calculated net wall areas.
"""

import math
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
)


def wall_length(wall: CanonicalWall) -> float:
    """Calculates Euclidean length of a wall in meters."""
    dx = wall.end_point.x - wall.start_point.x
    dy = wall.end_point.y - wall.start_point.y
    return float(math.hypot(dx, dy))


def wall_gross_area(wall: CanonicalWall) -> float:
    """Calculates gross vertical surface area of one side of a wall (length * height) in m²."""
    return float(wall_length(wall) * wall.height_m)


def gross_opening_area(opening: CanonicalOpening) -> float:
    """Calculates gross opening rectangle area (width * height) in m²."""
    return float(opening.width_m * opening.height_m)


def attached_opening_geometry(opening: CanonicalOpening, wall: CanonicalWall) -> Dict[str, Any]:
    """
    Computes 3D relative placement geometry and bounding box for an opening on a host wall.
    Operates strictly on pre-supplied opening offsets and dimensions.
    """
    w_len = wall_length(wall)
    offset = opening.offset_along_wall_m
    w_width = opening.width_m
    w_height = opening.height_m
    sill = opening.sill_height_m

    # Compute wall unit directional vector
    if w_len > 1e-6:
        ux = (wall.end_point.x - wall.start_point.x) / w_len
        uy = (wall.end_point.y - wall.start_point.y) / w_len
    else:
        ux, uy = 1.0, 0.0

    # 3D start point along wall baseline
    x_start = wall.start_point.x + ux * offset
    y_start = wall.start_point.y + uy * offset

    # 3D end point along wall baseline
    x_end = wall.start_point.x + ux * (offset + w_width)
    y_end = wall.start_point.y + uy * (offset + w_width)

    z_min = sill
    z_max = sill + w_height

    min_pt = Vector3D(x=min(x_start, x_end), y=min(y_start, y_end), z=z_min)
    max_pt = Vector3D(x=max(x_start, x_end), y=max(y_start, y_end), z=z_max)

    return {
        "opening_id": opening.id,
        "wall_id": wall.id,
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
    Calculates potential net wall area (gross area minus opening areas) in m².

    CRITICAL SAFETY REQUIREMENT:
    Calculated potential net area does NOT automatically grant deduction authority.
    The returned dictionary explicitly contains deduction_authorized=False unless
    explicitly authorized upstream by evidence rules.
    """
    gross = wall_gross_area(wall)
    openings_area = sum(gross_opening_area(op) for op in wall.openings)
    potential_net = max(0.0, gross - openings_area)

    # Check if wall or any opening carries explicit deduction authority
    auth = wall.deduction_authority or (any(op.deduction_authority for op in wall.openings) if wall.openings else False)

    return {
        "wall_id": wall.id,
        "gross_wall_area_m2": gross,
        "total_opening_area_m2": openings_area,
        "potential_net_area_m2": potential_net,
        "opening_count": len(wall.openings),
        "deduction_authorized": bool(auth),
        "authority_note": (
            "Deduction Authorized by Evidence" if auth else "Deduction NOT Authorized — Potential Net Geometry Only"
        ),
    }


def space_floor_area(space: CanonicalSpace) -> float:
    """Calculates floor area of a room/space boundary polygon using shoelace formula in m²."""
    if space.specified_floor_area_m2 is not None and space.specified_floor_area_m2 > 0:
        return float(space.specified_floor_area_m2)

    poly = space.boundary_polygon
    if len(poly) < 3:
        return 0.0

    n = len(poly)
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += poly[i].x * poly[j].y
        area -= poly[j].x * poly[i].y

    return float(abs(area) * 0.5)


def level_extents(level: CanonicalLevel) -> BoundingBox3D:
    """Calculates total 3D bounding box for all elements on a specific level."""
    min_x, max_x = float("inf"), float("-inf")
    min_y, max_y = float("inf"), float("-inf")

    z_min = level.elevation_m
    z_max = level.elevation_m + level.height_m

    def include_pt(x: float, y: float):
        nonlocal min_x, max_x, min_y, max_y
        min_x = min(min_x, x)
        max_x = max(max_x, x)
        min_y = min(min_y, y)
        max_y = max(max_y, y)

    # Walls
    for w in level.walls:
        include_pt(w.start_point.x, w.start_point.y)
        include_pt(w.end_point.x, w.end_point.y)

    # Polygons (Floors, Ceilings, Roofs, Soffits, Balconies)
    for group in [level.floors, level.ceilings, level.roofs, level.soffits, level.balconies]:
        for item in group:
            for pt in getattr(item, "polygon", []):
                include_pt(pt.x, pt.y)

    # Columns
    for col in level.columns:
        w2, d2 = col.width_m / 2.0, col.depth_m / 2.0
        include_pt(col.center.x - w2, col.center.y - d2)
        include_pt(col.center.x + w2, col.center.y + d2)

    if min_x == float("inf"):
        min_x, max_x, min_y, max_y = 0.0, 10.0, 0.0, 10.0

    return BoundingBox3D(
        min_point=Vector3D(x=min_x, y=min_y, z=z_min),
        max_point=Vector3D(x=max_x, y=max_y, z=z_max),
    )


def model_bounds(project: CanonicalProject) -> BoundingBox3D:
    """Calculates global 3D bounding box across all buildings and levels in a project."""
    min_x, max_x = float("inf"), float("-inf")
    min_y, max_y = float("inf"), float("-inf")
    min_z, max_z = float("inf"), float("-inf")

    for b in project.buildings:
        for lvl in b.levels:
            l_bounds = level_extents(lvl)
            min_x = min(min_x, l_bounds.min_point.x)
            max_x = max(max_x, l_bounds.max_point.x)
            min_y = min(min_y, l_bounds.min_point.y)
            max_y = max(max_y, l_bounds.max_point.y)
            min_z = min(min_z, l_bounds.min_point.z)
            max_z = max(max_z, l_bounds.max_point.z)

    if min_x == float("inf"):
        return BoundingBox3D(min_point=Vector3D(0, 0, 0), max_point=Vector3D(10, 10, 3))

    return BoundingBox3D(
        min_point=Vector3D(x=min_x, y=min_y, z=min_z),
        max_point=Vector3D(x=max_x, y=max_y, z=max_z),
    )


def surface_metadata(element: CanonicalElement) -> Dict[str, Any]:
    """Returns detailed surface metadata, provenance, substrate, and review details for an element."""
    return {
        "id": element.id,
        "name": element.name,
        "type": element.object_type.value if isinstance(element.object_type, Enum) else str(element.object_type),
        "substrate": element.substrate or "Not Specified",
        "finish": element.finish or "Not Specified",
        "confidence": element.confidence,
        "review_state": element.review_state.value if isinstance(element.review_state, Enum) else str(element.review_state),
        "provenance": element.provenance.to_dict(),
        "takeoff_eligible": element.takeoff_eligible,
        "deduction_authority": element.deduction_authority,
        "metadata": element.metadata,
    }
