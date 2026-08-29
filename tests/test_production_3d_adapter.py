"""
Unit and Integration test suite for Phase 5 Production 3D Model Adapter (pb_production_3d_adapter.py).

Verifies:
1. Real PlanReader production payload -> canonical model conversion.
2. No-evidence / incomplete-evidence fail-closed behavior (unknown dimensions remain None).
3. Known openings attach to host walls correctly.
4. Provenance survives end-to-end (source PDF, page, drawing ID).
5. B3/B5 deduction authority is unchanged.
6. Synthetic demo fixture cannot leak into production outputs (is_synthetic_demo = False).
7. Benchmark integration using real repository fixture files.
"""

import os
import json
import pytest
from pb_canonical_building import CanonicalProject, ObjectType, ReviewState
from pb_production_3d_adapter import planreader_to_canonical_model
from pb_bim_viewer import project_to_viewer_payload
from pb_synthetic_3d_fixture import get_synthetic_viewer_demo_model


def test_production_payload_conversion_basic():
    """Verify basic PlanReader production payload conversion into CanonicalProject."""
    payload = {
        "project_id": "proj_commercial_101",
        "project_name": "Commercial Office Tower",
        "is_synthetic_demo": False,
        "levels": [
            {"id": "lvl_0", "name": "Ground Level", "elevation_m": 0.0, "height_m": 3.2, "level_index": 0},
            {"id": "lvl_1", "name": "Level 1", "elevation_m": 3.2, "height_m": 2.8, "level_index": 1},
        ],
        "walls": [
            {
                "id": "wall_1",
                "name": "South Wall Ground",
                "level_id": "lvl_0",
                "start_point": {"x": 0.0, "y": 0.0},
                "end_point": {"x": 10.0, "y": 0.0},
                "height_m": 3.2,
                "thickness_m": 0.23,
                "is_external": True,
                "substrate": "Masonry",
                "finish": "Paint",
                "confidence": 0.95,
                "provenance": {"source_pdf": "A101.pdf", "page_number": 3, "drawing_id": "A101"},
                "deduction_authority": True,
            }
        ],
        "openings": [
            {
                "id": "op_1",
                "name": "Entry Door",
                "wall_id": "wall_1",
                "opening_type": "DOOR",
                "offset_along_wall_m": 2.0,
                "sill_height_m": 0.0,
                "width_m": 1.2,
                "height_m": 2.4,
                "mark": "D01",
                "is_authorised_deduction": True,
                "provenance": {"source_pdf": "SCH01.pdf", "page_number": 8, "drawing_id": "SCH01"},
            }
        ],
    }

    project = planreader_to_canonical_model(payload)
    assert project.id == "proj_commercial_101"
    assert project.name == "Commercial Office Tower"
    assert project.is_synthetic_demo is False

    assert len(project.buildings) == 1
    bld = project.buildings[0]
    assert len(bld.levels) == 2

    lvl0 = bld.levels[0]
    assert lvl0.name == "Ground Level"
    assert len(lvl0.walls) == 1

    wall = lvl0.walls[0]
    assert wall.id == "wall_1"
    assert wall.height_m == 3.2
    assert wall.thickness_m == 0.23
    assert wall.provenance.source_pdf == "A101.pdf"
    assert wall.provenance.page_number == 3
    assert wall.provenance.drawing_id == "A101"

    assert len(wall.openings) == 1
    op = wall.openings[0]
    assert op.id == "op_1"
    assert op.opening_type == ObjectType.DOOR
    assert op.deduction_authority is True
    assert op.provenance.source_pdf == "SCH01.pdf"


def test_unknown_geometry_remains_none_and_review_required():
    """Verify that unknown wall heights/thicknesses remain None and uncertain items stay REVIEW_REQUIRED."""
    payload = {
        "project_name": "Incomplete Evidence Project",
        "walls": [
            {
                "id": "wall_unknown",
                "name": "Wall Unknown Metrics",
                "start_point": {"x": 0.0, "y": 0.0},
                "end_point": {"x": 8.0, "y": 0.0},
                # height_m and thickness_m omitted!
                "confidence": 0.40,  # Low confidence
            }
        ]
    }

    project = planreader_to_canonical_model(payload)
    wall = project.buildings[0].levels[0].walls[0]

    assert wall.height_m is None
    assert wall.thickness_m is None
    assert wall.review_state == ReviewState.REVIEW_REQUIRED


def test_elevation_evidence_without_host_wall_cannot_create_opening():
    """Verify elevation evidence alone cannot create a physical 3D opening without a host wall match."""
    payload = {
        "project_name": "Unmatched Opening Test",
        "walls": [
            {
                "id": "wall_valid",
                "start_point": {"x": 0.0, "y": 0.0},
                "end_point": {"x": 10.0, "y": 0.0},
                "height_m": 3.0,
            }
        ],
        "openings": [
            {
                "id": "op_unmatched",
                "wall_id": "non_existent_wall_id",  # Invalid host wall!
                "opening_type": "WINDOW",
                "width_m": 1.5,
                "height_m": 1.5,
            }
        ]
    }

    project = planreader_to_canonical_model(payload)
    wall = project.buildings[0].levels[0].walls[0]
    assert len(wall.openings) == 0  # Unmatched opening is excluded from 3D host placement!


