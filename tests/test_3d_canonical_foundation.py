"""
Unit test suite for Phase 1-4 3D Canonical Building Model, Geometry Services,
Synthetic Demonstration Fixture, Viewer Payload Generator, and Security Regressions.
"""

import json
import math
import subprocess
import tempfile
import pytest
from pb_canonical_building import (
    CanonicalProject,
    CanonicalBuilding,
    CanonicalLevel,
    CanonicalWall,
    CanonicalOpening,
    CanonicalSpace,
    CanonicalFloor,
    CanonicalCeiling,
    CanonicalRoof,
    CanonicalSoffit,
    CanonicalBalcony,
    CanonicalParapet,
    CanonicalColumn,
    CanonicalBalustrade,
    CanonicalScreen,
    CanonicalFinishSurface,
    CanonicalElement,
    Vector2D,
    Vector3D,
    Provenance,
    ReviewState,
    ObjectType,
)
from pb_geometry_services import (
    wall_length,
    wall_gross_area,
    gross_opening_area,
    attached_opening_geometry,
    potential_net_wall_area,
    space_floor_area,
    level_extents,
    model_bounds,
    surface_metadata,
    validate_wall_geometry,
    validate_opening_geometry,
)
from pb_synthetic_3d_fixture import get_synthetic_viewer_demo_model
from pb_bim_viewer import project_to_viewer_payload, generate_bim_viewer_html


def test_canonical_model_creation_and_fail_closed_defaults():
    """BLOCKER 1: Verify fail-closed canonical defaults (0.0 confidence, REVIEW_REQUIRED, takeoff_eligible=False)."""
    elem = CanonicalElement()
    assert elem.confidence == 0.0
    assert elem.review_state == ReviewState.REVIEW_REQUIRED
    assert elem.takeoff_eligible is False
    assert elem.deduction_authority is False

    proj = CanonicalProject(name="Test Project")
    assert proj.confidence == 0.0
    assert proj.review_state == ReviewState.REVIEW_REQUIRED
    assert proj.takeoff_eligible is False

    bld = CanonicalBuilding(name="Test Building")
    assert bld.review_state == ReviewState.REVIEW_REQUIRED

    lvl = CanonicalLevel(name="Ground Level", elevation_m=0.0, height_m=3.0)
    assert lvl.review_state == ReviewState.REVIEW_REQUIRED

    wall = CanonicalWall(
        id="wall_custom_101",
        name="External Wall 1",
        start_point=Vector2D(0.0, 0.0),
        end_point=Vector2D(10.0, 0.0),
        height_m=3.0,
        thickness_m=0.2,
        is_external=True,
    )
    assert wall.confidence == 0.0
    assert wall.review_state == ReviewState.REVIEW_REQUIRED
    assert wall.takeoff_eligible is False

    door = CanonicalOpening(
        id="door_101",
        name="Entry Door",
        wall_id=wall.id,
        opening_type="DOOR",
        width_m=1.0,
        height_m=2.1,
    )
    assert door.confidence == 0.0
    assert door.review_state == ReviewState.REVIEW_REQUIRED


def test_malformed_confidence_and_review_state_fallback():
    """BLOCKER 1: Test that malformed confidence or review_state falls back to 0.0 / REVIEW_REQUIRED."""
    bad_dict = {
        "id": "bad_elem",
        "confidence": "not_a_number",
        "review_state": "UNKNOWN_ENUM_VAL",
        "takeoff_eligible": True,
    }
    elem = CanonicalElement.base_from_dict_args(bad_dict)
    assert elem["confidence"] == 0.0
    assert elem["review_state"] == ReviewState.REVIEW_REQUIRED


def test_parent_child_relationships():
    """Test structural parent/child references."""
    lvl = CanonicalLevel(name="Level 1")
    wall = CanonicalWall(name="Wall A", level_id=lvl.id)
    op = CanonicalOpening(name="Door A", wall_id=wall.id, level_id=lvl.id, parent_id=wall.id)

    wall.openings.append(op)
    wall.children_ids.append(op.id)
    lvl.walls.append(wall)

    assert op.wall_id == wall.id
    assert op.parent_id == wall.id
    assert op.id in wall.children_ids
    assert len(lvl.walls) == 1
    assert len(lvl.walls[0].openings) == 1


