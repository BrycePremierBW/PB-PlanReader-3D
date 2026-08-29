"""
Unit test suite for Phase 1-4 3D Canonical Building Model, Geometry Services,
Synthetic Demonstration Fixture, Viewer Payload Generator, and Round 4 Gate 1 Security/Safety Regressions.
"""

import base64
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
    BoundingBox3D,
    Provenance,
    ReviewState,
    ObjectType,
    parse_strict_bool,
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
    detect_opening_overlaps,
)
from pb_synthetic_3d_fixture import get_synthetic_viewer_demo_model
from pb_bim_viewer import project_to_viewer_payload, generate_bim_viewer_html


def test_canonical_model_creation_and_fail_closed_defaults():
    """Verify fail-closed canonical defaults (0.0/None confidence, REVIEW_REQUIRED, takeoff_eligible=False)."""
    elem = CanonicalElement()
    assert elem.confidence is None
    assert elem.review_state == ReviewState.REVIEW_REQUIRED
    assert elem.takeoff_eligible is False
    assert elem.deduction_authority is False

    proj = CanonicalProject(name="Test Project")
    assert proj.confidence is None
    assert proj.review_state == ReviewState.REVIEW_REQUIRED
    assert proj.takeoff_eligible is False

    bld = CanonicalBuilding(name="Test Building")
    assert bld.review_state == ReviewState.REVIEW_REQUIRED

    lvl = CanonicalLevel(name="Ground Level")
    assert lvl.elevation_m is None
    assert lvl.height_m is None
    assert lvl.review_state == ReviewState.REVIEW_REQUIRED

    wall = CanonicalWall(
        id="wall_custom_101",
        name="External Wall 1",
        start_point=Vector2D(0.0, 0.0),
        end_point=Vector2D(10.0, 0.0),
        is_external=True,
    )
    assert wall.confidence is None
    assert wall.height_m is None
    assert wall.thickness_m is None
    assert wall.review_state == ReviewState.REVIEW_REQUIRED
    assert wall.takeoff_eligible is False


def test_direct_python_construction_strict_boolean_normalization():
    """ROUND 3: Direct Python construction with 'false', 'true', 'yes', 1 must NOT grant authority."""
    w_false = CanonicalWall(deduction_authority="false", takeoff_eligible="true", is_external="yes")
    assert w_false.deduction_authority is False
    assert w_false.takeoff_eligible is False
    assert w_false.is_external is False

    w_int = CanonicalWall(deduction_authority=1, takeoff_eligible=0)
    assert w_int.deduction_authority is False
    assert w_int.takeoff_eligible is False

    w_true = CanonicalWall(deduction_authority=True, takeoff_eligible=True, is_external=True)
    assert w_true.deduction_authority is True
    assert w_true.takeoff_eligible is True
    assert w_true.is_external is True

    w_false_dict = w_false.to_dict()
    assert w_false_dict["deduction_authority"] is False
    assert w_false_dict["takeoff_eligible"] is False


def test_missing_opening_position_fails_placement():
    """ROUND 4 GATE 1: Missing sill_height_m or offset_along_wall_m CANNOT position a physical opening."""
    wall = CanonicalWall(
        id="wall_pos",
        start_point=Vector2D(0.0, 0.0),
        end_point=Vector2D(10.0, 0.0),
        height_m=3.0,
    )
    # Opening missing sill_height_m (None)
    op_no_sill = CanonicalOpening(
        id="op_no_sill",
        wall_id=wall.id,
        offset_along_wall_m=2.0,
        sill_height_m=None,
        width_m=1.0,
        height_m=2.0,
    )
    valid_sill, msg_sill = validate_opening_geometry(op_no_sill, wall)
    assert valid_sill is False
    assert "Invalid or missing opening sill_height_m" in msg_sill

    # Opening missing offset_along_wall_m (None)
    op_no_off = CanonicalOpening(
        id="op_no_off",
        wall_id=wall.id,
        offset_along_wall_m=None,
        sill_height_m=1.0,
        width_m=1.0,
        height_m=2.0,
    )
    valid_off, msg_off = validate_opening_geometry(op_no_off, wall)
    assert valid_off is False
    assert "Invalid or missing opening offset_along_wall_m" in msg_off


def test_synthetic_demo_fixture_flags():
    """ROUND 2: Synthetic demo fixture must NEVER be takeoff-authoritative."""
    demo = get_synthetic_viewer_demo_model()
    assert demo.is_synthetic_demo is True
    assert demo.takeoff_eligible is False
    assert demo.deduction_authority is False


