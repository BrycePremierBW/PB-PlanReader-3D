"""
Unit test suite for Phase 1-4 3D Canonical Building Model, Geometry Services,
Synthetic Demonstration Fixture, and Viewer Payload Generator.
"""

import json
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
)
from pb_synthetic_3d_fixture import get_synthetic_viewer_demo_model
from pb_bim_viewer import project_to_viewer_payload, generate_bim_viewer_html


def test_canonical_model_creation_and_stable_ids():
    """Test creation of all canonical elements and verify stable IDs and defaults."""
    proj = CanonicalProject(name="Test Project")
    assert proj.id.startswith("elem_") or len(proj.id) > 0
    assert proj.object_type == ObjectType.PROJECT
    assert proj.confidence == 1.0
    assert proj.review_state == ReviewState.CONFIRMED

    bld = CanonicalBuilding(name="Test Building")
    assert bld.object_type == ObjectType.BUILDING

    lvl = CanonicalLevel(name="Ground Level", elevation_m=0.0, height_m=3.0)
    assert lvl.object_type == ObjectType.LEVEL
    assert lvl.elevation_m == 0.0

    wall = CanonicalWall(
        id="wall_custom_101",
        name="External Wall 1",
        start_point=Vector2D(0.0, 0.0),
        end_point=Vector2D(10.0, 0.0),
        height_m=3.0,
        thickness_m=0.2,
        is_external=True,
    )
    assert wall.id == "wall_custom_101"
    assert wall.object_type == ObjectType.WALL
    assert wall.start_point.x == 0.0
    assert wall.end_point.x == 10.0

    door = CanonicalOpening(
        id="door_101",
        name="Entry Door",
        wall_id=wall.id,
        opening_type="DOOR",
        width_m=1.0,
        height_m=2.1,
    )
    assert door.object_type == ObjectType.DOOR

    win = CanonicalOpening(
        id="win_101",
        name="Front Window",
        wall_id=wall.id,
        opening_type="WINDOW",
        width_m=2.0,
        height_m=1.5,
    )
    assert win.object_type == ObjectType.WINDOW


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


def test_geometry_services_calculations():
    """Test metric geometry calculations from supplied data."""
    wall = CanonicalWall(
        id="wall_calc",
        start_point=Vector2D(0.0, 0.0),
        end_point=Vector2D(6.0, 8.0),  # 3-4-5 right triangle -> length 10m
        height_m=3.0,
    )

    length = wall_length(wall)
    assert pytest.approx(length, 1e-4) == 10.0

    gross_wall = wall_gross_area(wall)
    assert pytest.approx(gross_wall, 1e-4) == 30.0  # 10m * 3m = 30m²

    door = CanonicalOpening(
        id="d1",
        wall_id=wall.id,
        opening_type="DOOR",
        offset_along_wall_m=2.0,
        width_m=1.2,
        height_m=2.0,
    )
    win = CanonicalOpening(
        id="w1",
        wall_id=wall.id,
        opening_type="WINDOW",
        offset_along_wall_m=5.0,
        width_m=2.0,
        height_m=1.5,
    )
    wall.openings = [door, win]

    assert pytest.approx(gross_opening_area(door), 1e-4) == 2.4  # 1.2 * 2.0 = 2.4m²
    assert pytest.approx(gross_opening_area(win), 1e-4) == 3.0   # 2.0 * 1.5 = 3.0m²

    # Test attached opening geometry
    geom_d = attached_opening_geometry(door, wall)
    assert geom_d["wall_id"] == wall.id
    assert geom_d["width_m"] == 1.2
    assert geom_d["gross_area_m2"] == 2.4

    # Test potential net wall area & deduction authority safety guarantee
    p_net = potential_net_wall_area(wall)
    assert pytest.approx(p_net["gross_wall_area_m2"], 1e-4) == 30.0
    assert pytest.approx(p_net["total_opening_area_m2"], 1e-4) == 5.4
    assert pytest.approx(p_net["potential_net_area_m2"], 1e-4) == 24.6  # 30 - 5.4 = 24.6m²
    assert p_net["deduction_authorized"] is False  # CRITICAL: rendering geometry does NOT automatically grant deduction authority!
    assert "NOT Authorized" in p_net["authority_note"]


def test_space_shoelace_floor_area():
    """Test room/space boundary polygon area calculation using shoelace formula."""
    space = CanonicalSpace(
        name="Rectangular Room",
        boundary_polygon=[Vector2D(0, 0), Vector2D(5, 0), Vector2D(5, 4), Vector2D(0, 4)],
    )
    area = space_floor_area(space)
    assert pytest.approx(area, 1e-4) == 20.0  # 5m * 4m = 20m²


def test_serialization_round_trip():
    """Test full JSON serialization and deserialization round-trip."""
    project = get_synthetic_viewer_demo_model()

    json_str = project.to_json(indent=None)
    assert isinstance(json_str, str)
    assert len(json_str) > 100

    reconstructed = CanonicalProject.from_json(json_str)
    assert reconstructed.id == project.id
    assert reconstructed.name == project.name
    assert len(reconstructed.buildings) == len(project.buildings)

    bld_orig = project.buildings[0]
    bld_recon = reconstructed.buildings[0]
    assert len(bld_recon.levels) == len(bld_orig.levels)

    lvl0_recon = bld_recon.levels[0]
    assert len(lvl0_recon.walls) == len(bld_orig.levels[0].walls)
    assert lvl0_recon.walls[0].openings[0].mark == "D01"


def test_review_required_elements_and_provenance():
    """Test that review-required states and provenance fields are preserved."""
    project = get_synthetic_viewer_demo_model()
    roof_lvl = project.buildings[0].levels[2]  # Level 2 Roof Level

    assert roof_lvl.review_state == ReviewState.REVIEW_REQUIRED
    assert roof_lvl.confidence < 0.6
    assert "Unverified Section Height Note" in roof_lvl.provenance.scale_source

    parapet = roof_lvl.parapets[0]
    assert parapet.review_state == ReviewState.REVIEW_REQUIRED
    assert parapet.provenance.source_pdf == "Architectural_Set_RevB.pdf"


def test_viewer_payload_generation():
    """Test translation of canonical model to Three.js viewer JSON payload."""
    project = get_synthetic_viewer_demo_model()
    payload = project_to_viewer_payload(project)

    assert "project_id" in payload
    assert "bounds" in payload
    assert "levels" in payload
    assert "objects" in payload

    assert len(payload["levels"]) == 3
    assert len(payload["objects"]) > 10

    # Verify HTML viewer generation
    html_code = generate_bim_viewer_html(payload)
    assert "Three.js" in html_code
    assert "SYNTHETIC VIEWER DEMONSTRATION" in html_code
    assert "OrbitControls" in html_code


def test_malformed_input_fails_safely():
    """Test that malformed or partial dict input deserializes safely without crashing."""
    bad_dict = {
        "id": "bad_wall_1",
        "start_point": "invalid_type",
        "confidence": "not_a_float",
        "review_state": "INVALID_STATE_ENUM",
        "openings": [None, {}],
    }

    wall = CanonicalWall.from_dict(bad_dict)
    assert wall.id == "bad_wall_1"
    assert wall.confidence == 1.0  # Falls back to default
    assert wall.review_state == ReviewState.REVIEW_REQUIRED  # Safe fallback
    assert wall.start_point.x == 0.0
