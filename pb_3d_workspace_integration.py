"""
PlanReader 3D Canonical Workspace Integration Module.

Provides the apply(app) extension hook connecting the approved canonical 3D BIM model
and Three.js WebGL viewer directly into the actual PlanReader workspace application flow.

ARCHITECTURE GUARANTEES:
1. Smallest safe hook architecture (uses existing apply(app) extension pattern).
2. Converts workspace evidence automatically via planreader_workspace_to_canonical().
3. Connects to versioned persistence (pb_canonical_persistence.py) and diagnostics (pb_3d_diagnostics.py).
4. Zero second measurement engine; zero modification to B3 active correction branches.
"""

import streamlit as st
import streamlit.components.v1 as components
from typing import Any, Dict, Optional
from pb_production_3d_adapter import planreader_workspace_to_canonical
from pb_bim_viewer import project_to_viewer_payload, generate_bim_viewer_html
from pb_canonical_persistence import save_canonical_project_to_dict, load_canonical_project_from_dict
from pb_3d_diagnostics import generate_production_diagnostics_report


def render_workspace_3d_canonical_view(app: Any, workspace_id: Optional[int] = None) -> None:
    """
    Renders the approved canonical 3D WebGL BIM viewer inside the active PlanReader workspace view.
    """
    try:
        project, diagnostics = planreader_workspace_to_canonical(app, workspace_id)
    except Exception as e:
        st.error(f"Error building 3D canonical model from workspace: {e}")
        return

    st.subheader(f"3D Model: {project.name}")

    if project.is_synthetic_demo:
        st.warning("⚠️ SYNTHETIC VIEWER DEMONSTRATION FIXTURE — NOT BENCHMARK TRUTH / NOT TAKEOFF AUTHORITATIVE")

    # Render Viewer Component
    payload = project_to_viewer_payload(project)
    html_code = generate_bim_viewer_html(payload, height_px=750)
    components.html(html_code, height=760, scrolling=False)

    # Render Production Diagnostics Panel under expander
    with st.expander("🔍 Production 3D Model Diagnostics & Quantity Reconciliation", expanded=False):
        st.json(diagnostics)


def apply(app: Any) -> None:
    """
    Applies the Phase 5 3D Canonical Viewer extension hook to the PlanReader application instance.
    """
    if hasattr(app, "register_3d_viewer_hook"):
        app.register_3d_viewer_hook(render_workspace_3d_canonical_view)
