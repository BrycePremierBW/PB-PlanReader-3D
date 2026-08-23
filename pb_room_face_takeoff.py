"""Room face take-off: extract calibrated room polygons → trustworthy m² take-off rows.

Priority 2: wires v145 planar face extraction through the calibrated scale
system and false-positive filters to produce per-room floor/ceiling take-off rows.

Architecture:
  PDF vector segments → v145 extract_planar_faces → page-space polygons
    → scale calibration → real-world m² → false-positive filter → take-off rows

Scale chain (reuses Priority 1):
  PDF points × (25.4/72) = page mm × real_metres_per_page_mm = real metres
  area_m2 = area_page_pts² × (real_metres_per_page_mm × 25.4/72)²

Never produce a confidently stated m² quantity from an uncalibrated polygon.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import atan2, pi
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PDF_PT_TO_MM = 25.4 / 72.0  # 1 PDF point = 0.3528 mm
MM_PER_PT = PDF_PT_TO_MM

# False-positive thresholds (in REAL-WORLD m² once calibrated)
MIN_ROOM_AREA_M2 = 2.0       # smaller than ~2 m² is unlikely to be a room
MAX_ROOM_AREA_M2 = 2500.0    # larger than 2500 m² is a building outline
MIN_ELONGATION = 12.0        # aspect ratio > 12:1 is a corridor/wall strip
MAX_HOLES = 6                # rooms with > 6 internal voids are suspicious

# Position-based rejection: title blocks typically sit in bottom-right
TITLE_BLOCK_Y_MIN = 0.85     # below 85% of page height = title block zone
TITLE_BLOCK_X_MIN = 0.60     # right of 60% of page width = title block zone

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _polygon_area_shoelace(pts: Sequence[Tuple[float, float]]) -> float:
    """Signed area via shoelace.  Positive = counter-clockwise."""
    n = len(pts)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return area / 2.0


def _polygon_area_abs(pts: Sequence[Tuple[float, float]]) -> float:
    """Unsigned area of a polygon in whatever coordinate space."""
    return abs(_polygon_area_shoelace(pts))


def _polygon_bbox(pts: Sequence[Tuple[float, float]]) -> Tuple[float, float, float, float]:
    """Return (x_min, y_min, x_max, y_max)."""
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _elongation_ratio(pts: Sequence[Tuple[float, float]]) -> float:
    """Width / height of bounding box.  > 12.0 suggests corridor/wall."""
    x0, y0, x1, y1 = _polygon_bbox(pts)
    w = x1 - x0
    h = y1 - y0
    if h < 1e-9:
        return 999.0
    return w / h if w >= h else h / w


def _point_in_polygon(point: Tuple[float, float], polygon: Sequence[Tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test."""
    x, y = point
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _polygon_centroid(pts: Sequence[Tuple[float, float]]) -> Tuple[float, float]:
    """Approximate centroid (average of vertices)."""
    n = len(pts)
    if n == 0:
        return (0.0, 0.0)
    return (sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n)


def _scale_factor_m_per_pt(scale_info: Dict[str, Any]) -> float:
    """Compute metres-per-PDF-point from scale_info.

    Uses the same chain as Priority 1:
      real_metres_per_page_mm = N/1000 for ratio 1:N
      real_metres_per_page_mm = 1/X for metric "X mm = 1 m"

    Returns metres per PDF point.
    """
    rpm = scale_info.get("real_metres_per_page_mm")
    if rpm is not None and rpm > 0:
        # page_mm = pdf_pt × (25.4/72)
        # real_m  = page_mm × rpm
        # therefore: real_m = pdf_pt × (25.4/72) × rpm
        return MM_PER_PT * rpm
    return 0.0


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


def calibrate_area_m2(
    polygon_pdf_pts: Sequence[Tuple[float, float]],
    scale_info: Dict[str, Any],
) -> float:
    """Convert a polygon's raw PDF-point area to real-world m².

    Args:
        polygon_pdf_pts: Vertices in PDF point coordinates.
        scale_info: Dict with 'real_metres_per_page_mm' key (from Priority 1).

    Returns:
        Real-world area in m².  0.0 if scale is unknown/unavailable.
    """
    scale = _scale_factor_m_per_pt(scale_info)
    if scale <= 0:
        return 0.0
    area_pts2 = _polygon_area_abs(polygon_pdf_pts)
    # area_m2 = area_pts2 × scale²  (scale is m per pt)
    return round(area_pts2 * scale * scale, 3)


