"""PB PlanReader v1.2.11 screenshot-style Takeoff Studio.

This patch adds a single visual elevation/render workspace over the existing
PlanReader model. It deliberately reuses processed page images, calibrated page
scale, workspace settings, take-off rows and the existing Plotly 3D model rather
than introducing a second project database.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

try:
    from planreader_takeoff_studio import takeoff_studio_editor
except Exception:  # pragma: no cover - component failure degrades cleanly
    takeoff_studio_editor = None

SOURCE_PREFIX = "PB Takeoff Studio v1.2.11"
SETTING_PREFIX = "takeoff_studio_v1211_page_"

SUBSTRATE_PRESETS: List[Dict[str, str]] = [
    {"code": "EC1", "name": "Lineaboard Cladding", "color": "#A9BFD9"},
    {"code": "EC2", "name": "Textureboard Cladding", "color": "#A5B9DB"},
    {"code": "EC3", "name": "Easylap Cladding", "color": "#B7C4D5"},
    {"code": "RBL", "name": "Rendered Block", "color": "#B9C0C4"},
    {"code": "SOF", "name": "Soffits / Eaves", "color": "#D9C788"},
    {"code": "EC5", "name": "Timber Look Cladding", "color": "#A77C52"},
    {"code": "SCR", "name": "Aluminium Screens", "color": "#BEA8B5"},
    {"code": "BA1", "name": "Glass Balustrade", "color": "#B9C7C8"},
    {"code": "SHD", "name": "Sunhoods", "color": "#919B8D"},
    {"code": "BC", "name": "Cappings & Gutters", "color": "#9AA7AA"},
    {"code": "RS", "name": "Roof Sheet", "color": "#C45F74"},
    {"code": "DP", "name": "Downpipes", "color": "#84A77E"},
    {"code": "GD", "name": "Garage Doors", "color": "#7E8792"},
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


def _point(raw: Any) -> Dict[str, float]:
    if isinstance(raw, dict):
        return {"x": _clamp(raw.get("x"), 0.0, 100.0), "y": _clamp(raw.get("y"), 0.0, 100.0)}
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        return {"x": _clamp(raw[0], 0.0, 100.0), "y": _clamp(raw[1], 0.0, 100.0)}
    return {"x": 0.0, "y": 0.0}


def _legacy_box_points(raw: Dict[str, Any]) -> List[Dict[str, float]]:
    x = _clamp(raw.get("x", 15), 0.0, 100.0)
    y = _clamp(raw.get("y", 15), 0.0, 100.0)
    w = max(1.0, _num(raw.get("w"), 20.0))
    h = max(1.0, _num(raw.get("h"), 12.0))
    x2 = _clamp(x + w, 0.0, 100.0)
    y2 = _clamp(y + h, 0.0, 100.0)
    return [{"x": x, "y": y}, {"x": x2, "y": y}, {"x": x2, "y": y2}, {"x": x, "y": y2}]


def polygon_area_m2(
    points: Sequence[Dict[str, Any]],
    width_px: float,
    height_px: float,
    px_per_m: float,
    manual_m2: float = 0.0,
) -> float:
    """Return server-side polygon area in m² from percentage coordinates."""
    manual = _num(manual_m2)
    if manual > 0:
        return round(manual, 2)
    if px_per_m <= 0 or width_px <= 0 or height_px <= 0 or len(points or []) < 3:
        return 0.0
    pts = [_point(p) for p in points]
    twice = 0.0
    for index, a in enumerate(pts):
        b = pts[(index + 1) % len(pts)]
        ax = a["x"] / 100.0 * width_px
        ay = a["y"] / 100.0 * height_px
        bx = b["x"] / 100.0 * width_px
        by = b["y"] / 100.0 * height_px
        twice += ax * by - bx * ay
    pixel_area = abs(twice) / 2.0
    return round(pixel_area / (px_per_m * px_per_m), 2)


def is_perspective_page(page_type: str) -> bool:
    low = str(page_type or "").lower()
    return "render" in low or "artist" in low or "impression" in low or "perspective" in low


def _substrate_name(code: str) -> str:
    key = str(code or "").strip()
    for item in SUBSTRATE_PRESETS:
        if item["code"] == key:
            return item["name"]
    return key or "Other / To Confirm"


def normalise_studio_area(
    raw: Dict[str, Any],
    index: int,
    *,
    width_px: float,
    height_px: float,
    px_per_m: float,
    view_label: str,
) -> Dict[str, Any]:
    """Canonicalise one Studio area and recompute its geometry server-side."""
    raw = dict(raw or {})
    points_raw = raw.get("points")
    points = [_point(p) for p in points_raw] if isinstance(points_raw, list) and len(points_raw) >= 3 else _legacy_box_points(raw)
    area_id = str(raw.get("id") or f"A-{index:03d}").strip() or f"A-{index:03d}"
    manual = max(0.0, _num(raw.get("manual_m2")))
    progress = _clamp(raw.get("progress_pct"), 0.0, 100.0)
    area_m2 = polygon_area_m2(points, width_px, height_px, px_per_m, manual)
    status = str(raw.get("status") or "Paint Included").strip() or "Paint Included"
    if status not in {"Paint Included", "Separate Item", "Provisional", "Excluded"}:
        status = "Paint Included"
    return {
        "id": area_id,
        "label": str(raw.get("label") or area_id).strip() or area_id,
        "substrate": str(raw.get("substrate") or "OTHER").strip() or "OTHER",
        "elevation": str(raw.get("elevation") or view_label or "").strip(),
        "status": status,
        "progress_pct": round(progress, 1),
        "notes": str(raw.get("notes") or "").strip(),
        "manual_m2": round(manual, 2),
        "area_m2": area_m2,
        "points": points,
    }


def normalise_studio_areas(
    areas: Iterable[Dict[str, Any]],
    *,
    width_px: float,
    height_px: float,
    px_per_m: float,
    view_label: str,
) -> List[Dict[str, Any]]:
    return [
        normalise_studio_area(
            area,
            index,
            width_px=width_px,
            height_px=height_px,
            px_per_m=px_per_m,
            view_label=view_label,
        )
        for index, area in enumerate(list(areas or []), start=1)
    ]


def completion_summary(areas: Iterable[Dict[str, Any]]) -> Dict[str, float]:
    total = 0.0
    completed = 0.0
    for area in areas or []:
        if str(area.get("status") or "") == "Excluded":
            continue
        qty = max(0.0, _num(area.get("area_m2")))
        progress = _clamp(area.get("progress_pct"), 0.0, 100.0)
        total += qty
        completed += qty * progress / 100.0
    remaining = max(0.0, total - completed)
    return {
        "total_m2": round(total, 2),
        "completed_m2": round(completed, 2),
        "remaining_m2": round(remaining, 2),
        "completed_pct": round((completed / total * 100.0) if total > 0 else 0.0, 1),
    }


def build_studio_takeoff_rows(
    areas: Iterable[Dict[str, Any]],
    *,
    page_id: int,
    page_label: str,
    page_type: str,
    px_per_m: float,
) -> List[Dict[str, Any]]:
    """Build safe, unpriced measurement rows from Studio areas."""
    perspective = is_perspective_page(page_type)
    rows: List[Dict[str, Any]] = []
    for area in areas or []:
        qty = max(0.0, _num(area.get("area_m2")))
        manual = _num(area.get("manual_m2")) > 0
        if qty > 0:
            if perspective:
                quantity_status = "Provisional measured"
                confidence = "Derived"
            elif px_per_m > 0:
                quantity_status = "Measured"
                confidence = "Measured"
            elif manual:
                quantity_status = "Provisional measured"
                confidence = "Derived"
            else:
                quantity_status = "To measure"
                confidence = "To review"
        else:
            quantity_status = "To measure"
            confidence = "To review"
        status = str(area.get("status") or "Paint Included")
        inclusion = {
            "Paint Included": "INCLUSION",
            "Separate Item": "SEPARATE ITEM",
            "Provisional": "PROVISIONAL",
            "Excluded": "EXCLUSION",
        }.get(status, "INCLUSION")
        substrate_code = str(area.get("substrate") or "OTHER")
        progress = _clamp(area.get("progress_pct"), 0.0, 100.0)
        note_parts = [
            "Visual Takeoff Studio measurement; review finish system, coats, productivity and rate before final pricing.",
            f"Progress {progress:.0f}%.",
        ]
        if perspective:
            note_parts.append("Perspective render geometry is provisional and is not a substitute for a calibrated orthographic elevation.")
        if area.get("notes"):
            note_parts.append(str(area.get("notes")))
        rows.append(
            {
                "section": "Internal" if "internal" in str(area.get("elevation") or "").lower() else "External",
                "element": f"{substrate_code} {_substrate_name(substrate_code)}".strip(),
                "location": str(area.get("label") or area.get("id") or "Studio area"),
                "substrate": _substrate_name(substrate_code),
                "finish_system": "To be confirmed",
                "quantity": round(qty, 2),
                "unit": "m²",
                "quantity_status": quantity_status,
                "source_page": str(page_label or ""),
                "source_reference": f"{SOURCE_PREFIX} · page:{int(page_id)} · area:{area.get('id')}",
                "inclusion_status": inclusion,
                "coats": 0,
                "coverage_m2_per_litre": 0,
                "productivity_m2_per_hour": 0,
                "rate_per_unit": 0,
                "confidence": confidence,
                "notes": " ".join(note_parts),
                "row_role": "studio_area",
            }
        )
    return rows


def _state_key(page_id: int) -> str:
    return f"{SETTING_PREFIX}{int(page_id)}"


def _load_state(app: Any, workspace_id: int, page_id: int) -> Dict[str, Any]:
    raw = app.workspace_setting(workspace_id, _state_key(page_id), "{}")
    try:
        parsed = json.loads(str(raw or "{}"))
    except Exception:
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _save_state(app: Any, workspace_id: int, page_id: int, areas: Sequence[Dict[str, Any]]) -> None:
    payload = {"areas": list(areas or []), "saved_at": app.now_stamp()}
    app.set_workspace_setting(workspace_id, _state_key(page_id), json.dumps(payload, separators=(",", ":")))


def _replace_studio_rows(app: Any, workspace_id: int, page_id: int, rows: Sequence[Dict[str, Any]]) -> None:
    prefix = f"{SOURCE_PREFIX} · page:{int(page_id)} ·"
    app.lexecute(
        "DELETE FROM takeoff_rows WHERE workspace_id=? AND source_reference LIKE ?",
        (workspace_id, prefix + "%"),
    )
    sql = """INSERT INTO takeoff_rows(
        workspace_id,section,element,location,substrate,finish_system,quantity,unit,
        quantity_status,source_page,source_reference,inclusion_status,coats,
        coverage_m2_per_litre,productivity_m2_per_hour,rate_per_unit,confidence,
        notes,row_role,created_at,updated_at
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
    for row in rows:
        app.lexecute(
            sql,
            (
                workspace_id,
                row["section"], row["element"], row["location"], row["substrate"],
                row["finish_system"], row["quantity"], row["unit"], row["quantity_status"],
                row["source_page"], row["source_reference"], row["inclusion_status"],
                row["coats"], row["coverage_m2_per_litre"], row["productivity_m2_per_hour"],
                row["rate_per_unit"], row["confidence"], row["notes"], row["row_role"],
                app.now_stamp(), app.now_stamp(),
            ),
        )


def _page_priority(page_type: Any) -> int:
    low = str(page_type or "").lower()
    if "render" in low or "artist" in low or "impression" in low:
        return 0
    if "elevation" in low:
        return 1
    if "section" in low:
        return 2
    return 3


def _studio_pages(app: Any, workspace_id: int):
    pages = app.ldf(
        "SELECT id,page_label,page_type,image_path,px_per_m,width_px,height_px,selected "
        "FROM pages WHERE workspace_id=? AND selected=1 ORDER BY id",
        (workspace_id,),
    )
    if pages.empty:
        return pages
    pages = pages.copy()
    pages["_priority"] = pages["page_type"].map(_page_priority)
    pages = pages.sort_values(["_priority", "id"], kind="stable").drop(columns=["_priority"])
    return pages.reset_index(drop=True)


def _studio_panel(app: Any, workspace: Dict[str, Any]) -> None:
    workspace_id = int(workspace["id"])
    pages = _studio_pages(app, workspace_id)
    if pages.empty:
        app.st.info("Process and select at least one drawing page first.")
        return
    if takeoff_studio_editor is None:
        app.st.error("The Takeoff Studio component is unavailable. Existing 3D and Plan Mapper tools remain below under Advanced.")
        return

    c1, c2, c3 = app.st.columns([1.15, 1.7, 1.0])
    c1.text_input(
        "Project",
        value=f"{workspace.get('job_no') or ''} · {workspace.get('job_name') or ''}".strip(" ·"),
        disabled=True,
        key=f"studio_project_{workspace_id}",
    )
    labels = [f"#{int(r.id)} · {r.page_label} · {r.page_type}" for r in pages.itertuples()]
    chosen = c2.selectbox("Drawing", labels, key=f"studio_page_{workspace_id}")
    page = pages.iloc[labels.index(chosen)].to_dict()
    view_label = c3.text_input(
        "View",
        value=str(page.get("page_label") or page.get("page_type") or "Current view"),
        key=f"studio_view_{workspace_id}_{int(page['id'])}",
    )

    image_path = Path(str(page.get("image_path") or ""))
    if not image_path.exists():
        app.st.error("The processed image for this drawing page is missing. Re-process the source document, then reopen Takeoff Studio.")
        return

    width_px = _num(page.get("width_px"))
    height_px = _num(page.get("height_px"))
    px_per_m = max(0.0, _num(page.get("px_per_m")))
    page_id = int(page["id"])
    saved = _load_state(app, workspace_id, page_id)
    session_key = f"studio_state_{workspace_id}_{page_id}"
    if session_key not in app.st.session_state:
        app.st.session_state[session_key] = normalise_studio_areas(
            saved.get("areas") or [],
            width_px=width_px,
            height_px=height_px,
            px_per_m=px_per_m,
            view_label=view_label,
        )

    current = normalise_studio_areas(
        app.st.session_state.get(session_key) or [],
        width_px=width_px,
        height_px=height_px,
        px_per_m=px_per_m,
        view_label=view_label,
    )
    app.st.session_state[session_key] = current

    if is_perspective_page(str(page.get("page_type") or "")):
        app.st.warning(
            "This is a perspective render / artist's impression. Use it for the visual overlay and progress view, but treat calculated areas as provisional. "
            "For final measured m², switch the Drawing selector to a calibrated orthographic elevation or enter a verified manual m² override."
        )
    elif px_per_m <= 0:
        app.st.warning(
            "This page has no calibrated scale yet. You can lay out the areas visually, but m² will remain To measure unless you add a manual m² override. "
            "Calibrate the drawing in Plan Mapper for scale-based quantities."
        )

    result = takeoff_studio_editor(
        image_path.read_bytes(),
        areas=current,
        substrates=SUBSTRATE_PRESETS,
        px_per_m=px_per_m,
        page_type=str(page.get("page_type") or ""),
        view_label=view_label,
        revision=int(app.st.session_state.get(f"studio_rev_{workspace_id}_{page_id}", 0)),
        key=f"studio_widget_{workspace_id}_{page_id}",
        height=940,
    )
    if isinstance(result, dict):
        incoming = result.get("areas")
        if isinstance(incoming, list):
            current = normalise_studio_areas(
                incoming,
                width_px=width_px,
                height_px=height_px,
                px_per_m=px_per_m,
                view_label=view_label,
            )
            app.st.session_state[session_key] = current

    summary = completion_summary(current)
    rows = build_studio_takeoff_rows(
        current,
        page_id=page_id,
        page_label=str(page.get("page_label") or ""),
        page_type=str(page.get("page_type") or ""),
        px_per_m=px_per_m,
    )
    m1, m2, m3, m4 = app.st.columns(4)
    m1.metric("Studio areas", len(current))
    m2.metric("Total m²", f"{summary['total_m2']:,.2f}")
    m3.metric("Completed", f"{summary['completed_m2']:,.2f} m² ({summary['completed_pct']:.1f}%)")
    m4.metric("Remaining", f"{summary['remaining_m2']:,.2f} m²")

    b1, b2, b3 = app.st.columns([1.0, 1.2, 1.0])
    if b1.button("Save Studio layout", use_container_width=True, key=f"studio_save_{page_id}"):
        _save_state(app, workspace_id, page_id, current)
        app.st.success(f"Saved {len(current)} Studio area(s) for {page.get('page_label') or 'this drawing'}.")
    if b2.button("Save + sync areas to take-off", type="primary", use_container_width=True, key=f"studio_sync_{page_id}"):
        _save_state(app, workspace_id, page_id, current)
        _replace_studio_rows(app, workspace_id, page_id, rows)
        app.st.success(
            f"Synced {len(rows)} Studio measurement row(s) to the take-off. Rates, coats and productivity remain deliberately unpriced until reviewed."
        )
        app.st.rerun()
    current_export = app.pd.DataFrame(
        [
            {
                "ID": a.get("id"),
                "Label": a.get("label"),
                "Substrate": a.get("substrate"),
                "Substrate description": _substrate_name(str(a.get("substrate") or "")),
                "Elevation": a.get("elevation"),
                "Area m²": a.get("area_m2"),
                "Status": a.get("status"),
                "Progress %": a.get("progress_pct"),
                "Completed m²": round(_num(a.get("area_m2")) * _num(a.get("progress_pct")) / 100.0, 2),
                "Remaining m²": round(_num(a.get("area_m2")) * (1.0 - _num(a.get("progress_pct")) / 100.0), 2),
                "Notes": a.get("notes"),
            }
            for a in current
        ]
    )
    b3.download_button(
        "Export current CSV",
        data=current_export.to_csv(index=False).encode("utf-8"),
        file_name=f"{app.safe_name(workspace.get('job_no') or workspace.get('job_name') or 'PlanReader')}_{app.safe_name(page.get('page_label') or 'view')}_studio.csv",
        mime="text/csv",
        use_container_width=True,
        key=f"studio_csv_{page_id}",
    )


def _saved_studio_report(app: Any, workspace_id: int):
    pages = app.ldf(
        "SELECT id,page_label,page_type,px_per_m,width_px,height_px FROM pages WHERE workspace_id=? ORDER BY id",
        (workspace_id,),
    )
    records: List[Dict[str, Any]] = []
    for page in pages.to_dict("records") if not pages.empty else []:
        saved = _load_state(app, workspace_id, int(page["id"]))
        areas = normalise_studio_areas(
            saved.get("areas") or [],
            width_px=_num(page.get("width_px")),
            height_px=_num(page.get("height_px")),
            px_per_m=max(0.0, _num(page.get("px_per_m"))),
            view_label=str(page.get("page_label") or ""),
        )
        for area in areas:
            qty = _num(area.get("area_m2"))
            progress = _num(area.get("progress_pct"))
            records.append(
                {
                    "Page": page.get("page_label"),
                    "Page type": page.get("page_type"),
                    "ID": area.get("id"),
                    "Label": area.get("label"),
                    "Substrate": area.get("substrate"),
                    "Substrate description": _substrate_name(str(area.get("substrate") or "")),
                    "Area m²": qty,
                    "Status": area.get("status"),
                    "Progress %": progress,
                    "Completed m²": round(qty * progress / 100.0, 2),
                    "Remaining m²": round(qty * (1.0 - progress / 100.0), 2),
                    "Notes": area.get("notes"),
                }
            )
    return app.pd.DataFrame(records)


def _studio_reports_panel(app: Any, workspace: Dict[str, Any]) -> None:
    workspace_id = int(workspace["id"])
    report = _saved_studio_report(app, workspace_id)
    if report.empty:
        app.st.info("No saved Takeoff Studio layouts yet. Save a Studio layout first.")
        return
    included = report[report["Status"].ne("Excluded")].copy()
    c1, c2, c3 = app.st.columns(3)
    c1.metric("Saved areas", len(report))
    c2.metric("Included m²", f"{included['Area m²'].sum():,.2f}")
    c3.metric("Remaining m²", f"{included['Remaining m²'].sum():,.2f}")
    app.st.dataframe(report, use_container_width=True, hide_index=True, height=520)
    app.st.download_button(
        "Download complete Studio report CSV",
        data=report.to_csv(index=False).encode("utf-8"),
        file_name=f"{app.safe_name(workspace.get('job_no') or workspace.get('job_name') or 'PlanReader')}_takeoff_studio_report.csv",
        mime="text/csv",
        type="primary",
        use_container_width=True,
        key=f"studio_report_csv_{workspace_id}",
    )


def apply(app: Any) -> None:
    """Install Takeoff Studio additively on top of the existing 3D model page."""
    if getattr(app, "_pb_takeoff_studio_v1211_applied", False):
        return
    app._pb_takeoff_studio_v1211_applied = True
    app.polygon_area_m2 = polygon_area_m2
    app.normalise_studio_area = normalise_studio_area
    app.normalise_studio_areas = normalise_studio_areas
    app.studio_completion_summary = completion_summary
    app.build_studio_takeoff_rows = build_studio_takeoff_rows

    base_model_3d_page = app.model_3d_page

    def _v1211_model_3d_page(workspace, session_api_key="", ai_provider="OpenAI"):
        app.hero(workspace)
        tabs = app.st.tabs(["Takeoff Studio", "3D Model", "Reports & Export", "Advanced 3D Tools"])
        with tabs[0]:
            app.st.caption(
                "Visual elevation/render take-off: draw substrate areas directly on the processed drawing, edit them later, track completion and sync reviewed m² back to the take-off schedule."
            )
            _studio_panel(app, workspace)
        with tabs[1]:
            fig = app.build_3d_figure(int(workspace["id"]))
            if not getattr(fig, "data", None):
                app.st.info("No 3D masses or mapped zones yet. Build them in Advanced 3D Tools or from the Plan Mapper.")
            else:
                app.st.plotly_chart(fig, use_container_width=True, key=f"studio_3d_{workspace['id']}")
            app.st.caption("The true 3D view uses PlanReader's existing measured/derived model masses. The Takeoff Studio render overlay is a separate visual measurement workspace, not fake BIM geometry.")
        with tabs[2]:
            _studio_reports_panel(app, workspace)
        with tabs[3]:
            original_hero = app.hero
            app.hero = lambda *_args, **_kwargs: None
            try:
                base_model_3d_page(workspace, session_api_key, ai_provider)
            finally:
                app.hero = original_hero

    app.model_3d_page = _v1211_model_3d_page
