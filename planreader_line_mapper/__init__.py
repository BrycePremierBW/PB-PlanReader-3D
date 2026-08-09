"""PlanReader draw-first measurement mapper custom Streamlit component.

A static HTML/JS component (no bundler) that renders a plan page and lets the
user draw real measurement shapes for take-off rows directly on the plan.

Two tools are provided:

- **Line** – click two points for a lineal length (walls, doors, frames,
  skirting). Length is measured in metres from the saved drawing scale.
- **Outline** – click each corner then double-click / press **Close outline**
  to finish a closed shape for an area (building footprint, ceilings). Area is
  measured in square metres.

Every completed shape is bound to the take-off row selected in the row picker,
uses that row's colour, and is returned to Python as JSON so the app can
persist the geometry and sync take-off quantities.

Shape coordinates are percentages (0-100) of the image. ``revision`` is bumped
by Python whenever it wants the component to adopt a new set of shapes - the
same adoption contract used elsewhere in the app.
"""
import base64
import json
import math
from pathlib import Path

import streamlit.components.v1 as components

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"

_plan_line_mapper = components.declare_component(
    "planreader_line_mapper",
    path=str(FRONTEND_DIR),
)


def _finite(v, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default
    return f


def plan_line_editor(
    image_bytes,
    lines=None,
    rows=None,
    px_per_m=0.0,
    revision=0,
    key=None,
    height=760,
    active_row_id=None,
):
    """Render a plan page for drawing take-off measurements.

    ``image_bytes`` is the PNG bytes of the rendered plan page. ``lines`` is the
    current list of drawn shape dicts (kind 'line' or 'polygon', percent
    coordinates x1/y1/x2/y2 or points[], label, colour, unit, takeoff_row_id,
    length_m/area_m2/perimeter_m). ``rows`` is the list of draw targets from
    :func:`takeoff_rows_for_mapper` (id, label, unit, colour, quantity).
    ``px_per_m`` is the page's saved drawing scale used to show real lengths
    and areas; pass 0 when no scale exists. ``active_row_id`` optionally names
    the row to select when the component first adopts the current data.

    Returns the current shapes list (or None if the user has not edited).
    """
    image_data_uri = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
    cleaned_lines = []
    for ln in lines or []:
        if not isinstance(ln, dict):
            continue
        points = ln.get("points") or []
        if isinstance(points, str):
            try:
                points = json.loads(points)
            except Exception:
                points = []
        cleaned_lines.append({
            "id": str(ln.get("id") or ""),
            "takeoff_row_id": ln.get("takeoff_row_id"),
            "label": str(ln.get("label") or ""),
            "unit": str(ln.get("unit") or ""),
            "colour": str(ln.get("colour") or "#1f6fb2"),
            "kind": "polygon" if str(ln.get("kind") or "line") == "polygon" else "line",
            "x1": _finite(ln.get("x1"), 0.0),
            "y1": _finite(ln.get("y1"), 0.0),
            "x2": _finite(ln.get("x2"), 0.0),
            "y2": _finite(ln.get("y2"), 0.0),
            "points": [list(p) for p in (ln.get("points") or [])],
            "length_m": _finite(ln.get("length_m"), 0.0),
            "area_m2": _finite(ln.get("area_m2"), 0.0),
            "perimeter_m": _finite(ln.get("perimeter_m"), 0.0),
            "quantity_status": str(ln.get("quantity_status") or "Drawn"),
            "moved": 1 if ln.get("moved") else 0,
            "notes": str(ln.get("notes") or ""),
        })
    cleaned_rows = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        cleaned_rows.append({
            "id": int(r.get("id") or 0),
            "label": str(r.get("label") or ""),
            "unit": str(r.get("unit") or ""),
            "colour": str(r.get("colour") or "#1f6fb2"),
            "quantity": _finite(r.get("quantity"), 0.0),
        })
    return _plan_line_mapper(
        image=image_data_uri,
        lines=cleaned_lines,
        rows=cleaned_rows,
        px_per_m=_finite(px_per_m, 0.0),
        revision=int(revision or 0),
        default=None,
        key=key,
        height=height,
        active_row_id=_active_row_id(active_row_id, cleaned_rows),
    )


def _active_row_id(active_row_id, rows) -> int:
    try:
        want = int(active_row_id)
    except (TypeError, ValueError):
        want = 0
    ids = [int(r.get("id") or 0) for r in rows]
    if want in ids:
        return want
    return ids[0] if ids else 0