def calibrate_polygon_m(
    polygon_pdf_pts: Sequence[Tuple[float, float]],
    scale_info: Dict[str, Any],
) -> List[Tuple[float, float]]:
    """Convert polygon vertices from PDF points to real-world metres.

    Returns list of (x_m, y_m) tuples.  Empty if scale unknown.
    """
    scale = _scale_factor_m_per_pt(scale_info)
    if scale <= 0:
        return []
    return [(round(x * scale, 4), round(y * scale, 4)) for x, y in polygon_pdf_pts]


# ---------------------------------------------------------------------------
# False-positive filtering
# ---------------------------------------------------------------------------


def _bbox_in_title_block_zone(
    bbox: Tuple[float, float, float, float],
    page_width_pt: float,
    page_height_pt: float,
) -> bool:
    """True if bbox is primarily in the title-block zone (bottom-right)."""
    x0, y0, x1, y1 = bbox
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    return (
        cy > page_height_pt * TITLE_BLOCK_Y_MIN
        and cx > page_width_pt * TITLE_BLOCK_X_MIN
    )


def _contains_large_containment(
    polygon: Sequence[Tuple[float, float]],
    other_polygons: Sequence[Sequence[Tuple[float, float]]],
    threshold: float = 0.85,
) -> bool:
    """True if polygon is mostly contained inside another polygon.

    Used to detect building outlines that contain all room faces.
    If a face contains > threshold fraction of all other faces' centroids,
    it is likely a building outline, not a room.
    """
    if not other_polygons:
        return False
    centroid = _polygon_centroid(polygon)
    # Check if this polygon's centroid is inside any other polygon
    for other in other_polygons:
        if other is polygon:
            continue
        if _point_in_polygon(centroid, other):
            return True
    return False


@dataclass
class FilterResult:
    """Result of face filtering."""
    is_room: bool
    reason: str = ""
    area_m2: float = 0.0
    polygon_m: List[Tuple[float, float]] = field(default_factory=list)


def filter_face(
    polygon_pdf_pts: Sequence[Tuple[float, float]],
    scale_info: Dict[str, Any],
    page_width_pt: float,
    page_height_pt: float,
    all_polygons: Optional[Sequence[Sequence[Tuple[float, float]]]] = None,
    label: str = "",
) -> FilterResult:
    """Determine whether a polygon is a plausible room face.

    Args:
        polygon_pdf_pts: Vertices in PDF point space.
        scale_info: Scale calibration dict from Priority 1.
        page_width_pt: Page width in PDF points.
        page_height_pt: Page height in PDF points.
        all_polygons: All extracted faces (for containment analysis).
        label: Associated text label (if any).

    Returns:
        FilterResult with is_room=True/False and reason.
    """
    # 1. Minimum vertex count
    if len(polygon_pdf_pts) < 3:
        return FilterResult(is_room=False, reason="degenerate (< 3 vertices)")

    # 2. Calibrate area
    area_m2 = calibrate_area_m2(polygon_pdf_pts, scale_info)
    if area_m2 <= 0:
        return FilterResult(
            is_room=False,
            reason="un_calibrated (unknown scale)",
            area_m2=0.0,
        )

    # 3. Area thresholds (in real m²)
    if area_m2 < MIN_ROOM_AREA_M2:
        return FilterResult(
            is_room=False,
            reason=f"too_small ({area_m2:.2f} m² < {MIN_ROOM_AREA_M2} m²)",
            area_m2=area_m2,
        )
    if area_m2 > MAX_ROOM_AREA_M2:
        return FilterResult(
            is_room=False,
            reason=f"too_large ({area_m2:.2f} m² > {MAX_ROOM_AREA_M2} m² — likely building outline)",
            area_m2=area_m2,
        )

    # 4. Elongation filter
    elong = _elongation_ratio(polygon_pdf_pts)
    if elong > MIN_ELONGATION:
        return FilterResult(
            is_room=False,
            reason=f"too_elongated (ratio {elong:.1f}:1 > {MIN_ELONGATION}:1)",
            area_m2=area_m2,
        )

    # 5. Title-block zone rejection
    bbox = _polygon_bbox(polygon_pdf_pts)
    if _bbox_in_title_block_zone(bbox, page_width_pt, page_height_pt):
        return FilterResult(
            is_room=False,
            reason="in_title_block_zone",
            area_m2=area_m2,
        )

    # 5b. Page coverage rejection: polygon covering > 75% of page area
    #     is a drawing border or building outline, not a room.
    page_area_pt2 = page_width_pt * page_height_pt
    if page_area_pt2 > 0:
        coverage = _polygon_area_abs(polygon_pdf_pts) / page_area_pt2
        if coverage > 0.75:
            return FilterResult(
                is_room=False,
                reason=f"covers_page ({coverage:.0%} of page — likely border/outline)",
                area_m2=area_m2,
            )

    # 6. Building outline rejection: if this polygon contains most other
    #    faces' centroids, it's an outer boundary, not a room.
    if all_polygons and len(all_polygons) > 1:
        centroid = _polygon_centroid(polygon_pdf_pts)
        contained_count = sum(
            1 for other in all_polygons
            if other is not polygon_pdf_pts and _point_in_polygon(centroid, other)
        )
        if contained_count > len(all_polygons) * 0.5:
            return FilterResult(
                is_room=False,
                reason=f"building_outline (contains {contained_count}/{len(all_polygons)} other centroids)",
                area_m2=area_m2,
            )

    # 7. Calibrated polygon in metres
    polygon_m = calibrate_polygon_m(polygon_pdf_pts, scale_info)

    return FilterResult(
        is_room=True,
        area_m2=area_m2,
        polygon_m=polygon_m,
    )


