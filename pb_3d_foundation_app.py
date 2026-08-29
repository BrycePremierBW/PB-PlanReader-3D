"""
PlanReader 3D Foundation Streamlit Web Application.

Provides a live interactive WebGL 3D BIM Viewer interface supporting both
real production project payloads (via pb_production_3d_adapter) and the synthetic
demonstration fixture (for visual testing).

Features:
- Real PlanReader Production Project Upload & Processing
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
            current_project = planreader_to_canonical_model(prod_payload)
            st.sidebar.success(f"Loaded Production Project: {current_project.name}")
        except Exception as e:
            st.sidebar.error(f"Error parsing production JSON: {e}")

    if current_project is None:
        # Default production example project structure for quick inspection
        example_prod_payload = {
            "project_id": "proj_prod_commercial_001",
            "project_name": "Commercial Office Tower Project",
            "is_synthetic_demo": False,
            "levels": [
                {"id": "lvl_g", "name": "Ground Lobby", "elevation_m": 0.0, "height_m": 3.5, "level_index": 0},
                {"id": "lvl_1", "name": "Level 1 Office", "elevation_m": 3.5, "height_m": 3.0, "level_index": 1},
            ],
            "walls": [
                {
                    "id": "wall_g_curtain",
                    "name": "Ground Lobby Main Curtain Wall",
                    "level_id": "lvl_g",
                    "start_point": {"x": 0.0, "y": 0.0},
                    "end_point": {"x": 15.0, "y": 0.0},
                    "height_m": 3.5,
                    "thickness_m": 0.25,
                    "is_external": True,
                    "substrate": "Structural Glazing",
                    "finish": "Clear Anodised Aluminium",
                    "confidence": 0.95,
                    "provenance": {"source_pdf": "A101_Lobby_Plan.pdf", "page_number": 2, "drawing_id": "A101"},
                },
                {
                    "id": "wall_l1_ext",
                    "name": "Level 1 South Exterior Facade Wall",
                    "level_id": "lvl_1",
                    "start_point": {"x": 0.0, "y": 0.0},
                    "end_point": {"x": 15.0, "y": 0.0},
                    "height_m": 3.0,
                    "thickness_m": 0.20,
                    "is_external": True,
                    "substrate": "Precast Concrete Panel",
                    "finish": "Architectural Paint",
                    "confidence": 0.90,
                    "provenance": {"source_pdf": "A102_Level1_Plan.pdf", "page_number": 3, "drawing_id": "A102"},
                },
            ],
            "openings": [
                {
                    "id": "door_g_entry",
                    "name": "Lobby Main Entrance Door",
                    "wall_id": "wall_g_curtain",
                    "opening_type": "DOOR",
                    "offset_along_wall_m": 5.0,
                    "sill_height_m": 0.0,
                    "width_m": 2.4,
                    "height_m": 2.8,
                    "mark": "D-01",
                    "is_authorised_deduction": True,
                    "provenance": {"source_pdf": "SCH01_DoorSchedule.pdf", "page_number": 8, "drawing_id": "SCH01"},
                },
                {
                    "id": "win_l1_front",
                    "name": "Level 1 Office Facade Window",
                    "wall_id": "wall_l1_ext",
                    "opening_type": "WINDOW",
                    "offset_along_wall_m": 2.0,
                    "sill_height_m": 0.9,
                    "width_m": 3.0,
                    "height_m": 1.6,
                    "mark": "W-101",
                    "is_authorised_deduction": True,
                    "provenance": {"source_pdf": "SCH02_WindowSchedule.pdf", "page_number": 9, "drawing_id": "SCH02"},
                },
            ],
            "polygons": [
                {
                    "id": "floor_g_slab",
                    "name": "Lobby Ground Floor Slab",
                    "type": "FLOOR",
                    "level_id": "lvl_g",
                    "polygon": [{"x": 0, "y": 0}, {"x": 15, "y": 0}, {"x": 15, "y": 10}, {"x": 0, "y": 10}],
                    "thickness_m": 0.20,
                    "elevation_offset_m": 0.0,
                    "substrate": "Reinforced Concrete",
                    "finish": "Granite Tiles",
                }
            ],
        }
        current_project = planreader_to_canonical_model(example_prod_payload)

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