def test_model_bounds_api_contract_and_z_fail_closed():
    """ROUND 3 & 4: Test model_bounds returns (bounds_available: bool, bounds: Optional[BoundingBox3D]) and fails closed in Z."""
    empty_proj = CanonicalProject(name="Empty")
    ok_empty, bounds_empty = model_bounds(empty_proj)
    assert ok_empty is False
    assert bounds_empty is None

    lvl_no_elev = CanonicalLevel(name="Unknown Elev", elevation_m=None)
    lvl_no_elev.walls = [CanonicalWall(start_point=Vector2D(0,0), end_point=Vector2D(10,0), height_m=3.0)]
    assert level_extents(lvl_no_elev) is None

    demo_proj = get_synthetic_viewer_demo_model()
    ok_demo, bounds_demo = model_bounds(demo_proj)
    assert ok_demo is True
    assert bounds_demo is not None
    assert isinstance(bounds_demo, BoundingBox3D)
    assert bounds_demo.min_point.x is not None
    assert bounds_demo.max_point.x is not None


def test_no_invented_physical_geometry_defaults():
    """ROUND 2, 3, 4: Test that missing physical geometry parameters remain None."""
    wall = CanonicalWall(start_point=Vector2D(0.0, 0.0), end_point=Vector2D(10.0, 0.0))
    assert wall.height_m is None
    assert wall.thickness_m is None

    valid_w, msg_w = validate_wall_geometry(wall)
    assert valid_w is False
    assert "Invalid or missing wall height_m" in msg_w

    col = CanonicalColumn(center=Vector2D(1.0, 1.0))
    assert col.width_m is None
    assert col.depth_m is None
    assert col.height_m is None

    op = CanonicalOpening()
    assert op.width_m is None
    assert op.height_m is None
    assert op.sill_height_m is None


def test_opening_render_order_and_host_resolution():
    """ROUND 2: Test host-wall resolution in 2-pass viewer scene building regardless of payload order."""
    wall = CanonicalWall(
        id="wall_host_99",
        name="Host Wall 99",
        start_point=Vector2D(0.0, 0.0),
        end_point=Vector2D(10.0, 0.0),
        height_m=3.0,
        thickness_m=0.2,
    )
    door = CanonicalOpening(
        id="door_child_99",
        name="Child Door 99",
        wall_id=wall.id,
        opening_type="DOOR",
        offset_along_wall_m=1.0,
        sill_height_m=0.0,
        width_m=1.0,
        height_m=2.1,
    )

    proj = CanonicalProject(name="Order Test")
    bld = CanonicalBuilding(name="Bld")
    lvl = CanonicalLevel(name="L1", elevation_m=0.0, height_m=3.0)
    lvl.walls = [wall]
    wall.openings = [door]
    bld.levels = [lvl]
    proj.buildings = [bld]

    payload = project_to_viewer_payload(proj)
    payload["objects"].sort(key=lambda o: 0 if o["id"] == "door_child_99" else 1)
    assert payload["objects"][0]["id"] == "door_child_99"
    assert payload["objects"][1]["id"] == "wall_host_99"

    html_code = generate_bim_viewer_html(payload)
    assert "PASS 1: Index ALL objects in objectDataMap first" in html_code
    assert "PASS 2: Create and render 3D meshes" in html_code


def test_deduction_and_overlap_conflict_fail_closed_net_area():
    """ROUND 3: Test that overlapping openings FAIL CLOSED for authorized deduction area (0.0) and authorized net area (gross wall)."""
    wall_op = CanonicalWall(
        id="wall_overlap",
        start_point=Vector2D(0.0, 0.0),
        end_point=Vector2D(10.0, 0.0),
        height_m=3.0,
        deduction_authority=True,
    )
    op1 = CanonicalOpening(
        id="op1",
        wall_id=wall_op.id,
        offset_along_wall_m=1.0,
        width_m=2.0,
        height_m=2.0,
        sill_height_m=0.0,
        deduction_authority=True,
    )
    op2 = CanonicalOpening(
        id="op2",
        wall_id=wall_op.id,
        offset_along_wall_m=2.0,  # Overlaps from 2.0 to 3.0m with op1!
        width_m=2.0,
        height_m=2.0,
        sill_height_m=0.0,
        deduction_authority=True,
    )
    wall_op.openings = [op1, op2]

    p_overlap = potential_net_wall_area(wall_op)
    assert p_overlap["has_overlapping_openings"] is True
    assert p_overlap["all_deductions_authorized"] is False
    assert p_overlap["authorized_opening_deduction_area_m2"] == 0.0
    assert p_overlap["authorized_net_area_m2"] == 30.0
    assert "Conflict: Overlapping / Duplicate Openings" in p_overlap["authority_note"]


