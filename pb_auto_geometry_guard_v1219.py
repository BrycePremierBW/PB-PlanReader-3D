"""Safety guards for PlanReader v1.2.19 automatic geometry.

The automatic pipeline must accelerate estimating without ever reclaiming control
from an estimator. This patch makes two precedence rules explicit:

1. if a page's current px/m differs from PlanReader's last automatic px/m, that
   change is treated as a manual calibration and will not be overwritten;
2. existing non-auto floor-area or detailed external-wall rows suppress matching
   automatic rows rather than being double-counted.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence, Tuple

import pb_auto_geometry_v1219 as auto


def _normalise(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    return re.sub(r"\b(?:floor area|internal area|area)\b", " ", text).strip()


def _last_auto_px_per_m(app: Any, page: Dict[str, Any]) -> float:
    workspace_id = int(page.get("workspace_id") or 0)
    page_id = int(page.get("id") or 0)
    if workspace_id <= 0 or page_id <= 0:
        return 0.0
    report = auto._setting_get(app, workspace_id)
    for item in reversed(list(report.get("calibrations") or [])):
        if int(item.get("page_id") or 0) != page_id:
            continue
        if str(item.get("method") or "") == "Manual/existing":
            continue
        value = auto._num(item.get("px_per_m"))
        if value > 0:
            return value
    return 0.0


def is_manual_calibration_override(app: Any, page: Dict[str, Any]) -> bool:
    current = auto._num(page.get("px_per_m"))
    if current <= 0:
        return False
    scale_text = str(page.get("scale_text") or "")
    if not scale_text.startswith("Auto "):
        return True
    prior_auto = _last_auto_px_per_m(app, page)
    if prior_auto <= 0:
        # During the first automatic run there is no prior report yet. An Auto
        # label therefore remains auto-owned until the report is saved.
        return False
    return abs(current - prior_auto) / max(prior_auto, 1e-9) > 0.003


def _manual_floor_keys(app: Any, workspace_id: int) -> set[str]:
    rows = app.lquery(
        """SELECT location,source_reference FROM takeoff_rows
           WHERE workspace_id=? AND row_role='floor_area'
             AND COALESCE(source_reference,'') NOT LIKE ?""",
        (workspace_id, auto.SOURCE_PREFIX + "%"),
    )
    return {_normalise(row.get("location")) for row in rows if _normalise(row.get("location"))}


def _manual_external_pages(app: Any, workspace_id: int) -> set[str]:
    rows = app.lquery(
        """SELECT section,element,source_page,source_reference,unit FROM takeoff_rows
           WHERE workspace_id=? AND COALESCE(source_reference,'') NOT LIKE ?""",
        (workspace_id, auto.SOURCE_PREFIX + "%"),
    )
    pages: set[str] = set()
    for row in rows:
        if str(row.get("unit") or "") != "m²":
            continue
        text = f"{row.get('section') or ''} {row.get('element') or ''} {row.get('source_reference') or ''}".lower()
        if not any(token in text for token in ("external", "facade", "façade", "cladding", "wall", "takeoff studio")):
            continue
        source_page = str(row.get("source_page") or "").strip()
        if source_page:
            pages.add(source_page)
    return pages


def apply(app: Any) -> None:
    if getattr(app, "_pb_auto_geometry_guard_v1219_applied", False):
        return
    app._pb_auto_geometry_guard_v1219_applied = True

    base_calibrate = auto._auto_calibrate_page
    base_units = auto._build_unit_rows
    base_facades = auto._build_facade_rows

    def _safe_calibrate(app_obj: Any, page: Dict[str, Any]):
        if is_manual_calibration_override(app_obj, page):
            return {
                "page_id": int(page["id"]),
                "method": "Manual/existing",
                "px_per_m": auto._num(page.get("px_per_m")),
                "confidence": "Manual",
            }
        return base_calibrate(app_obj, page)

    def _safe_units(app_obj: Any, workspace_id: int, pages: Sequence[Dict[str, Any]]):
        rows, summary = base_units(app_obj, workspace_id, pages)
        manual = _manual_floor_keys(app_obj, workspace_id)
        if not manual:
            return rows, summary
        keep_rows: List[Tuple[Any, ...]] = []
        keep_summary = []
        blocked: set[str] = set()
        for row in rows:
            key = _normalise(row[3])
            if key and any(key == item or key in item or item in key for item in manual):
                blocked.add(key)
                continue
            keep_rows.append(row)
        for item in summary:
            key = _normalise(item.get("label"))
            if key not in blocked:
                keep_summary.append(item)
        return keep_rows, keep_summary

    def _safe_facades(app_obj: Any, workspace_id: int, pages: Sequence[Dict[str, Any]]):
        rows, facades = base_facades(app_obj, workspace_id, pages)
        manual_pages = _manual_external_pages(app_obj, workspace_id)
        if not manual_pages:
            return rows, facades
        filtered = [row for row in rows if str(row[9] or "").strip() not in manual_pages]
        for facade in facades:
            if str(facade.get("page_label") or "").strip() in manual_pages:
                facade["superseded_by_manual"] = True
        return filtered, facades

    auto._auto_calibrate_page = _safe_calibrate
    auto._build_unit_rows = _safe_units
    auto._build_facade_rows = _safe_facades
    app.is_manual_auto_geometry_calibration = lambda page: is_manual_calibration_override(app, dict(page))
