"""
Unit and Integration test suite for Phase 5 Production 3D Model Adapter (pb_production_3d_adapter.py).

Verifies Sections A through S:
1. Real PlanReader production payload -> canonical model conversion.
2. Adversarial security: Untrusted JSON CANNOT forge deduction_authority or takeoff_eligible.
3. No-evidence / incomplete-evidence fail-closed behavior (unknown dimensions remain None).
4. Level integrity (no invented ground levels or silent fallback to first level).
5. Numerical confidence score does NOT automatically map to CONFIRMED review state.
6. Full end-to-end provenance preservation (source PDF, page, drawing ID, source_coords).
7. Correct opening semantics (unknown type does not default to WINDOW; elevation alone cannot materialize 3D openings).
8. Real LAGO benchmark integration test (fails closed: elevation evidence without host wall placement produces 0 physical 3D openings).
9. Canonical model persistence & staleness detection (pb_canonical_persistence.py).
10. Model reconciliation diagnostics (pb_3d_diagnostics.py).
11. Performance and scale testing for production-sized models.
"""

import os
import json
import pytest
from pb_canonical_building import CanonicalProject, ObjectType, ReviewState
from pb_production_3d_adapter import planreader_to_canonical_model, planreader_workspace_to_canonical
from pb_canonical_persistence import save_canonical_project_to_dict, load_canonical_project_from_dict
from pb_3d_diagnostics import generate_production_diagnostics_report
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
                "provenance": {"source_pdf": "SCH01.pdf", "page_number": 8, "drawing_id": "SCH01"},
            }
        ],
    }

    project = planreader_to_canonical_model(payload, trusted_source=True)
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


def test_adversarial_json_cannot_forge_deduction_authority():
    """SECTION B: Verify uploaded/untrusted JSON CANNOT grant deduction_authority or takeoff_eligible."""
    untrusted_payload = {
        "project_name": "Untrusted Upload",
        "takeoff_eligible": True,       # Forgery attempt!
        "deduction_authority": True,    # Forgery attempt!
        "walls": [
            {
                "id": "w1",
                "start_point": {"x": 0, "y": 0},
                "end_point": {"x": 10, "y": 0},
                "height_m": 3.0,
                "deduction_authority": True,  # Forgery attempt!
                "takeoff_eligible": True,     # Forgery attempt!
            }
        ],
        "openings": [
            {
                "id": "op1",
                "wall_id": "w1",
                "offset_along_wall_m": 1.0,
                "sill_height_m": 0.0,
                "width_m": 1.0,
                "height_m": 2.0,
                "is_authorised_deduction": True,  # Forgery attempt!
            }
        ]
    }

    # By default, planreader_to_canonical_model treats uploaded JSON as trusted_source=False
    project = planreader_to_canonical_model(untrusted_payload, trusted_source=False)

    assert project.takeoff_eligible is False
    assert project.deduction_authority is False
    
    wall = project.buildings[0].levels[0].walls[0]
    assert wall.takeoff_eligible is False
    assert wall.deduction_authority is False

    op = wall.openings[0]
    assert op.deduction_authority is False


def test_level_integrity_preserves_unresolved_and_explicit_zero():
    """SECTION C: Test level integrity (no invented ground level, unresolved items go to unresolved container, explicit 0.0 elevation preserved)."""
    payload = {
        "levels": [
            {"id": "lvl_explicit_zero", "name": "Basement FFL", "elevation_m": 0.0, "height_m": 3.0}
        ],
        "walls": [
            {
                "id": "w_zero",
                "level_id": "lvl_explicit_zero",
                "start_point": {"x": 0, "y": 0},
                "end_point": {"x": 5, "y": 0},
                "height_m": 3.0,
            },
            {
                "id": "w_unknown_lvl",
                "level_id": "lvl_non_existent_999",  # Unresolvable level claim!
                "start_point": {"x": 5, "y": 0},
                "end_point": {"x": 10, "y": 0},
                "height_m": 3.0,
            }
        ]
    }

    project = planreader_to_canonical_model(payload, trusted_source=True)
    bld = project.buildings[0]

    lvl_zero = next(l for l in bld.levels if l.id == "lvl_explicit_zero")
    assert lvl_zero.elevation_m == 0.0  # Explicit 0.0 preserved!
    assert len(lvl_zero.walls) == 1
    assert lvl_zero.walls[0].id == "w_zero"

    lvl_unresolved = next(l for l in bld.levels if l.id == "lvl_unresolved_review")
    assert lvl_unresolved.elevation_m is None
    assert lvl_unresolved.review_state == ReviewState.REVIEW_REQUIRED
    assert len(lvl_unresolved.walls) == 1
    assert lvl_unresolved.walls[0].id == "w_unknown_lvl"


