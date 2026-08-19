"""v1.2.21 compatibility gate for unit/partition floor plans.

The original v1.2.19 row builder only accepted page types containing the literal
word ``floor``.  This adapter feeds strongly identified unit/partition layouts
through that proven builder while preserving the v1.2.19 manual-precedence guard.
"""
from __future__ import annotations

from typing import Any, Dict, Sequence

import pb_auto_geometry_v1219 as auto
import pb_unit_floor_area_v1221 as unit


def normalise_unit_plan_pages(pages: Sequence[Dict[str, Any]]) -> list[Dict[str, Any]]:
    out: list[Dict[str, Any]] = []
    for raw in pages or []:
        page = dict(raw)
        page_type = str(page.get("page_type") or "")
        if "floor" not in page_type.lower() and unit.page_has_unit_plan_evidence(
            page_type, page.get("extracted_text"), page.get("page_label")
        ):
            # Only the classification presented to the row builder changes. The
            # real drawing-register page_type remains untouched in the database.
            page["page_type"] = f"Floor Plan / {page_type or 'Unit layout'}"
        out.append(page)
    return out


def apply(app: Any) -> None:
    if getattr(app, "_pb_unit_floor_area_gate_v1221_applied", False):
        return
    app._pb_unit_floor_area_gate_v1221_applied = True

    # At production apply order this is the v1.2.19 safety wrapper, not the raw
    # builder, so manual floor-area rows still suppress matching automatic rows.
    base_units = auto._build_unit_rows

    def _unit_plan_aware_rows(app_obj: Any, workspace_id: int, pages: Sequence[Dict[str, Any]]):
        return base_units(app_obj, int(workspace_id), normalise_unit_plan_pages(pages))

    auto._build_unit_rows = _unit_plan_aware_rows
    app.normalise_unit_plan_pages = normalise_unit_plan_pages
