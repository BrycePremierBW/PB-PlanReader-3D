"""PlanReader v1.3.1 substrate QA and zoomable polygon editor.

Uses elevations as the primary substrate/finish evidence and artist impressions as
secondary visual evidence. The result is a whole-building 3D QA view that highlights
faces whose substrate still needs estimator verification. It never lets an artist
impression override a drawing/elevation callout.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

import plotly.graph_objects as go

import pb_3d_quickstart_v1213 as quick3d
import pb_3d_surface_editor_v1212 as surface3d
import pb_floor_mapper_v127 as floor_base
from planreader_floor_mapper_v131 import floor_mapper_editor as zoomable_floor_mapper_editor

VERSION = "1.3.1"
SETTING_KEY = "substrate_qa_v131"
STATUS_COLOURS = {
    "confirmed": "#2E8B57",
    "probable": "#D7A21B",
    "needs_check": "#D4553D",
    "conflict": "#B33A3A",
    "unreviewed": "#8993A1",
}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _page_groups(app: Any, workspace_id: int) -> Dict[str, List[Dict[str, Any]]]:
    pages = app.lquery(
        "SELECT p.*,d.file_name FROM pages p JOIN documents d ON d.id=p.document_id "
        "WHERE p.workspace_id=? AND p.selected=1 ORDER BY p.page_no,p.id",
        (int(workspace_id),),
    )
    result = {"elevations": [], "artists": [], "support": []}
    for raw in pages:
        page = dict(raw)
        page_type = str(page.get("page_type") or "").lower()
        label = str(page.get("page_label") or "").lower()
        if "elevation" in page_type or "elevation" in label:
            result["elevations"].append(page)
        elif any(k in page_type for k in ("render", "artist", "impression", "perspective")):
            result["artists"].append(page)
        elif any(k in page_type for k in ("finishes schedule", "finish schedule", "specification")):
            result["support"].append(page)
    return result


def _schema() -> Dict[str, Any]:
    surface = {
        "type": "object",
        "properties": {
            "surface_id": {"type": "string"},
            "substrate_code": {"type": "string"},
            "substrate_name": {"type": "string"},
            "status": {"type": "string", "enum": ["confirmed", "probable", "needs_check", "conflict"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 100},
            "elevation_reference": {"type": "string"},
            "artist_reference": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["surface_id", "substrate_code", "substrate_name", "status", "confidence", "elevation_reference", "artist_reference", "reason"],
        "additionalProperties": False,
    }
    issue = {
        "type": "object",
        "properties": {
            "reference": {"type": "string"},
            "detail": {"type": "string"},
        },
        "required": ["reference", "detail"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "surfaces": {"type": "array", "items": surface},
            "conflicts": {"type": "array", "items": issue},
            "unmatched_elevation_items": {"type": "array", "items": issue},
        },
        "required": ["summary", "surfaces", "conflicts", "unmatched_elevation_items"],
        "additionalProperties": False,
    }


def _surface_inventory(masses: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    surfaces = surface3d.derive_surfaces(masses)
    result = []
    for item in surfaces:
        if item.get("face") == "bottom":
            continue
        result.append({
            "surface_id": str(item.get("surface_id") or ""),
            "mass_label": str(item.get("mass_label") or ""),
            "level_name": str(item.get("level_name") or ""),
            "face": str(item.get("face_label") or item.get("face") or ""),
            "area_m2": round(_num(item.get("area_m2")), 2),
            "current_finish": str(item.get("finish") or ""),
            "model_confidence": str(item.get("confidence") or ""),
            "currently_inferred_substrate": surface3d.infer_substrate(item.get("finish")),
        })
    return result


def _blocks_for_pages(groups: Dict[str, List[Dict[str, Any]]], inventory: Sequence[Dict[str, Any]]) -> List[Any]:
    blocks: List[Any] = [
        ("text", "CURRENT 3D SURFACE INVENTORY:\n" + json.dumps(list(inventory), indent=2))
    ]
    for page in groups.get("elevations") or []:
        blocks.append(("text", f"ELEVATION AUTHORITY: {page.get('file_name')} · {page.get('page_label')} · page {page.get('page_no')}\nEXTRACTED TEXT:\n{str(page.get('extracted_text') or '')[:10000]}"))
        path = Path(str(page.get("image_path") or ""))
        if path.exists():
            blocks.append(("image", str(path)))
    for page in groups.get("support") or []:
        blocks.append(("text", f"FINISH/SPEC SUPPORT: {page.get('file_name')} · {page.get('page_label')} · page {page.get('page_no')}\nEXTRACTED TEXT:\n{str(page.get('extracted_text') or '')[:10000]}"))
        path = Path(str(page.get("image_path") or ""))
        if path.exists():
            blocks.append(("image", str(path)))
    for page in groups.get("artists") or []:
        blocks.append(("text", f"ARTIST IMPRESSION — SECONDARY VISUAL EVIDENCE ONLY: {page.get('file_name')} · {page.get('page_label')} · page {page.get('page_no')}"))
        path = Path(str(page.get("image_path") or ""))
        if path.exists():
            blocks.append(("image", str(path)))
    return blocks


def reconcile_substrates(app: Any, workspace_id: int, api_key: str, model: str, provider: str) -> Dict[str, Any]:
    groups = _page_groups(app, workspace_id)
    if not groups["elevations"]:
        raise RuntimeError("No selected elevation pages were found. Classify/select the elevations first.")

    masses = [dict(row) for row in app.lquery("SELECT * FROM model_masses WHERE workspace_id=? ORDER BY z,id", (int(workspace_id),))]
    if not masses and groups["artists"]:
        render_data = app.run_ai_render_read(
            int(workspace_id), [int(p["id"]) for p in groups["artists"]], api_key, model, provider
        )
        app.apply_render_to_model(int(workspace_id), render_data, mode="merge")
        masses = [dict(row) for row in app.lquery("SELECT * FROM model_masses WHERE workspace_id=? ORDER BY z,id", (int(workspace_id),))]
    if not masses:
        raise RuntimeError("No 3D building geometry exists yet. Add a render/artist impression or build the 3D model first.")

    inventory = _surface_inventory(masses)
    prompt = """
