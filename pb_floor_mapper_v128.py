"""PB PlanReader v1.2.8 editable polygon floor-area support.

Extends the v1.2.7 floor mapper so floor areas can be irregular polygons rather
than rectangles. Legacy rectangle data remains supported and is converted to a
four-point polygon by the frontend when edited.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

import pb_floor_mapper_v127 as base


def _num(value: Any, default: float = 0.0) -> float:
    return base._num(value, default)


def _points_from_shape(shape: Dict[str, Any]) -> List[Dict[str, float]]:
    points = shape.get("points")
    if isinstance(points, list) and len(points) >= 3:
        cleaned: List[Dict[str, float]] = []
        for p in points:
            if isinstance(p, dict):
                cleaned.append({"x": _num(p.get("x")), "y": _num(p.get("y"))})
            elif isinstance(p, (list, tuple)) and len(p) >= 2:
                cleaned.append({"x": _num(p[0]), "y": _num(p[1])})
        if len(cleaned) >= 3:
            return cleaned

    # Backwards compatibility for v1.2.7 rectangle boxes.
    x = _num(shape.get("x"))
    y = _num(shape.get("y"))
    w = _num(shape.get("w"))
    h = _num(shape.get("h"))
    if w > 0 and h > 0:
        return [
            {"x": x, "y": y},
            {"x": x + w, "y": y},
            {"x": x + w, "y": y + h},
            {"x": x, "y": y + h},
        ]
    return []


def measured_floor_area_m2(
    shape: Dict[str, Any], width_px: float, height_px: float, px_per_m: float
) -> float:
    """Measure a floor polygon using the shoelace formula.

    Polygon points are stored as percentages of the rendered page so the shape
    remains aligned through responsive resizing. A manual override still wins.
    """
    manual = _num(shape.get("manual_m2"))
    if manual > 0:
        return round(manual, 2)
    if px_per_m <= 0 or width_px <= 0 or height_px <= 0:
        return 0.0
    points = _points_from_shape(shape)
    if len(points) < 3:
        return 0.0

    pixel_points = [
        (_num(p.get("x")) / 100.0 * width_px, _num(p.get("y")) / 100.0 * height_px)
        for p in points
    ]
    twice_area = 0.0
    for idx, (x1, y1) in enumerate(pixel_points):
        x2, y2 = pixel_points[(idx + 1) % len(pixel_points)]
        twice_area += x1 * y2 - x2 * y1
    area_px2 = abs(twice_area) / 2.0
    return round(area_px2 / (px_per_m * px_per_m), 2)


def build_floor_area_rows(
    boxes: Iterable[Dict[str, Any]],
    width_px: float,
    height_px: float,
    px_per_m: float,
    page_label: str,
    page_id: int,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for index, box in enumerate(boxes or [], start=1):
        area = measured_floor_area_m2(box, width_px, height_px, px_per_m)
        label = str(box.get("label") or "").strip() or f"{page_label or 'Floor plan'} · area {index}"
        box_id = str(box.get("id") or f"area-{index}")
        rows.append(
            {
                "section": "Internal",
                "element": "Floor area",
                "location": label,
                "substrate": "",
                "finish_system": "",
                "quantity": area,
                "unit": "m²",
                "quantity_status": "Measured" if area > 0 else "To measure",
                "source_page": page_label,
                "source_reference": f"{base.SOURCE_PREFIX} · page:{int(page_id)} · box:{box_id}",
                "inclusion_status": "INCLUSION",
                "coats": 0,
                "coverage_m2_per_litre": 0,
                "productivity_m2_per_hour": 0,
                "rate_per_unit": 0,
                "confidence": "Measured" if area > 0 and px_per_m > 0 else ("Derived" if area > 0 else "To review"),
                "notes": "Internal floor-area pricing basis only; editable polygon, not a painted surface quantity.",
                "row_role": "floor_area",
            }
        )
    return rows


def apply(app: Any) -> None:
    """Install polygon measurement support, then the existing v1.2.7 UI patch."""
    if getattr(app, "_pb_floor_mapper_v128_applied", False):
        return
    app._pb_floor_mapper_v128_applied = True

    # The v1.2.7 panel resolves these globals at call time, so replacing them
    # upgrades its calculation path without duplicating the rest of the UI.
    base.measured_box_area_m2 = measured_floor_area_m2
    base.build_floor_area_rows = build_floor_area_rows
    base.apply(app)
    app.measured_box_area_m2 = measured_floor_area_m2
    app.measured_floor_area_m2 = measured_floor_area_m2
    app.build_floor_area_rows = build_floor_area_rows
