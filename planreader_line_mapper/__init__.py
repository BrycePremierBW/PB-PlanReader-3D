"""PlanReader measurement-line mapper custom Streamlit component.

A static HTML/JS component (no bundler) that renders a plan page and shows
auto-mapped take-off measurements as coloured, draggable measurement lines.

Line coordinates are percentages (0-100) of the image. The user can drag a
whole line to move it, drag either endpoint to change its length or angle, draw
new lines, and delete lines. Every edit is returned to Python as JSON so the
app can persist the corrected geometry and re-measure lineal-metre quantities
from the drawing scale.

``revision`` is bumped by Python whenever it wants the component to adopt a new
set of lines (e.g. after an auto-map) - the same adoption contract used by the
substrate box editor.
"""
import base64
from pathlib import Path

import streamlit.components.v1 as components

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"

_plan_line_mapper = components.declare_component(
    "planreader_line_mapper",
    path=str(FRONTEND_DIR),
)


def plan_line_editor(
    image_bytes,
    lines=None,
    px_per_m=0.0,
    revision=0,
    key=None,
    height=760,
):
    """Render a plan page with editable measurement lines.

    ``image_bytes`` is the PNG bytes of the rendered plan page. ``lines`` is the
    current list of line dicts (percent coordinates x1/y1/x2/y2, label, colour,
    unit, takeoff_row_id, moved). ``px_per_m`` is the page's saved drawing scale
    used to show real-world line lengths; pass 0 when no scale exists.

    Returns the current lines list (or None if the user has not edited).
    """
    image_data_uri = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
    return _plan_line_mapper(
        image=image_data_uri,
        lines=list(lines or []),
        px_per_m=float(px_per_m or 0.0),
        revision=int(revision or 0),
        default=None,
        key=key,
        height=height,
    )
