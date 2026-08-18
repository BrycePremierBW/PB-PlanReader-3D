"""PlanReader v1.2.13 quick 3D workflow and visible Surface Editor tabs.

Adds a one-click Build / Refresh path using existing calibrated mapped zones and
valid render/artist-impression pages, and makes the 3D Surface Editor an explicit
top-level tab instead of relying on the older nested wrapper layout.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pb_3d_surface_editor_v1212 as surface_v1212
import pb_takeoff_studio_v1211 as studio_v1211
from pb_studio_path_guard_v1213 import is_regular_image_file

QUICK_SOURCE_PREFIX = "PB Quick 3D v1.2.13 · zone:"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _render_pages(app: Any, workspace_id: int) -> List[Dict[str, Any]]:
    """Return valid render/artist-impression pages using case-insensitive matching."""
    rows = app.lquery(
        "SELECT id,page_label,page_type,image_path FROM pages "
        "WHERE workspace_id=? AND selected=1 ORDER BY id",
        (workspace_id,),
    )
    result = []
    for row in rows:
        low = str(row.get("page_type") or "").lower()
        if ("render" in low or "artist" in low or "impression" in low or "perspective" in low) and is_regular_image_file(row.get("image_path")):
            result.append(dict(row))
    return result


def _calibrated_zones(app: Any, workspace_id: int) -> List[Dict[str, Any]]:
    rows = app.lquery(
        "SELECT * FROM mapped_zones WHERE workspace_id=? ORDER BY id",
        (workspace_id,),
    )
    return [dict(row) for row in rows if _num(row.get("px_per_m")) > 0 and _num(row.get("w_px")) > 0 and _num(row.get("h_px")) > 0]


def refresh_zone_masses(app: Any, workspace_id: int) -> int:
    """Rebuild only masses created by this quick workflow from calibrated zones."""
    zones = _calibrated_zones(app, workspace_id)
    app.lexecute(
        "DELETE FROM model_masses WHERE workspace_id=? AND source_reference LIKE ?",
        (workspace_id, QUICK_SOURCE_PREFIX + "%"),
    )
    count = 0
    for zone in zones:
        pxpm = _num(zone.get("px_per_m"))
        if pxpm <= 0:
            continue
        qstatus = str(zone.get("quantity_status") or "").lower()
        confidence = "Measured" if "measured" in qstatus and "provisional" not in qstatus else "Derived"
        zone_id = int(_num(zone.get("id"), 0))
        source = f"{QUICK_SOURCE_PREFIX}{zone_id}"
        original_source = str(zone.get("source_reference") or "").strip()
        notes = "Auto-built from calibrated PlanReader mapped zone."
        if original_source:
            notes += f" Original source: {original_source}."
        app.lexecute(
            """INSERT INTO model_masses(
                workspace_id,label,level_name,x,y,z,width,depth,height,finish,
                source_reference,confidence,notes,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                workspace_id,
                str(zone.get("name") or f"Zone {zone_id}"),
                "Ground",
                _num(zone.get("x_px")) / pxpm,
                _num(zone.get("y_px")) / pxpm,
                0.0,
                _num(zone.get("w_px")) / pxpm,
                _num(zone.get("h_px")) / pxpm,
                max(0.1, _num(zone.get("wall_height_m"), 2.7)),
                str(zone.get("finish_system") or zone.get("substrate") or ""),
                source,
                confidence,
                notes,
                app.now_stamp(),
            ),
        )
        count += 1
    return count