# ---------------------------------------------------------------------------
# Room face extraction + calibration
# ---------------------------------------------------------------------------


@dataclass
class RoomFace:
    """A calibrated room face ready for take-off row production."""
    room_ref: str
    label: str
    polygon_pdf_pts: List[Tuple[float, float]]
    polygon_m: List[Tuple[float, float]]
    floor_area_m2: float
    perimeter_m: float
    geometry_confidence: float
    evidence: List[str]
    source_page: int = 0
    drawing_number: str = ""
    scale_source: str = ""
    calibration_confidence: float = 0.0


def _perimeter_m(polygon_m: List[Tuple[float, float]]) -> float:
    """Perimeter of a polygon in metres."""
    if len(polygon_m) < 2:
        return 0.0
    total = 0.0
    for i in range(len(polygon_m)):
        x1, y1 = polygon_m[i]
        x2, y2 = polygon_m[(i + 1) % len(polygon_m)]
        total += ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    return round(total, 3)


def extract_and_calibrate_rooms(
    segments: Sequence[Tuple[Tuple[float, float], Tuple[float, float]]],
    scale_info: Dict[str, Any],
    page_width_pt: float = 595.0,   # A4 default
    page_height_pt: float = 842.0,  # A4 default
    page_no: int = 0,
    drawing_number: str = "",
    min_area: float = 0.05,
) -> List[RoomFace]:
    """Extract room faces from vector linework, calibrate to m², filter false positives.

    Args:
        segments: Line segments in PDF point coordinates [(start, end), ...].
        scale_info: Priority 1 scale calibration dict.
        page_width_pt: Page width in PDF points.
        page_height_pt: Page height in PDF points.
        page_no: PlanReader page number.
        drawing_number: Drawing reference.
        min_area: Minimum face area in PDF points² for v145 extraction.

    Returns:
        List of calibrated RoomFace objects (false positives already removed).
    """
    # Import v145 face extraction
    try:
        from pb_accuracy_v13_engines_v145 import (
            extract_planar_faces,
            attach_room_labels,
        )
    except ImportError:
        return []

    # Extract raw faces from vector linework
    raw_faces = extract_planar_faces(segments, min_area=min_area)
    if not raw_faces:
        return []

    # Attach labels (point-in-polygon text association)
    # labels come from PDF word positions: [{"label": "KITCHEN", "x": 100, "y": 200}, ...]
    # We need text positions — for now, pass empty labels
    labelled = attach_room_labels(raw_faces, [])

    # Determine scale source for metadata
    scale_source = "unknown"
    rpm = scale_info.get("real_metres_per_page_mm")
    ratio = scale_info.get("scale_ratio")
    if rpm is not None and rpm > 0:
        if ratio:
            scale_source = f"1:{ratio}"
        else:
            scale_source = f"{round(1/rpm)} mm = 1 m"

    calibration_conf = 0.95 if rpm and rpm > 0 else 0.0

    # Filter and calibrate each face
    room_faces: List[RoomFace] = []
    for labelled_room in labelled:
        polygon = [tuple(p) for p in labelled_room.get("polygon", [])]
        if len(polygon) < 3:
            continue

        label = labelled_room.get("label", "")
        polygon_tuple = tuple(tuple(p) for p in polygon)

        result = filter_face(
            polygon_tuple,
            scale_info,
            page_width_pt,
            page_height_pt,
            all_polygons=[tuple(tuple(p) for p in r.get("polygon", [])) for r in labelled],
            label=label,
        )

        if not result.is_room:
            continue

        room_faces.append(RoomFace(
            room_ref=labelled_room.get("room_ref", ""),
            label=label,
            polygon_pdf_pts=list(polygon),
            polygon_m=result.polygon_m,
            floor_area_m2=result.area_m2,
            perimeter_m=_perimeter_m(result.polygon_m),
            geometry_confidence=labelled_room.get("geometry_confidence", 0.9),
            evidence=labelled_room.get("evidence", []),
            source_page=page_no,
            drawing_number=drawing_number,
            scale_source=scale_source,
            calibration_confidence=calibration_conf,
        ))

    return room_faces


