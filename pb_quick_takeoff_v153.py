"""PlanReader v1.5.3 standalone scoped Quick Take-off mode.

Quick Take-off creates a normal local PlanReader workspace with jobhub_job_id=NULL.
It never creates or updates a JobHub job.  A scope gate is applied at AI import
and again around the Subscription Take-off page so a ceilings-only workspace
cannot silently accumulate unrelated painting rows.
"""
from __future__ import annotations

import copy
import csv
import io
import json
import shutil
from typing import Any, Dict, Iterable, List

SETTING_MODE = "quick_takeoff_mode_v153"
SETTING_SCOPE = "quick_takeoff_scope_v153"

SCOPES = {
    "ceilings": "Ceilings only",
    "internal_walls": "Internal walls only",
    "doors": "Doors / frames only",
    "external": "External walls / cladding only",
    "soffits": "Soffits / eaves only",
    "full": "Full painting take-off",
}


def _text(row: Dict[str, Any], *keys: str) -> str:
    return " ".join(str(row.get(key) or "") for key in keys).lower()


def row_matches_scope(row: Dict[str, Any], scope: str) -> bool:
    """Return True when a take-off row belongs to the selected Quick scope.

    Ceilings deliberately keys off the element/finish/substrate rather than the
    section alone.  A section named 'Internal walls and ceilings' must not cause
    an Internal walls row to leak into a ceilings-only take-off.
    """
    scope = str(scope or "full")
    if scope == "full":
        return True
    element_finish = _text(row, "element", "finish_system", "substrate")
    element = _text(row, "element")
    section_element = _text(row, "section", "element", "substrate", "finish_system")
    if scope == "ceilings":
        return "ceiling" in element_finish and not any(token in element for token in ("wall", "door", "frame"))
    if scope == "internal_walls":
        return "wall" in element and "external" not in section_element and "cladding" not in section_element
    if scope == "doors":
        return any(token in section_element for token in ("door", "frame", "architrave", "jamb"))
    if scope == "external":
        return any(token in section_element for token in ("external", "cladding", "render", "facade", "façade")) and not any(
            token in section_element for token in ("soffit", "eave")
        )
    if scope == "soffits":
        return any(token in section_element for token in ("soffit", "eave"))
    return True


def filter_ai_payload(payload: Dict[str, Any], scope: str) -> Dict[str, Any]:
    """Copy an AI result and retain only the requested commercial scope."""
    result = copy.deepcopy(dict(payload or {}))
    result["takeoff_rows"] = [
        dict(row) for row in (result.get("takeoff_rows") or [])
        if row_matches_scope(dict(row), scope)
    ]
    if str(scope or "full") != "full":
        # Quick scoped take-offs are measurement schedules, not hidden partial
        # 3D/opening/register imports.  Those can be created later only if the
        # estimator explicitly switches to a full job workflow.
        result["register_items"] = []
        result["model_masses"] = []
        result["model_openings"] = []
    return result


def _is_quick(app: Any, workspace_id: int) -> bool:
    return str(app.workspace_setting(int(workspace_id), SETTING_MODE, "")) == "1"


def _scope(app: Any, workspace_id: int) -> str:
    value = str(app.workspace_setting(int(workspace_id), SETTING_SCOPE, "full") or "full")
    return value if value in SCOPES else "full"


def prune_workspace_rows(app: Any, workspace_id: int, scope: str) -> int:
    """Remove out-of-scope take-off rows and their mapped line attachments."""
    if scope == "full":
        return 0
    rows = [dict(r) for r in app.lquery("SELECT * FROM takeoff_rows WHERE workspace_id=?", (int(workspace_id),))]
    remove_ids = [int(r["id"]) for r in rows if not row_matches_scope(r, scope)]
    for row_id in remove_ids:
        app.lexecute("DELETE FROM measurement_lines WHERE workspace_id=? AND takeoff_row_id=?", (int(workspace_id), row_id))
        app.lexecute("DELETE FROM takeoff_rows WHERE workspace_id=? AND id=?", (int(workspace_id), row_id))
    return len(remove_ids)


def _csv_bytes(rows: Iterable[Dict[str, Any]]) -> bytes:
    rows = [dict(r) for r in rows]
    if not rows:
        return b""
    preferred = [
        "section", "element", "location", "substrate", "finish_system", "quantity", "unit",
        "quantity_status", "source_page", "source_reference", "inclusion_status", "confidence", "notes",
    ]
    fields = [name for name in preferred if any(name in row for row in rows)]
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8-sig")


