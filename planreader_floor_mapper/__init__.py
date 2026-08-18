"""Interactive floor-area mapper for PB PlanReader.

The component lets an estimator drag a calibration line over a known drawing
dimension and draw/resize rectangular internal floor-area zones. Geometry is
stored as percentages so it remains stable when the browser resizes.
"""
from __future__ import annotations

import base64
from pathlib import Path

import streamlit.components.v1 as components

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"

_floor_mapper = components.declare_component(
    "planreader_floor_mapper",
    path=str(FRONTEND_DIR),
)


def floor_mapper_editor(
    image_bytes,
    boxes=None,
    calibration=None,
    revision=0,
    key=None,
    height=860,
):
    """Render the floor mapper and return ``{boxes, calibration}`` when edited."""
    image_data_uri = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
    return _floor_mapper(
        image=image_data_uri,
        boxes=list(boxes or []),
        calibration=calibration or None,
        revision=int(revision or 0),
        default=None,
        key=key,
        height=height,
    )
