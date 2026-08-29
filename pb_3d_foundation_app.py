"""
PlanReader Interactive 3D Model Foundation Streamlit Application.

Standalone application demonstrating the 3D Canonical Building Model,
Geometry Services, Synthetic Demonstration Model fixture, and modern
commercial BIM viewer.
"""

import json
import streamlit as st
import streamlit.components.v1 as components
from pb_canonical_building import CanonicalProject, ReviewState
from pb_geometry_services import model_bounds, potential_net_wall_area, wall_length, wall_gross_area
from pb_synthetic_3d_fixture import get_synthetic_viewer_demo_model
from pb_bim_viewer import project_to_viewer_payload, generate_bim_viewer_html


def render_3d_foundation_page():
    st.set_page_config(
        page_title="PlanReader 3D BIM Viewer",
        page_icon="🏗️",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.sidebar.title("🏗️ PlanReader 3D BIM")
    st.sidebar.caption("Canonical Building Geometry Engine v1.0")

    demo_mode = st.sidebar.radio(
        "Select Model Source",
        ["Synthetic Demonstration Model", "Upload Canonical JSON"],
        index=0,
    )

    if demo_mode == "Synthetic Demonstration Model":
        project = get_synthetic_viewer_demo_model()
        st.sidebar.success("Loaded Synthetic Demonstration Fixture")
    else:
        uploaded_file = st.sidebar.file_uploader("Upload Canonical Project JSON", type=["json"])
        if uploaded_file:
            try:
                json_str = uploaded_file.read().decode("utf-8")
                project = CanonicalProject.from_json(json_str)
                st.sidebar.success("Successfully loaded uploaded canonical model")
            except Exception as e:
                st.sidebar.error(f"Error loading JSON: {e}")
                project = get_synthetic_viewer_demo_model()
        else:
            project = get_synthetic_viewer_demo_model()

    # Main Header & Conditional Demo Warning
    st.title("PlanReader Interactive 3D Model")
    
    if getattr(project, "is_synthetic_demo", False):
        st.warning("⚠️ **SYNTHETIC VIEWER DEMONSTRATION — NOT BENCHMARK TRUTH**")

    # Generate 3D Viewer HTML Payload
    payload = project_to_viewer_payload(project)
    html_code = generate_bim_viewer_html(payload, height_px=750)

    # Render Interactive 3D BIM Viewer Component
    components.html(html_code, height=760, scrolling=False)

    # Model Summary Expanders (Technical / Advanced / Diagnostics)
    st.markdown("---")
    col1, col2, col3 = st.columns(3)
    
    bounds_ok, bounds = model_bounds(project)
    with col1:
        st.metric("Project Name", project.name)
        st.metric("Buildings", len(project.buildings))
    with col2:
        total_levels = sum(len(b.levels) for b in project.buildings)
        st.metric("Total Levels", total_levels)
        if bounds_ok and bounds and bounds.min_point.x is not None and bounds.max_point.x is not None:
            st.metric("Global Bounds Width (X)", f"{bounds.max_point.x - bounds.min_point.x:.1f} m")
        else:
            st.metric("Global Bounds Width (X)", "Not Available")
    with col3:
        total_walls = sum(len(lvl.walls) for b in project.buildings for lvl in b.levels)
        st.metric("Total Walls", total_walls)
        if bounds_ok and bounds and bounds.min_point.z is not None and bounds.max_point.z is not None:
            st.metric("Global Bounds Height (Z)", f"{bounds.max_point.z - bounds.min_point.z:.1f} m")
        else:
            st.metric("Global Bounds Height (Z)", "Not Available")

    with st.expander("📋 Model Inspection & Export Data (Canonical Object Graph)"):
        st.json(payload)


if __name__ == "__main__":
    render_3d_foundation_page()