def test_mixed_authorized_and_unauthorized_opening_deductions():
    """BLOCKER 2: Test that one authorized opening does NOT authorize other openings on the wall."""
    wall = CanonicalWall(
        id="wall_mixed",
        start_point=Vector2D(0.0, 0.0),
        end_point=Vector2D(10.0, 0.0),
        height_m=3.0,
        deduction_authority=True,  # Wall has deduction authority
    )

    # Opening A: Authorized (1.2m x 2.0m = 2.4 m²)
    op_auth = CanonicalOpening(
        id="op_auth",
        wall_id=wall.id,
        offset_along_wall_m=1.0,
        width_m=1.2,
        height_m=2.0,
        deduction_authority=True,
    )
    # Opening B: Unauthorized (2.0m x 1.5m = 3.0 m²)
    op_unauth = CanonicalOpening(
        id="op_unauth",
        wall_id=wall.id,
        offset_along_wall_m=5.0,
        width_m=2.0,
        height_m=1.5,
        deduction_authority=False,
    )
    wall.openings = [op_auth, op_unauth]

    p_net = potential_net_wall_area(wall)
    assert pytest.approx(p_net["gross_wall_area_m2"], 1e-4) == 30.0
    assert pytest.approx(p_net["observed_opening_area_m2"], 1e-4) == 5.4
    assert pytest.approx(p_net["authorized_opening_deduction_area_m2"], 1e-4) == 2.4
    assert pytest.approx(p_net["authorized_net_area_m2"], 1e-4) == 27.6  # 30 - 2.4 = 27.6m²
    assert pytest.approx(p_net["unauthorized_opening_area_m2"], 1e-4) == 3.0
    assert p_net["all_deductions_authorized"] is False  # Mixed openings MUST NOT be reported as all authorized!


def test_malformed_geometry_validation():
    """BLOCKER 2: Test validation of negative dimensions, zero length, and NaN coordinates."""
    # Zero length wall
    wall_zero = CanonicalWall(start_point=Vector2D(1.0, 1.0), end_point=Vector2D(1.0, 1.0), height_m=3.0)
    valid, msg = validate_wall_geometry(wall_zero)
    assert valid is False

    # Negative opening dimensions
    wall = CanonicalWall(start_point=Vector2D(0.0, 0.0), end_point=Vector2D(10.0, 0.0), height_m=3.0)
    bad_op = CanonicalOpening(width_m=-1.5, height_m=2.0)
    valid_op, _ = validate_opening_geometry(bad_op, wall)
    assert valid_op is False

    # Opening exceeding wall bounds
    over_op = CanonicalOpening(offset_along_wall_m=8.0, width_m=3.0, height_m=2.0)  # 8 + 3 = 11m > 10m wall length
    valid_over, _ = validate_opening_geometry(over_op, wall)
    assert valid_over is False


def test_empty_model_bounds_no_made_up_data():
    """BLOCKER 5: Test that empty project bounds do NOT fabricate 10m x 10m x 3m bounds."""
    empty_proj = CanonicalProject(name="Empty Project")
    bounds_ok, bounds = model_bounds(empty_proj)
    assert bounds_ok is False
    assert bounds is None


def test_serialization_round_trip():
    """Test full JSON serialization and deserialization round-trip."""
    project = get_synthetic_viewer_demo_model()

    json_str = project.to_json(indent=None)
    assert isinstance(json_str, str)
    assert len(json_str) > 100

    reconstructed = CanonicalProject.from_json(json_str)
    assert reconstructed.id == project.id
    assert reconstructed.name == project.name
    assert reconstructed.is_synthetic_demo is True
    assert len(reconstructed.buildings) == len(project.buildings)

    bld_orig = project.buildings[0]
    bld_recon = reconstructed.buildings[0]
    assert len(bld_recon.levels) == len(bld_orig.levels)

    lvl0_recon = bld_recon.levels[0]
    assert len(lvl0_recon.walls) == len(bld_orig.levels[0].walls)
    assert lvl0_recon.walls[0].openings[0].mark == "D01"


def test_xss_prevention_in_generated_viewer():
    """BLOCKER 6: Test that user/PDF text with XSS payloads is escaped and not executed as HTML."""
    xss_payload = "<img src=x onerror=alert(1)>"
    proj = CanonicalProject(name=f"Project {xss_payload}")
    bld = CanonicalBuilding(name=f"Building {xss_payload}", substrate=xss_payload, finish=xss_payload)
    proj.buildings.append(bld)

    payload = project_to_viewer_payload(proj)
    html_code = generate_bim_viewer_html(payload)

    # HTML code must contain escapeHtml function and textContent assignments
    assert "escapeHtml" in html_code
    assert ".textContent =" in html_code
    # XSS payload in script JSON string must not appear as raw unescaped HTML element tags
    assert "<img src=x onerror=alert(1)>" not in html_code.split("const modelData = ")[0]


def test_generated_viewer_javascript_syntax_with_node():
    """BLOCKER 8: Check generated viewer JavaScript syntax using node --check."""
    project = get_synthetic_viewer_demo_model()
    payload = project_to_viewer_payload(project)
    html_code = generate_bim_viewer_html(payload)

    # Extract inline JavaScript code from script tags
    js_parts = []
    in_script = False
    for line in html_code.splitlines():
        if "<script>" in line:
            in_script = True
            continue
        elif "</script>" in line:
            in_script = False
            continue
        elif in_script and not line.strip().startswith("<script src="):
            js_parts.append(line)

    js_code = "\n".join(js_parts)
    assert len(js_code) > 500

    # Test with node --check if node executable is available
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
            tmp.write(js_code)
            tmp_path = tmp.name

        res = subprocess.run(["node", "--check", tmp_path], capture_output=True, text=True)
        assert res.returncode == 0, f"Node JS syntax check failed: {res.stderr}"
    except FileNotFoundError:
        pytest.skip("Node executable not found in PATH for JS syntax check")
