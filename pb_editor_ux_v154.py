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
    """Install the internal substrate vocabulary into loaded editor modules."""
    try:
        import pb_takeoff_studio_v1211 as studio
    except Exception:
        return

    studio.SUBSTRATE_PRESETS[:] = merge_substrate_presets(studio.SUBSTRATE_PRESETS)

    # The 3D surface editor resolves its presets dynamically from the Takeoff
    # Studio module, so extending the shared list also extends its selector.
    try:
        import pb_3d_surface_editor_v1212 as surface
    except Exception:
        return

    if getattr(surface, "_pb_internal_substrate_inference_v154", False):
        return

    base_infer = surface.infer_substrate

    def infer_with_internal(finish: Any) -> str:
        text = str(finish or "").lower()
        rules = [
            (("plasterboard", "gyprock", "drywall"), "PB"),
            (("fibre cement", "fiber cement", "fibrecement", "cement sheet"), "FC"),
            (("mdf", "skirting", "architrave", "timber trim"), "MDF"),
            (("plywood", "ply board", "ply sheet"), "PLY"),
            (("concrete", "internal masonry", "internal blockwork"), "CON"),
            (("metal", "steel", "metalwork"), "MET"),
        ]
        for needles, code in rules:
            if any(needle in text for needle in needles):
                return code
        return base_infer(finish)

    surface.infer_substrate = infer_with_internal
    surface._pb_internal_substrate_inference_v154 = True