You are the substrate QA engine for a professional painting take-off system.
Cross-reference the CURRENT 3D SURFACE INVENTORY against architectural ELEVATION images,
finish/specification support pages, and ARTIST IMPRESSIONS.

Evidence priority is strict:
1. Written elevation/finish/specification callouts are authoritative for substrate and finish.
2. Elevation linework and regions are the next strongest source.
3. Artist impressions/perspectives are secondary visual corroboration only.
4. Never infer a dimension from an artist impression and never allow an artist impression to override a drawing callout.
5. If elevation evidence and artist impression disagree, status must be conflict.
6. If a 3D face cannot be mapped confidently to a documented substrate, status must be needs_check.
7. probable is allowed only where the evidence is strong but not explicit.
8. confirmed requires clear drawing/schedule evidence for that face/substrate.
9. Return only surface_id values that exist in the supplied inventory.
10. Substrate codes should use drawing codes where visible (for example EC01, FC, RBL, SOF); otherwise use a concise stable code such as RENDER, FC, MASONRY, TIMBER, METAL, SOFFIT or OTHER.

The purpose is not to make the model look pretty. The purpose is to show an estimator exactly which whole-building faces still require checking.
"""
    data = app.run_ai_structured(
        provider, api_key, model, prompt, _blocks_for_pages(groups, inventory), _schema(), "substrate_qa_v131"
    )
    payload = {
        "version": VERSION,
        "generated_at": app.now_stamp(),
        "elevation_page_ids": [int(p["id"]) for p in groups["elevations"]],
        "artist_page_ids": [int(p["id"]) for p in groups["artists"]],
        "support_page_ids": [int(p["id"]) for p in groups["support"]],
        "result": data,
    }
    app.set_workspace_setting(int(workspace_id), SETTING_KEY, json.dumps(payload, separators=(",", ":")))
    return payload


def load_result(app: Any, workspace_id: int) -> Dict[str, Any]:
    raw = app.workspace_setting(int(workspace_id), SETTING_KEY, "{}")
    try:
        data = json.loads(str(raw or "{}"))
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def _qa_map(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    result = payload.get("result") if isinstance(payload, dict) else {}
    rows = result.get("surfaces") if isinstance(result, dict) else []
    return {str(row.get("surface_id") or ""): dict(row) for row in (rows or []) if row.get("surface_id")}


def build_qa_figure(app: Any, workspace_id: int, payload: Dict[str, Any]) -> go.Figure:
    masses = [dict(row) for row in app.lquery("SELECT * FROM model_masses WHERE workspace_id=? ORDER BY z,id", (int(workspace_id),))]
    surfaces = [s for s in surface3d.derive_surfaces(masses) if s.get("face") != "bottom"]
    qmap = _qa_map(payload)
    fig = go.Figure()
    for surface in surfaces:
        sid = str(surface.get("surface_id") or "")
        qa = qmap.get(sid) or {}
        status = str(qa.get("status") or "unreviewed")
        color = STATUS_COLOURS.get(status, STATUS_COLOURS["unreviewed"])
        points = surface.get("points") or []
        if len(points) != 4:
            continue
        hover = (
            f"<b>{surface.get('mass_label')}</b><br>{surface.get('face_label')}<br>"
            f"Area: {_num(surface.get('area_m2')):.2f} m²<br>"
            f"Substrate: {qa.get('substrate_name') or qa.get('substrate_code') or 'Not verified'}<br>"
            f"QA: {status.replace('_',' ').title()} · {_num(qa.get('confidence')):.0f}%<br>"
            f"Elevation: {qa.get('elevation_reference') or 'No direct reference'}<br>"
            f"Artist impression: {qa.get('artist_reference') or 'No corroboration'}<br>"
            f"{qa.get('reason') or 'Not reconciled yet'}<extra></extra>"
        )
        fig.add_trace(go.Mesh3d(
            x=[p[0] for p in points], y=[p[1] for p in points], z=[p[2] for p in points],
            i=[0, 0], j=[1, 2], k=[2, 3], color=color, opacity=0.92,
            flatshading=True, hovertemplate=hover, name=sid, showscale=False,
        ))
    fig.update_layout(
        margin=dict(l=0, r=0, t=35, b=0),
        height=720,
        showlegend=False,
        title="Whole-building substrate QA — green confirmed · amber probable · red check/conflict",
        scene=dict(aspectmode="data", xaxis_title="X", yaxis_title="Y", zaxis_title="Height"),
    )
    return fig


def _qa_panel(app: Any, workspace: Dict[str, Any], session_api_key: str = "", ai_provider: str = "OpenAI") -> None:
    workspace_id = int(workspace["id"])
    groups = _page_groups(app, workspace_id)
    masses = app.lquery("SELECT id FROM model_masses WHERE workspace_id=?", (workspace_id,))
    app.st.divider()
    app.st.markdown("### 🏢 Whole-building substrate QA render")
    app.st.caption(
        "Cross-checks elevation images against artist impressions and the current 3D building. "
        "Elevations/finish schedules control substrate decisions; artist impressions only corroborate appearance. "
        "Unclear or conflicting faces are highlighted for estimator checking."
    )
    c1, c2, c3 = app.st.columns(3)
    c1.metric("Elevation pages", len(groups["elevations"]))
    c2.metric("Artist impressions", len(groups["artists"]))
    c3.metric("3D masses", len(masses))

    ai_key = app.resolve_ai_key(ai_provider, session_api_key)
    model = app.default_ai_model(ai_provider)
    disabled = not bool(ai_key and groups["elevations"] and (masses or groups["artists"]))
    if app.st.button(
        "🏢 Build / refresh whole-building substrate QA",
        type="primary", use_container_width=True, disabled=disabled,
        key=f"substrate_qa_build_{workspace_id}",
    ):
        try:
            payload = app._run_with_progress(
                lambda: reconcile_substrates(app, workspace_id, ai_key, model, ai_provider),
                "Cross-referencing elevations with artist impressions and 3D faces",
            )
            app.st.session_state[f"substrate_qa_v131_{workspace_id}"] = payload
            app.st.success("Whole-building substrate QA refreshed. Red faces need checking; amber faces are probable; green faces have drawing-backed evidence.")
            app.st.rerun()
        except Exception as exc:
            app.st.error(f"Could not build substrate QA render: {app._ai_error_hint(exc) if hasattr(app, '_ai_error_hint') else exc}")

    payload = app.st.session_state.get(f"substrate_qa_v131_{workspace_id}") or load_result(app, workspace_id)
    if payload and (payload.get("result") or {}).get("surfaces"):
        fig = build_qa_figure(app, workspace_id, payload)
        app.st.plotly_chart(fig, use_container_width=True, key=f"substrate_qa_figure_{workspace_id}")
        rows = []
        for row in (payload.get("result") or {}).get("surfaces") or []:
            if str(row.get("status")) not in {"needs_check", "conflict", "probable"}:
                continue
            rows.append({
                "Surface": row.get("surface_id"),
                "Substrate": row.get("substrate_name") or row.get("substrate_code"),
                "Status": str(row.get("status") or "").replace("_", " ").title(),
                "Confidence": f"{_num(row.get('confidence')):.0f}%",
                "Elevation evidence": row.get("elevation_reference"),
                "Artist evidence": row.get("artist_reference"),
                "Why check": row.get("reason"),
            })
        if rows:
            app.st.markdown("**Faces requiring verification**")
            app.st.dataframe(app.pd.DataFrame(rows), use_container_width=True, hide_index=True)
        conflicts = (payload.get("result") or {}).get("conflicts") or []
        if conflicts:
            app.st.warning(f"{len(conflicts)} elevation / artist-impression conflict(s) found. These stay flagged until manually verified.")


def apply(app: Any) -> None:
    if getattr(app, "_pb_substrate_qa_v131_applied", False):
        return
    app._pb_substrate_qa_v131_applied = True

    # Upgrade the existing v1.2.7/v1.2.8 polygon workflow without replacing its
    # persistence or measurement calculations. The base panel resolves this global
    # at call time, so the new component inherits all existing saved polygon data.
    floor_base.floor_mapper_editor = zoomable_floor_mapper_editor

    base_quick_panel = quick3d.quick_build_panel

    def quick_panel_with_substrate_qa(app_obj: Any, workspace: Dict[str, Any], session_api_key: str = "", ai_provider: str = "OpenAI") -> None:
        base_quick_panel(app_obj, workspace, session_api_key, ai_provider)
        _qa_panel(app_obj, workspace, session_api_key, ai_provider)

    quick3d.quick_build_panel = quick_panel_with_substrate_qa
    app.reconcile_substrates_v131 = lambda workspace_id, api_key, model, provider="OpenAI": reconcile_substrates(app, workspace_id, api_key, model, provider)
    app.build_substrate_qa_figure_v131 = lambda workspace_id, payload: build_qa_figure(app, workspace_id, payload)
