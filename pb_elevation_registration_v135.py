"""PlanReader v1.3.5 floor-plan <-> elevation registration engine.

This module makes the calibrated floor footprint the horizontal source of truth
and registers elevation drawings back to the corresponding building sides.  It
is deliberately conservative: unresolved orientation, height or scale evidence
remains reviewable instead of being invented.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Iterable, List, Sequence, Tuple

VERSION = "1.3.5"
SETTING_KEY = "elevation_registration_v135"
CARDINALS = ("North", "East", "South", "West")
_DIM_RE = re.compile(r"(?<![:\d])(\d{3,5}(?:\.\d+)?)\s*(mm|m)?(?!\s*[:\d])", re.I)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def _length(a: Sequence[float], b: Sequence[float]) -> float:
    return math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))


def _signed_area(points: Sequence[Tuple[float, float]]) -> float:
    pts = list(points)
    return 0.5 * sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))


def orientation_from_text(*values: Any) -> str:
    text = " ".join(str(v or "") for v in values).lower()
    patterns = {
        "North": (r"\bnorth\s+elevation\b", r"\belevation\s*[-:]?\s*north\b", r"\bnorth\s+facade\b", r"\bnorth\s+fa[cç]ade\b"),
        "East": (r"\beast\s+elevation\b", r"\belevation\s*[-:]?\s*east\b", r"\beast\s+facade\b", r"\beast\s+fa[cç]ade\b"),
        "South": (r"\bsouth\s+elevation\b", r"\belevation\s*[-:]?\s*south\b", r"\bsouth\s+facade\b", r"\bsouth\s+fa[cç]ade\b"),
        "West": (r"\bwest\s+elevation\b", r"\belevation\s*[-:]?\s*west\b", r"\bwest\s+facade\b", r"\bwest\s+fa[cç]ade\b"),
    }
    for side, tests in patterns.items():
        if any(re.search(test, text) for test in tests):
            return side
    return ""


def dimension_candidates_m(text: Any) -> List[float]:
    values: List[float] = []
    for match in _DIM_RE.finditer(str(text or "")):
        value = _num(match.group(1))
        unit = str(match.group(2) or "").lower()
        if unit == "mm" or (not unit and value >= 100):
            value /= 1000.0
        elif unit != "m":
            continue
        if 0.25 <= value <= 150.0:
            values.append(round(value, 4))
    return sorted(set(values))


def classify_outward_side(a: Tuple[float, float], b: Tuple[float, float], polygon_signed_area: float) -> str:
    dx, dy = b[0] - a[0], b[1] - a[1]
    if abs(dx) + abs(dy) <= 1e-9:
        return ""
    # For CCW polygons, the exterior is to the right of an edge. For CW it is
    # to the left.  Y increases north in the precision-plan world.
    if polygon_signed_area >= 0:
        nx, ny = dy, -dx
    else:
        nx, ny = -dy, dx
    if abs(nx) >= abs(ny):
        return "East" if nx > 0 else "West"
    return "North" if ny > 0 else "South"


def footprint_facades(prisms: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {
        side: {"side": side, "segments": [], "edge_length_m": 0.0, "projected_width_m": 0.0, "levels": set()}
        for side in CARDINALS
    }
    all_points: List[Tuple[float, float]] = []
    for prism in prisms or []:
        points = [(float(x), float(y)) for x, y in (prism.get("points") or [])]
        if len(points) < 3:
            continue
        all_points.extend(points)
        signed = _signed_area(points)
        for idx, (a, b) in enumerate(zip(points, points[1:] + points[:1]), start=1):
            side = classify_outward_side(a, b, signed)
            if not side:
                continue
            length = _length(a, b)
            if length <= 0.02:
                continue
            wall_ref = f"{side[0]}{len(result[side]['segments']) + 1:02d}"
            result[side]["segments"].append({
                "wall_ref": wall_ref,
                "side": side,
                "a": [round(a[0], 4), round(a[1], 4)],
                "b": [round(b[0], 4), round(b[1], 4)],
                "length_m": round(length, 4),
                "level_name": str(prism.get("level_name") or "Ground / unregistered"),
                "level_index": int(_num(prism.get("level_index"), 0)),
                "source_polygon": str(prism.get("id") or ""),
                "confidence": str(prism.get("confidence") or "Measured plan geometry"),
            })
            result[side]["edge_length_m"] += length
            result[side]["levels"].add(str(prism.get("level_name") or "Ground / unregistered"))
    if all_points:
        xs = [p[0] for p in all_points]
        ys = [p[1] for p in all_points]
        width_x = max(xs) - min(xs)
        width_y = max(ys) - min(ys)
        result["North"]["projected_width_m"] = width_x
        result["South"]["projected_width_m"] = width_x
        result["East"]["projected_width_m"] = width_y
        result["West"]["projected_width_m"] = width_y
    for side in CARDINALS:
        result[side]["edge_length_m"] = round(result[side]["edge_length_m"], 4)
        result[side]["projected_width_m"] = round(result[side]["projected_width_m"], 4)
        result[side]["levels"] = sorted(result[side]["levels"])
    return result


def best_dimension_match(plan_width_m: float, candidates: Sequence[float]) -> Dict[str, Any]:
    plan_width_m = max(0.0, _num(plan_width_m))
    valid = [float(v) for v in candidates if 0.25 <= _num(v) <= 150.0]
    if plan_width_m <= 0 or not valid:
        return {"dimension_m": None, "difference_m": None, "difference_pct": None, "confidence": 0}
    best = min(valid, key=lambda v: abs(v - plan_width_m))
    diff = abs(best - plan_width_m)
    pct = diff / plan_width_m * 100.0
    confidence = max(0, min(100, int(round(100.0 - pct * 8.0))))
    return {
        "dimension_m": round(best, 4), "difference_m": round(diff, 4),
        "difference_pct": round(pct, 3), "confidence": confidence,
    }


def _elevation_pages(app: Any, workspace_id: int) -> List[Dict[str, Any]]:
    rows = app.lquery(
        "SELECT id,page_no,page_label,page_type,extracted_text,px_per_m,width_px,height_px,image_path "
        "FROM pages WHERE workspace_id=? ORDER BY page_no,id",
        (int(workspace_id),),
    )
    out = []
    for row in rows:
        text = " ".join([str(row.get("page_label") or ""), str(row.get("page_type") or ""), str(row.get("extracted_text") or "")])
        if "elevation" not in text.lower() and "facade" not in text.lower() and "façade" not in text.lower():
            continue
        item = dict(row)
        item["orientation"] = orientation_from_text(row.get("page_label"), row.get("page_type"), row.get("extracted_text"))
        item["dimension_candidates_m"] = dimension_candidates_m(row.get("extracted_text"))
        out.append(item)
    return out


def register_elevations(app: Any, workspace_id: int) -> Dict[str, Any]:
    prisms = app.build_precision_prisms(int(workspace_id)) if hasattr(app, "build_precision_prisms") else []
    facades = footprint_facades(prisms)
    pages = _elevation_pages(app, workspace_id)
    registrations: List[Dict[str, Any]] = []
    used_sides = set()
    for page in pages:
        explicit = str(page.get("orientation") or "")
        candidates = list(page.get("dimension_candidates_m") or [])
        options = [explicit] if explicit in CARDINALS else list(CARDINALS)
        scored = []
        for side in options:
            plan_width = _num(facades[side].get("projected_width_m"))
            match = best_dimension_match(plan_width, candidates)
            # Explicit cardinal naming is strong evidence. When unnamed, width
            # agreement helps choose among the four sides but never marks it verified.
            score = match["confidence"] + (80 if explicit == side else 0) - (8 if side in used_sides and not explicit else 0)
            scored.append((score, side, match))
        scored.sort(key=lambda item: item[0], reverse=True)
        _, side, match = scored[0] if scored else (0, "", {})
        if side:
            used_sides.add(side)
        width_pct = match.get("difference_pct")
        if explicit and width_pct is not None and width_pct <= 2.0:
            status = "Verified cross-view"
            confidence = "Verified"
        elif explicit:
            status = "Orientation identified; width needs review"
            confidence = "High" if width_pct is None or width_pct <= 5.0 else "Review"
        elif width_pct is not None and width_pct <= 2.0:
            status = "Probable width match; orientation needs review"
            confidence = "Review"
        else:
            status = "Needs estimator registration"
            confidence = "Review"
        registrations.append({
            "page_id": int(page["id"]),
            "page_no": int(_num(page.get("page_no"), 0)),
            "page_label": str(page.get("page_label") or f"Page {page.get('page_no') or ''}"),
            "orientation": side,
            "orientation_explicit": bool(explicit),
            "plan_width_m": facades.get(side, {}).get("projected_width_m") if side else None,
            "elevation_dimension_m": match.get("dimension_m"),
            "difference_pct": match.get("difference_pct"),
            "scale_px_per_m": round(_num(page.get("px_per_m")), 4),
            "status": status,
            "confidence": confidence,
            "source": "Cardinal elevation title + plan footprint" if explicit else "Plan/elevation dimension comparison",
        })
    payload = {
        "version": VERSION,
        "facades": facades,
        "elevations": registrations,
        "unregistered_sides": [side for side in CARDINALS if side not in {r.get("orientation") for r in registrations}],
    }
    app.set_workspace_setting(int(workspace_id), SETTING_KEY, json.dumps(payload, separators=(",", ":")))
    return payload


def wall_records(app: Any, workspace_id: int, registration: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    reg = registration or register_elevations(app, workspace_id)
    openings = []
    try:
        raw = app.workspace_setting(int(workspace_id), "opening_register_v134", "[]")
        parsed = json.loads(str(raw or "[]"))
        openings = parsed if isinstance(parsed, list) else []
    except Exception:
        openings = []
    by_wall = app.openings_by_wall(openings) if hasattr(app, "openings_by_wall") else {}
    default_height = max(0.5, _num(app.workspace_setting(int(workspace_id), "default_wall_height_m", 2.7), 2.7))
    rows: List[Dict[str, Any]] = []
    for side in CARDINALS:
        for segment in reg.get("facades", {}).get(side, {}).get("segments", []):
            gross = _num(segment.get("length_m")) * default_height
            attached = by_wall.get(str(segment.get("wall_ref")), []) + by_wall.get(side, [])
            deducted = app.deducted_opening_area_m2(attached) if hasattr(app, "deducted_opening_area_m2") else 0.0
            rows.append({
                "wall_ref": segment.get("wall_ref"), "side": side,
                "level": segment.get("level_name"), "length_m": segment.get("length_m"),
                "height_m": round(default_height, 3),
                "height_status": "Provisional until elevation/section height is registered",
                "gross_m2": round(gross, 3), "opening_deduction_m2": round(deducted, 3),
                "net_m2": round(max(0.0, gross - deducted), 3),
                "source_polygon": segment.get("source_polygon"),
                "geometry_confidence": segment.get("confidence"),
            })
    return rows


def registration_panel(app: Any, workspace: Dict[str, Any]) -> None:
    wid = int(workspace["id"])
    app.st.markdown("### Floor plan ↔ elevation registration")
    app.st.caption(
        "PlanReader now uses the calibrated floor footprint for X/Y wall geometry, then registers elevation drawings back to those sides. "
        "Annotations and finish codes are evidence layers, not measurement geometry. Missing orientation/height evidence stays flagged for review."
    )
    reg = register_elevations(app, wid)
    facades = reg.get("facades") or {}
    cols = app.st.columns(4)
    for col, side in zip(cols, CARDINALS):
        col.metric(side, f"{_num(facades.get(side, {}).get('projected_width_m')):.2f} m")
        col.caption(f"{len(facades.get(side, {}).get('segments') or [])} wall segments")

    elevations = reg.get("elevations") or []
    if elevations:
        app.st.dataframe(app.pd.DataFrame(elevations), use_container_width=True, hide_index=True)
    else:
        app.st.warning("No elevation sheets are confidently identified yet. Plan wall geometry is retained, but elevations still need registration.")

    rows = wall_records(app, wid, reg)
    if rows:
        app.st.markdown("#### Registered wall geometry")
        app.st.dataframe(app.pd.DataFrame(rows), use_container_width=True, hide_index=True)
        app.st.caption("Wall lengths come from calibrated plan polygons. Wall heights remain provisional until elevation/section height evidence is solved. Door/window deduction choices already feed the net m² column.")

    unresolved = [r for r in elevations if r.get("confidence") == "Review"]
    if unresolved or reg.get("unregistered_sides"):
        app.st.warning(
            f"Registration review required: {len(unresolved)} elevation sheet(s) need checking; "
            f"unregistered sides: {', '.join(reg.get('unregistered_sides') or []) or 'none'}."
        )
    else:
        app.st.success("All identified elevation sides are cross-registered to the calibrated plan footprint.")


def apply(app: Any) -> None:
    if getattr(app, "_pb_elevation_registration_v135_applied", False):
        return
    app._pb_elevation_registration_v135_applied = True
    base_model_page = app.model_3d_page

    def _model_page(workspace, session_api_key="", ai_provider="OpenAI"):
        with app.st.expander("Registered building geometry · plan ↔ elevations", expanded=True):
            registration_panel(app, workspace)
        return base_model_page(workspace, session_api_key, ai_provider)

    app.orientation_from_text_v135 = orientation_from_text
    app.dimension_candidates_m_v135 = dimension_candidates_m
    app.footprint_facades_v135 = footprint_facades
    app.register_elevations_v135 = lambda workspace_id: register_elevations(app, int(workspace_id))
    app.registered_wall_records_v135 = lambda workspace_id: wall_records(app, int(workspace_id))
    app.model_3d_page = _model_page