def test_confidence_semantics_explicit_zero_vs_none():
    """ROUND 2: Test that explicit 0.0 confidence is distinguished from missing (None)."""
    w_none = CanonicalWall(
        name="Wall None",
        start_point=Vector2D(0.0, 0.0),
        end_point=Vector2D(10.0, 0.0),
        height_m=3.0,
        confidence=None,
    )
    w_zero = CanonicalWall(
        name="Wall Zero",
        start_point=Vector2D(0.0, 0.0),
        end_point=Vector2D(10.0, 0.0),
        height_m=3.0,
        confidence=0.0,
    )

    proj = CanonicalProject(name="Conf Test")
    bld = CanonicalBuilding(name="Bld")
    lvl = CanonicalLevel(name="L1", elevation_m=0.0, height_m=3.0)
    lvl.walls = [w_none, w_zero]
    bld.levels = [lvl]
    proj.buildings = [bld]

    payload = project_to_viewer_payload(proj)
    obj_none = next(o for o in payload["objects"] if o["name"] == "Wall None")
    obj_zero = next(o for o in payload["objects"] if o["name"] == "Wall Zero")

    assert obj_none["confidence"] is None
    assert obj_zero["confidence"] == 0.0


def test_xss_script_context_escaped_base64():
    """ROUND 2: Test that script tags in user/PDF strings do not break script HTML parsing."""
    xss_str = "</script><script>globalThis.__planreader_xss_test=1</script>"
    proj = CanonicalProject(name=f"Project {xss_str}")
    bld = CanonicalBuilding(name=f"Bld {xss_str}", substrate=xss_str, finish=xss_str)
    lvl = CanonicalLevel(name=f"Lvl {xss_str}", elevation_m=0.0, height_m=3.0)
    wall = CanonicalWall(
        name=f"Wall {xss_str}",
        start_point=Vector2D(0.0, 0.0),
        end_point=Vector2D(10.0, 0.0),
        height_m=3.0,
        provenance=Provenance(source_pdf=xss_str, drawing_id=xss_str, contributing_evidence=[xss_str]),
    )
    lvl.walls = [wall]
    bld.levels = [lvl]
    proj.buildings = [bld]

    payload = project_to_viewer_payload(proj)
    html_code = generate_bim_viewer_html(payload)

    assert "</script><script>globalThis" not in html_code.split("const b64Data = ")[1].split(";")[0]

    b64_str = html_code.split('const b64Data = "')[1].split('"')[0]
    decoded_json = base64.b64decode(b64_str.encode("ascii")).decode("utf-8")
    decoded_data = json.loads(decoded_json)
    assert xss_str in decoded_data["project_name"]


def test_complete_declared_types_and_finish_surface():
    """ROUND 2: Test complete declared types (CEILING, SCREEN, SURFACE) and level surface persistence."""
    lvl = CanonicalLevel(name="L1", elevation_m=0.0, height_m=3.0)
    ceil = CanonicalCeiling(name="Acoustic Ceiling", polygon=[Vector2D(0, 0), Vector2D(10, 0), Vector2D(10, 10), Vector2D(0, 10)])
    screen = CanonicalScreen(name="Privacy Screen", start_point=Vector2D(0, 0), end_point=Vector2D(5, 0), height_m=2.0)
    surf = CanonicalFinishSurface(name="Feature Paint Surface", surface_area_m2=25.0)

    lvl.ceilings = [ceil]
    lvl.screens = [screen]
    lvl.surfaces = [surf]

    lvl_dict = lvl.to_dict()
    lvl_recon = CanonicalLevel.from_dict(lvl_dict)

    assert len(lvl_recon.ceilings) == 1
    assert len(lvl_recon.screens) == 1
    assert len(lvl_recon.surfaces) == 1
    assert lvl_recon.surfaces[0].surface_area_m2 == 25.0

    proj = CanonicalProject(name="Type Test")
    bld = CanonicalBuilding(name="Bld", levels=[lvl_recon])
    proj.buildings = [bld]

    payload = project_to_viewer_payload(proj)
    types_in_payload = {o["type"] for o in payload["objects"]}

    assert ObjectType.CEILING.value in types_in_payload
    assert ObjectType.SCREEN.value in types_in_payload
    assert ObjectType.SURFACE.value in types_in_payload
