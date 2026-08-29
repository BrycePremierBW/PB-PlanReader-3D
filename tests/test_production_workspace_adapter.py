"""
Unit and Integration test suite for Phase 5H Semantic Closure & Multi-Level Production Proof
(tests/test_production_workspace_adapter.py).

Verifies Sections A through AP:
1. Real LAGO evidence translation into CanonicalEvidenceObservation objects.
2. Page fallback absent (no pages -> zero mapper setting reads).
3. v139 takeoff_rows call signature requiring wall iterable (reg_walls), failing if integer workspace ID passed.
4. Robust v139 source_reference wall_ref identity parsing ('PB Unified Building v1.3.9 · W-E101' -> 'W-E101').
5. Duplicate reconciliation candidate detection (status = 'ambiguous', no last-write-wins).
6. Automatic B5 opening authority with manual_override_confirmed=False & field-by-field failure tests.
7. Fingerprint ordered vs unordered semantics (polygon vertex order change alters hash).
8. Document-page workspace ownership validation (missing/foreign document -> rejected).
9. Bounded session-state cache.
10. Application wrapper idempotency (double apply does not double wrap).
11. Real LAGO elevation evidence fail-closed integration test (0 physical openings).
12. Wrong-level opening rejection (opening level != wall level -> wrong_level conflict, 0 deduction).
13. Delayed wall deduction gate (wall deduction authority True ONLY when opening passes physical geometry validation).
"""

import os
import json
import sqlite3
from pathlib import Path

import cv2
import pytest
from pb_canonical_building import (
    CanonicalProject,
    CanonicalBuilding,
    CanonicalLevel,
    CanonicalWall,
    CanonicalRoof,
    CanonicalOpening,
    ReviewState,
    ObjectType,
    Vector2D,
    CanonicalEvidenceObservation,
    parse_strict_bool,
)
from pb_production_3d_adapter import (
    registered_wall_to_canonical_input,
    revalidate_b5_opening,
    resolve_canonical_level,
    require_workspace_id,
    collect_workspace_3d_evidence,
    planreader_to_canonical_model,
    planreader_workspace_to_canonical,
)
from pb_canonical_persistence import (
    save_workspace_canonical_model,
    load_workspace_canonical_model,
    compute_workspace_source_fingerprint,
    PERSISTENCE_KEY,
)
from pb_3d_diagnostics import generate_production_diagnostics_report
from pb_geometry_services import potential_net_wall_area, validate_opening_geometry
from pb_floor_mapper_v127 import calibration_px_per_m
from pb_floor_mapper_v128 import _points_from_shape
from pb_elevation_calibration_v177 import (
    COORD_SPACE_RENDER_PIXEL,
    calibration_from_scale_bar_positions,
)
from pb_elevation_raster_extract_v177 import detect_raster_rect_candidates


_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_LAGO_FIXTURE = _FIXTURES / "lago_cd3001_east_elevation_v177.json"
_LAGO_POSITIVE_CROP = _FIXTURES / "lago_cd3001_p86_e1east_glazed_open_group_150dpi.png"


def _real_lago_raster_candidates():
    """Run the committed page-86 crop through the real v177 raster producer."""
    with _LAGO_FIXTURE.open(encoding="utf-8") as fh:
        fixture = json.load(fh)
    image = cv2.imread(str(_LAGO_POSITIVE_CROP), cv2.IMREAD_GRAYSCALE)
    assert image is not None, f"real LAGO crop not readable: {_LAGO_POSITIVE_CROP}"
    dpi = float(fixture["render"]["dpi"])
    px_per_m = float(fixture["calibration"]["scale_pt_per_m"]) * dpi / 72.0
    calibration = calibration_from_scale_bar_positions(
        [0.0, px_per_m, 2.0 * px_per_m, 3.0 * px_per_m],
        1.0,
        coord_space=COORD_SPACE_RENDER_PIXEL,
        render_dpi=dpi,
    )
    source = fixture["source"]
    detected = detect_raster_rect_candidates(
        image,
        calibration,
        source_filename=source["local_source_alias"],
        source_page=source["page_1_based"],
        drawing_ref=source["drawing_no"],
        elevation_side=source["elevation_side"],
        calibration_source="page-86-cd3001-e1-east",
    )
    candidates = []
    for index, candidate in enumerate(detected, 1):
        candidate_payload = candidate.as_dict()
        candidate_payload["candidate_id"] = f"page86-raster-{index}"
        candidates.append(candidate_payload)
    return fixture, detected, candidates


class MockAppDB:
    """Mock PlanReader application instance mirroring real database & setting string storage."""
    def __init__(self, db_conn=None):
        self.settings = {}
        self.db_conn = db_conn
        self.read_setting_calls = []

    def set_workspace_setting(self, wid: int, key: str, value: any):
        self.settings[(int(wid), str(key))] = str(value) if not isinstance(value, str) else value

    def workspace_setting(self, wid: int, key: str, default: any = None):
        self.read_setting_calls.append((int(wid), str(key)))
        return self.settings.get((int(wid), str(key)), default)

    def lquery(self, query_str: str, params: tuple = ()):
        if self.db_conn:
            cursor = self.db_conn.cursor()
            cursor.execute(query_str, params)
            cols = [desc[0] for desc in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]
        return []


def test_section_a_b_real_v127_v128_mapper_math():
    """SECTION A & B: Test real pb_floor_mapper_v127.calibration_px_per_m and pb_floor_mapper_v128._points_from_shape."""
    calib = {"x1": 10.0, "y1": 10.0, "x2": 60.0, "y2": 10.0, "len_m": 10.0}
    # 50% of 1000px = 500px -> 500px / 10m = 50 px/m
    px_m = calibration_px_per_m(calib, 1000.0, 1000.0)
    assert abs(px_m - 50.0) < 1e-3

    rect_shape = {"x": 10.0, "y": 10.0, "w": 40.0, "h": 30.0}
    pts = _points_from_shape(rect_shape)
    assert len(pts) == 4
    assert pts[0] == {"x": 10.0, "y": 10.0}
    assert pts[2] == {"x": 50.0, "y": 40.0}


def test_section_c_no_pages_causes_zero_mapper_reads(tmp_path):
    """SECTION C: Test zero pages in workspace causes ZERO floor_mapper_v127_page_1 reads."""
    db_file = tmp_path / "planreader.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE workspaces (id INTEGER PRIMARY KEY, job_no TEXT, job_name TEXT, builder_client TEXT, site_address TEXT)")
    conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, workspace_id INTEGER, file_name TEXT, sha256 TEXT, category TEXT, page_count INTEGER, source_type TEXT)")
    conn.execute("CREATE TABLE pages (id INTEGER PRIMARY KEY, workspace_id INTEGER, document_id INTEGER, page_no INTEGER, page_label TEXT, page_type TEXT, scale_text TEXT, px_per_m REAL, width_px REAL, height_px REAL, render_zoom REAL, selected INTEGER)")

    conn.execute("INSERT INTO workspaces (id, job_no, job_name) VALUES (101, 'JOB-99', 'Commercial Building CD3001')")
    conn.commit()

    app = MockAppDB(conn)
    snapshot = collect_workspace_3d_evidence(app, 101)
    
    assert len(snapshot["pages"]) == 0
    assert len(snapshot["mapper_shapes"]) == 0
    # SECTION C: Prove ZERO page 1 mapper setting reads were performed!
    assert not any("floor_mapper_v127_page_1" in k for _, k in app.read_setting_calls)
    conn.close()


def test_section_e_v139_takeoff_rows_call_signature():
    """SECTION E: Test v139 takeoff_rows signature requiring wall iterable (reg_walls), failing if integer workspace ID passed."""
    import pb_unified_building_v139 as ub
    walls = [{"wall_ref": "W01", "substrate": "Concrete", "side": "EAST", "net_m2": 45.0, "gross_m2": 50.0, "opening_deduction_m2": 5.0}]
    
    # Correct signature takes wall iterable
    rows = ub.takeoff_rows(walls)
    assert len(rows) == 1
    assert "W01" in rows[0]["source_reference"]

    # Passing integer workspace ID raises TypeError or AttributeError
    with pytest.raises((TypeError, AttributeError)):
        ub.takeoff_rows(101)


def test_section_f_g_v139_source_reference_parsing_and_ambiguous_reconciliation():
    """SECTION F & G: Test v139 source_reference wall_ref parsing and multi-candidate ambiguity rejection."""
    project_payload = {
        "walls": [
            {
                "wall_ref": "W01",
                "a": {"x": 0, "y": 0},
                "b": {"x": 10, "y": 0},
                "height_m": 3.0,
                "height_status": "confirmed",
            }
        ]
    }
    project, _ = planreader_to_canonical_model(project_payload, is_validated_internal_workspace=True)

    # Two duplicate takeoff rows matching W01 -> must be marked ambiguous (NO LAST-WRITE-WINS!)
    ws_data = {
        "takeoff_rows": [
            {"source_reference": "PB Unified Building v1.3.9 · W01", "quantity": 30.0, "unit": "m²", "row_role": "wall"},
            {"source_reference": "PB Unified Building v1.3.9 · W01", "quantity": 30.0, "unit": "m²", "row_role": "wall"},
        ]
    }

    diagnostics = generate_production_diagnostics_report(project, workspace_data=ws_data)
    rec = diagnostics["per_wall_quantity_reconciliation"][0]
    assert rec["reconciliation_status"] == "ambiguous"
    assert "Rejection of last-write-wins" in rec["explanation"]