def quick_build_panel(app: Any, workspace: Dict[str, Any], session_api_key: str = "", ai_provider: str = "OpenAI") -> None:
    workspace_id = int(workspace["id"])
    render_pages = _render_pages(app, workspace_id)
    zones = _calibrated_zones(app, workspace_id)
    masses = app.lquery("SELECT id,confidence FROM model_masses WHERE workspace_id=?", (workspace_id,))

    app.st.markdown("### Quick 3D Build")
    app.st.caption(
        "One button builds or refreshes the 3D model from the information already in PlanReader. "
        "Calibrated mapped zones provide measured geometry; render / artist-impression pages can add visual building form with AI."
    )
    m1, m2, m3 = app.st.columns(3)
    m1.metric("Calibrated zones", len(zones))
    m2.metric("Valid render pages", len(render_pages))
    m3.metric("Current model masses", len(masses))

    if render_pages:
        app.st.success("Render source ready: " + ", ".join(str(p.get("page_label") or f"Page {p['id']}") for p in render_pages[:5]))
    if zones:
        app.st.success(f"Measured drawing source ready: {len(zones)} calibrated mapped zone(s).")
    if not render_pages and not zones:
        app.st.warning(
            "No quick-build source is ready yet. Process the plans, then either calibrate/map a floor-plan zone or classify a page as Render / Artist's Impression."
        )

    ai_key = app.resolve_ai_key(ai_provider, session_api_key)
    use_ai = bool(render_pages and ai_key)
    if render_pages and not ai_key:
        app.st.info("Render pages are available, but no AI key is configured. Quick Build will still create 3D masses from calibrated mapped zones.")

    replace_visual = app.st.checkbox(
        "Refresh AI-assumed geometry instead of stacking duplicates",
        value=True,
        help="Keeps Measured/Verified masses and replaces non-measured AI geometry before adding the latest visual interpretation.",
        key=f"quick3d_replace_{workspace_id}",
    )

    disabled = not bool(render_pages or zones)
    if app.st.button("⚡ Build / Refresh 3D Model", type="primary", use_container_width=True, disabled=disabled, key=f"quick3d_build_{workspace_id}"):
        messages = []
        if use_ai:
            model = app.default_ai_model(ai_provider)
            page_ids = [int(p["id"]) for p in render_pages]
            try:
                data = app._run_with_progress(
                    lambda: app.run_ai_render_read(workspace_id, page_ids, ai_key, model, ai_provider),
                    f"Reading {len(page_ids)} render page(s) into the 3D model",
                )
                counts = app.apply_render_to_model(workspace_id, data, mode="replace" if replace_visual else "merge")
                messages.append(f"AI visual model: {counts.get('masses', 0)} mass(es), {counts.get('openings', 0)} opening(s)")
            except Exception as exc:
                app.st.warning(f"AI visual build could not complete: {app._ai_error_hint(exc)}")

        zone_count = refresh_zone_masses(app, workspace_id) if zones else 0
        if zone_count:
            messages.append(f"Calibrated drawing geometry: {zone_count} mass(es)")

        if messages:
            app.st.success("3D model refreshed. " + " · ".join(messages))
            app.st.session_state[f"quick3d_show_surface_{workspace_id}"] = True
            app.st.rerun()
        else:
            app.st.error("No 3D geometry was created. Check that the selected source pages are processed and that mapped zones are calibrated.")

    if masses:
        fig = app.build_3d_figure(workspace_id)
        if getattr(fig, "data", None):
            app.st.plotly_chart(fig, use_container_width=True, key=f"quick3d_preview_{workspace_id}")
            app.st.caption("Preview of the current PlanReader model. Use the 3D Surface Editor tab to assign substrates and track progress face-by-face.")


def apply(app: Any) -> None:
    """Install a clear top-level 3D workspace over the existing model page."""
    if getattr(app, "_pb_3d_quickstart_v1213_applied", False):
        return
    app._pb_3d_quickstart_v1213_applied = True
    app.refresh_quick_3d_zone_masses = lambda workspace_id: refresh_zone_masses(app, int(workspace_id))

    base_model_page = app.model_3d_page

    def _v1213_model_page(workspace, session_api_key="", ai_provider="OpenAI"):
        app.hero(workspace)
        tabs = app.st.tabs(["⚡ Quick 3D Build", "🎨 3D Surface Editor", "📐 Takeoff Studio", "🧰 Existing 3D Tools"])
        with tabs[0]:
            quick_build_panel(app, workspace, session_api_key, ai_provider)
        with tabs[1]:
            app.st.markdown("### 3D Surface Editor")
            app.st.caption("Click or select a real model face, then assign substrate, inclusion status, progress and notes. Face m² comes directly from the current 3D geometry.")
            surface_v1212.surface_editor_panel(app, workspace)
        with tabs[2]:
            app.st.markdown("### Takeoff Studio")
            studio_v1211._studio_panel(app, workspace)
        with tabs[3]:
            app.st.caption("Original PlanReader 3D model, masses, openings, render-reading and export tools.")
            original_hero = app.hero
            app.hero = lambda *_args, **_kwargs: None
            try:
                base_model_page(workspace, session_api_key, ai_provider)
            finally:
                app.hero = original_hero

    app.model_3d_page = _v1213_model_page
