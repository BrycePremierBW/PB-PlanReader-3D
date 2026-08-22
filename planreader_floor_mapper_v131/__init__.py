"""Zoomable editable polygon floor mapper for PlanReader v1.3.1."""
from __future__ import annotations

import base64
from pathlib import Path

import streamlit.components.v1 as components

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"

_floor_mapper = components.declare_component(
    "planreader_floor_mapper_v131",
    path=str(FRONTEND_DIR),
)


def floor_mapper_editor(
    image_bytes,
    boxes=None,
    calibration=None,
    revision=0,
    key=None,
    height=900,
):
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
