"""PlanReader v1.7.5 production safety fence for legacy opening paths.

P5 B0-B6 is the authority for automatic opening deductions.  Older PlanReader
modules remain useful for the existing register/3D UI, but their historic
``deduct`` boolean is not sufficient evidence to reduce wall area.

This adapter is deliberately fail-closed:
- automatic records deduct only when they carry a completed, P5-approved
  rough-opening decision;
- explicit estimator manual entries may deduct only when they have real
  dimensions and an assigned wall;
- legacy review/unknown records never deduct merely because ``deduct=True``;
- v139/v145 independent net-area paths are re-gated through the same rule.
"""
from __future__ import annotations

import json
import math
from typing import Any, Dict, Iterable, List

VERSION = "1.7.5"
ELIGIBLE_STATUSES = {"auto_eligible", "derived_eligible"}
DEDUCTED_DECISIONS = {"deducted"}
ROUGH_OPENING = "rough_opening"
MIN_CONFIDENCE = 0.70

_P5_FIELDS = {
    "opening_instance_id",
    "opening_type",
    "type_mark",
    "level",
    "position_along_wall_m",
    "dimension_basis",
    "dimension_source",
    "geometry_confidence",
    "dimension_confidence",
    "association_confidence",
    "reconciliation_confidence",
    "reconciliation_complete",
    "deduction_status",
    "deduction_decision",
    "source_observations",
    "plan_geometry_signature",
    "notes",
    "manual_override_confirmed",
}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _valid_wall_ref(value: Any) -> bool:
    ref = str(value or "").strip()
    return bool(ref and ref.lower() not in {"unassigned wall", "unassigned", "unknown", "none"})


def _has_real_dimensions(raw: Dict[str, Any]) -> bool:
    return _num(raw.get("width_m")) > 0 and _num(raw.get("height_m")) > 0


def is_safe_deduction(raw: Dict[str, Any]) -> bool:
    """Return True only when a legacy-shaped opening is safe to subtract.

    P5 automatic path requires the same essential evidence contract as B5.
    Manual estimator entries are an explicit human override, but still require
    a real wall assignment and non-zero dimensions.
    """
    item = dict(raw or {})
    if not bool(item.get("deduct", False)):
        return False
    if not _valid_wall_ref(item.get("wall_ref") or item.get("resolved_wall_ref")):
        return False
    if not _has_real_dimensions(item):
        return False

    confidence_text = str(item.get("confidence") or "").strip().lower()
    manual = bool(item.get("manual_override_confirmed")) or confidence_text == "manual estimator entry"
    if manual:
        return True

    if not bool(item.get("reconciliation_complete", False)):
        return False
    if str(item.get("deduction_status") or "") not in ELIGIBLE_STATUSES:
        return False
    if str(item.get("deduction_decision") or "") not in DEDUCTED_DECISIONS:
        return False
    if str(item.get("dimension_basis") or "") != ROUGH_OPENING:
        return False

    confidences = [
        _num(item.get("geometry_confidence"), 0.0),
        _num(item.get("dimension_confidence"), 0.0),
        _num(item.get("association_confidence"), 0.0),
    ]
    return min(confidences) >= MIN_CONFIDENCE


def opening_area_m2(raw: Dict[str, Any]) -> float:
    item = dict(raw or {})
    qty = max(1, int(_num(item.get("quantity"), 1)))
    return round(max(0.0, _num(item.get("width_m"))) * max(0.0, _num(item.get("height_m"))) * qty, 4)


def deducted_area_m2(openings: Iterable[Dict[str, Any]]) -> float:
    return round(sum(opening_area_m2(item) for item in openings or [] if is_safe_deduction(item)), 4)


def net_wall_area_m2(gross_wall_m2: float, openings: Iterable[Dict[str, Any]]) -> float:
    return round(max(0.0, _num(gross_wall_m2) - deducted_area_m2(openings)), 4)


def _preserving_normaliser(legacy_normalise):
    def normalise(raw: Dict[str, Any]) -> Dict[str, Any]:
        incoming = dict(raw or {})
        # Old v134 defaulted a missing deduct flag to True.  Automatic safety
        # must be opt-in, so missing flags now fail closed.
        if "deduct" not in incoming:
            incoming["deduct"] = False
        base = legacy_normalise(incoming)
        for key in _P5_FIELDS:
            if key in incoming:
                base[key] = incoming[key]
        return base
    return normalise


