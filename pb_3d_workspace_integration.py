"""
PlanReader 3D Canonical Workspace Integration Module.

Provides the apply(app) extension hook connecting the approved canonical 3D BIM model
and Three.js WebGL viewer directly into the actual PlanReader workspace application flow
(pb_planreader_v133_app.py).

ARCHITECTURE GUARANTEES:
1. Smallest safe hook architecture (uses existing apply(app) extension pattern with idempotency guard).
2. Preserves and executes original app.model_3d_page while embedding the canonical 3D WebGL viewer.
3. Converts workspace evidence automatically via planreader_workspace_to_canonical().
4. Safe canonical caching in Streamlit session_state keyed by (workspace_id, snapshot_fingerprint, schema_version).
5. Integrates persistence (pb_canonical_persistence.py) and diagnostics (pb_3d_diagnostics.py).
"""

import streamlit as st
import streamlit.components.v1 as components
from typing import Any, Dict, Optional
from pb_production_3d_adapter import planreader_workspace_to_canonical, require_workspace_id
from pb_bim_viewer import project_to_viewer_payload, generate_bim_viewer_html
from pb_canonical_persistence import load_workspace_canonical_model, save_workspace_canonical_model
from pb_3d_diagnostics import generate_production_diagnostics_report


def render_workspace_3d_canonical_view(app: Any, workspace: Any = None) -> None:
    """
    SECTION L, J, P, R: Renders the approved canonical 3D WebGL BIM viewer inside the active PlanReader workspace view.
    """
    try:
        workspace_id = None
        if isinstance(workspace, dict):
            workspace_id = workspace.get("id")
        elif isinstance(workspace, (int, str, float)):
            workspace_id = workspace
        elif hasattr(app, "current_workspace"):
            try:
                curr = app.current_workspace() if callable(app.current_workspace) else app.current_workspace
                if isinstance(curr, dict):
                    workspace_id = curr.get("id")
            except Exception:
                pass

        # SECTION 3 & 4: Require valid active workspace ID!
        wid_int = require_workspace_id(workspace_id)
    except Exception as e:
        st.error(f"Workspace Context Error: {e}")
        return

    # Load canonical project & diagnostics from workspace evidence snapshot v3
    try:
        ws_result = planreader_workspace_to_canonical(app, wid_int)
        project = ws_result.project
        snapshot = ws_result.snapshot
        snapshot_fp = ws_result.snapshot_fingerprint
        diagnostics = ws_result.diagnostics
    except Exception as e:
        st.error(f"Error building 3D canonical model for workspace #{wid_int}: {e}")
        return

    st.markdown("### 🏗️ Canonical 3D WebGL BIM Model")

    if project.is_synthetic_demo:
        st.warning("⚠️ SYNTHETIC VIEWER DEMONSTRATION FIXTURE — NOT BENCHMARK TRUTH / NOT TAKEOFF AUTHORITATIVE")

    # SECTION J & 43: Initial Persistence & Refresh Lifecycle
    is_fresh, saved_proj, status_msg, saved_payload = load_workspace_canonical_model(app, wid_int, current_snapshot=snapshot)
    if saved_payload is None:
        save_workspace_canonical_model(app, wid_int, project, snapshot=snapshot)
        is_fresh = True  # SECTION 43: Immediately fresh on initial save!
        st.caption("ℹ️ Initial model saved to workspace 3D persistence store (`canonical_3d_model_v1`).")
    elif not is_fresh:
        st.warning(status_msg)
        if st.button("🔄 Refresh 3D Model from Source Evidence", key=f"refresh_3d_model_{wid_int}"):
            save_workspace_canonical_model(app, wid_int, project, snapshot=snapshot)
            st.success("Model refreshed and saved to workspace persistence store.")
            st.rerun()

    # SECTION 44 & 45: Safe Canonical Caching in Streamlit Session State
    if "_CANONICAL_MODEL_SESSION_CACHE" not in st.session_state:
        st.session_state["_CANONICAL_MODEL_SESSION_CACHE"] = {}

    cache_dict = st.session_state["_CANONICAL_MODEL_SESSION_CACHE"]
    cache_key = (wid_int, snapshot_fp, "1.0.0")

    if cache_key in cache_dict:
        html_code = cache_dict[cache_key]
    else:
        payload = project_to_viewer_payload(project)
        html_code = generate_bim_viewer_html(payload, height_px=750)
        cache_dict[cache_key] = html_code

    # Render WebGL Viewer Component
    components.html(html_code, height=760, scrolling=False)

    # SECTION P & 57: Expanded Estimator QA Summary Diagnostics Panel
    with st.expander("📊 Estimator QA Summary & Quantity Reconciliation", expanded=False):
        qa = diagnostics.get("estimator_qa_summary", {})
        st.caption(f"Source Fingerprint Revision: `{snapshot_fp}` | Status: {'Fresh' if is_fresh else 'Stale'}")
        
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
    SECTION L & 62: Applies the Phase 5 3D Canonical Viewer extension hook to the PlanReader application instance.
    Idempotency guard: calling apply(app) twice will NOT wrap model_3d_page twice.
    """
    if getattr(app, "_canonical_3d_extension_installed", False):
        return  # Already installed!

    if hasattr(app, "model_3d_page"):
        orig_model_3d = getattr(app, "model_3d_page")

        def model_3d_page_canonical_wrapper(workspace: Any = None, *args, **kwargs):
            # 1. Render original reconstruction/model page (do NOT swallow exceptions!)
            if callable(orig_model_3d):
                orig_model_3d(workspace, *args, **kwargs)
            
            st.markdown("---")
            # 2. Render approved canonical 3D WebGL BIM viewer
            render_workspace_3d_canonical_view(app, workspace)

        setattr(app, "model_3d_page", model_3d_page_canonical_wrapper)
        setattr(app, "_canonical_3d_extension_installed", True)
