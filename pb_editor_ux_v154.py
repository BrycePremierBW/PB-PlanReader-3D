"""PlanReader v1.5.4 editor UX additions.

Extends the existing Takeoff Studio/3D surface-editor substrate vocabulary with
common internal painting substrates. This module changes display/selection
metadata only; it does not alter geometry, calibration, quantities or deductions.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

INTERNAL_SUBSTRATES: List[Dict[str, str]] = [
    {"code": "PB", "name": "Plasterboard / Gyprock", "color": "#D8D1C4"},
    {"code": "FC", "name": "Fibre Cement Sheet", "color": "#C3CBD0"},
    {"code": "MDF", "name": "MDF / Timber Trim", "color": "#B88F6A"},
    {"code": "PLY", "name": "Plywood", "color": "#C7A67A"},
    {"code": "CON", "name": "Concrete / Masonry", "color": "#A9ADB2"},
    {"code": "MET", "name": "Metal / Steel", "color": "#8FA0AF"},
]


def merge_substrate_presets(existing: Iterable[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Append missing internal presets without changing existing preset definitions."""
    merged = [dict(item) for item in (existing or [])]
    seen = {str(item.get("code") or "").strip().upper() for item in merged}
    for item in INTERNAL_SUBSTRATES:
        if item["code"].upper() not in seen:
            merged.append(dict(item))
            seen.add(item["code"].upper())
    return merged


def install() -> None:
    """Install the internal substrate vocabulary into the shared editor presets."""
    try:
        import pb_takeoff_studio_v1211 as studio
    except Exception:
        return

    studio.SUBSTRATE_PRESETS[:] = merge_substrate_presets(studio.SUBSTRATE_PRESETS)
    # pb_3d_surface_editor_v1212 reads this shared list dynamically via
    # substrate_presets(), so the same options appear there without eagerly
    # importing the 3D editor during app startup.