def test_deduction_authority_is_unchanged():
    """Verify B3/B5 deduction authority boolean is strictly preserved from production evidence."""
    payload = {
        "walls": [
            {
                "id": "w1",
                "start_point": {"x": 0, "y": 0},
                "end_point": {"x": 10, "y": 0},
                "height_m": 3.0,
                "deduction_authority": True,
            }
        ],
        "openings": [
            {
                "id": "op_auth",
                "wall_id": "w1",
                "offset_along_wall_m": 1.0,
                "sill_height_m": 0.0,
                "width_m": 1.0,
                "height_m": 2.0,
                "is_authorised_deduction": True,
            },
            {
                "id": "op_unauth",
                "wall_id": "w1",
                "offset_along_wall_m": 4.0,
                "sill_height_m": 0.0,
                "width_m": 1.0,
                "height_m": 2.0,
                "is_authorised_deduction": False,
            },
        ]
    }

    project = planreader_to_canonical_model(payload)
    wall = project.buildings[0].levels[0].walls[0]
    assert wall.deduction_authority is True
    
    op_auth = next(o for o in wall.openings if o.id == "op_auth")
    op_unauth = next(o for o in wall.openings if o.id == "op_unauth")

    assert op_auth.deduction_authority is True
    assert op_unauth.deduction_authority is False


def test_synthetic_fixture_cannot_leak_into_production():
    """Verify production adapter ensures is_synthetic_demo = False."""
    payload = {
        "project_name": "Real Production Document",
        "is_synthetic_demo": False,
        "walls": [
            {"id": "w1", "start_point": {"x": 0, "y": 0}, "end_point": {"x": 5, "y": 0}, "height_m": 3.0}
        ]
    }

    project = planreader_to_canonical_model(payload)
    assert project.is_synthetic_demo is False

    viewer_payload = project_to_viewer_payload(project)
    assert viewer_payload["is_synthetic_demo"] is False


def test_real_benchmark_fixture_integration():
    """Integration test loading real PlanReader benchmark JSON fixture files."""
    fpath = "tests/fixtures/lago_cd3001_east_elevation_v177.json"
    if not os.path.exists(fpath):
        pytest.skip(f"Fixture {fpath} not found")

    with open(fpath, "r", encoding="utf-8") as f:
        fixture_data = json.load(f)

    # Convert benchmark fixture info into production payload input
    src = fixture_data.get("source", {})
    pos_regions = fixture_data.get("positive_regions", [])

    prod_payload = {
        "project_name": f"Lago Benchmark {src.get('drawing_no', '')}",
        "is_synthetic_demo": False,
        "levels": [
            {
                "id": "lvl_east",
                "name": src.get("elevation_label", "East Elevation"),
                "elevation_m": 0.0,
                "height_m": 3.0,
                "provenance": {
                    "source_pdf": src.get("pdf"),
                    "page_number": src.get("page_1_based"),
                    "drawing_id": src.get("drawing_no"),
                    "scale_source": src.get("stated_scale"),
                }
            }
        ],
        "walls": [
            {
                "id": "wall_east_facade",
                "name": "East Facade Elevation Baseline",
                "level_id": "lvl_east",
                "start_point": {"x": 0.0, "y": 0.0},
                "end_point": {"x": 20.0, "y": 0.0},
                "height_m": 3.0,
                "thickness_m": 0.23,
                "confidence": 0.95,
                "provenance": {
                    "source_pdf": src.get("pdf"),
                    "page_number": src.get("page_1_based"),
                    "drawing_id": src.get("drawing_no"),
                }
            }
        ]
    }

    openings_payload = []
    for idx, reg in enumerate(pos_regions):
        for op_item in reg.get("true_positive_openings", []):
            mark_str = op_item if isinstance(op_item, str) else op_item.get("mark", f"OP-{idx}")
            op_type_str = "WINDOW" if "window" in str(op_item).lower() or "w" in str(mark_str).lower() else "DOOR"
            
            openings_payload.append({
                "id": f"bench_op_{idx}_{mark_str}",
                "name": f"Bench Opening {mark_str}",
                "wall_id": "wall_east_facade",
                "opening_type": op_type_str,
                "offset_along_wall_m": 2.0 + idx * 3.0,
                "sill_height_m": 0.9,
                "width_m": 1.5,
                "height_m": 1.5,
                "mark": mark_str,
                "is_authorised_deduction": True,
                "provenance": {
                    "source_pdf": src.get("pdf"),
                    "page_number": src.get("page_1_based"),
                    "drawing_id": src.get("drawing_no"),
                }
            })

    prod_payload["openings"] = openings_payload

    project = planreader_to_canonical_model(prod_payload)
    assert project.name.startswith("Lago Benchmark")
    assert project.is_synthetic_demo is False
    assert len(project.buildings[0].levels[0].walls) == 1
    
    wall = project.buildings[0].levels[0].walls[0]
    assert wall.provenance.drawing_id == src.get("drawing_no")
    assert len(wall.openings) == len(openings_payload)