def test_section_m_automatic_b5_opening_authority_and_field_failures():
    """SECTION M: Test automatic B5 opening authority without manual override, and prove field-by-field failure closed."""
    valid_b5_op = {
        "id": "op_auto_1",
        "resolved_wall_ref": "W-1",
        "width_m": 1.5,
        "height_m": 2.0,
        "deduct": True,
        "manual_override_confirmed": False,  # AUTOMATIC B5!
        "reconciliation_complete": True,
        "deduction_status": "auto_eligible",
        "deduction_decision": "deducted",
        "dimension_basis": "rough_opening",
        "geometry_confidence": 0.95,
        "dimension_confidence": 0.95,
        "association_confidence": 0.95,
    }

    # Valid automatic B5 pass
    assert revalidate_b5_opening(valid_b5_op) is True

    # Mutate each field individually to prove fail closed!
    bad_status = {**valid_b5_op, "deduction_status": "ineligible"}
    assert revalidate_b5_opening(bad_status) is False

    bad_decision = {**valid_b5_op, "deduction_decision": "rejected"}
    assert revalidate_b5_opening(bad_decision) is False

    bad_reconcile = {**valid_b5_op, "reconciliation_complete": False}
    assert revalidate_b5_opening(bad_reconcile) is False

    bad_confidence = {**valid_b5_op, "geometry_confidence": 0.30}
    assert revalidate_b5_opening(bad_confidence) is False


def _e2e_b5_payload(**op_overrides):
    """Builds a single-wall, single-opening canonical payload with FULL automatic B5 fields."""
    op = {
        "id": "op_e2e_b5",
        "opening_type": "WINDOW",
        "resolved_wall_ref": "W-E101",
        "level": "Ground",
        "offset_along_wall_m": 2.0,
        "sill_height_m": 0.9,
        "width_m": 1.5,
        "height_m": 2.0,
        "deduct": True,
        "manual_override_confirmed": False,  # AUTOMATIC B5!
        "reconciliation_complete": True,
        "deduction_status": "auto_eligible",
        "deduction_decision": "deducted",
        "dimension_basis": "rough_opening",
        "geometry_confidence": 0.95,
        "dimension_confidence": 0.95,
        "association_confidence": 0.95,
    }
    op.update({k: v for k, v in op_overrides.items()})
    return {
        "walls": [
            {
                "wall_ref": "W-E101",
                "level": "Ground",
                "a": {"x": 0, "y": 0},
                "b": {"x": 10, "y": 0},
                "height_m": 3.0,
                "thickness_m": 0.3,
                "openings": [op],
            }
        ]
    }


def test_section_f_e2e_automatic_b5_canonical_deduction_and_field_failure():
    """Blocker #9: FULL end-to-end automatic B5 deduction through the canonical model.

    Verifies the WHOLE gate chain, not just the isolated revalidate_b5_opening():
      1. Wall deduction gate resolves True.
      2. Opening deduction_authority resolves True (review_state CONFIRMED).
      3. potential_net_wall_area() returns a REDUCED net area (gross - opening).
      4. Breaking ANY one of the ~9 required B5 fields independently fails the deduction
         gate closed: opening authority False, wall gate False, and net area == gross
         (no deduction applied).
    """
    # 1) PASS path — full automatic B5 through the model.
    project, skipped = planreader_to_canonical_model(_e2e_b5_payload(), is_validated_internal_workspace=True)
    wall = project.buildings[0].levels[0].walls[0]
    op = wall.openings[0]
    assert parse_strict_bool(wall.deduction_authority) is True
    assert parse_strict_bool(op.deduction_authority) is True
    assert op.review_state == ReviewState.CONFIRMED
    assert op.wall_id == wall.id
    assert op.parent_id == wall.id
    assert op.level_id == wall.level_id

    p_net = potential_net_wall_area(wall)
    gross = p_net["gross_wall_area_m2"]
    net = p_net["authorized_net_area_m2"]
    assert gross == 30.0  # 10 m x 3 m wall
    assert net < gross  # REDUCED net — deduction actually applied
    assert abs(p_net["authorized_opening_deduction_area_m2"] - (1.5 * 2.0)) < 1e-6

    # 2) FAIL paths — break each required B5 field ONE AT A TIME and confirm the full gate
    #    (opening authority, wall gate, reduced net) fails closed.
    required_field_mutations = {
        "missing wall_ref":              {"resolved_wall_ref": None},
        "wrong nonblank wall_ref":       {"resolved_wall_ref": "W-OTHER"},
        "deduct disabled":               {"deduct": False},
        "missing width":                 {"width_m": None},
        "non-positive height":           {"height_m": 0},
        "reconciliation incomplete":     {"reconciliation_complete": False},
        "bad deduction_status":          {"deduction_status": "ineligible"},
        "bad deduction_decision":        {"deduction_decision": "rejected"},
        "bad dimension_basis":           {"dimension_basis": "guess"},
        "introspect geometry_confidence": {"geometry_confidence": 0.30},
        "introspect dimension_confidence": {"dimension_confidence": 0.30},
        "introspect association_confidence": {"association_confidence": 0.30},
    }
    for label, overrides in required_field_mutations.items():
        project_f, skipped_f = planreader_to_canonical_model(
            _e2e_b5_payload(**overrides), is_validated_internal_workspace=True
        )
        wall_f = project_f.buildings[0].levels[0].walls[0]
        op_f = wall_f.openings[0]
        assert parse_strict_bool(wall_f.deduction_authority) is False, label
        assert parse_strict_bool(op_f.deduction_authority) is False, label
        assert op_f.review_state == ReviewState.REVIEW_REQUIRED, label
        net_f = potential_net_wall_area(wall_f)["authorized_net_area_m2"]
        assert abs(net_f - potential_net_wall_area(wall_f)["gross_wall_area_m2"]) < 1e-6, (
            f"{label}: net must equal gross (no deduction applied)"
        )


def test_section_r_fingerprint_ordered_vs_unordered_semantics():
    """SECTION R: Test ordered geometry order change alters SHA-256 fingerprint."""
    snapshot1 = {
        "workspace_metadata": {"id": 101},
        "registered_walls": [
            {"wall_ref": "W-1", "a": {"x": 0, "y": 0}, "b": {"x": 10, "y": 0}},
        ]
    }
    # Change vertex order of wall endpoints
    snapshot2 = {
        "workspace_metadata": {"id": 101},
        "registered_walls": [
            {"wall_ref": "W-1", "a": {"x": 10, "y": 0}, "b": {"x": 0, "y": 0}},
        ]
    }

    fp1 = compute_workspace_source_fingerprint(snapshot1)
    fp2 = compute_workspace_source_fingerprint(snapshot2)
    assert fp1 != fp2  # Geometric order change MUST alter fingerprint!


def test_fingerprint_preserves_only_actual_geometry_order():
    """Blocker #7: unordered rows stay stable; coordinates and winding do not."""
    base = {
        "workspace_metadata": {"id": 101},
        "registered_walls": [{
            "wall_ref": "W-1",
            "a": {"x": 0, "y": 0},
            "b": {"x": 10, "y": 0},
            "openings": [
                {"id": "op-a", "width_m": 1.0},
                {"id": "op-b", "width_m": 2.0},
            ],
        }],
        "roof_data": {
            "caps": [{
                "id": "roof-1",
                "points": [[0.0, 1.0], [2.0, 3.0], [4.0, 5.0]],
                "triangles": [[0, 1, 2]],
            }],
        },
    }
    openings_reordered = json.loads(json.dumps(base))
    openings_reordered["registered_walls"][0]["openings"].reverse()
    assert compute_workspace_source_fingerprint(base) == compute_workspace_source_fingerprint(openings_reordered)

    coordinate_reversed = json.loads(json.dumps(base))
    coordinate_reversed["roof_data"]["caps"][0]["points"][0] = [1.0, 0.0]
    assert compute_workspace_source_fingerprint(base) != compute_workspace_source_fingerprint(coordinate_reversed)

    winding_reversed = json.loads(json.dumps(base))
    winding_reversed["roof_data"]["caps"][0]["triangles"][0] = [0, 2, 1]
    assert compute_workspace_source_fingerprint(base) != compute_workspace_source_fingerprint(winding_reversed)


def test_section_q_document_page_workspace_ownership(tmp_path):
    """SECTION Q: Test page referencing missing or foreign document ID is rejected."""
    db_file = tmp_path / "planreader.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE workspaces (id INTEGER PRIMARY KEY, job_no TEXT, job_name TEXT, builder_client TEXT, site_address TEXT)")
    conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, workspace_id INTEGER, file_name TEXT, sha256 TEXT, category TEXT, page_count INTEGER, source_type TEXT)")
    conn.execute("CREATE TABLE pages (id INTEGER PRIMARY KEY, workspace_id INTEGER, document_id INTEGER, page_no INTEGER, page_label TEXT, page_type TEXT, scale_text TEXT, px_per_m REAL, width_px REAL, height_px REAL, render_zoom REAL, selected INTEGER)")

    conn.execute("INSERT INTO workspaces (id, job_no, job_name) VALUES (101, 'JOB-99', 'Commercial Building CD3001')")
    conn.execute("INSERT INTO documents (id, workspace_id, file_name) VALUES (10, 101, 'doc_10.pdf')")
    # Page references document 999 which does NOT exist in workspace!
    conn.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, page_label) VALUES (1, 101, 999, 1, 'A101')")
    conn.commit()

    app = MockAppDB(conn)
    snapshot = collect_workspace_3d_evidence(app, 101)
    
    # Page referencing missing document 999 is omitted from pages snapshot!
    assert len(snapshot["pages"]) == 0
    assert any("stale_reference" in d.get("type", "") for d in snapshot["diagnostics_log"])
    conn.close()


def test_section_62_application_wrapper_idempotency():
    """SECTION 62: Test apply(app) idempotency guard."""
    from pb_3d_workspace_integration import apply as apply_3d_canonical_integration

    class DummyApp:
        def model_3d_page(self, workspace): pass

    app = DummyApp()
    apply_3d_canonical_integration(app)
    first_wrapper = app.model_3d_page

    apply_3d_canonical_integration(app)
    second_wrapper = app.model_3d_page

    assert first_wrapper is second_wrapper


