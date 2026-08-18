"""PlanReader v1.2.12 clickable 3D surface editor.

Adds face-level selection and take-off metadata to the existing measured/derived
3D masses without replacing the v1.2.11 Takeoff Studio. Surface geometry is
always regenerated from the current model mass dimensions; only estimator
metadata is persisted.
"""
from __future__ import annotations

import json
import math
from typing import Any, Dict, Iterable, List, Sequence, Tuple

import plotly.graph_objects as go

try:
    import pb_takeoff_studio_v1211 as studio_v1211
except Exception:  # pragma: no cover
    studio_v1211 = None

SOURCE_PREFIX = "PB 3D Surface Editor v1.2.12"
SETTING_KEY = "3d_surface_editor_v1212"
STATUS_OPTIONS = ["Paint Included", "Separate Item", "Provisional", "Excluded"]
FACE_LABELS = {
    "front": "Front",
    "rear": "Rear",
    "left": "Left",
    "right": "Right",
    "top": "Top / Roof",
    "bottom": "Underside",
}

FALLBACK_SUBSTRATES = [
    {"code": "RBL", "name": "Rendered Block", "color": "#B9C0C4"},
    {"code": "SOF", "name": "Soffits / Eaves", "color": "#D9C788"},
    {"code": "EC5", "name": "Timber Look Cladding", "color": "#A77C52"},
    {"code": "RS", "name": "Roof Sheet", "color": "#C45F74"},
    {"code": "OTHER", "name": "Other / To Confirm", "color": "#80A6C9"},
]


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _clamp(value: Any, low: float, high: float) -> float:
    return max(low, min(high, _num(value, low)))


def substrate_presets() -> List[Dict[str, str]]:
    if studio_v1211 is not None and getattr(studio_v1211, "SUBSTRATE_PRESETS", None):
        return [dict(item) for item in studio_v1211.SUBSTRATE_PRESETS]
    return [dict(item) for item in FALLBACK_SUBSTRATES]


def substrate_name(code: str) -> str:
    key = str(code or "OTHER")
    for item in substrate_presets():
        if item["code"] == key:
            return item["name"]
    return key or "Other / To Confirm"


def substrate_color(code: str, fallback: str = "#C9BFA6") -> str:
    key = str(code or "")
    for item in substrate_presets():
        if item["code"] == key:
            return item.get("color") or fallback
    return fallback


def infer_substrate(finish: Any) -> str:
    text = str(finish or "").lower()
    rules = [
        (("soffit", "eave"), "SOF"),
        (("linea", "lineaboard"), "EC1"),
        (("textureboard",), "EC2"),
        (("easylap",), "EC3"),
        (("render", "blockwork", "masonry"), "RBL"),
        (("timber", "wood"), "EC5"),
        (("screen",), "SCR"),
        (("balustrade", "glass"), "BA1"),
        (("sunhood", "sun hood"), "SHD"),
        (("gutter", "capping"), "BC"),
        (("roof", "sheet"), "RS"),
        (("downpipe",), "DP"),
        (("garage",), "GD"),
    ]
    for needles, code in rules:
        if any(needle in text for needle in needles):
            return code
    return "OTHER"


def _quad(surface_id: str, mass: Dict[str, Any], face: str, points: Sequence[Tuple[float, float, float]], area_m2: float) -> Dict[str, Any]:
    confidence = str(mass.get("confidence") or "Assumed")
    return {
        "surface_id": surface_id,
        "mass_id": int(_num(mass.get("id"), 0)),
        "mass_label": str(mass.get("label") or f"Mass {mass.get('id') or ''}").strip(),
        "level_name": str(mass.get("level_name") or ""),
        "face": face,
        "face_label": FACE_LABELS.get(face, face.title()),
        "points": [tuple(float(v) for v in point) for point in points],
        "area_m2": round(max(0.0, area_m2), 3),
        "finish": str(mass.get("finish") or ""),
        "confidence": confidence,
        "source_reference": str(mass.get("source_reference") or ""),
    }


