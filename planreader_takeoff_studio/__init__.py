"""Screenshot-style elevation/render take-off studio for PB PlanReader."""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Iterable

import streamlit.components.v1 as components

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"

_takeoff_studio = components.declare_component(
    "planreader_takeoff_studio",
    path=str(FRONTEND_DIR),
)


def takeoff_studio_editor(
    image_bytes: bytes,
    *,
    areas: Iterable[dict[str, Any]] | None = None,
    substrates: Iterable[dict[str, Any]] | None = None,
    px_per_m: float = 0.0,
    page_type: str = "",
    view_label: str = "",
    revision: int = 0,
    key: str | None = None,
    height: int = 940,
):
    """Render the Takeoff Studio and return the edited area payload."""
    image_data_uri = "data:image/png;base64," + base64.b64encode(bytes(image_bytes)).decode("ascii")
    return _takeoff_studio(
        image=image_data_uri,
        areas=list(areas or []),
        substrates=list(substrates or []),
        px_per_m=float(px_per_m or 0.0),
        page_type=str(page_type or ""),
        view_label=str(view_label or ""),
        revision=int(revision or 0),
        default=None,
        key=key,
        height=int(height),
    )