def test_section_n_real_lago_elevation_fail_closed_assertion():
    """SECTION N & A: Test real LAGO elevation benchmark (0 plan host walls -> 0 physical 3D openings -> 0 deductions).

    Blocker #1: candidates come from detect_raster_rect_candidates() operating on
    the committed real page-86 crop with its measured calibration. Independent
    benchmark truth is never re-labelled as detector output. Zero plan host walls
    still fails closed to zero physical instances and deductions.
    """
    fixture_data, detected, real_candidates = _real_lago_raster_candidates()
    assert len(detected) > 0
    assert len(real_candidates) == len(detected)

    lago_payload = {
        "workspace_metadata": {"id": "lago_cd3001"},
        "registered_walls": [],  # Zero plan host walls!
        "elevation_opening_candidates": real_candidates,
    }

    res = planreader_to_canonical_model(lago_payload, is_validated_internal_workspace=True)
    project = res[0]
    bld = project.buildings[0]

    total_physical_openings = sum(len(w.openings) for l in bld.levels for w in l.walls)
    assert total_physical_openings == 0
    observations = project.evidence_observations
    assert len(observations) == len(detected)
    assert all(obs.producer_version == "v177" for obs in observations)
    assert all(obs.page_no == fixture_data["source"]["page_1_based"] for obs in observations)
    assert all(obs.drawing_reference == fixture_data["source"]["drawing_no"] for obs in observations)
    assert all(obs.side == fixture_data["source"]["elevation_side"] for obs in observations)
    assert all(obs.level_name is None for obs in observations)  # no fabricated Ground assignment
    assert all(obs.source_coords["source_filename"] == fixture_data["source"]["local_source_alias"] for obs in observations)
    assert all(obs.source_coords["coord_space"] == COORD_SPACE_RENDER_PIXEL for obs in observations)
    assert all(obs.source_coords["calibration_source"] == "page-86-cd3001-e1-east" for obs in observations)


def test_section_c_wrong_level_opening_rejection():
    """SECTION C: Test opening assigned to Level 2 attached to Ground Floor wall causes wrong_level conflict and 0 deduction."""
    payload = {
        "walls": [
            {
                "wall_ref": "W-G01",
                "level": "Ground",
                "a": {"x": 0, "y": 0},
                "b": {"x": 10, "y": 0},
                "height_m": 3.0,
                "openings": [
                    {
                        "id": "op_wrong_lvl",
                        "opening_type": "DOOR",
                        "level": "Level 2",  # WRONG LEVEL!
                        "offset_along_wall_m": 2.0,
                        "sill_height_m": 0.0,
                        "width_m": 1.0,
                        "height_m": 2.1,
                        "deduct": True,
                        "manual_override_confirmed": True,
                    }
                ]
            }
        ]
    }

    project, skipped = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    wall = project.buildings[0].levels[0].walls[0]
    opening = wall.openings[0]

    # Deduction authority MUST be set to False due to wrong level conflict!
    assert opening.deduction_authority is False
    assert wall.deduction_authority is False
    assert opening.metadata["physical_state"] == "wrong_level"
    assert opening.wall_id == wall.id
    assert opening.level_id != wall.level_id  # contradictory evidence is not rewritten onto the host
    assert any("Wrong level conflict" in str(s.get("reason", "")) for s in skipped)


def test_all_opening_level_identities_must_agree_and_numeric_zero_is_preserved():
    base_opening = {
        "id": "op_level_identity",
        "opening_type": "DOOR",
        "offset_along_wall_m": 2.0,
        "sill_height_m": 0.0,
        "width_m": 1.0,
        "height_m": 2.1,
        "deduct": True,
        "manual_override_confirmed": True,
        "resolved_wall_ref": "W-G01",
    }

    conflicting = {
        "walls": [{
            "wall_ref": "W-G01",
            "level": "Ground",
            "a": {"x": 0, "y": 0},
            "b": {"x": 10, "y": 0},
            "height_m": 3.0,
            "openings": [{**base_opening, "level": "Ground", "level_id": "Level 2"}],
        }]
    }
    project, _ = planreader_to_canonical_model(conflicting, is_validated_internal_workspace=True)
    wall = next(level.walls[0] for level in project.buildings[0].levels if level.walls)
    opening = wall.openings[0]
    assert opening.metadata["physical_state"] == "wrong_level"
    assert opening.deduction_authority is False
    assert opening.level_id is None

    numeric_zero = {
        "levels": [{"id": "level_zero", "name": "Ground", "elevation_m": 0.0}],
        "walls": [{
            "wall_ref": "W-0",
            "level": 0,
            "a": {"x": 0, "y": 0},
            "b": {"x": 10, "y": 0},
            "height_m": 3.0,
            "openings": [{**base_opening, "resolved_wall_ref": "W-0", "level": 0}],
        }],
    }
    project_zero, _ = planreader_to_canonical_model(numeric_zero, is_validated_internal_workspace=True)
    wall_zero = next(level.walls[0] for level in project_zero.buildings[0].levels if level.walls)
    opening_zero = wall_zero.openings[0]
    assert opening_zero.metadata["physical_state"] == "physical_b5_authorised"
    assert opening_zero.level_id == wall_zero.level_id


def test_wrong_nonblank_host_reference_fails_closed_before_b5_deduction():
    payload = _e2e_b5_payload(resolved_wall_ref="W-OTHER")
    project, skipped = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    wall = next(level.walls[0] for level in project.buildings[0].levels if level.walls)
    opening = wall.openings[0]
    assert opening.metadata["physical_state"] == "wrong_host"
    assert opening.wall_id is None
    assert opening.parent_id is None
    assert opening.deduction_authority is False
    assert wall.deduction_authority is False
    areas = potential_net_wall_area(wall)
    assert areas["authorized_net_area_m2"] == areas["gross_wall_area_m2"]
    assert any("Wrong host conflict" in str(item.get("reason", "")) for item in skipped)


def test_physical_non_authorised_opening_remains_physical_in_diagnostics():
    payload = {
        "walls": [{
            "wall_ref": "W-QA",
            "level": "Ground",
            "a": {"x": 0, "y": 0},
            "b": {"x": 8, "y": 0},
            "height_m": 3.0,
            "openings": [{
                "id": "op_observed",
                "resolved_wall_ref": "W-QA",
                "offset_along_wall_m": 1.0,
                "sill_height_m": 0.9,
                "width_m": 1.2,
                "height_m": 1.2,
                "deduct": False,
            }],
        }],
    }
    project, skipped = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    report = generate_production_diagnostics_report(project, skipped_items=skipped)
    qa = report["estimator_qa_summary"]
    assert qa["physical_openings"] == 1
    assert qa["evidence_only_openings"] == 0
    assert qa["authorised_b5_deductions"] == 0
    assert qa["opening_state_counts"]["physical_not_authorised"] == 1


