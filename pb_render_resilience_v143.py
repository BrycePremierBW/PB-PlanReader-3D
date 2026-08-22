"""PlanReader v1.4.3 resilient 3D preview.

Always presents the best truthful 3D geometry available for a project.  Priority:
registered wall model -> calibrated floor polygons -> existing model masses.
No fallback is promoted to verified measurement merely because it can render.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import plotly.graph_objects as go

VERSION = "1.4.3"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def choose_render_source(registered_walls, precision_prisms, model_masses) -> str:
    if registered_walls:
        return "registered_walls"
    if precision_prisms:
        return "precision_prisms"
    if model_masses:
        return "model_masses"
    return "none"


def _mass_figure(rows: List[Dict[str, Any]]) -> go.Figure:
    fig = go.Figure()
    for idx, row in enumerate(rows):
        x = _num(row.get("x"), idx * 4.0)
        y = _num(row.get("y"), 0.0)
        z = _num(row.get("z"), 0.0)
        w = max(0.1, _num(row.get("width_m") or row.get("width"), 3.0))
        d = max(0.1, _num(row.get("depth_m") or row.get("depth"), 3.0))
        h = max(0.1, _num(row.get("height_m") or row.get("height"), 2.7))
        xs = [x,x+w,x+w,x,x,x+w,x+w,x]
        ys = [y,y,y+d,y+d,y,y,y+d,y+d]
        zs = [z,z,z,z,z+h,z+h,z+h,z+h]
        faces = [(0,1,2),(0,2,3),(4,6,5),(4,7,6),(0,4,5),(0,5,1),(1,5,6),(1,6,2),(2,6,7),(2,7,3),(3,7,4),(3,4,0)]
        i,j,k = zip(*faces)
        fig.add_trace(go.Mesh3d(
            x=xs,y=ys,z=zs,i=i,j=j,k=k,opacity=.75,flatshading=True,
            name=str(row.get("name") or row.get("label") or f"Mass {idx+1}"),
            hovertemplate=(f"<b>{row.get('name') or row.get('label') or f'Mass {idx+1}'}</b><br>"
                           "Fallback: existing model mass<br>Review against registered drawings<extra></extra>"),
            showscale=False,
        ))
    fig.update_layout(height=720,margin=dict(l=0,r=0,t=10,b=0),showlegend=False,
                      scene=dict(aspectmode="data",xaxis_title="X (m)",yaxis_title="Y (m)",zaxis_title="Height (m)"))
    return fig


def resilient_preview(app: Any, workspace: Dict[str, Any]) -> Tuple[str, Any]:
    wid = int(workspace["id"])
    walls = []
    prisms = []
    masses = []
    try:
        if hasattr(app, "build_registered_walls_v139"):
            walls = app.build_registered_walls_v139(wid) or []
    except Exception:
        walls = []
    try:
        if hasattr(app, "build_precision_prisms"):
            prisms = app.build_precision_prisms(wid) or []
    except Exception:
        prisms = []
    try:
        masses = [dict(r) for r in app.lquery("SELECT * FROM model_masses WHERE workspace_id=? ORDER BY id", (wid,))]
    except Exception:
        masses = []

    source = choose_render_source(walls, prisms, masses)
    if source == "registered_walls":
        return source, app.registered_building_figure_v139(walls)
    if source == "precision_prisms":
        return source, app.build_precision_figure(prisms, xray=False, level_filter="All")
    if source == "model_masses":
        return source, _mass_figure(masses)
    return "none", None


def panel(app: Any, workspace: Dict[str, Any]) -> None:
    app.st.markdown("### 3D render status")
    source, fig = resilient_preview(app, workspace)
    labels = {
        "registered_walls": "Registered plan + elevation wall model",
        "precision_prisms": "Calibrated plan geometry fallback",
        "model_masses": "Existing 3D model fallback",
    }
    if fig is None:
        app.st.warning("No measurable geometry is available yet. Upload/read the plans or map a floor footprint; PlanReader will not invent a building just to show a render.")
        return
    app.st.success(f"3D preview ready · {labels[source]}")
    if source != "registered_walls":
        app.st.caption("This is a safe fallback preview while elevation registration is incomplete. It remains reviewable and is not promoted to verified geometry.")
    app.st.plotly_chart(fig, use_container_width=True, key=f"resilient_render_{int(workspace['id'])}_{source}")


def apply(app: Any) -> None:
    if getattr(app, "_pb_render_resilience_v143_applied", False):
        return
    app._pb_render_resilience_v143_applied = True
    base_model_page = app.model_3d_page

    def _model_page(workspace, session_api_key="", ai_provider="OpenAI"):
        with app.st.expander("Reliable 3D preview", expanded=True):
            panel(app, workspace)
        try:
            return base_model_page(workspace, session_api_key, ai_provider)
        except Exception as exc:
            app.st.warning(f"Advanced 3D tools hit an error, but the reliable preview above is still available. Detail: {exc}")
            return None

    app.choose_render_source_v143 = choose_render_source
    app.resilient_3d_preview_v143 = lambda workspace: resilient_preview(app, workspace)
    app.model_3d_page = _model_page
