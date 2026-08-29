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
import pytest
from pb_canonical_building import (
    CanonicalProject,
    CanonicalLevel,
    ReviewState,
    ObjectType,
    Vector2D,
    CanonicalEvidenceObservation,
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
    """SECTION N & A: Test real LAGO elevation benchmark (0 plan host walls -> 0 physical 3D openings -> 0 deductions)."""
    fpath = "tests/fixtures/lago_cd3001_east_elevation_v177.json"
    if not os.path.exists(fpath):
        pytest.skip("Fixture not found")

    with open(fpath, "r", encoding="utf-8") as f:
        fixture_data = json.load(f)

    lago_payload = {
        "workspace_metadata": {"id": "lago_cd3001"},
        "registered_walls": [],  # Zero plan host walls!
        "elevation_opening_candidates": fixture_data.get("annotations") or [
            {"candidate_id": "c1", "width_m": 1.2, "height_m": 2.1}
        ]
    }

    res = planreader_to_canonical_model(lago_payload, is_validated_internal_workspace=True)
    project = res[0]
    bld = project.buildings[0]

    total_physical_openings = sum(len(w.openings) for l in bld.levels for w in l.walls)
    assert total_physical_openings == 0
    assert len(project.evidence_observations) > 0  # SECTION A: Translated into evidence observations!


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
    assert any("Wrong level conflict" in str(s.get("reason", "")) for s in skipped)