class RuntimeProducerMockApp:
    """Full mock app exposing EVERY producer the production adapter consumes at runtime."""

    def __init__(self, db_conn):
        self.settings = {}
        self.db_conn = db_conn
        self.read_setting_calls = []

    def set_workspace_setting(self, wid, key, value):
        self.settings[(int(wid), str(key))] = str(value) if not isinstance(value, str) else value

    def workspace_setting(self, wid, key, default=None):
        self.read_setting_calls.append((int(wid), str(key)))
        return self.settings.get((int(wid), str(key)), default)

    def lquery(self, query_str, params=()):
        cur = self.db_conn.cursor()
        cur.execute(query_str, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def build_registered_walls_v139(self, wid):
        class _Wall:
            def to_dict(self):
                return {
                    "wall_ref": "W-E101", "level": "Ground",
                    "a": {"x": 0.0, "y": 0.0}, "b": {"x": 10.0, "y": 0.0},
                    "height_m": 3.0, "thickness_m": 0.3,
                    "provenance": {"wall_ref": "W-E101", "drawing_id": "A101", "page_no": 1},
                    "openings": [],
                }
        return [_Wall()]

    def registered_wall_takeoff_rows_v139(self, walls):
        return [
            {"id": 1, "wall_ref": "W-E101", "quantity": 30.0, "unit": "m²", "row_role": "wall",
             "source_reference": "PB Unified Building v1.3.9 · W-E101"}
        ]

    def roof_evidence_v140(self, wid):
        return {"pitches_deg": [], "parapet": True, "flat": True,
                "status": "Flat/parapet roof evidence identified", "confidence": "High"}

    def roof_caps_v140(self, wid, walls):
        return []


def test_v140_fixture_matches_live_roof_producer_contract():
    """Blocker #8: the committed contract is generated by the real v140 producers."""
    from pb_roof_envelope_v140 import roof_caps, roof_evidence

    class RoofProducerApp:
        def lquery(self, query, params=()):
            return [{
                "page_label": "Roof Plan",
                "page_type": "Roof Plan",
                "extracted_text": "ROOF PITCH 22.5 DEG",
            }]

        def build_precision_prisms(self, workspace_id):
            return [{
                "points": [[0.0, 0.0], [18.5, 0.0], [18.5, 12.0], [0.0, 12.0]],
                "triangles": [[3, 0, 1], [1, 2, 3]],
                "level_name": "Ground",
            }]

    app = RoofProducerApp()
    walls = [{"wall_ref": "W-G", "height_m": 3.2}]
    live = {
        "producer": "v140",
        "evidence": roof_evidence(app, 101),
        "caps": roof_caps(app, 101, walls),
    }
    with (_FIXTURES / "v140_roof_evidence_contract.json").open(encoding="utf-8") as fh:
        expected = json.load(fh)
    expected.pop("_comment", None)
    assert live == expected


def test_v140_evidence_without_caps_is_explicitly_evidence_only():
    payload = {
        "workspace_id": 101,
        "roof_data": {
            "producer": "v140",
            "evidence": {
                "pitches_deg": [],
                "parapet": True,
                "flat": True,
                "status": "Flat/parapet roof evidence identified",
                "confidence": "High",
            },
            "caps": [],
        },
    }
    project, skipped = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    assert sum(len(level.roofs) for level in project.buildings[0].levels) == 0
    report = generate_production_diagnostics_report(project, workspace_data=payload, skipped_items=skipped)
    qa = report["estimator_qa_summary"]
    assert qa["roof_geometry_rendered"] == 0
    assert qa["roof_evidence_observations"] == 1
    assert qa["roof_evidence_only"] == 1


def test_section_x_runtime_production_pipeline_evidence():
    """Blocker #10 (runtime proof): Exercises the REAL production runtime entry point
    planreader_workspace_to_canonical end-to-end against a full producer-backed mock app.

    Proves the whole chain runs at runtime and emits DISTINCT runtime artifacts:
      - a non-empty, DETERMINISTIC source fingerprint (SHA-style runtime identity that
        replays identically for identical evidence),
      - a production diagnostic report tied to the workspace,
      - a canonical project with levels, walls and calibrated floors,
      - per-wall takeoff reconciliation candidates from REAL rows.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE workspaces (id INTEGER PRIMARY KEY, job_no TEXT, job_name TEXT, builder_client TEXT, site_address TEXT)")
    conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, workspace_id INTEGER, file_name TEXT, sha256 TEXT, category TEXT, page_count INTEGER, source_type TEXT)")
    conn.execute("CREATE TABLE pages (id INTEGER PRIMARY KEY, workspace_id INTEGER, document_id INTEGER, page_no INTEGER, page_label TEXT, page_type TEXT, scale_text TEXT, px_per_m REAL, width_px REAL, height_px REAL, render_zoom REAL, selected INTEGER)")
    conn.execute("INSERT INTO workspaces (id, job_no, job_name) VALUES (201, 'JOB-RUN', 'Runtime Proof Building')")
    conn.execute("INSERT INTO documents (id, workspace_id, file_name) VALUES (50, 201, 'revA.pdf')")
    conn.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, page_label, page_type, scale_text, px_per_m, width_px, height_px, render_zoom, selected) "
                 "VALUES (2, 201, 50, 1, 'Ground Floor', 'Floor Plan', '1:100', NULL, 1000.0, 1000.0, 1.0, 1)")
    conn.commit()

    app = RuntimeProducerMockApp(conn)
    # Real v1.2.7 mapper state: boxes + calibration (px_per_m is DERIVED at runtime by
    # calibration_px_per_m, never read as a persisted fallback).
    # SECTION 5: setting key uses page_id = 2 (NOT page_no = 1)!
    app.set_workspace_setting(201, "floor_mapper_v127_page_2", json.dumps({
        "boxes": [{"x": 10.0, "y": 10.0, "w": 20.0, "h": 20.0}],
        "calibration": {"x1": 10.0, "y1": 10.0, "x2": 60.0, "y2": 10.0, "len_m": 10.0},
    }))

    result = planreader_workspace_to_canonical(app, 201)

    # Distinct runtime artifacts exist.
    assert result.snapshot_fingerprint and len(result.snapshot_fingerprint) == 16
    assert result.project is not None
    assert result.diagnostics is not None
    assert result.diagnostics.get("workspace_id") == 201

    # Deterministic runtime identity: identical evidence replays to the SAME fingerprint.
    replay = planreader_workspace_to_canonical(app, 201)
    assert replay.snapshot_fingerprint == result.snapshot_fingerprint

    # Real levels/walls/floors materialised.
    bld = result.project.buildings[0]
    assert len(bld.levels) >= 1
    ground = {l.name.lower(): l for l in bld.levels}
    assert any("ground" in key for key in ground), f"Ground storey missing: {[l.name for l in bld.levels]}"
    assert any(len(l.walls) > 0 for l in bld.levels)
    assert any(len(l.floors) >= 1 for l in bld.levels)  # mapper floor materialised on a real storey

    # Walls carry a strong wall_ref identity.
    assert any(w.provenance.wall_ref == "W-E101" for l in bld.levels for w in l.walls)

    # Per-wall reconciliation ran against the real takeoff row (W-E101, 30 m²).
    rec = result.diagnostics.get("per_wall_quantity_reconciliation") or []
    assert any(r.get("wall_ref") == "W-E101" for r in rec)

    conn.close()


def test_runtime_collector_keeps_three_mapper_storeys_isolated():
    """Blocker #5: runtime pages resolve only through registered storey authority."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE workspaces (id INTEGER PRIMARY KEY, job_no TEXT, job_name TEXT, builder_client TEXT, site_address TEXT)")
    conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, workspace_id INTEGER, file_name TEXT, sha256 TEXT, category TEXT, page_count INTEGER, source_type TEXT)")
    conn.execute("CREATE TABLE pages (id INTEGER PRIMARY KEY, workspace_id INTEGER, document_id INTEGER, page_no INTEGER, page_label TEXT, page_type TEXT, scale_text TEXT, px_per_m REAL, width_px REAL, height_px REAL, render_zoom REAL, selected INTEGER)")
    conn.execute("INSERT INTO workspaces (id, job_no, job_name) VALUES (301, 'JOB-3L', 'Three Storey Runtime')")
    conn.execute("INSERT INTO documents (id, workspace_id, file_name) VALUES (60, 301, 'three-level.pdf')")
    for page_id, page_no, page_label in ((11, 1, "Ground"), (12, 2, "Level 1"), (13, 3, "Level 2")):
        conn.execute(
            "INSERT INTO pages (id, workspace_id, document_id, page_no, page_label, page_type, width_px, height_px, selected) VALUES (?, 301, 60, ?, ?, 'Floor Plan', 1000.0, 1000.0, 1)",
            (page_id, page_no, page_label),
        )
    conn.commit()

    class ThreeLevelApp(RuntimeProducerMockApp):
        def build_registered_walls_v139(self, wid):
            class Wall:
                def __init__(self, ref, level, y):
                    self.data = {
                        "wall_ref": ref,
                        "level": level,
                        "a": {"x": 0.0, "y": y},
                        "b": {"x": 10.0, "y": y},
                        "height_m": 3.0,
                        "height_status": "confirmed",
                        "openings": [],
                    }

                def to_dict(self):
                    return dict(self.data)

            return [
                Wall("W-G", "Ground", 0.0),
                Wall("W-L1", "Level 1", 10.0),
                Wall("W-L2", "Level 2", 20.0),
            ]

        def registered_wall_takeoff_rows_v139(self, walls):
            return []

    app = ThreeLevelApp(conn)
    for page_id in (11, 12, 13):
        app.set_workspace_setting(301, f"floor_mapper_v127_page_{page_id}", json.dumps({
            "boxes": [{"id": f"floor-{page_id}", "x": 10.0, "y": 10.0, "w": 20.0, "h": 20.0}],
            "calibration": {"x1": 10.0, "y1": 10.0, "x2": 60.0, "y2": 10.0, "len_m": 10.0},
        }))

    result = planreader_workspace_to_canonical(app, 301)
    assert [shape["_source_level"]["id"] for shape in result.snapshot["mapper_shapes"]] == [
        "ground", "level_1", "level_2"
    ]
    floors_by_level = {
        level.id: [floor.id for floor in level.floors]
        for level in result.project.buildings[0].levels
        if level.floors
    }
    assert floors_by_level == {
        "ground": ["floor-11"],
        "level_1": ["floor-12"],
        "level_2": ["floor-13"],
    }
    assert all(
        floor.takeoff_eligible
        for level in result.project.buildings[0].levels
        for floor in level.floors
    )
    conn.close()


def test_section_1_b5_authority_delegation_and_rejected_variants():
    """SECTION 1: Test revalidate_b5_opening delegates to pb_opening_production_v175.is_authorised_deduction and rejects non-v175 variants."""
    from pb_opening_production_v175 import is_authorised_deduction

    # Manual override confirmed without wall_ref -> rejected by v175 and Phase 5!
    no_wall = {"manual_override_confirmed": True, "width_m": 1.0, "height_m": 2.0, "deduct": True}
    assert is_authorised_deduction(no_wall) is False
    assert revalidate_b5_opening(no_wall) is False

    # Manual override confirmed without dimensions -> rejected!
    no_dims = {"manual_override_confirmed": True, "wall_ref": "W1", "deduct": True}
    assert is_authorised_deduction(no_dims) is False
    assert revalidate_b5_opening(no_dims) is False

    # Rejection of unbacked status/decision variants
    variants = [
        {"deduction_status": "approved", "wall_ref": "W1", "width_m": 1.0, "height_m": 2.0, "deduct": True},
        {"deduction_status": "confirmed_deduction", "wall_ref": "W1", "width_m": 1.0, "height_m": 2.0, "deduct": True},
        {"deduction_decision": "approved", "wall_ref": "W1", "width_m": 1.0, "height_m": 2.0, "deduct": True},
        {"dimension_basis": "net_opening", "wall_ref": "W1", "width_m": 1.0, "height_m": 2.0, "deduct": True},
        {"dimension_basis": "schedule_dimension", "wall_ref": "W1", "width_m": 1.0, "height_m": 2.0, "deduct": True},
        {"dimension_basis": "provenance_confirmed", "wall_ref": "W1", "width_m": 1.0, "height_m": 2.0, "deduct": True},
    ]
    for var in variants:
        assert revalidate_b5_opening(var) == is_authorised_deduction(var)
        assert revalidate_b5_opening(var) is False


def test_section_2_validate_opening_geometry_tuple_unpack():
    """SECTION 2: Test validate_opening_geometry returns (bool, str) tuple and wall deduction gate remains False on geometry failure."""
    wall = CanonicalWall(
        id="w_short", name="Short Wall", start_point=Vector2D(x=0, y=0), end_point=Vector2D(x=2.0, y=0), height_m=3.0
    )
    # Opening exceeds wall length (width 3.0m on 2.0m wall)
    op = CanonicalOpening(
        id="op_huge", name="Huge Window", width_m=3.0, height_m=1.5, offset_along_wall_m=0.0, sill_height_m=0.9
    )
    is_valid, msg = validate_opening_geometry(op, wall)
    assert is_valid is False
    assert "exceeds wall length" in msg


