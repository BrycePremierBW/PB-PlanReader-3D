"""
PlanReader 3D Canonical Workspace Integration Module.

Provides the apply(app) extension hook connecting the approved canonical 3D BIM model
and Three.js WebGL viewer directly into the actual PlanReader workspace application flow
(pb_planreader_v133_app.py).

ARCHITECTURE GUARANTEES:
1. Smallest safe hook architecture (uses existing apply(app) extension pattern).
2. Preserves and executes original app.model_3d_page while embedding the canonical 3D WebGL viewer.
3. Converts workspace evidence automatically via planreader_workspace_to_canonical().
4. Integrates persistence (pb_canonical_persistence.py) and diagnostics (pb_3d_diagnostics.py).
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
    SECTION L & J: Renders the approved canonical 3D WebGL BIM viewer inside the active PlanReader workspace view.
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

    # Load canonical project from workspace evidence snapshot
    try:
        project, diagnostics = planreader_workspace_to_canonical(app, workspace_id)
    except Exception as e:
        st.error(f"Error building 3D canonical model for workspace #{workspace_id}: {e}")
        return

    st.markdown("### 🏗️ Canonical 3D WebGL BIM Model")

    if project.is_synthetic_demo:
        st.warning("⚠️ SYNTHETIC VIEWER DEMONSTRATION FIXTURE — NOT BENCHMARK TRUTH / NOT TAKEOFF AUTHORITATIVE")

    # SECTION J: Initial Persistence & Refresh Lifecycle
    is_fresh, saved_proj, status_msg, saved_payload = load_workspace_canonical_model(app, workspace_id)
    if saved_payload is None:
        save_workspace_canonical_model(app, workspace_id, project)
        st.caption("ℹ️ Model saved to workspace 3D persistence store (`canonical_3d_model_v1`).")
    elif "Stale" in status_msg:
        st.warning(status_msg)
        if st.button("🔄 Refresh 3D Model from Source Evidence", key=f"refresh_3d_model_{workspace_id}"):
            save_workspace_canonical_model(app, workspace_id, project)
            st.success("Model refreshed and saved to workspace persistence store.")
            st.rerun()

    # Render WebGL Viewer Component
    payload = project_to_viewer_payload(project)
    html_code = generate_bim_viewer_html(payload, height_px=750)
    components.html(html_code, height=760, scrolling=False)

    # SECTION O: Estimator QA Summary Diagnostics Panel
    with st.expander("📊 Estimator QA Summary & Quantity Reconciliation", expanded=False):
        qa = diagnostics.get("estimator_qa_summary", {})
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Physical Walls Rendered", qa.get("physical_walls_rendered", 0))
        col2.metric("Physical Openings Rendered", qa.get("physical_openings", 0))
        col3.metric("B5 Authorised Deductions", qa.get("authorised_b5_deductions", 0))
        col4.metric("Calibrated Floors", qa.get("calibrated_floors", 0))
        
        st.markdown("#### Per-Wall Quantity Reconciliation")
        st.dataframe(diagnostics.get("per_wall_quantity_reconciliation", []))
        
        with st.expander("Technical Diagnostics Data", expanded=False):
            st.json(diagnostics)


def apply(app: Any) -> None:
    """
    SECTION L: Applies the Phase 5 3D Canonical Viewer extension hook to the PlanReader application instance.
    Wraps app.model_3d_page while PRESERVING and EXECUTING the original 3D page callable!
    """
    if hasattr(app, "model_3d_page"):
        orig_model_3d = getattr(app, "model_3d_page")

        def model_3d_page_canonical_wrapper(workspace: Any = None, *args, **kwargs):
            # 1. Render original reconstruction/model page
            if callable(orig_model_3d):
                try:
                    orig_model_3d(workspace, *args, **kwargs)
                except Exception as e:
                    st.warning(f"Original 3D page notification: {e}")
            
            st.markdown("---")
            # 2. Render approved canonical 3D WebGL BIM viewer
            render_workspace_3d_canonical_view(app, workspace)

        setattr(app, "model_3d_page", model_3d_page_canonical_wrapper)
        setattr(app, "_canonical_3d_extension_installed", True)
