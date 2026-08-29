"""
PlanReader 3D Foundation Streamlit Web Application.

Provides a live interactive WebGL 3D BIM Viewer interface supporting both
real production project payloads (via pb_production_3d_adapter) and the synthetic
demonstration fixture (for visual testing).

Features:
- Real PlanReader Production Project Upload & Workspace Processing
- Empty State Handling ("No production project loaded")
- Fail-Closed Synthetic Demo Fixture Warning Badge
- Interactive WebGL 3D BIM Viewer (Three.js)
- End-to-End Drawing Provenance & Evidence Trace Panels
- Level Extents & Deductions Diagnostic Breakdown
"""

import json
import streamlit as st
import streamlit.components.v1 as components
from pb_canonical_building import CanonicalProject, parse_strict_bool
from pb_synthetic_3d_fixture import get_synthetic_viewer_demo_model
from pb_production_3d_adapter import planreader_to_canonical_model
from pb_bim_viewer import project_to_viewer_payload, generate_bim_viewer_html
from pb_geometry_services import model_bounds

st.set_page_config(
    page_title="PlanReader 3D BIM Viewer",
    page_icon="🏗️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.sidebar.title("PlanReader 3D Controls")
st.sidebar.markdown("---")

data_source = st.sidebar.radio(
    "Data Source Selection",
    ["Real Production Project (Upload / Payload)", "Synthetic Demo Fixture (Testing Only)"],
    index=0,
)

current_project = None

if data_source == "Synthetic Demo Fixture (Testing Only)":
    current_project = get_synthetic_viewer_demo_model()
    st.sidebar.info("Using Synthetic Viewer Demo Fixture. Takeoff eligibility is disabled.")
else:
    st.sidebar.subheader("Production Project Upload")
    uploaded_file = st.sidebar.file_uploader("Upload PlanReader Production JSON Output", type=["json"])
    
    if uploaded_file is not None:
        try:
            prod_payload = json.load(uploaded_file)
            current_project = planreader_to_canonical_model(prod_payload, trusted_source=False)
            st.sidebar.success(f"Loaded Production Project: {current_project.name}")
        except Exception as e:
            st.sidebar.error(f"Error parsing production JSON: {e}")

# SECTION I: Remove fake production examples!
if current_project is None:
    st.title("PlanReader 3D BIM Viewer")
    st.info("ℹ️ No production project loaded.")
    st.markdown("""
    ### Getting Started
    - **To view a real project**: Upload an authoritative PlanReader JSON production payload using the file uploader in the sidebar, or select an active workspace.
    - **For visual interface testing**: Switch to **Synthetic Demo Fixture (Testing Only)** in the sidebar mode selector.
    """)
else:
    # Banner Warning: Synthetic Demo Fixture Warning
    if current_project.is_synthetic_demo:
        st.warning("⚠️ SYNTHETIC VIEWER DEMONSTRATION FIXTURE — NOT BENCHMARK TRUTH / NOT TAKEOFF AUTHORITATIVE")

    st.title(f"3D Model: {current_project.name}")

    bounds_ok, bounds = model_bounds(current_project)
    if bounds_ok and bounds is not None:
        st.caption(f"Global 3D Bounding Extents: Min({bounds.min_point.x:.1f}, {bounds.min_point.y:.1f}, {bounds.min_point.z:.1f}) → Max({bounds.max_point.x:.1f}, {bounds.max_point.y:.1f}, {bounds.max_point.z:.1f})")

    # Generate Three.js 3D WebGL Viewer Component
    viewer_payload = project_to_viewer_payload(current_project)
    html_code = generate_bim_viewer_html(viewer_payload, height_px=750)

    components.html(html_code, height=760, scrolling=False)