def _preserving_save(app: Any, normalise):
    def save(_app: Any, workspace_id: int, openings: Iterable[Dict[str, Any]]) -> None:
        existing_by_id: Dict[str, Dict[str, Any]] = {}
        try:
            raw = app.workspace_setting(int(workspace_id), "opening_register_v134", "[]")
            parsed = json.loads(str(raw or "[]"))
            if isinstance(parsed, list):
                existing_by_id = {
                    str(item.get("id")): dict(item)
                    for item in parsed
                    if isinstance(item, dict) and item.get("id")
                }
        except Exception:
            existing_by_id = {}

        payload: List[Dict[str, Any]] = []
        for incoming in openings or []:
            row = dict(incoming or {})
            previous = existing_by_id.get(str(row.get("id") or ""), {})
            merged = {**previous, **row}
            payload.append(normalise(merged))
        app.set_workspace_setting(int(workspace_id), "opening_register_v134", json.dumps(payload, separators=(",", ":")))
    return save


def _patch_unified_building(app: Any) -> None:
    try:
        import pb_unified_building_v139 as unified
    except Exception:
        return
    if getattr(unified, "_pb_p5_safety_fenced", False):
        return
    original_build = unified.build_registered_walls

    def safe_build_registered_walls(app_obj: Any, workspace_id: int):
        walls = original_build(app_obj, workspace_id)
        for wall in walls or []:
            attached = list(wall.get("openings") or [])
            deducted = 0.0
            for opening in attached:
                applied = is_safe_deduction(opening)
                opening["applied_deduction"] = applied
                if applied:
                    deducted += opening_area_m2(opening)
            gross = _num(wall.get("gross_m2"), _num(wall.get("length_m")) * _num(wall.get("height_m")))
            wall["opening_deduction_m2"] = round(deducted, 3)
            wall["net_m2"] = round(max(0.0, gross - deducted), 3)
            wall["openings"] = attached
        return walls

    unified.build_registered_walls = safe_build_registered_walls
    unified._pb_p5_safety_fenced = True
    app.build_registered_walls_v139 = lambda wid: safe_build_registered_walls(app, int(wid))


def _patch_accuracy_engine() -> None:
    try:
        import pb_accuracy_v13_engines_v145 as accuracy
    except Exception:
        return
    if getattr(accuracy, "_pb_p5_safety_fenced", False):
        return

    original_detect = accuracy.detect_openings
    original_room_summary = accuracy.room_quantity_summary
    original_facade_net = accuracy.facade_net_area

    def safe_detect_openings(candidates):
        rows = original_detect(candidates)
        for row in rows:
            row["deduct"] = is_safe_deduction(row)
            if not row["deduct"]:
                row.setdefault("deduction_decision", "not_deducted")
        return rows

    def safe_room_quantity_summary(rooms, openings):
        safe_rows = [{**dict(o), "deduct": is_safe_deduction(o)} for o in openings or []]
        return original_room_summary(rooms, safe_rows)

    def safe_facade_net_area(regions, openings):
        safe_rows = [{**dict(o), "deduct": is_safe_deduction(o)} for o in openings or []]
        return original_facade_net(regions, safe_rows)

    accuracy.detect_openings = safe_detect_openings
    accuracy.room_quantity_summary = safe_room_quantity_summary
    accuracy.facade_net_area = safe_facade_net_area
    accuracy._pb_p5_safety_fenced = True


def apply(app: Any) -> None:
    """Install the P5 fail-closed policy after the legacy reconstruction stack."""
    if getattr(app, "_pb_opening_safety_fence_v175_applied", False):
        return
    app._pb_opening_safety_fence_v175_applied = True

    try:
        import pb_opening_deductions_v134 as legacy
    except Exception:
        legacy = None

    if legacy is not None:
        normalise = _preserving_normaliser(legacy.normalise_opening)
        save = _preserving_save(app, normalise)
        legacy.normalise_opening = normalise
        legacy.opening_area_m2 = opening_area_m2
        legacy.deducted_area_m2 = deducted_area_m2
        legacy.net_wall_area_m2 = net_wall_area_m2
        legacy._save = save
        app.normalise_opening = normalise
        app.opening_area_m2 = opening_area_m2
        app.deducted_opening_area_m2 = deducted_area_m2
        app.net_wall_area_m2 = net_wall_area_m2

    app.opening_safe_to_deduct = is_safe_deduction
    app.p5_opening_pipeline_version = VERSION

    _patch_unified_building(app)
    _patch_accuracy_engine()