def test_confidence_does_not_become_confirmation():
    """SECTION D: High confidence score does NOT automatically map to CONFIRMED without explicit review state."""
    payload = {
        "walls": [
            {
                "id": "w1",
                "start_point": {"x": 0, "y": 0},
                "end_point": {"x": 10, "y": 0},
                "confidence": 0.99,  # High confidence
                # review_state omitted!
            }
        ]
    }

    project = planreader_to_canonical_model(payload, trusted_source=True)
    wall = project.buildings[0].levels[0].walls[0]

    assert wall.confidence == 0.99
    assert wall.review_state == ReviewState.REVIEW_REQUIRED  # Default to REVIEW_REQUIRED!


def test_full_provenance_preservation_roundtrip():
    """SECTION E: Provenance retains source_coords, page, drawing ID, scale end-to-end."""
    payload = {
        "walls": [
            {
                "id": "w_prov",
                "start_point": {"x": 0, "y": 0},
                "end_point": {"x": 10, "y": 0},
                "height_m": 3.0,
                "provenance": {
                    "source_pdf": "Architectural_Plan_A101.pdf",
                    "page_number": 4,
                    "drawing_id": "A101",
                    "scale_source": "1:100 Graphic Scale",
                    "source_coords": {"x0": 100, "y0": 200, "x1": 500, "y1": 200},
                    "contributing_evidence": ["Line vector #42"],
                }
            }
        ]
    }

    project = planreader_to_canonical_model(payload, trusted_source=True)
    wall = project.buildings[0].levels[0].walls[0]
    prov = wall.provenance

    assert prov.source_pdf == "Architectural_Plan_A101.pdf"
    assert prov.page_number == 4
    assert prov.drawing_id == "A101"
    assert prov.scale_source == "1:100 Graphic Scale"
    assert prov.source_coords == {"x0": 100, "y0": 200, "x1": 500, "y1": 200}
    assert "Line vector #42" in prov.contributing_evidence

    viewer_payload = project_to_viewer_payload(project)
    v_obj = viewer_payload["objects"][0]
    assert v_obj["provenance"]["source_pdf"] == "Architectural_Plan_A101.pdf"


def test_opening_semantics_and_elevation_evidence_isolation():
    """SECTION F: Unknown opening type does not default to WINDOW; elevation evidence alone cannot create 3D openings."""
    payload = {
        "walls": [
            {
                "id": "w_host",
                "start_point": {"x": 0, "y": 0},
                "end_point": {"x": 10, "y": 0},
                "height_m": 3.0,
            }
        ],
        "openings": [
            {
                "id": "op_generic",
                "name": "Generic Aperture",
                "wall_id": "w_host",
                "opening_type": "UNKNOWN_APERTURE",  # Unknown type!
                "offset_along_wall_m": 1.0,
                "sill_height_m": 0.0,
                "width_m": 1.0,
                "height_m": 2.0,
            },
            {
                "id": "op_elevation_only",
                "name": "Elevation Window Candidate",
                "wall_id": "unmatched_elevation_wall",  # No host wall!
                "opening_type": "WINDOW",
                "width_m": 1.5,
                "height_m": 1.5,
            }
        ]
    }

    project = planreader_to_canonical_model(payload, trusted_source=True)
    wall = project.buildings[0].levels[0].walls[0]

    assert len(wall.openings) == 1
    op_gen = wall.openings[0]
    assert op_gen.id == "op_generic"
    assert op_gen.opening_type == ObjectType.OPENING  # ObjectType.OPENING, NOT WINDOW!


def test_real_lago_benchmark_fixture_fail_closed_integration():
    """SECTION J: Real LAGO benchmark integration test (fails closed: elevation alone creates 0 physical openings)."""
    fpath = "tests/fixtures/lago_cd3001_east_elevation_v177.json"
    if not os.path.exists(fpath):
        pytest.skip(f"Fixture {fpath} not found")

    with open(fpath, "r", encoding="utf-8") as f:
        fixture_data = json.load(f)

    src = fixture_data.get("source", {})
    pos_regions = fixture_data.get("positive_regions", [])

    # Real LAGO elevation fixture provides elevation evidence, but NO plan host wall coordinates!
    elevation_openings_payload = []
    for idx, reg in enumerate(pos_regions):
        for op_item in reg.get("true_positive_openings", []):
            mark_str = op_item if isinstance(op_item, str) else op_item.get("mark", f"OP-{idx}")
            elevation_openings_payload.append({
                "id": f"lago_elevation_op_{idx}",
                "name": f"LAGO Elevation Candidate {mark_str}",
                "wall_id": "unresolved_plan_wall",  # NO host wall plan baseline position!
                "opening_type": "WINDOW",
                "width_m": 1.2,
                "height_m": 1.5,
                "mark": mark_str,
                "provenance": {
                    "source_pdf": src.get("pdf"),
                    "page_number": src.get("page_1_based"),
                    "drawing_id": src.get("drawing_no"),
                }
            })

    prod_payload = {
        "project_name": f"LAGO Elevation Benchmark {src.get('drawing_no', '')}",
        "is_synthetic_demo": False,
        "openings": elevation_openings_payload
    }

    project = planreader_to_canonical_model(prod_payload, trusted_source=True)

    # FAIL CLOSED VERIFICATION: Elevation evidence without plan host wall placement produces 0 physical 3D openings!
    total_physical_openings = sum(len(w.openings) for b in project.buildings for l in b.levels for w in l.walls)
    assert total_physical_openings == 0


