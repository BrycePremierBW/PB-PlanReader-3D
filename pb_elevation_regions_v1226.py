"""PlanReader v1.2.26 elevation substrate zoning and 3D cross-reference.

Confirmed material codes are no longer treated as a whole-face label when an
elevation contains several materials. Code callout positions are reconciled with
the calibrated facade drawing cluster and converted into reviewable 2D/3D zones.

Automatic region boundaries are explicitly Derived. They are visual guidance and
provenance until an estimator adjusts/accepts the polygon; no invented split is
silently promoted to a measured take-off quantity.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

import plotly.graph_objects as go

import pb_3d_surface_editor_v1212 as surface
import pb_auto_geometry_v1219 as auto
import pb_material_schedule_v1222 as material
import pb_selected_evidence_floor_v1226 as selected

VERSION = "1.2.26"
SETTING_KEY = "elevation_substrate_regions_v1226"
AUTO_NOTE = "[AUTO v1.2.26 REGION]"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _load(app: Any, workspace_id: int) -> Dict[str, Any]:
    raw = app.workspace_setting(int(workspace_id), SETTING_KEY, "{}")
    try:
        parsed = json.loads(str(raw or "{}"))
        return dict(parsed or {}) if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _save(app: Any, workspace_id: int, state: Dict[str, Any]) -> None:
    app.set_workspace_setting(int(workspace_id), SETTING_KEY, json.dumps(state, separators=(",", ":"), default=str))


def _page_map(app: Any, workspace_id: int) -> Dict[int, Dict[str, Any]]:
    return {int(row["id"]): dict(row) for row in app.lquery(
        """SELECT id,page_label,page_type,image_path,width_px,height_px,px_per_m,selected
           FROM pages WHERE workspace_id=? AND COALESCE(selected,0)=1 ORDER BY id""",
        (int(workspace_id),),
    )}


def _bbox_center(bbox: Sequence[Any]) -> Tuple[float, float]:
    if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
        return 0.0, 0.0
    x0, y0, x1, y1 = map(_num, bbox[:4])
    return (x0 + x1) / 2.0, (y0 + y1) / 2.0


def _rect_points(x0: float, y0: float, x1: float, y1: float) -> List[List[float]]:
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def _polygon_area_px(points: Sequence[Sequence[Any]]) -> float:
    if len(points or []) < 3:
        return 0.0
    total = 0.0
    for index, a in enumerate(points):
        b = points[(index + 1) % len(points)]
        total += _num(a[0]) * _num(b[1]) - _num(b[0]) * _num(a[1])
    return abs(total) / 2.0


def _page_percent(points: Sequence[Sequence[Any]], width: float, height: float) -> List[Dict[str, float]]:
    if width <= 0 or height <= 0:
        return []
    return [
        {"x": max(0.0, min(100.0, _num(point[0]) / width * 100.0)), "y": max(0.0, min(100.0, _num(point[1]) / height * 100.0))}
        for point in points
    ]


def _group_occurrences(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for item in items:
        code = str(item.get("code") or "").upper()
        substrate = str(item.get("substrate") or "")
        if not code or not substrate or item.get("status") != "Confirmed" or not item.get("bbox"):
            continue
        grouped[(code, substrate)].append(dict(item))
    out = []
    for (code, substrate), values in grouped.items():
        centers = [_bbox_center(item.get("bbox")) for item in values]
        out.append({
            "code": code, "substrate": substrate,
            "cx": sum(point[0] for point in centers) / len(centers),
            "cy": sum(point[1] for point in centers) / len(centers),
            "occurrences": values,
        })
    return out


def _region_bands(groups: Sequence[Dict[str, Any]], facade_bbox: Sequence[Any]) -> List[Dict[str, Any]]:
    if not groups or len(facade_bbox or []) < 4:
        return []
    x, y, w, h = map(_num, facade_bbox[:4])
    if w <= 0 or h <= 0:
        return []
    groups = [dict(item) for item in groups]
    if len(groups) == 1:
        item = groups[0]
        item["polygon_px"] = _rect_points(x, y, x + w, y + h)
        item["boundary_basis"] = "Single confirmed substrate code on elevation"
        return [item]

    x_values = [item["cx"] for item in groups]
    y_values = [item["cy"] for item in groups]
    x_spread = (max(x_values) - min(x_values)) / max(w, 1.0)
    y_spread = (max(y_values) - min(y_values)) / max(h, 1.0)
    axis = "y" if y_spread > x_spread * 1.20 else "x"
    groups.sort(key=lambda item: item["cy"] if axis == "y" else item["cx"])
    values = [item["cy"] if axis == "y" else item["cx"] for item in groups]
    low = y if axis == "y" else x
    high = y + h if axis == "y" else x + w
    boundaries = [low] + [(values[index] + values[index + 1]) / 2.0 for index in range(len(values) - 1)] + [high]
    out = []
    for index, item in enumerate(groups):
        a, b = boundaries[index], boundaries[index + 1]
        row = dict(item)
        if axis == "y":
            row["polygon_px"] = _rect_points(x, a, x + w, b)
            row["boundary_basis"] = "Approximate horizontal material band from elevation callout positions"
        else:
            row["polygon_px"] = _rect_points(a, y, b, y + h)
            row["boundary_basis"] = "Approximate vertical material band from elevation callout positions"
        out.append(row)
    return out


def build_regions(app: Any, workspace_id: int, report: Dict[str, Any]) -> Dict[str, Any]:
    pages = _page_map(app, int(workspace_id))
    state = selected.filter_material_state(app, int(workspace_id), material._setting_get(app, int(workspace_id)))
    by_page: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for occurrence in state.get("occurrences") or []:
        by_page[int(occurrence.get("page_id") or 0)].append(dict(occurrence))

    regions: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    provenance = selected._load_provenance(app, int(workspace_id))
    for facade in report.get("facades") or []:
        page_id = int(facade.get("page_id") or 0)
        page = pages.get(page_id)
        face = str(facade.get("face") or "")
        bbox = facade.get("bbox")
        if not page or not face or not bbox:
            continue
        grouped = _group_occurrences(by_page.get(page_id, []))
        bands = _region_bands(grouped, bbox)
        width = _num(page.get("width_px")); height = _num(page.get("height_px")); pxpm = _num(page.get("px_per_m"))
        for index, band in enumerate(bands, 1):
            polygon_px = band.get("polygon_px") or []
            area_m2 = _polygon_area_px(polygon_px) / (pxpm * pxpm) if pxpm > 0 else 0.0
            region = {
                "id": f"elev:{page_id}:{face}:{band['code']}:{index}",
                "page_id": page_id, "page_label": str(page.get("page_label") or ""), "face": face,
                "code": band["code"], "substrate": band["substrate"],
                "polygon_px": polygon_px, "points": _page_percent(polygon_px, width, height),
                "area_m2": round(area_m2, 2), "confidence": "Derived",
                "boundary_basis": band.get("boundary_basis"),
                "evidence": [str(item.get("text") or "") for item in band.get("occurrences") or []],
            }
            regions.append(region)
            if len(bands) > 1:
                issues.append({
                    "category": "Elevation substrate boundary", "severity": "Medium", "code": band["code"],
                    "page_id": page_id, "page_label": str(page.get("page_label") or ""),
                    "message": f"{band['code']} = {band['substrate']}. PlanReader located the callout and created an approximate region; adjust/confirm its polygon before using the split as measured m².",
                    "bbox": None, "bbox_mode": "xyxy", "source": str(band.get("boundary_basis") or ""),
                })

        # Attach full-facade geometry to every auto facade take-off row from this sheet.
        x, y, w, h = map(_num, bbox[:4])
        facade_points = _page_percent(_rect_points(x, y, x + w, y + h), width, height)
        for row in app.lquery(
            "SELECT source_reference,location,quantity,unit FROM takeoff_rows WHERE workspace_id=? AND source_page=? AND source_reference LIKE ?",
            (int(workspace_id), str(page.get("page_label") or ""), auto.SOURCE_PREFIX + "%facade:%"),
        ):
            ref = str(row.get("source_reference") or "")
            provenance[ref] = {
                "kind": "area", "unit": str(row.get("unit") or "m²"), "location": str(row.get("location") or ""),
                "quantity": _num(row.get("quantity")), "authoritative_source": "calibrated elevation",
                "sources": [{
                    "page_id": page_id, "page_label": str(page.get("page_label") or ""),
                    "source_kind": "Elevation", "polygon": None, "points": facade_points,
                    "bbox": bbox, "evidence_text": "Calibrated elevation drawing cluster",
                }],
            }

    selected._save_provenance(app, int(workspace_id), provenance)
    return {"version": VERSION, "generated_at": app.now_stamp(), "regions": regions, "issues": issues}


def _overall_model_bounds(masses: Sequence[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    if not masses:
        return None
    x0 = min(_num(m.get("x")) for m in masses); y0 = min(_num(m.get("y")) for m in masses); z0 = min(_num(m.get("z")) for m in masses)
    x1 = max(_num(m.get("x")) + _num(m.get("width")) for m in masses)
    y1 = max(_num(m.get("y")) + _num(m.get("depth")) for m in masses)
    z1 = max(_num(m.get("z")) + _num(m.get("height")) for m in masses)
    if x1 <= x0 or y1 <= y0 or z1 <= z0:
        return None
    return {"x0": x0, "x1": x1, "y0": y0, "y1": y1, "z0": z0, "z1": z1}


def _region_3d_points(region: Dict[str, Any], bounds: Dict[str, float]) -> List[Tuple[float, float, float]]:
    points = region.get("points") or []
    if len(points) < 3:
        return []
    face = str(region.get("face") or "")
    out = []
    for point in points:
        u = max(0.0, min(1.0, _num(point.get("x")) / 100.0))
        v = max(0.0, min(1.0, _num(point.get("y")) / 100.0))
        z = bounds["z1"] - v * (bounds["z1"] - bounds["z0"])
        if face == "front": out.append((bounds["x0"] + u * (bounds["x1"] - bounds["x0"]), bounds["y0"], z))
        elif face == "rear": out.append((bounds["x0"] + u * (bounds["x1"] - bounds["x0"]), bounds["y1"], z))
        elif face == "left": out.append((bounds["x0"], bounds["y0"] + u * (bounds["y1"] - bounds["y0"]), z))
        elif face == "right": out.append((bounds["x1"], bounds["y0"] + u * (bounds["y1"] - bounds["y0"]), z))
    return out


def build_region_figure(app: Any, workspace_id: int, regions: Sequence[Dict[str, Any]]):
    masses = [dict(row) for row in app.lquery("SELECT * FROM model_masses WHERE workspace_id=? ORDER BY z,id", (int(workspace_id),))]
    bounds = _overall_model_bounds(masses)
    fig = app.build_3d_figure(int(workspace_id)) if hasattr(app, "build_3d_figure") else go.Figure()
    if not bounds:
        return fig
    for region in regions:
        points = _region_3d_points(dict(region), bounds)
        if len(points) != 4:
            continue
        xs = [p[0] for p in points]; ys = [p[1] for p in points]; zs = [p[2] for p in points]
        code = str(region.get("code") or "OTHER")
        colour = surface.substrate_color(code, "#80A6C9")
        hover = f"<b>{code} · {region.get('substrate')}</b><br>{region.get('page_label')} · {region.get('face')}<br>Derived zone: {region.get('area_m2',0):.2f} m²<br>{region.get('boundary_basis')}<extra></extra>"
        fig.add_trace(go.Mesh3d(
            x=xs, y=ys, z=zs, i=[0,0], j=[1,2], k=[2,3], color=colour,
            opacity=0.82, flatshading=True, hovertemplate=hover,
            name=f"{region.get('face')} · {code}", showlegend=True,
        ))
    fig.update_layout(title="3D substrate zoning from selected elevations", uirevision="pb-elevation-regions-v1226")
    return fig


def region_panel(app: Any, workspace: Dict[str, Any]) -> None:
    workspace_id = int(workspace["id"])
    state = _load(app, workspace_id)
    regions = list(state.get("regions") or [])
    if not regions:
        return
    app.st.markdown("### Elevation → 3D substrate zoning")
    app.st.caption("These zones show where confirmed elevation material callouts sit on the 3D envelope. Multi-material boundaries stay Derived until their drawing polygon is checked/edited.")
    app.st.plotly_chart(build_region_figure(app, workspace_id, regions), use_container_width=True, key=f"elev_region_3d_v1226_{workspace_id}")
    app.st.dataframe(app.pd.DataFrame([{
        "Page": r.get("page_label"), "Face": r.get("face"), "Code": r.get("code"),
        "Substrate": r.get("substrate"), "Derived zone m²": r.get("area_m2"), "Basis": r.get("boundary_basis"),
    } for r in regions]), hide_index=True, use_container_width=True)


def apply(app: Any) -> None:
    if getattr(app, "_pb_elevation_regions_v1226_applied", False):
        return
    app._pb_elevation_regions_v1226_applied = True

    base_analyse = auto.analyse_workspace
    def _analyse_with_regions(app_obj: Any, workspace_id: int):
        report = base_analyse(app_obj, int(workspace_id))
        region_state = build_regions(app_obj, int(workspace_id), report)
        _save(app_obj, int(workspace_id), region_state)
        if region_state.get("issues"):
            state = selected.filter_material_state(app_obj, int(workspace_id), material._setting_get(app_obj, int(workspace_id)))
            existing = [dict(item) for item in state.get("review_issues") or []]
            fingerprints = {(str(i.get("category")), int(i.get("page_id") or 0), str(i.get("code") or ""), str(i.get("message") or "")) for i in existing}
            for issue in region_state["issues"]:
                key = (str(issue.get("category")), int(issue.get("page_id") or 0), str(issue.get("code") or ""), str(issue.get("message") or ""))
                if key not in fingerprints:
                    existing.append(dict(issue)); fingerprints.add(key)
            state["review_issues"] = selected.filter_review_issues(app_obj, int(workspace_id), existing)
            material._setting_set(app_obj, int(workspace_id), state)
        report["elevation_regions"] = len(region_state.get("regions") or [])
        return report
    auto.analyse_workspace = _analyse_with_regions

    base_surface_panel = surface.surface_editor_panel
    def _surface_with_regions(app_obj: Any, workspace: Dict[str, Any]):
        base_surface_panel(app_obj, workspace)
        app_obj.st.divider()
        region_panel(app_obj, workspace)
    surface.surface_editor_panel = _surface_with_regions

    app.elevation_substrate_regions_v1226 = lambda workspace_id: _load(app, int(workspace_id)).get("regions") or []
