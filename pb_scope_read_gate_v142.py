"""PlanReader v1.4.2 scope UI gate for the simplified Read Project step."""
from __future__ import annotations

from typing import Any, Dict


def _valid_scope(state: Dict[str, Any]) -> bool:
    if not state.get("enabled"):
        return True
    return any(str(mode) == "Included" for mode in (state.get("groups") or {}).values())


def apply(app: Any) -> None:
    if getattr(app, "_pb_scope_read_gate_v142_applied", False):
        return
    app._pb_scope_read_gate_v142_applied = True
    base_read = app.subscription_takeoff_page

    def _read_with_scope(workspace, session_api_key="", ai_provider="OpenAI"):
        app.project_scope_page(workspace)
        state = app.project_scope_state(int(workspace["id"])) if hasattr(app, "project_scope_state") else {"enabled": False}
        app.st.divider()
        if not _valid_scope(state):
            app.st.warning("Save at least one Included building/block before PlanReader reads this project.")
            return None
        if state.get("enabled"):
            app.st.success(f"Active measurement scope: {app.project_scope_summary(int(workspace['id']))}")
            app.st.caption("Reference-only and excluded buildings can still provide document context, but their floor polygons do not enter reconstruction or measured quantities.")
        else:
            app.st.info("Project scope is set to Whole project. Turn on the scope limiter above for a Block/Building-only tender.")
        return base_read(workspace, session_api_key, ai_provider)

    app.subscription_takeoff_page = _read_with_scope
    app.valid_project_scope_v142 = _valid_scope