def derive_mass_surfaces(mass: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return six stable faces derived from one cuboid model mass."""
    mass = dict(mass or {})
    mass_id = int(_num(mass.get("id"), 0))
    x, y, z = _num(mass.get("x")), _num(mass.get("y")), _num(mass.get("z"))
    w = max(0.0, _num(mass.get("width")))
    d = max(0.0, _num(mass.get("depth")))
    h = max(0.0, _num(mass.get("height")))
    if mass_id <= 0 or w <= 0 or d <= 0 or h <= 0:
        return []
    faces = [
        ("front", [(x, y, z), (x + w, y, z), (x + w, y, z + h), (x, y, z + h)], w * h),
        ("rear", [(x, y + d, z), (x + w, y + d, z), (x + w, y + d, z + h), (x, y + d, z + h)], w * h),
        ("left", [(x, y, z), (x, y + d, z), (x, y + d, z + h), (x, y, z + h)], d * h),
        ("right", [(x + w, y, z), (x + w, y + d, z), (x + w, y + d, z + h), (x + w, y, z + h)], d * h),
        ("top", [(x, y, z + h), (x + w, y, z + h), (x + w, y + d, z + h), (x, y + d, z + h)], w * d),
        ("bottom", [(x, y, z), (x + w, y, z), (x + w, y + d, z), (x, y + d, z)], w * d),
    ]
    return [_quad(f"mass:{mass_id}:{face}", mass, face, points, area) for face, points, area in faces]


def derive_surfaces(masses: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    surfaces: List[Dict[str, Any]] = []
    for mass in masses or []:
        surfaces.extend(derive_mass_surfaces(dict(mass)))
    return surfaces


def default_surface_state(surface: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "substrate": infer_substrate(surface.get("finish")),
        "status": "Excluded" if surface.get("face") == "bottom" else "Provisional",
        "progress_pct": 0.0,
        "notes": "",
    }


def normalise_override(raw: Dict[str, Any], surface: Dict[str, Any]) -> Dict[str, Any]:
    base = default_surface_state(surface)
    raw = dict(raw or {})
    status = str(raw.get("status") or base["status"])
    if status not in STATUS_OPTIONS:
        status = base["status"]
    return {
        "substrate": str(raw.get("substrate") or base["substrate"] or "OTHER"),
        "status": status,
        "progress_pct": round(_clamp(raw.get("progress_pct"), 0.0, 100.0), 1),
        "notes": str(raw.get("notes") or "").strip(),
    }


def surface_records(surfaces: Sequence[Dict[str, Any]], overrides: Dict[str, Any]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    overrides = dict(overrides or {})
    for surface in surfaces:
        state = normalise_override(overrides.get(surface["surface_id"]) or {}, surface)
        item = dict(surface)
        item.update(state)
        records.append(item)
    return records


def completion_summary(records: Iterable[Dict[str, Any]]) -> Dict[str, float]:
    total = completed = 0.0
    for item in records or []:
        if str(item.get("status")) == "Excluded":
            continue
        area = max(0.0, _num(item.get("area_m2")))
        progress = _clamp(item.get("progress_pct"), 0.0, 100.0)
        total += area
        completed += area * progress / 100.0
    remaining = max(0.0, total - completed)
    return {
        "total_m2": round(total, 2),
        "completed_m2": round(completed, 2),
        "remaining_m2": round(remaining, 2),
        "completed_pct": round(completed / total * 100.0, 1) if total > 0 else 0.0,
    }


def build_surface_takeoff_rows(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in records or []:
        area = max(0.0, _num(item.get("area_m2")))
        status = str(item.get("status") or "Provisional")
        inclusion = {
            "Paint Included": "INCLUSION",
            "Separate Item": "SEPARATE ITEM",
            "Provisional": "PROVISIONAL",
            "Excluded": "EXCLUSION",
        }.get(status, "PROVISIONAL")
        confidence_text = str(item.get("confidence") or "").strip().lower()
        measured = confidence_text in {"measured", "verified"}
        quantity_status = "Measured" if measured and area > 0 else ("Provisional measured" if area > 0 else "To measure")
        confidence = "Measured" if measured else "Derived"
        substrate = str(item.get("substrate") or "OTHER")
        progress = _clamp(item.get("progress_pct"), 0.0, 100.0)
        notes = [
            "3D face quantity derived from current PlanReader model dimensions; recheck if model geometry changes.",
            f"Progress {progress:.0f}%.",
        ]
        if not measured:
            notes.append("Model mass is not marked Measured/Verified, so this surface remains provisional.")
        if item.get("notes"):
            notes.append(str(item.get("notes")))
        rows.append({
            "section": "External",
            "element": f"3D {item.get('face_label') or item.get('face')} · {substrate_name(substrate)}",
            "location": str(item.get("mass_label") or item.get("surface_id")),
            "substrate": substrate_name(substrate),
            "finish_system": "To be confirmed",
            "quantity": round(area, 2),
            "unit": "m²",
            "quantity_status": quantity_status,
            "source_page": str(item.get("source_reference") or "3D model"),
            "source_reference": f"{SOURCE_PREFIX} · {item.get('surface_id')}",
            "inclusion_status": inclusion,
            "coats": 0,
            "coverage_m2_per_litre": 0,
            "productivity_m2_per_hour": 0,
            "rate_per_unit": 0,
            "confidence": confidence,
            "notes": " ".join(notes),
            "row_role": "model_surface",
        })
    return rows


def selected_surface_from_event(event: Any, trace_surface_ids: Sequence[str]) -> str:
    if not event:
        return ""
    try:
        selection = event.get("selection", {}) if hasattr(event, "get") else getattr(event, "selection", {})
        points = selection.get("points", []) if hasattr(selection, "get") else getattr(selection, "points", [])
        if not points:
            return ""
        point = points[0]
        custom = point.get("customdata") if hasattr(point, "get") else getattr(point, "customdata", None)
        if isinstance(custom, (list, tuple)) and custom:
            custom = custom[0]
        if custom:
            return str(custom)
        curve = point.get("curve_number") if hasattr(point, "get") else getattr(point, "curve_number", None)
        if curve is None:
            curve = point.get("curveNumber") if hasattr(point, "get") else getattr(point, "curveNumber", None)
        curve = int(curve)
        if 0 <= curve < len(trace_surface_ids):
            return str(trace_surface_ids[curve])
    except Exception:
        return ""
    return ""


def _progress_colour(progress: float) -> str:
    progress = _clamp(progress, 0.0, 100.0)
    if progress >= 99.5:
        return "#4EAD55"
    if progress > 0:
        return "#D9903D"
    return "#8993A1"


def build_surface_figure(app: Any, records: Sequence[Dict[str, Any]], selected_id: str = "", *, show_bottom: bool = False, xray: bool = False, colour_mode: str = "Substrate"):
    fig = go.Figure()
    trace_surface_ids: List[str] = []
    masses_for_edges: Dict[int, Dict[str, Any]] = {}
    for item in records:
        if item.get("face") == "bottom" and not show_bottom:
            continue
        points = item.get("points") or []
        if len(points) != 4:
            continue
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        zs = [p[2] for p in points]
        if colour_mode == "Progress":
            color = _progress_colour(_num(item.get("progress_pct")))
        else:
            fallback = app.finish_to_colour(item.get("finish")) if hasattr(app, "finish_to_colour") else "#C9BFA6"
            color = substrate_color(str(item.get("substrate") or ""), fallback)
        if str(item.get("surface_id")) == str(selected_id):
            color = "#2476E8"
        area = _num(item.get("area_m2"))
        hover = (
            f"<b>{item.get('mass_label')}</b><br>{item.get('face_label')}<br>"
            f"{substrate_name(str(item.get('substrate') or 'OTHER'))}<br>"
            f"Area: {area:.2f} m²<br>Status: {item.get('status')}<br>"
            f"Progress: {_num(item.get('progress_pct')):.0f}%<extra></extra>"
        )
        surface_id = str(item.get("surface_id"))
        fig.add_trace(go.Mesh3d(
            x=xs, y=ys, z=zs,
            i=[0, 0], j=[1, 2], k=[2, 3],
            color=color,
            opacity=0.34 if xray else (1.0 if surface_id == selected_id else 0.92),
            flatshading=True,
            lighting=dict(ambient=0.72, diffuse=0.78, specular=0.16, roughness=0.82, fresnel=0.08),
            lightposition=dict(x=120, y=-180, z=240),
            customdata=[surface_id] * 4,
            hovertemplate=hover,
            name=f"{item.get('mass_label')} · {item.get('face_label')}",
            showlegend=False,
        ))
        trace_surface_ids.append(surface_id)
        masses_for_edges[int(_num(item.get("mass_id"), 0))] = item

    # Add crisp model outlines after selectable surfaces. These traces are not mapped as surfaces.
    grouped: Dict[int, List[Dict[str, Any]]] = {}
    for item in records:
        grouped.setdefault(int(_num(item.get("mass_id"), 0)), []).append(item)
    for mass_id, items in grouped.items():
        coords = [point for item in items for point in (item.get("points") or [])]
        if not coords:
            continue
        xs = [p[0] for p in coords]; ys = [p[1] for p in coords]; zs = [p[2] for p in coords]
        x0, x1 = min(xs), max(xs); y0, y1 = min(ys), max(ys); z0, z1 = min(zs), max(zs)
        corners = [(x0,y0,z0),(x1,y0,z0),(x1,y1,z0),(x0,y1,z0),(x0,y0,z1),(x1,y0,z1),(x1,y1,z1),(x0,y1,z1)]
        edges = [(0,1),(1,2),(2,3),(3,0),(4,5),(5,6),(6,7),(7,4),(0,4),(1,5),(2,6),(3,7)]
        ex: List[Any] = []; ey: List[Any] = []; ez: List[Any] = []
        for a, b in edges:
            ex += [corners[a][0], corners[b][0], None]
            ey += [corners[a][1], corners[b][1], None]
            ez += [corners[a][2], corners[b][2], None]
        fig.add_trace(go.Scatter3d(x=ex, y=ey, z=ez, mode="lines", line=dict(color="#313B47", width=3), hoverinfo="skip", showlegend=False, name=f"edges-{mass_id}"))

    fig.update_layout(
        height=690,
        margin=dict(l=0, r=0, t=42, b=0),
        title="Clickable 3D surface model",
        paper_bgcolor="#0D131B",
        plot_bgcolor="#0D131B",
        font=dict(color="#DDE6F0"),
        scene=dict(
            bgcolor="#111A24",
            xaxis=dict(title="X (m)", showbackground=True, backgroundcolor="#17212C", gridcolor="#2B3744", zerolinecolor="#3B4858"),
            yaxis=dict(title="Y (m)", showbackground=True, backgroundcolor="#17212C", gridcolor="#2B3744", zerolinecolor="#3B4858"),
            zaxis=dict(title="Z (m)", showbackground=True, backgroundcolor="#17212C", gridcolor="#2B3744", zerolinecolor="#3B4858"),
            aspectmode="data",
            camera=dict(eye=dict(x=1.55, y=-1.75, z=1.25)),
        ),
        hovermode="closest",
        clickmode="event+select",
        uirevision="pb-3d-surface-editor-v1212",
    )
    return fig, trace_surface_ids


def _load_overrides(app: Any, workspace_id: int) -> Dict[str, Dict[str, Any]]:
    raw = app.workspace_setting(workspace_id, SETTING_KEY, "{}")
    try:
        parsed = json.loads(str(raw or "{}"))
    except Exception:
        parsed = {}
    surfaces = parsed.get("surfaces") if isinstance(parsed, dict) else {}
    return dict(surfaces or {}) if isinstance(surfaces, dict) else {}


def _save_overrides(app: Any, workspace_id: int, overrides: Dict[str, Any]) -> None:
    app.set_workspace_setting(workspace_id, SETTING_KEY, json.dumps({"surfaces": overrides, "saved_at": app.now_stamp()}, separators=(",", ":")))


def _replace_rows(app: Any, workspace_id: int, rows: Sequence[Dict[str, Any]]) -> None:
    app.lexecute("DELETE FROM takeoff_rows WHERE workspace_id=? AND source_reference LIKE ?", (workspace_id, SOURCE_PREFIX + "%"))
    sql = """INSERT INTO takeoff_rows(
        workspace_id,section,element,location,substrate,finish_system,quantity,unit,
        quantity_status,source_page,source_reference,inclusion_status,coats,
        coverage_m2_per_litre,productivity_m2_per_hour,rate_per_unit,confidence,
        notes,row_role,created_at,updated_at
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
    for row in rows:
        app.lexecute(sql, (
            workspace_id, row["section"], row["element"], row["location"], row["substrate"], row["finish_system"],
            row["quantity"], row["unit"], row["quantity_status"], row["source_page"], row["source_reference"],
            row["inclusion_status"], row["coats"], row["coverage_m2_per_litre"], row["productivity_m2_per_hour"],
            row["rate_per_unit"], row["confidence"], row["notes"], row["row_role"], app.now_stamp(), app.now_stamp(),
        ))


def surface_editor_panel(app: Any, workspace: Dict[str, Any]) -> None:
    workspace_id = int(workspace["id"])
    masses = app.lquery("SELECT * FROM model_masses WHERE workspace_id=? ORDER BY z,id", (workspace_id,))
    surfaces = derive_surfaces(masses)
    if not surfaces:
        app.st.info("No 3D masses exist yet. Build or import the model first, then reopen the 3D Surface Editor.")
        return

    overrides = _load_overrides(app, workspace_id)
    records = surface_records(surfaces, overrides)
    record_by_id = {item["surface_id"]: item for item in records}
    selected_key = f"v1212_selected_surface_{workspace_id}"
    if app.st.session_state.get(selected_key) not in record_by_id:
        first = next((r["surface_id"] for r in records if r.get("face") != "bottom"), records[0]["surface_id"])
        app.st.session_state[selected_key] = first

    ctrl1, ctrl2, ctrl3 = app.st.columns([1.0, 1.0, 1.25])
    show_bottom = ctrl1.toggle("Show underside", value=False, key=f"v1212_bottom_{workspace_id}")
    xray = ctrl2.toggle("X-ray model", value=False, key=f"v1212_xray_{workspace_id}")
    colour_mode = ctrl3.radio("Colour by", ["Substrate", "Progress"], horizontal=True, key=f"v1212_colour_{workspace_id}")

    selected_id = str(app.st.session_state[selected_key])
    fig, trace_ids = build_surface_figure(app, records, selected_id, show_bottom=show_bottom, xray=xray, colour_mode=colour_mode)
    model_col, edit_col = app.st.columns([2.35, 1.0], gap="large")
    with model_col:
        event = app.st.plotly_chart(
            fig,
            use_container_width=True,
            key=f"v1212_surface_plot_{workspace_id}",
            on_select="rerun",
            selection_mode="points",
            config={"displaylogo": False, "scrollZoom": True, "responsive": True},
        )
        clicked = selected_surface_from_event(event, trace_ids)
        if clicked and clicked in record_by_id and clicked != selected_id:
            app.st.session_state[selected_key] = clicked
            app.st.rerun()

    selected_id = str(app.st.session_state[selected_key])
    selected = record_by_id[selected_id]
    with edit_col:
        options = [r["surface_id"] for r in records if show_bottom or r.get("face") != "bottom"]
        label_map = {r["surface_id"]: f"{r['mass_label']} · {r['face_label']} · {r['area_m2']:.2f} m²" for r in records}
        chosen = app.st.selectbox(
            "Selected surface",
            options,
            index=options.index(selected_id) if selected_id in options else 0,
            format_func=lambda value: label_map.get(value, value),
            key=f"v1212_surface_select_{workspace_id}",
        )
        if chosen != selected_id:
            app.st.session_state[selected_key] = chosen
            app.st.rerun()
        selected = record_by_id[chosen]
        app.st.metric("Surface area", f"{selected['area_m2']:.2f} m²")
        app.st.caption(f"{selected['mass_label']} · {selected['face_label']} · model confidence: {selected['confidence'] or 'Assumed'}")
        presets = substrate_presets()
        codes = [item["code"] for item in presets]
        substrate = app.st.selectbox(
            "Substrate",
            codes,
            index=codes.index(selected["substrate"]) if selected["substrate"] in codes else codes.index("OTHER") if "OTHER" in codes else 0,
            format_func=lambda code: f"{code} · {substrate_name(code)}",
            key=f"v1212_substrate_{workspace_id}_{chosen}",
        )
        status = app.st.selectbox(
            "Status",
            STATUS_OPTIONS,
            index=STATUS_OPTIONS.index(selected["status"]),
            key=f"v1212_status_{workspace_id}_{chosen}",
        )
        progress = app.st.slider(
            "Progress",
            min_value=0,
            max_value=100,
            value=int(round(_num(selected["progress_pct"]))),
            step=1,
            format="%d%%",
            key=f"v1212_progress_{workspace_id}_{chosen}",
        )
        completed = selected["area_m2"] * progress / 100.0
        app.st.caption(f"Completed {completed:.2f} m² · Remaining {max(0.0, selected['area_m2'] - completed):.2f} m²")
        notes = app.st.text_area("Notes", value=str(selected.get("notes") or ""), key=f"v1212_notes_{workspace_id}_{chosen}", height=92)
        save_col, reset_col = app.st.columns(2)
        if save_col.button("Save surface", type="primary", use_container_width=True, key=f"v1212_save_{workspace_id}_{chosen}"):
            overrides[chosen] = {"substrate": substrate, "status": status, "progress_pct": float(progress), "notes": notes}
            _save_overrides(app, workspace_id, overrides)
            app.st.success("Surface settings saved.")
            app.st.rerun()
        if reset_col.button("Reset", use_container_width=True, key=f"v1212_reset_{workspace_id}_{chosen}"):
            overrides.pop(chosen, None)
            _save_overrides(app, workspace_id, overrides)
            app.st.rerun()

    records = surface_records(surfaces, _load_overrides(app, workspace_id))
    summary = completion_summary(records)
    m1, m2, m3, m4 = app.st.columns(4)
    m1.metric("3D surfaces", len(records))
    m2.metric("Included / provisional m²", f"{summary['total_m2']:.2f}")
    m3.metric("Completed", f"{summary['completed_m2']:.2f} m² ({summary['completed_pct']:.1f}%)")
    m4.metric("Remaining", f"{summary['remaining_m2']:.2f} m²")

    app.st.caption("Face m² is derived from the current 3D mass dimensions. Surfaces from masses not marked Measured/Verified stay provisional when synced to the take-off.")
    sync1, sync2 = app.st.columns([1.4, 1.0])
    if sync1.button("Save + sync all 3D surfaces to take-off", type="primary", use_container_width=True, key=f"v1212_sync_{workspace_id}"):
        latest = surface_records(surfaces, _load_overrides(app, workspace_id))
        rows = build_surface_takeoff_rows(latest)
        _replace_rows(app, workspace_id, rows)
        app.st.success(f"Synced {len(rows)} model-surface rows. Rates, coats and productivity remain zero until reviewed.")
        app.st.rerun()
    report = app.pd.DataFrame([
        {
            "Surface": r["surface_id"], "Mass": r["mass_label"], "Level": r["level_name"], "Face": r["face_label"],
            "Substrate": r["substrate"], "Area m²": r["area_m2"], "Status": r["status"], "Progress %": r["progress_pct"],
            "Completed m²": round(r["area_m2"] * _num(r["progress_pct"]) / 100.0, 2), "Confidence": r["confidence"], "Notes": r["notes"],
        }
        for r in records
    ])
    sync2.download_button(
        "Download 3D surface CSV",
        data=report.to_csv(index=False).encode("utf-8"),
        file_name=f"{app.safe_name(workspace.get('job_no') or workspace.get('job_name') or 'PlanReader')}_3d_surfaces.csv",
        mime="text/csv",
        use_container_width=True,
        key=f"v1212_csv_{workspace_id}",
    )


def apply(app: Any) -> None:
    """Install the v1.2.12 editor ahead of the existing v1.2.11 Studio page."""
    if getattr(app, "_pb_3d_surface_editor_v1212_applied", False):
        return
    app._pb_3d_surface_editor_v1212_applied = True
    app.derive_mass_surfaces = derive_mass_surfaces
    app.derive_3d_surfaces = derive_surfaces
    app.build_surface_takeoff_rows = build_surface_takeoff_rows
    app.build_surface_model_figure = build_surface_figure

    base_model_page = app.model_3d_page

    def _v1212_model_page(workspace, session_api_key="", ai_provider="OpenAI"):
        app.hero(workspace)
        app.st.markdown("### 3D Surface Editor")
        app.st.caption("Click a face of the real PlanReader model, assign its painting substrate/status and track progress. Existing Takeoff Studio and advanced 3D tools remain below unchanged.")
        surface_editor_panel(app, workspace)
        app.st.divider()
        original_hero = app.hero
        app.hero = lambda *_args, **_kwargs: None
        try:
            return base_model_page(workspace, session_api_key, ai_provider)
        finally:
            app.hero = original_hero

    app.model_3d_page = _v1212_model_page
