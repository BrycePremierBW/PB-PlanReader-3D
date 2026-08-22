"""PlanReader v1.3.2 precision polygon 3D preview.

The legacy 3D model stores rectangular ``model_masses``.  That is useful for
quick modelling but it throws away the irregular geometry already captured by
the calibrated floor mapper.  This module builds a render-only building scene
straight from those saved polygons so the visible model and floor take-off use
the same calibrated source geometry.

No quantity, level or height is silently upgraded to Verified.  Polygon plan
geometry may be measured, while inferred storey placement/default wall height
remain clearly identified as provisional visual geometry.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Iterable, List, Sequence, Tuple

import plotly.graph_objects as go

import pb_floor_mapper_v127 as mapper_v127
import pb_floor_mapper_v128 as mapper_v128

VERSION = "1.3.2"
SOURCE_PREFIX = f"PB Precision 3D v{VERSION}"
SETTING_PREFIX = mapper_v127.SETTING_PREFIX


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _signed_area(points: Sequence[Tuple[float, float]]) -> float:
    return 0.5 * sum(
        x1 * y2 - x2 * y1
        for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1])
    )


def polygon_area(points: Sequence[Tuple[float, float]]) -> float:
    return abs(_signed_area(list(points)))


def _cross(a: Tuple[float, float], b: Tuple[float, float], c: Tuple[float, float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _point_in_triangle(p: Tuple[float, float], a: Tuple[float, float], b: Tuple[float, float], c: Tuple[float, float]) -> bool:
    c1, c2, c3 = _cross(a, b, p), _cross(b, c, p), _cross(c, a, p)
    has_neg = c1 < -1e-10 or c2 < -1e-10 or c3 < -1e-10
    has_pos = c1 > 1e-10 or c2 > 1e-10 or c3 > 1e-10
    return not (has_neg and has_pos)


def triangulate_polygon(points: Sequence[Tuple[float, float]]) -> List[Tuple[int, int, int]]:
    """Ear-clip a simple polygon, supporting concave mapped floor shapes."""
    pts = list(points)
    if len(pts) < 3:
        return []
    order = list(range(len(pts)))
    if _signed_area(pts) < 0:
        order.reverse()
    triangles: List[Tuple[int, int, int]] = []
    guard = 0
    while len(order) > 3 and guard < len(pts) * len(pts) * 2:
        guard += 1
        clipped = False
        for pos, current in enumerate(order):
            prev = order[pos - 1]
            nxt = order[(pos + 1) % len(order)]
            a, b, c = pts[prev], pts[current], pts[nxt]
            if _cross(a, b, c) <= 1e-10:
                continue
            if any(
                idx not in {prev, current, nxt} and _point_in_triangle(pts[idx], a, b, c)
                for idx in order
            ):
                continue
            triangles.append((prev, current, nxt))
            del order[pos]
            clipped = True
            break
        if not clipped:
            # Degenerate/self-intersecting input: do not fabricate a misleading
            # surface.  The caller can still render its outline for review.
            return []
    if len(order) == 3:
        triangles.append(tuple(order))
    return triangles


def infer_level(page_label: Any, page_text: Any = "") -> Tuple[str, int, str]:
    text = f"{page_label or ''} {page_text or ''}".lower()
    basement = re.search(r"\b(?:basement|b)\s*[- ]?(\d+)\b", text)
    if basement:
        n = max(1, int(basement.group(1)))
        return f"Basement {n}", -n, "Derived from drawing label/text"
    level = re.search(r"\b(?:level|lvl|l)\s*[- ]?(\d{1,2})\b", text)
    if level:
        n = int(level.group(1))
        return f"Level {n}", n, "Derived from drawing label/text"
    if re.search(r"\bground(?: floor| level)?\b|\blg\b", text):
        return "Ground", 0, "Derived from drawing label/text"
    return "Ground / unregistered", 0, "Level not explicit; visual placement provisional"


def _shape_points_m(shape: Dict[str, Any], width_px: float, height_px: float, px_per_m: float) -> List[Tuple[float, float]]:
    if px_per_m <= 0 or width_px <= 0 or height_px <= 0:
        return []
    points = mapper_v128._points_from_shape(shape)
    result: List[Tuple[float, float]] = []
    for point in points:
        x = _num(point.get("x")) / 100.0 * width_px / px_per_m
        # Flip drawing Y so the 3D world reads in the conventional direction.
        y = (100.0 - _num(point.get("y"))) / 100.0 * height_px / px_per_m
        result.append((x, y))
    return result


def build_precision_prisms(app: Any, workspace_id: int) -> List[Dict[str, Any]]:
    settings = app.lquery(
        "SELECT key,value FROM workspace_settings WHERE workspace_id=? AND key LIKE ? ORDER BY key",
        (int(workspace_id), SETTING_PREFIX + "%"),
    )
    if not settings:
        return []
    default_height = max(0.5, _num(app.workspace_setting(workspace_id, "default_wall_height_m", 2.7), 2.7))
    prisms: List[Dict[str, Any]] = []
    for setting in settings:
        match = re.search(r"(\d+)$", str(setting.get("key") or ""))
        if not match:
            continue
        page_id = int(match.group(1))
        pages = app.lquery(
            "SELECT id,page_label,page_type,extracted_text,width_px,height_px,px_per_m,image_path FROM pages WHERE id=? AND workspace_id=?",
            (page_id, int(workspace_id)),
        )
        if not pages:
            continue
        page = dict(pages[0])
        try:
            state = json.loads(str(setting.get("value") or "{}"))
        except Exception:
            continue
        if not isinstance(state, dict):
            continue
        width_px, height_px = _num(page.get("width_px")), _num(page.get("height_px"))
        if (width_px <= 0 or height_px <= 0) and page.get("image_path"):
            try:
                width_px, height_px = mapper_v127._image_size(mapper_v127.Path(str(page["image_path"])))
            except Exception:
                pass
        px_per_m = _num(page.get("px_per_m"))
        if px_per_m <= 0:
            px_per_m = mapper_v127.calibration_px_per_m(state.get("calibration"), width_px, height_px)
        if px_per_m <= 0:
            continue
        level_name, level_index, level_reason = infer_level(page.get("page_label"), page.get("extracted_text"))
        z0 = level_index * default_height
        for index, shape in enumerate(state.get("boxes") or [], start=1):
            if not isinstance(shape, dict):
                continue
            points = _shape_points_m(shape, width_px, height_px, px_per_m)
            triangles = triangulate_polygon(points)
            if len(points) < 3 or polygon_area(points) <= 0.01:
                continue
            label = str(shape.get("label") or "").strip() or f"{page.get('page_label') or 'Floor plan'} · area {index}"
            prisms.append({
                "id": str(shape.get("id") or f"page-{page_id}-shape-{index}"),
                "label": label,
                "page_id": page_id,
                "page_label": str(page.get("page_label") or ""),
                "level_name": level_name,
                "level_index": level_index,
                "level_reason": level_reason,
                "z": z0,
                "height": default_height,
                "points": points,
                "triangles": triangles,
                "floor_area_m2": round(polygon_area(points), 3),
                "scale_px_per_m": px_per_m,
                "confidence": "Measured plan geometry" if triangles else "Needs polygon review",
            })
    return prisms


def _add_prism(fig: go.Figure, prism: Dict[str, Any], *, xray: bool = False) -> None:
    points = list(prism["points"])
    n = len(points)
    z0 = _num(prism.get("z"))
    z1 = z0 + _num(prism.get("height"), 2.7)
    xs = [p[0] for p in points] + [p[0] for p in points]
    ys = [p[1] for p in points] + [p[1] for p in points]
    zs = [z0] * n + [z1] * n
    faces: List[Tuple[int, int, int]] = []
    for a, b, c in prism.get("triangles") or []:
        faces.append((a + n, b + n, c + n))
    for idx in range(n):
        nxt = (idx + 1) % n
        faces.extend([(idx, nxt, nxt + n), (idx, nxt + n, idx + n)])
    if faces:
        i, j, k = zip(*faces)
        hover = (
            f"<b>{prism.get('label')}</b><br>{prism.get('level_name')}<br>"
            f"Floor area: {_num(prism.get('floor_area_m2')):.2f} m²<br>"
            f"Height: {_num(prism.get('height')):.2f} m<br>"
            f"{prism.get('confidence')}<extra></extra>"
        )
        fig.add_trace(go.Mesh3d(
            x=xs, y=ys, z=zs, i=list(i), j=list(j), k=list(k),
            opacity=0.42 if xray else 0.88,
            flatshading=True,
            lighting=dict(ambient=0.7, diffuse=0.65, roughness=0.8, specular=0.12),
            hovertemplate=hover,
            name=str(prism.get("label") or "Mapped area"),
            showscale=False,
        ))
    outline = points + [points[0]]
    fig.add_trace(go.Scatter3d(
        x=[p[0] for p in outline], y=[p[1] for p in outline], z=[z1] * len(outline),
        mode="lines", line=dict(width=4), hoverinfo="skip", showlegend=False,
    ))


def build_precision_figure(prisms: Iterable[Dict[str, Any]], *, xray: bool = False, level_filter: str = "All") -> go.Figure:
    fig = go.Figure()
    visible = [p for p in prisms if level_filter == "All" or str(p.get("level_name")) == level_filter]
    for prism in visible:
        _add_prism(fig, prism, xray=xray)
    fig.update_layout(
        margin=dict(l=0, r=0, t=10, b=0),
        height=720,
        showlegend=False,
        scene=dict(
            aspectmode="data",
            xaxis_title="X (m)", yaxis_title="Y (m)", zaxis_title="Height (m)",
            camera=dict(eye=dict(x=1.45, y=-1.55, z=1.05)),
        ),
    )
    return fig


def precision_panel(app: Any, workspace: Dict[str, Any]) -> None:
    workspace_id = int(workspace["id"])
    prisms = build_precision_prisms(app, workspace_id)
    app.st.markdown("### Precision 3D building preview")
    app.st.caption(
        "Uses the same calibrated editable polygons as the floor take-off instead of converting them to rectangular masses. "
        "This makes plan shape measurable and visually faithful; elevations, openings, roofs and non-explicit level heights remain review items until documented."
    )
    if not prisms:
        app.st.info("No calibrated floor polygons are saved yet. Map the floor-plan footprint/areas in Plan Mapper, save them, then return here.")
        return
    levels = sorted({str(p.get("level_name") or "Ground / unregistered") for p in prisms}, key=lambda name: min(_num(p.get("level_index")) for p in prisms if str(p.get("level_name")) == name))
    c1, c2, c3, c4 = app.st.columns(4)
    c1.metric("Mapped polygons", len(prisms))
    c2.metric("Mapped floor m²", f"{sum(_num(p.get('floor_area_m2')) for p in prisms):,.2f}")
    c3.metric("Levels recognised", len(levels))
    c4.metric("Geometry source", "Calibrated plans")
    controls = app.st.columns([2, 1])
    level_filter = controls[0].selectbox("Show level", ["All"] + levels, key=f"precision3d_level_{workspace_id}")
    xray = controls[1].toggle("X-ray", value=False, key=f"precision3d_xray_{workspace_id}")
    fig = build_precision_figure(prisms, xray=xray, level_filter=level_filter)
    app.st.plotly_chart(fig, use_container_width=True, key=f"precision3d_figure_{workspace_id}")
    provisional = [p for p in prisms if "provisional" in str(p.get("level_reason")).lower() or not p.get("triangles")]
    if provisional:
        app.st.warning(f"{len(provisional)} mapped shape(s) still have provisional level placement or need polygon review. Plan geometry is retained; PlanReader has not silently guessed missing evidence.")


def apply(app: Any) -> None:
    if getattr(app, "_pb_precision_3d_v132_applied", False):
        return
    app._pb_precision_3d_v132_applied = True
    base_model_page = app.model_3d_page

    def _precision_model_page(workspace, session_api_key="", ai_provider="OpenAI"):
        app.hero(workspace)
        with app.st.expander("Precision 3D · measured plan geometry", expanded=True):
            precision_panel(app, workspace)
        original_hero = app.hero
        app.hero = lambda *_args, **_kwargs: None
        try:
            return base_model_page(workspace, session_api_key, ai_provider)
        finally:
            app.hero = original_hero

    app.build_precision_prisms = lambda workspace_id: build_precision_prisms(app, int(workspace_id))
    app.build_precision_figure = build_precision_figure
    app.model_3d_page = _precision_model_page