def apply(app: Any) -> None:
    if getattr(app, "_pb_quick_takeoff_v153_applied", False):
        return

    base_selector = app.sidebar_workspace_selector
    base_import_ai = app.import_ai_result
    base_subscription = app.subscription_takeoff_page

    def selector_with_quick(bridge):
        with app.st.sidebar.expander("⚡ Quick Take-off", expanded=False):
            app.st.caption("Standalone measurement only — no JobHub job is created.")
            scope = app.st.selectbox(
                "Quick scope",
                list(SCOPES.keys()),
                format_func=lambda key: SCOPES[key],
                key="pb_quick_scope_new_v153",
            )
            name = app.st.text_input("Name (optional)", value="", key="pb_quick_name_v153")
            if app.st.button("Start Quick Take-off", type="primary", use_container_width=True, key="pb_quick_start_v153"):
                label = str(name or "").strip() or SCOPES[scope]
                wid = app.create_standalone_workspace("", f"Quick Take-off · {label}", "", "")
                app.set_workspace_setting(wid, SETTING_MODE, "1")
                app.set_workspace_setting(wid, SETTING_SCOPE, scope)
                app.st.session_state["workspace_id"] = int(wid)
                app.st.rerun()

        wid = base_selector(bridge)
        if wid and _is_quick(app, int(wid)):
            current_scope = _scope(app, int(wid))
            app.st.sidebar.success(f"Quick Take-off · {SCOPES[current_scope]}")
            if app.st.sidebar.button("Delete this Quick Take-off", use_container_width=True, key=f"pb_quick_delete_{wid}"):
                try:
                    path = app.workspace_path(int(wid))
                except Exception:
                    path = None
                app.lexecute("DELETE FROM workspaces WHERE id=? AND jobhub_job_id IS NULL", (int(wid),))
                if path:
                    shutil.rmtree(path, ignore_errors=True)
                app.st.session_state.pop("workspace_id", None)
                app.st.rerun()
        return wid

    def scoped_import_ai(workspace_id: int, data: Dict[str, Any]):
        if _is_quick(app, int(workspace_id)):
            data = filter_ai_payload(data, _scope(app, int(workspace_id)))
        return base_import_ai(workspace_id, data)

    def subscription_with_scope(workspace: Dict[str, Any], session_api_key: str, ai_provider: str):
        wid = int(workspace["id"])
        if not _is_quick(app, wid):
            return base_subscription(workspace, session_api_key, ai_provider)

        scope = _scope(app, wid)
        prune_workspace_rows(app, wid, scope)
        app.st.info(
            f"⚡ Quick Take-off: **{SCOPES[scope]}**. This is a standalone PlanReader workspace and is not linked to JobHub."
        )
        cols = app.st.columns([1.4, 1.0])
        with cols[0]:
            selected = app.st.selectbox(
                "Take-off scope",
                list(SCOPES.keys()),
                index=list(SCOPES.keys()).index(scope),
                format_func=lambda key: SCOPES[key],
                key=f"pb_quick_scope_existing_{wid}",
            )
            if selected != scope:
                app.set_workspace_setting(wid, SETTING_SCOPE, selected)
                prune_workspace_rows(app, wid, selected)
                app.st.rerun()
        rows_now = [dict(r) for r in app.lquery("SELECT * FROM takeoff_rows WHERE workspace_id=? ORDER BY id", (wid,))]
        with cols[1]:
            app.st.download_button(
                "Download quick take-off CSV",
                data=_csv_bytes(rows_now),
                file_name=f"quick_takeoff_{scope}.csv",
                mime="text/csv",
                disabled=not rows_now,
                use_container_width=True,
                key=f"pb_quick_csv_{wid}",
            )

        result = base_subscription(workspace, session_api_key, ai_provider)
        removed = prune_workspace_rows(app, wid, _scope(app, wid))
        if removed:
            app.st.caption(f"Quick scope guard removed {removed} out-of-scope row(s).")
        return result

    app.sidebar_workspace_selector = selector_with_quick
    app.import_ai_result = scoped_import_ai
    app.subscription_takeoff_page = subscription_with_scope
    app._pb_quick_takeoff_v153_applied = True