def test_section_3_never_invent_sill_or_offset_defaults():
    """SECTION 3: Test missing sill/offset fails geometry validation while explicit 0.0 passes."""
    wall = CanonicalWall(id="w1", name="Wall 1", start_point=Vector2D(x=0, y=0), end_point=Vector2D(x=10, y=0), height_m=3.0)

    op_no_sill = CanonicalOpening(id="o1", name="O1", width_m=1.0, height_m=2.0, offset_along_wall_m=1.0, sill_height_m=None)
    val_sill, _ = validate_opening_geometry(op_no_sill, wall)
    assert val_sill is False  # Missing sill -> invalid geometry

    op_zero_sill = CanonicalOpening(id="o2", name="O2", width_m=1.0, height_m=2.0, offset_along_wall_m=1.0, sill_height_m=0.0)
    val_zero_sill, _ = validate_opening_geometry(op_zero_sill, wall)
    assert val_zero_sill is True  # Sill 0.0 -> valid geometry

    op_no_off = CanonicalOpening(id="o3", name="O3", width_m=1.0, height_m=2.0, offset_along_wall_m=None, sill_height_m=0.0)
    val_off, _ = validate_opening_geometry(op_no_off, wall)
    assert val_off is False  # Missing offset -> invalid geometry

    op_zero_off = CanonicalOpening(id="o4", name="O4", width_m=1.0, height_m=2.0, offset_along_wall_m=0.0, sill_height_m=0.0)
    val_zero_off, _ = validate_opening_geometry(op_zero_off, wall)
    assert val_zero_off is True  # Offset 0.0 -> valid geometry


def test_section_5_v127_setting_key_uses_page_id(tmp_path):
    """SECTION 5: Test floor_mapper_v127 uses page_id (e.g. 73) NOT page_no (e.g. 4)."""
    db_file = tmp_path / "planreader.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE workspaces (id INTEGER PRIMARY KEY, job_no TEXT, job_name TEXT, builder_client TEXT, site_address TEXT)")
    conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, workspace_id INTEGER, file_name TEXT, sha256 TEXT, category TEXT, page_count INTEGER, source_type TEXT)")
    conn.execute("CREATE TABLE pages (id INTEGER PRIMARY KEY, workspace_id INTEGER, document_id INTEGER, page_no INTEGER, page_label TEXT, page_type TEXT, scale_text TEXT, px_per_m REAL, width_px REAL, height_px REAL, render_zoom REAL, selected INTEGER)")

    conn.execute("INSERT INTO workspaces (id, job_no, job_name) VALUES (101, 'JOB-73', 'Test Job')")
    conn.execute("INSERT INTO documents (id, workspace_id, file_name) VALUES (10, 101, 'doc.pdf')")
    conn.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, page_label, width_px, height_px) VALUES (73, 101, 10, 4, 'Ground', 1000.0, 1000.0)")
    conn.commit()

    app = MockAppDB(conn)
    app.set_workspace_setting(101, "floor_mapper_v127_page_73", json.dumps({"boxes": [{"x": 10, "y": 10, "w": 20, "h": 20}]}))

    snapshot = collect_workspace_3d_evidence(app, 101)
    assert any("floor_mapper_v127_page_73" in k for _, k in app.read_setting_calls)
    assert not any("floor_mapper_v127_page_4" in k for _, k in app.read_setting_calls)
    assert len(snapshot["mapper_shapes"]) == 1
    conn.close()


def test_section_6_7_8_mapper_conversion_and_manual_m2():
    """SECTION 6, 7, 8: Test percentage-to-metric conversion and manual_m2 evidence observation."""
    payload = {
        "workspace_id": 101,
        "mapper_shapes": [
            {
                "box_id": "box_pct_1",
                "page_width_px": 1000.0,
                "page_height_px": 1000.0,
                "px_per_m": 50.0,
                "raw_box": {"x": 10.0, "y": 10.0, "w": 40.0, "h": 30.0},
                "_source_level": {
                    "id": "ground",
                    "name": "Ground",
                    "derivation_source": "mapper_explicit_storey",
                },
            },
            {
                "box_id": "box_manual_1",
                "manual_m2": 45.0,
                "raw_box": {"manual_m2": 45.0},
                "_source_level": {
                    "id": "ground",
                    "name": "Ground",
                    "derivation_source": "mapper_explicit_storey",
                },
            }
        ]
    }

    project, _ = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    bld = project.buildings[0]
    ground = bld.levels[0]

    assert len(ground.floors) == 1
    fl = ground.floors[0]
    # width_px=1000, px_per_m=50, x=10% -> pixel x=100 -> metric x=2.0m
    assert abs(fl.polygon[0].x - 2.0) < 1e-3
    assert abs(fl.polygon[0].y - 2.0) < 1e-3
    assert abs(fl.polygon[2].x - 10.0) < 1e-3  # 10% + 40% = 50% -> 500px -> 10.0m

    # manual_m2 box becomes evidence observation, NOT physical floor!
    manual_obs = [obs for obs in project.evidence_observations if obs.kind == "manual_floor_area_allowance"]
    assert len(manual_obs) == 1
    assert "45.0" in manual_obs[0].reason_physical_unavailable


@pytest.mark.parametrize(
    "points",
    [
        # Bow-tie proper crossing.
        [{"x": 0, "y": 0}, {"x": 100, "y": 100}, {"x": 0, "y": 100}, {"x": 100, "y": 0}],
        # Non-adjacent repeated/touching vertex.
        [{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 50, "y": 50}, {"x": 100, "y": 100}, {"x": 50, "y": 50}, {"x": 0, "y": 100}],
        # Non-adjacent collinear overlap along the bottom edge.
        [{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 100}, {"x": 25, "y": 0}, {"x": 75, "y": 0}, {"x": 0, "y": 100}],
    ],
)
def test_invalid_mapper_polygons_are_evidence_only_and_never_takeoff_eligible(points):
    payload = {
        "workspace_id": 101,
        "mapper_shapes": [{
            "box_id": "invalid-floor",
            "page_width_px": 1000.0,
            "page_height_px": 1000.0,
            "px_per_m": 50.0,
            "raw_box": {"points": points},
            "_source_level": {
                "id": "ground",
                "name": "Ground",
                "derivation_source": "mapper_explicit_storey",
            },
        }],
    }
    project, skipped = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    assert sum(len(level.floors) for level in project.buildings[0].levels) == 0
    assert any(obs.kind == "rejected_floor_polygon" for obs in project.evidence_observations)
    assert any(item.get("type") == "FLOOR" for item in skipped)


def test_free_form_page_level_is_not_floor_storey_authority():
    payload = {
        "workspace_id": 101,
        "mapper_shapes": [{
            "box_id": "weak-level-floor",
            "page_width_px": 1000.0,
            "page_height_px": 1000.0,
            "px_per_m": 50.0,
            "raw_box": {"x": 10, "y": 10, "w": 20, "h": 20},
            "_source_level": "Ground Floor",
            "_source_level_label": "Ground Floor",
        }],
    }
    project, skipped = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    assert sum(len(level.floors) for level in project.buildings[0].levels) == 0
    assert any(obs.kind == "unresolved_floor_level" for obs in project.evidence_observations)
    assert any("no explicit storey identity" in str(item.get("reason", "")) for item in skipped)


def test_three_storey_mapper_floors_remain_isolated_by_structured_level_identity():
    level_specs = [
        ("ground", "Ground"),
        ("level_1", "Level 1"),
        ("level_2", "Level 2"),
    ]
    payload = {
        "workspace_id": 101,
        "levels": [
            {"id": level_id, "name": name, "elevation_m": index * 3.2}
            for index, (level_id, name) in enumerate(level_specs)
        ],
        "mapper_shapes": [
            {
                "box_id": f"floor-{level_id}",
                "page_width_px": 1000.0,
                "page_height_px": 1000.0,
                "px_per_m": 50.0,
                "raw_box": {"x": 10, "y": 10, "w": 20, "h": 20},
                "_source_level": {
                    "id": level_id,
                    "name": name,
                    "derivation_source": "mapper_explicit_storey",
                },
            }
            for level_id, name in level_specs
        ],
    }
    project, _ = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    levels = {level.id: level for level in project.buildings[0].levels}
    for level_id, _ in level_specs:
        assert len(levels[level_id].floors) == 1
        assert levels[level_id].floors[0].level_id == level_id
        assert levels[level_id].floors[0].takeoff_eligible is True


def test_section_10_real_lago_9_true_positive_openings_translation():
    """SECTION 10: The adapter translates every real live detector candidate as evidence only."""
    _, detected, candidates = _real_lago_raster_candidates()
    payload = {"workspace_id": "lago_101", "elevation_opening_candidates": candidates, "walls": []}
    project, _ = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)

    obs = project.evidence_observations
    assert len(obs) == len(detected) > 0
    assert {o.id for o in obs} == {f"page86-raster-{i}" for i in range(1, len(detected) + 1)}
    total_physical_openings = sum(len(w.openings) for l in project.buildings[0].levels for w in l.walls)
    assert total_physical_openings == 0


def test_section_14_legacy_27m_roof_z_fencing():
    """SECTION 14: Test roof cap Z is accepted ONLY when supporting wall height is confirmed; fallback 2.7m is fenced."""
    unconfirmed_payload = {
        "walls": [
            {"wall_ref": "W1", "level": "Ground", "a": {"x": 0, "y": 0}, "b": {"x": 10, "y": 0}, "height_m": 2.7, "height_status": "inferred"}
        ],
        "roof_data": {
            "evidence": {"pitches_deg": [22.5], "flat": False},
            "caps": [{"id": "r1", "level": "Ground", "points": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}], "z": 2.7}],
        }
    }
    proj_unconf, _ = planreader_to_canonical_model(unconfirmed_payload, is_validated_internal_workspace=True)
    roof_unconf = proj_unconf.buildings[0].levels[0].roofs[0]
    assert roof_unconf.metadata["z"] is None  # Fenced!
    assert roof_unconf.review_state == ReviewState.REVIEW_REQUIRED
    assert roof_unconf.takeoff_eligible is False

    confirmed_payload = {
        "walls": [
            {"wall_ref": "W1", "level": "Ground", "a": {"x": 0, "y": 0}, "b": {"x": 10, "y": 0}, "height_m": 3.2, "height_status": "confirmed"}
        ],
        "roof_data": {
            "evidence": {"pitches_deg": [22.5], "flat": False},
            "caps": [{"id": "r2", "level": "Ground", "points": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}], "z": 3.2}],
        }
    }
    proj_conf, _ = planreader_to_canonical_model(confirmed_payload, is_validated_internal_workspace=True)
    roof_conf = proj_conf.buildings[0].levels[0].roofs[0]
    assert roof_conf.metadata["z"] == 3.2  # Accepted!
    assert roof_conf.review_state == ReviewState.CONFIRMED
    assert roof_conf.takeoff_eligible is True