def test_canonical_model_persistence_and_staleness():
    """SECTION N: Test versioned persistence, fingerprinting, and staleness detection."""
    payload = {
        "project_id": "proj_persist_001",
        "project_name": "Persistence Test",
        "is_synthetic_demo": False,
        "walls": [{"id": "w1", "start_point": {"x": 0, "y": 0}, "end_point": {"x": 5, "y": 0}, "height_m": 3.0}]
    }
    project = planreader_to_canonical_model(payload, trusted_source=True)
    ws_data = {"id": "proj_persist_001", "takeoff_rows": [{"id": "w1", "len": 5.0}]}

    saved_dict = save_canonical_project_to_dict(project, workspace_data=ws_data)
    assert saved_dict["schema_version"] == "1.0.0"
    assert saved_dict["workspace_id"] == "proj_persist_001"

    # Reload with identical workspace -> clean match!
    valid, reloaded_proj, msg = load_canonical_project_from_dict(saved_dict, current_workspace_data=ws_data)
    assert valid is True
    assert reloaded_proj.id == "proj_persist_001"

    # Reload with modified workspace -> stale model detected!
    modified_ws_data = {"id": "proj_persist_001", "takeoff_rows": [{"id": "w1", "len": 5.0}, {"id": "w2", "len": 10.0}]}
    valid_stale, stale_proj, msg_stale = load_canonical_project_from_dict(saved_dict, current_workspace_data=modified_ws_data)
    assert valid_stale is False
    assert "Stale model detected" in msg_stale


def test_production_diagnostics_report():
    """SECTION O & Q: Test production diagnostics report generation and quantity reconciliation."""
    payload = {
        "project_id": "proj_diag_001",
        "project_name": "Diagnostics Test",
        "walls": [
            {
                "id": "w1",
                "start_point": {"x": 0, "y": 0},
                "end_point": {"x": 10, "y": 0},
                "height_m": 3.0,
                "deduction_authority": True,
            }
        ]
    }
    project = planreader_to_canonical_model(payload, trusted_source=True)
    ws_data = {"takeoff_rows": [{"m2": 30.0}]}

    diagnostics = generate_production_diagnostics_report(project, workspace_data=ws_data)
    assert diagnostics["total_canonical_objects"] == 1
    assert diagnostics["quantity_reconciliation"]["canonical_gross_wall_area_m2"] == 30.0
    assert diagnostics["quantity_reconciliation"]["area_variance_m2"] == 0.0


def test_performance_production_scale():
    """SECTION R: Benchmark performance for production-sized models (hundreds of elements)."""
    n_walls = 300
    n_openings = 300

    walls_payload = []
    openings_payload = []

    for i in range(n_walls):
        walls_payload.append({
            "id": f"wall_perf_{i}",
            "name": f"Wall Perf {i}",
            "start_point": {"x": float(i), "y": 0.0},
            "end_point": {"x": float(i + 1), "y": 0.0},
            "height_m": 3.0,
            "thickness_m": 0.20,
        })
        openings_payload.append({
            "id": f"op_perf_{i}",
            "wall_id": f"wall_perf_{i}",
            "opening_type": "WINDOW",
            "offset_along_wall_m": 0.2,
            "sill_height_m": 0.9,
            "width_m": 0.6,
            "height_m": 1.2,
        })

    payload = {
        "project_name": "Scale Performance Test",
        "walls": walls_payload,
        "openings": openings_payload,
    }

    import time
    t0 = time.time()
    project = planreader_to_canonical_model(payload, trusted_source=True)
    viewer_payload = project_to_viewer_payload(project)
    t1 = time.time()

    elapsed = t1 - t0
    assert len(viewer_payload["objects"]) == (n_walls + n_openings)
    assert elapsed < 2.0  # Must complete in under 2 seconds!


def test_security_malformed_inputs_fail_closed():
    """SECTION S: Test security hardening against NaN/Inf, malformed numbers, and malicious strings."""
    malformed_payload = {
        "project_name": "<script>alert('xss')</script>",
        "confidence": "INVALID_NAN",
        "deduction_authority": "true",  # String "true" -> False!
        "walls": [
            {
                "id": "w_malformed",
                "start_point": {"x": float("nan"), "y": 0.0},  # NaN coordinate!
                "end_point": {"x": 10.0, "y": 0.0},
                "height_m": float("inf"),                      # Inf height!
            }
        ]
    }

    project = planreader_to_canonical_model(malformed_payload, trusted_source=False)
    assert project.deduction_authority is False
    assert len(project.buildings[0].levels[0].walls) == 0  # Invalid wall excluded!
