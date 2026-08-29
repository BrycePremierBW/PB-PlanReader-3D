"""
PlanReader 3D Canonical Workspace Integration Module.

Provides the apply(app) extension hook connecting the approved canonical 3D BIM model
and Three.js WebGL viewer directly into the actual PlanReader workspace application flow
(pb_planreader_v133_app.py).

ARCHITECTURE GUARANTEES:
1. Smallest safe hook architecture (uses existing apply(app) extension pattern).
2. Extends app.model_3d_page so opening a workspace -> 3D model page displays canonical WebGL viewer.
3. Converts workspace evidence automatically via planreader_workspace_to_canonical().
4. Integrates persistence (pb_canonical_persistence.py) and diagnostics (pb_3d_diagnostics.py).
5. Zero second measurement engine; zero modification to B3 active correction branches.
"""

import streamlit as st
import streamlit.components.v1 as components
from typing import Any, Dict, Optional
from pb_production_3d_adapter import planreader_workspace_to_canonical
from pb_bim_viewer import project_to_viewer_payload, generate_bim_viewer_html
from pb_canonical_persistence import load_workspace_canonical_model, save_workspace_canonical_model
from pb_3d_diagnostics import generate_production_diagnostics_report


def render_workspace_3d_canonical_view(app: Any, workspace: Any = None) -> None:
    """
    Renders the approved canonical 3D WebGL BIM viewer inside the active PlanReader workspace view.
    """
    workspace_id = 1
    if isinstance(workspace, dict):
        workspace_id = workspace.get("id", 1)
    elif isinstance(workspace, (int, str)) and str(workspace).isdigit():
        workspace_id = int(workspace)
    elif hasattr(app, "current_workspace"):
        try:
            curr = app.current_workspace() if callable(app.current_workspace) else app.current_workspace
            if isinstance(curr, dict):
                workspace_id = curr.get("id", 1)
        except Exception:
            pass

    # Load canonical project from workspace evidence
    try:
        project, diagnostics = planreader_workspace_to_canonical(app, workspace_id)
    except Exception as e:
        st.error(f"Error building 3D canonical model from workspace #{workspace_id}: {e}")
        return

    st.subheader(f"3D Model: {project.name}")

    if project.is_synthetic_demo:
        st.warning("⚠️ SYNTHETIC VIEWER DEMONSTRATION FIXTURE — NOT BENCHMARK TRUTH / NOT TAKEOFF AUTHORITATIVE")

    # SECTION 8: Persistence and Staleness Check
    is_fresh, saved_proj, status_msg, saved_payload = load_workspace_canonical_model(app, workspace_id, current_workspace_data=workspace if isinstance(workspace, dict) else None)
    if "Stale" in status_msg:
        st.warning(status_msg)
        if st.button("🔄 Refresh 3D Model from Source Evidence", key=f"refresh_3d_model_{workspace_id}"):
            save_workspace_canonical_model(app, workspace_id, project, workspace_data=workspace if isinstance(workspace, dict) else None)
            st.success("Model refreshed and saved to workspace persistence store.")
            st.rerun()

    # Render WebGL Viewer Component
    payload = project_to_viewer_payload(project)
    html_code = generate_bim_viewer_html(payload, height_px=750)
    components.html(html_code, height=760, scrolling=False)

    # Render Production Diagnostics Panel under expander
    with st.expander("🔍 Production 3D Model Diagnostics & Per-Wall Quantity Reconciliation", expanded=False):
        st.json(diagnostics)


def apply(app: Any) -> None:
    """
    Applies the Phase 5 3D Canonical Viewer extension hook to the PlanReader application instance.
    Wraps app.model_3d_page so production users see the canonical WebGL viewer directly.
    """
    if hasattr(app, "model_3d_page"):
        orig_model_3d = getattr(app, "model_3d_page")

        def model_3d_page_canonical_wrapper(workspace: Any = None, *args, **kwargs):
            render_workspace_3d_canonical_view(app, workspace)

        setattr(app, "model_3d_page", model_3d_page_canonical_wrapper)
        setattr(app, "_canonical_3d_extension_installed", True)