def test_invalid_v140_roof_cap_is_evidence_only_and_not_materialised():
    payload = {
        "workspace_id": 101,
        "walls": [{
            "wall_ref": "W1", "level": "Ground",
            "a": {"x": 0, "y": 0}, "b": {"x": 10, "y": 0},
            "height_m": 3.2, "height_status": "confirmed",
        }],
        "roof_data": {
            "evidence": {"pitches_deg": [22.5], "flat": False, "status": "pitch evidence"},
            "caps": [{
                "id": "roof-bowtie", "level": "Ground", "z": 3.2,
                "points": [[0, 0], [10, 10], [0, 10], [10, 0]],
                "triangles": [[0, 1, 2], [0, 2, 3]],
            }],
        },
    }
    project, skipped = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    assert sum(len(level.roofs) for level in project.buildings[0].levels) == 0
    assert any(obs.kind == "rejected_roof_cap" for obs in project.evidence_observations)
    assert any(item.get("id") == "roof-bowtie" for item in skipped)


def test_section_15_registered_wall_missing_endpoints_fail_closed():
    """SECTION 15: Test registered_wall_to_canonical_input does not fabricate fallback endpoints."""
    wall_no_pts = {"wall_ref": "W_NO_PTS", "height_m": 3.0}
    c_input = registered_wall_to_canonical_input(wall_no_pts)
    assert c_input["a"] is None
    assert c_input["b"] is None

    payload = {"walls": [c_input]}
    project, skipped = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    bld = project.buildings[0]
    total_walls = sum(len(l.walls) for l in bld.levels)
    assert total_walls == 0  # Wall skipped (fail closed)
    assert any("Missing or invalid start/end coordinates" in s.get("reason", "") for s in skipped)


def test_section_19_strict_persistence_workspace_id_matrix():
    """SECTION 19: Test require_workspace_id and load_workspace_canonical_model reject non-integral float, strings, None, bool, corrupt IDs safely."""
    bad_ids = [None, True, False, 0, -5, 101.5, "101.5", "abc"]
    for bad_id in bad_ids:
        with pytest.raises(ValueError):
            require_workspace_id(bad_id)

        # load_workspace_canonical_model returns (False, None, msg, None) safely without crashing!
        ok, proj, msg, _ = load_workspace_canonical_model(None, bad_id)
        assert ok is False
        assert proj is None


def test_section_21_three_storey_level_registry_isolation():
    """SECTION 21: Test 3-storey model maintains 3 distinct level containers without 3m visual stacking assumption."""
    payload = {
        "levels": [
            {"id": "lvl_g", "name": "Ground Floor", "elevation_m": 0.0, "level_index": 0},
            {"id": "lvl_1", "name": "Level 1", "elevation_m": 3.2, "level_index": 1},
            {"id": "lvl_2", "name": "Level 2", "elevation_m": 6.0, "level_index": 2},
        ],
        "walls": [
            {"wall_ref": "WG", "level": "Ground Floor", "a": {"x": 0, "y": 0}, "b": {"x": 10, "y": 0}, "height_m": 3.2},
            {"wall_ref": "W1", "level": "Level 1", "a": {"x": 0, "y": 0}, "b": {"x": 10, "y": 0}, "height_m": 2.8},
            {"wall_ref": "W2", "level": "Level 2", "a": {"x": 0, "y": 0}, "b": {"x": 10, "y": 0}, "height_m": 2.8},
        ]
    }

    project, _ = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    bld = project.buildings[0]

    active_levels = [l for l in bld.levels if l.walls or l.floors or l.roofs]
    assert len(active_levels) == 3
    
    # Each storey holds exactly its own wall
    ground = next(l for l in bld.levels if "ground" in l.name.lower() and l.walls)
    assert len(ground.walls) == 1 and ground.walls[0].provenance.wall_ref == "WG"

    l1 = next(l for l in bld.levels if "level 1" in l.name.lower() and l.walls)
    assert len(l1.walls) == 1 and l1.walls[0].provenance.wall_ref == "W1"

    l2 = next(l for l in bld.levels if "level 2" in l.name.lower() and l.walls)
    assert len(l2.walls) == 1 and l2.walls[0].provenance.wall_ref == "W2"


def test_phase5j_opening_host_identity():
    """SECTION A, B, C: Test host identity enforcement, c_opening.wall_id, and wrong-host fail-closed behavior."""
    payload = {
        "walls": [
            {
                "wall_ref": "W1",
                "level": "Ground",
                "a": {"x": 0, "y": 0},
                "b": {"x": 10, "y": 0},
                "height_m": 3.0,
                "openings": [
                    {
                        "id": "op_wrong_host",
                        "opening_type": "DOOR",
                        "resolved_wall_ref": "W2",  # WRONG HOST! (Nested under W1, claims W2)
                        "offset_along_wall_m": 2.0,
                        "sill_height_m": 0.0,
                        "width_m": 1.0,
                        "height_m": 2.1,
                        "deduct": True,
                        "manual_override_confirmed": True,
                    },
                    {
                        "id": "op_correct_host",
                        "opening_type": "WINDOW",
                        "resolved_wall_ref": "W1",  # CORRECT HOST!
                        "offset_along_wall_m": 5.0,
                        "sill_height_m": 0.9,
                        "width_m": 1.2,
                        "height_m": 1.2,
                        "deduct": True,
                        "manual_override_confirmed": True,
                    }
                ]
            }
        ]
    }

    project, skipped = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    w1 = project.buildings[0].levels[0].walls[0]
    op_wrong = w1.openings[0]
    op_correct = w1.openings[1]

    # Wrong host: deduction authority is FALSE, wall_id is None, physical_state is wrong_host
    assert op_wrong.deduction_authority is False
    assert op_wrong.wall_id is None
    assert op_wrong.metadata["physical_state"] == "wrong_host"
    assert op_wrong.metadata["claimed_wall_ref"] == "W2"
    assert op_wrong.metadata["actual_container_wall_ref"] == "W1"

    # Correct host: deduction authority is TRUE, wall_id equals w1.id
    assert op_correct.deduction_authority is True
    assert op_correct.wall_id == w1.id
    assert op_correct.metadata["claimed_wall_ref"] == "W1"

    # Wall net area deduction comes ONLY from correct host opening (1.44 m²), not wrong host (2.1 m²)
    from pb_geometry_services import potential_net_wall_area
    net_info = potential_net_wall_area(w1)
    assert net_info["gross_wall_area_m2"] == 30.0
    assert abs(net_info["authorized_opening_deduction_area_m2"] - 1.44) < 1e-3
    assert abs(net_info["authorized_net_area_m2"] - 28.56) < 1e-3


def test_phase5j_no_lago_defaults_in_generic_elevation():
    """SECTION D & E: Test generic elevation candidate translation contains ZERO invented LAGO defaults."""
    payload = {
        "workspace_id": "ws_generic",
        "elevation_opening_candidates": [
            {
                "candidate_id": "cand_empty",
                # Missing page_no, drawing_no, side, level, confidence, producer
            }
        ]
    }

    project, _ = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    obs = project.evidence_observations[0]

    assert obs.page_no is None
    assert obs.drawing_reference is None
    assert obs.side is None
    assert obs.level_name is None
    assert obs.confidence is None
    assert obs.producer is None
    assert obs.producer_version is None


def test_phase5j_exact_v139_wall_ref_identity():
    """SECTION I & V: Test exact v139 wall ref matching for N01, E01, S01, W01 without W-only regex."""
    payload = {
        "registered_walls": [
            {"wall_ref": "N01", "a": {"x": 0, "y": 0}, "b": {"x": 10, "y": 0}, "height_m": 3.0, "height_status": "confirmed"},
            {"wall_ref": "E01", "a": {"x": 10, "y": 0}, "b": {"x": 10, "y": 10}, "height_m": 3.0, "height_status": "confirmed"},
        ],
        "takeoff_rows": [
            {"id": 1, "unit": "m²", "row_role": "wall", "quantity": 30.0, "source_reference": "PB Unified Building v1.3.9 · N01"},
            {"id": 2, "unit": "m²", "row_role": "wall", "quantity": 30.0, "source_reference": "PB Unified Building v1.3.9 · E01"},
            {"id": 3, "unit": "m²", "row_role": "wall", "quantity": 30.0, "source_reference": "PB Unified Building v1.3.9 · UNKNOWN99"},
        ]
    }

    project, _ = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    diag = generate_production_diagnostics_report(project, workspace_data=payload)

    recs = diag["per_wall_quantity_reconciliation"]
    n01_rec = next(r for r in recs if r["wall_ref"] == "N01")
    e01_rec = next(r for r in recs if r["wall_ref"] == "E01")

    assert n01_rec["reconciliation_status"] == "matched"
    assert e01_rec["reconciliation_status"] == "matched"


def test_phase5j_roof_form_and_objective_z_proof():
    """SECTION J, K, L: Test pitch does not invent GABLE, and roof Z requires ALL contributing wall heights to be confirmed."""
    # Positive pitch alone leaves roof_type = UNKNOWN
    payload_pitch = {
        "walls": [{"wall_ref": "W1", "a": {"x": 0, "y": 0}, "b": {"x": 10, "y": 0}, "height_m": 4.5, "height_status": "confirmed"}],
        "roof_data": {
            "evidence": {"pitches_deg": [22.5], "flat": False},
            "caps": [{"id": "cap1", "polygon": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}], "z": 4.5}]
        }
    }
    proj_p, _ = planreader_to_canonical_model(payload_pitch, is_validated_internal_workspace=True)
    roof_p = proj_p.buildings[0].levels[0].roofs[0]
    assert roof_p.roof_type == "UNKNOWN"
    assert roof_p.elevation == 4.5
    assert roof_p.metadata["z"] == 4.5

    # One confirmed wall + one unconfirmed wall -> Roof Z rejected!
    payload_mixed = {
        "walls": [
            {"wall_ref": "W1", "a": {"x": 0, "y": 0}, "b": {"x": 10, "y": 0}, "height_m": 3.0, "height_status": "confirmed"},
            {"wall_ref": "W2", "a": {"x": 10, "y": 0}, "b": {"x": 10, "y": 10}, "height_m": 2.7, "height_status": "inferred"},
        ],
        "roof_data": {
            "evidence": {"pitches_deg": [15.0], "flat": False},
            "caps": [{"id": "cap2", "polygon": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}], "z": 2.7}]
        }
    }
    proj_m, _ = planreader_to_canonical_model(payload_mixed, is_validated_internal_workspace=True)
    roof_m = proj_m.buildings[0].levels[0].roofs[0]
    assert roof_m.elevation is None
    assert roof_m.metadata["z"] is None
    assert roof_m.review_state == ReviewState.REVIEW_REQUIRED