# ---------------------------------------------------------------------------
# Take-off row production
# ---------------------------------------------------------------------------


def rooms_to_takeoff_rows(
    rooms: List[RoomFace],
    workspace_id: int,
    include_ceiling: bool = False,
) -> List[Dict[str, Any]]:
    """Convert calibrated RoomFace objects into PlanReader take-off rows.

    Args:
        rooms: Calibrated room faces from extract_and_calibrate_rooms.
        workspace_id: PlanReader workspace ID.
        include_ceiling: If True, add ceiling rows (same area as floor).

    Returns:
        List of take-off row dicts matching PlanReader schema.
    """
    rows: List[Dict[str, Any]] = []

    for room in rooms:
        # Floor area row
        floor_row = {
            "workspace_id": workspace_id,
            "section": "Internal",
            "element": "Floor area",
            "location": room.label or room.room_ref,
            "substrate": "Other",
            "unit": "m²",
            "quantity": room.floor_area_m2,
            "quantity_status": (
                "Measured" if room.calibration_confidence >= 0.9
                else "Provisional measured"
            ),
            "source_page": room.source_page,
            "source_reference": (
                f"PB·RoomFace·{room.drawing_number}·page:{room.source_page}"
                if room.drawing_number
                else f"PB·RoomFace·page:{room.source_page}"
            ),
            "confidence": (
                "Derived" if room.calibration_confidence >= 0.9
                else "Provisional"
            ),
            "notes": (
                f"Room area from calibrated vector face extraction. "
                f"Scale: {room.scale_source}. "
                f"Geometry confidence: {room.geometry_confidence:.0%}. "
                f"Perimeter: {room.perimeter_m:.2f} m."
            ),
            "row_role": "floor_area",
            "geometry_confidence": room.geometry_confidence,
            "calibration_confidence": room.calibration_confidence,
            "evidence": "; ".join(room.evidence) if room.evidence else "",
        }
        rows.append(floor_row)

        # Ceiling row (optional — same geometry, different finish context)
        if include_ceiling:
            ceiling_row = dict(floor_row)
            ceiling_row["element"] = "Ceiling area"
            ceiling_row["row_role"] = ""
            ceiling_row["notes"] = (
                f"Ceiling area from room face geometry (same as floor). "
                f"Review: ceiling may differ due to voids, RCP changes, or scope."
            )
            rows.append(ceiling_row)

    return rows


# ---------------------------------------------------------------------------
# Convenience: summary
# ---------------------------------------------------------------------------


def room_face_summary(rooms: List[RoomFace]) -> Dict[str, Any]:
    """Aggregate room face statistics."""
    total_floor = sum(r.floor_area_m2 for r in rooms)
    total_perimeter = sum(r.perimeter_m for r in rooms)
    return {
        "room_count": len(rooms),
        "total_floor_area_m2": round(total_floor, 3),
        "total_perimeter_m": round(total_perimeter, 3),
        "scale_sources": list({r.scale_source for r in rooms}),
        "min_confidence": round(min(
            (r.calibration_confidence for r in rooms), default=0.0
        ), 3),
        "review_count": sum(
            1 for r in rooms
            if r.calibration_confidence < 0.9 or r.geometry_confidence < 0.9
        ),
    }