def test_phase5j_persistence_missing_fingerprint_rejection():
    """SECTION Z: Test load_workspace_canonical_model rejects saved model if missing source_revision_fingerprint."""
    class MockApp:
        def workspace_setting(self, wid, key, default=None):
            return json.dumps({
                "schema_version": "1.0.0",
                "persistence_key": "canonical_3d_model_v1",
                "workspace_id": 101,
                # "source_revision_fingerprint" IS MISSING!
                "model_data": CanonicalProject(id="proj_1").to_dict(),
            })

    ok, proj, msg, _ = load_workspace_canonical_model(MockApp(), 101)
    assert ok is False
    assert proj is None
    assert "Missing or invalid source_revision_fingerprint" in msg


def test_phase5k_v175_v178_persisted_evidence():
    """PHASE 5K - Section 1, 2, 14: Test live v175 page evidence integration and fingerprint determinism/sensitivity."""
    from pb_opening_production_v175 import apply as apply_opening_production_v175
    from pb_canonical_persistence import compute_workspace_source_fingerprint

    class DummyApp:
        def __init__(self):
            self.settings = {}
            self.analyse_stored_page_v130 = lambda page_id: {}

        def lquery(self, query, params=()):
            if "FROM workspaces" in query:
                return [{"id": 101, "job_no": "J101"}]
            if "FROM documents" in query:
                return [{"id": 5, "workspace_id": 101, "file_name": "elevations.pdf"}]
            if "FROM pages" in query:
                return [{"id": 42, "workspace_id": 101, "document_id": 5, "page_no": 2, "page_label": "Elevations"}]
            return []

        def workspace_setting(self, wid, key, default=None):
            return self.settings.get(key, default)

    app = DummyApp()
    apply_opening_production_v175(app)

    # Store real v175 page evidence
    app.settings["opening_evidence_v175_pages"] = json.dumps([42])
    app.settings["opening_evidence_v175_page_42"] = json.dumps({
        "elevation_openings": [{
            "id": "elev_op_1",
            "width_m": 1.8,
            "height_m": 2.1,
            "accepted": True,
            "reason": "v1.7.5 detected opening",
        }],
        "elevation_provenance": [{
            "source_filename": "elevations.pdf",
            "source_page": 2,
            "drawing_ref": "E-01",
            "elevation_side": "North",
            "wall_ref": "W01",
            "source_coords": {"x1": 10, "y1": 20, "x2": 100, "y2": 200},
            "coordinate_space": "pdf_pixels",
            "calibration": {"px_per_m": 50.0},
        }],
        "elevation_diagnostics": [{"note": "Clean detection"}],
    })

    snap1 = collect_workspace_3d_evidence(app, 101)
    assert len(snap1["evidence_observations"]) == 1
    obs = snap1["evidence_observations"][0]
    assert obs["candidate_id"] == "elev_op_1"
    assert obs["drawing_reference"] == "E-01"
    assert obs["source_filename"] == "elevations.pdf"
    assert obs["wall_ref"] == "W01"
    assert obs["producer"] == "pb_opening_production_v175"
    assert obs["deduction_authority"] is False
    assert obs["no_instance_creation"] is True

    fp1 = compute_workspace_source_fingerprint(snap1)

    # Mutate elevation provenance -> fingerprint MUST change!
    app.settings["opening_evidence_v175_page_42"] = json.dumps({
        "elevation_openings": [{
            "id": "elev_op_1",
            "width_m": 2.4,  # Mutated width
            "height_m": 2.1,
            "accepted": True,
        }],
        "elevation_provenance": [{
            "source_filename": "elevations.pdf",
            "source_page": 2,
            "drawing_ref": "E-01",
        }],
        "elevation_diagnostics": [],
    })
    snap2 = collect_workspace_3d_evidence(app, 101)
    fp2 = compute_workspace_source_fingerprint(snap2)
    assert fp1 != fp2


def test_phase5k_wrong_host_bim_payload():
    """PHASE 5K - Section 3, 4, 5: Test wrong-host openings do NOT cut wall mesh and wall_id is None for wrong host in payload."""
    from pb_bim_viewer import project_to_viewer_payload

    w1 = CanonicalWall(
        id="W1",
        name="Wall 1",
        start_point=Vector2D(x=0.0, y=0.0),
        end_point=Vector2D(x=10.0, y=0.0),
        thickness_m=0.2,
        height_m=3.0,
    )
    # Valid host-attached opening for W1
    op_valid = CanonicalOpening(
        id="OP_VALID",
        name="Valid Door",
        wall_id="W1",
        offset_along_wall_m=2.0,
        sill_height_m=0.0,
        width_m=1.0,
        height_m=2.1,
        deduction_authority=True,
    )
    op_valid.metadata["physical_state"] = "physical_b5_authorised"

    # Wrong host opening claiming W2 on wall W1
    op_wrong = CanonicalOpening(
        id="OP_WRONG",
        name="Wrong Host Door",
        wall_id="W2",
        offset_along_wall_m=5.0,
        sill_height_m=0.0,
        width_m=1.0,
        height_m=2.1,
        deduction_authority=False,
    )
    op_wrong.metadata["physical_state"] = "wrong_host"

    w1.openings = [op_valid, op_wrong]
    lvl = CanonicalLevel(id="L1", name="Level 1", walls=[w1])
    bld = CanonicalBuilding(id="B1", name="Building 1", levels=[lvl])
    proj = CanonicalProject(id="P1", name="Project 1", buildings=[bld])

    payload = project_to_viewer_payload(proj)
    objs = payload["objects"]

    # Check wall W1 payload object
    w1_payload = next(o for o in objs if o["id"] == "W1")
    # Wall openings attached list contains ONLY valid W1 opening (1 item, not 2)
    assert len(w1_payload["openings"]) == 1
    assert w1_payload["openings"][0]["id"] == "OP_VALID"

    # Check wrong host opening payload object in global objects list
    wrong_op_payload = next(o for o in objs if o["id"] == "OP_WRONG")
    assert wrong_op_payload["wall_id"] is None
    assert wrong_op_payload["is_host_attached"] is False


def test_phase5k_duplicate_name_different_source_polygon():
    """PHASE 5K - Section 6, 7: Test two walls with same level_name = 'Level 1' but different source_polygon IDs create two distinct storeys."""
    wall_a = {
        "wall_ref": "WA",
        "a": {"x": 0, "y": 0},
        "b": {"x": 10, "y": 0},
        "height_m": 3.0,
        "level_name": "Level 1",
        "level_index": 1,
        "source_polygon": "poly_building_north",
    }
    wall_b = {
        "wall_ref": "WB",
        "a": {"x": 0, "y": 20},
        "b": {"x": 10, "y": 20},
        "height_m": 3.0,
        "level_name": "Level 1",
        "level_index": 1,
        "source_polygon": "poly_building_south",
    }

    payload = {"walls": [wall_a, wall_b]}
    proj, _ = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    bld = proj.buildings[0]

    # Two distinct level storeys created because source_polygon differs
    assert len(bld.levels) == 2


def test_phase5k_roof_z_reproduction_verification():
    """PHASE 5K - Section 11: Test roof cap Z verification against reproduced expected wall top Z."""
    # Case A: Confirmed heights 3.0 and 3.4, cap_z = 3.4 => cap Z accepted
    payload_a = {
        "walls": [
            {"wall_ref": "W1", "a": {"x": 0, "y": 0}, "b": {"x": 10, "y": 0}, "height_m": 3.0, "height_status": "confirmed"},
            {"wall_ref": "W2", "a": {"x": 10, "y": 0}, "b": {"x": 10, "y": 10}, "height_m": 3.4, "height_status": "confirmed"},
        ],
        "roof_data": {
            "evidence": {"pitches_deg": [15.0], "flat": False},
            "caps": [{"id": "cap1", "polygon": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}], "z": 3.4}]
        }
    }
    proj_a, _ = planreader_to_canonical_model(payload_a, is_validated_internal_workspace=True)
    roof_a = proj_a.buildings[0].levels[0].roofs[0]
    assert roof_a.elevation == 3.4
    assert roof_a.review_state == ReviewState.CONFIRMED

    # Case B: Confirmed heights 3.0 and 3.4, cap_z = 9.0 (mismatch > 0.05m) => cap Z rejected!
    payload_b = {
        "walls": [
            {"wall_ref": "W1", "a": {"x": 0, "y": 0}, "b": {"x": 10, "y": 0}, "height_m": 3.0, "height_status": "confirmed"},
            {"wall_ref": "W2", "a": {"x": 10, "y": 0}, "b": {"x": 10, "y": 10}, "height_m": 3.4, "height_status": "confirmed"},
        ],
        "roof_data": {
            "evidence": {"pitches_deg": [15.0], "flat": False},
            "caps": [{"id": "cap2", "polygon": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}], "z": 9.0}]
        }
    }
    proj_b, _ = planreader_to_canonical_model(payload_b, is_validated_internal_workspace=True)
    roof_b = proj_b.buildings[0].levels[0].roofs[0]
    assert roof_b.elevation is None
    assert roof_b.review_state == ReviewState.REVIEW_REQUIRED


    roof_a = proj_a.buildings[0].levels[0].roofs[0]
    assert roof_a.elevation == 3.4
    assert roof_a.review_state == ReviewState.CONFIRMED

    # Case B: Confirmed heights 3.0 and 3.4, cap_z = 9.0 (mismatch > 0.05m) => cap Z rejected!
    payload_b = {
        "walls": [
            {"wall_ref": "W1", "a": {"x": 0, "y": 0}, "b": {"x": 10, "y": 0}, "height_m": 3.0, "height_status": "confirmed"},
            {"wall_ref": "W2", "a": {"x": 10, "y": 0}, "b": {"x": 10, "y": 10}, "height_m": 3.4, "height_status": "confirmed"},
        ],
        "roof_data": {
            "evidence": {"pitches_deg": [15.0], "flat": False},
            "caps": [{"id": "cap2", "polygon": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}], "z": 9.0}]
        }
    }
    proj_b, _ = planreader_to_canonical_model(payload_b, is_validated_internal_workspace=True)
    roof_b = proj_b.buildings[0].levels[0].roofs[0]
    assert roof_b.elevation is None
    assert roof_b.review_state == ReviewState.REVIEW_REQUIRED


def test_phase5k_model_bounds_includes_roof_z():
    """PHASE 5K - Section 12: Test model_bounds includes objective roof elevation Z."""
    from pb_geometry_services import model_bounds

    lvl = CanonicalLevel(
        id="L1",
        name="Ground",
        elevation_m=0.0,
        walls=[
            CanonicalWall(id="W1", name="W1", start_point=Vector2D(x=0.0, y=0.0), end_point=Vector2D(x=10.0, y=0.0), height_m=3.0)
        ],
        roofs=[
            CanonicalRoof(id="R1", name="Roof 1", elevation=3.4, polygon=[Vector2D(x=0.0, y=0.0), Vector2D(x=10.0, y=0.0), Vector2D(x=10.0, y=10.0)])
        ]
    )
    proj = CanonicalProject(id="P1", name="Project 1", buildings=[CanonicalBuilding(id="B1", name="B1", levels=[lvl])])
    has_b, bbox = model_bounds(proj)
    assert has_b is True
    assert bbox is not None
    assert bbox.max_point.z == 3.4


def test_phase5l_v175_v178_exact_asdict_contract():
    """PHASE 5L - Section 1: Persisted evidence uses exact asdict(ElevationOpening) and real v178 provenance shape."""
    from dataclasses import asdict
    from pb_elevation_evidence_v172 import ElevationOpening
    from pb_production_3d_adapter import collect_workspace_3d_evidence

    class DummyApp:
        def lquery(self, sql, args=()):
            if "FROM workspaces" in sql:
                return [{"id": 99, "job_no": "J1", "job_name": "Job 1", "builder_client": "Client", "site_address": "Addr"}]
            if "FROM documents" in sql:
                return [{"id": 55, "workspace_id": 99, "file_name": "A101.pdf", "sha256": "abc", "category": "elevation", "page_count": 1, "source_type": "pdf"}]
            if "FROM pages" in sql:
                return [{"id": 101, "workspace_id": 99, "document_id": 55, "page_no": 1, "page_label": "E1", "page_type": "elevation", "scale_text": "1:100", "px_per_m": 100, "width_px": 1000, "height_px": 1000, "render_zoom": 1.0, "selected": 1}]
            return []

        def workspace_setting(self, wid, key, default=None):
            if key == "opening_evidence_v175_pages":
                return json.dumps([101])
            if key == "opening_evidence_v175_page_101":
                op = ElevationOpening(
                    elevation_page_no=1,
                    elevation_side="East",
                    bbox_px=(100.0, 200.0, 300.0, 400.0),
                    width_m=1.2,
                    height_m=2.1,
                    drawing_ref="CD3001",
                    drawing_title="E1 EAST ELEVATION",
                    level="Ground",
                    wall_ref="W1",
                )
                prov = {
                    "source_filename": "A101.pdf",
                    "source_page": 1,
                    "drawing_ref": "CD3001",
                    "drawing_title": "E1 EAST ELEVATION",
                    "elevation_side": "East",
                    "coord_space": "render_pixel",
                    "level": "Ground",
                    "wall_ref": "W1",
                    "calibration_source": "auto",
                    "calibration_state": "valid",
                }
                return json.dumps({
                    "elevation_openings": [asdict(op)],
                    "elevation_provenance": [prov],
                    "elevation_diagnostics": [{"status": "accepted"}],
                })
            return default

    app = DummyApp()
    snap = collect_workspace_3d_evidence(app, 99)
    obs = snap["evidence_observations"]
    assert len(obs) == 1
    o = obs[0]
    assert o["source_filename"] == "A101.pdf"
    assert o["drawing_reference"] == "CD3001"
    assert o["drawing_title"] == "E1 EAST ELEVATION"
    assert o["side"] == "East"
    assert o["level_name"] == "Ground"
    assert o["wall_ref"] == "W1"
    assert o["calibration_source"] == "auto"
    assert o["calibration_state"] == "valid"
    assert o["accepted_state"] is True
    assert o["width_m"] == 1.2
    assert o["height_m"] == 2.1


def test_phase5l_v135_storey_identity_source_polygon():
    """PHASE 5L - Section 4: Actual v135 wall_records provide source_polygon without level_index."""
    payload = {
        "walls": [
            {
                "wall_ref": "W1",
                "a": {"x": 0, "y": 0},
                "b": {"x": 5, "y": 0},
                "level": "Level 1",
                "source_polygon": "poly_A",
            },
            {
                "wall_ref": "W2",
                "a": {"x": 0, "y": 0},
                "b": {"x": 5, "y": 0},
                "level": "Level 1",
                "source_polygon": "poly_B",
            },
        ]
    }
    proj, _ = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    b = proj.buildings[0]
    assert len(b.levels) == 2
    level_names = [lvl.name for lvl in b.levels]
    assert len(level_names) == 2


def test_phase5l_roof_z_semantics():
    """PHASE 5L - Section 5: Roof Z semantics for Ground (3.4), Level 1 (6.4), Level 2 (unresolved)."""
    # Ground at 0.0 + height 3.4 => roof Z 3.4
    p_ground = {
        "levels": [{"id": "L0", "name": "Ground", "elevation_m": 0.0}],
        "walls": [{"wall_ref": "W0", "level": "Ground", "a": {"x": 0, "y": 0}, "b": {"x": 10, "y": 0}, "height_m": 3.4, "height_status": "confirmed"}],
        "roof_data": {"caps": [{"id": "cap0", "level": "Ground", "polygon": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}], "z": 3.4}]}
    }
    proj0, _ = planreader_to_canonical_model(p_ground, is_validated_internal_workspace=True)
    r0 = next(r for lvl in proj0.buildings[0].levels for r in lvl.roofs if r.id == "cap0")
    assert r0.elevation == 3.4

    # Level 1 at 3.4 + height 3.0 => roof Z 6.4
    p_l1 = {
        "levels": [{"id": "L1", "name": "Level 1", "elevation_m": 3.4}],
        "walls": [{"wall_ref": "W1", "level": "Level 1", "a": {"x": 0, "y": 0}, "b": {"x": 10, "y": 0}, "height_m": 3.0, "height_status": "confirmed"}],
        "roof_data": {"caps": [{"id": "cap1", "level": "Level 1", "polygon": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}], "z": 3.0}]}
    }
    proj1, _ = planreader_to_canonical_model(p_l1, is_validated_internal_workspace=True)
    r1 = next(r for lvl in proj1.buildings[0].levels for r in lvl.roofs if r.id == "cap1")
    assert r1.elevation == 6.4

    # Level 2 unresolved elevation => no absolute roof Z
    p_l2 = {
        "levels": [{"id": "L2", "name": "Level 2", "elevation_m": None}],
        "walls": [{"wall_ref": "W2", "level": "Level 2", "a": {"x": 0, "y": 0}, "b": {"x": 10, "y": 0}, "height_m": 3.0, "height_status": "confirmed"}],
        "roof_data": {"caps": [{"id": "cap2", "level": "Level 2", "polygon": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}], "z": 3.0}]}
    }
    proj2, _ = planreader_to_canonical_model(p_l2, is_validated_internal_workspace=True)
    r2 = next(r for lvl in proj2.buildings[0].levels for r in lvl.roofs if r.id == "cap2")
    assert r2.elevation is None
    assert r2.review_state == ReviewState.REVIEW_REQUIRED


def test_phase5l_model_bounds_math_isfinite():
    """PHASE 5L - Section 6: Assert math.isfinite() for every returned bound coordinate."""
    import math
    from pb_geometry_services import model_bounds

    lvl = CanonicalLevel(
        id="L1",
        name="Ground",
        elevation_m=0.0,
        walls=[CanonicalWall(id="W1", name="W1", start_point=Vector2D(x=0.0, y=0.0), end_point=Vector2D(x=10.0, y=0.0), height_m=3.0)]
    )
    proj = CanonicalProject(id="P1", name="P1", buildings=[CanonicalBuilding(id="B1", name="B1", levels=[lvl])])
    has_b, bbox = model_bounds(proj)
    assert has_b is True
    assert math.isfinite(bbox.min_point.x)
    assert math.isfinite(bbox.max_point.x)
    assert math.isfinite(bbox.min_point.y)
    assert math.isfinite(bbox.max_point.y)
    assert math.isfinite(bbox.min_point.z)
    assert math.isfinite(bbox.max_point.z)

    # Unresolved level elevation returns (False, None)
    lvl_unresolved = CanonicalLevel(
        id="L2",
        name="Unresolved",
        elevation_m=None,
        walls=[CanonicalWall(id="W2", name="W2", start_point=Vector2D(x=0.0, y=0.0), end_point=Vector2D(x=10.0, y=0.0), height_m=3.0)]
    )
    proj_unresolved = CanonicalProject(id="P2", name="P2", buildings=[CanonicalBuilding(id="B2", name="B2", levels=[lvl_unresolved])])
    has_b2, bbox2 = model_bounds(proj_unresolved)
    assert has_b2 is False
    assert bbox2 is None


def test_phase5l_v140_producer_version():
    """PHASE 5L - Section 7: get_producer_versions() includes v140_roof = 1.4.0."""
    from pb_production_3d_adapter import get_producer_versions
    v_map = get_producer_versions()
    assert v_map.get("v140_roof") == "1.4.0"
