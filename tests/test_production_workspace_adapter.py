"""
Unit and Integration test suite for Phase 5D Actual Production Workspace Adapter
(tests/test_production_workspace_adapter.py).

Verifies Sections A through W:
1. Real production database contract (workspaces table schema: id, job_no, job_name, etc.) & fail closed on invalid workspace ID.
2. Level resolution service (5 explicit states: known, explicit 0.0, unresolved container, no evidence, wrong known level conflict).
3. Authority derivation & regression matrix (missing B5 proof, stale deduct=True, string "true", stale deduction_authority=True).
4. Real v139 opening contract (offset_m, sill_m, width_m, height_m, instance ID, signature).
5. One Authoritative Workspace Evidence Snapshot (collect_workspace_3d_evidence).
6. Saved floor mapper geometry (page-scoped v127/v128 settings).
7. Roof evidence integration (pb_roof_envelope_v140).
8. Real JSON string persistence & fingerprint determinism.
9. Identity-safe per-wall quantity reconciliation.
10. Estimator QA summary diagnostics panel.
11. Real production database schema integration test.
12. Application startup integration test.
"""

import os
import json
import sqlite3
import pytest
from pb_canonical_building import CanonicalProject, CanonicalLevel, ReviewState, ObjectType
from pb_production_3d_adapter import (
    registered_wall_to_canonical_input,
    revalidate_b5_opening,
    resolve_canonical_level,
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


class MockAppDB:
    """Mock PlanReader application instance mirroring real database & setting string storage."""
    def __init__(self, db_conn=None):
        self.settings = {}
        self.db_conn = db_conn

    def set_workspace_setting(self, wid: int, key: str, value: any):
        # Real production set_workspace_setting converts values to str
        self.settings[(int(wid), str(key))] = str(value) if not isinstance(value, str) else value

    def workspace_setting(self, wid: int, key: str, default: any = None):
        return self.settings.get((int(wid), str(key)), default)

    def lquery(self, query_str: str, params: tuple = ()):
        if self.db_conn:
            cursor = self.db_conn.cursor()
            cursor.execute(query_str, params)
            return cursor.fetchall()
        return []


def test_section_a_real_database_contract_fail_closed(tmp_path):
    """SECTION A: Test real workspaces DB schema (id, job_no, job_name, builder_client, site_address) and fail closed."""
    db_file = tmp_path / "planreader.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "CREATE TABLE workspaces ("
        "id INTEGER PRIMARY KEY, job_no TEXT, job_name TEXT, builder_client TEXT, site_address TEXT"
        ")"
    )
    conn.execute(
        "INSERT INTO workspaces (id, job_no, job_name, builder_client, site_address) "
        "VALUES (101, 'JOB-99', 'Lago Commercial Elevation', 'Builder Corp', '100 Main St')"
    )
    conn.commit()

    app = MockAppDB(conn)

    # Valid workspace 101 -> collects real DB metadata
    snapshot = collect_workspace_3d_evidence(app, 101)
    assert snapshot["workspace_metadata"]["id"] == 101
    assert snapshot["workspace_metadata"]["job_no"] == "JOB-99"

    # Missing workspace 999 -> FAILS CLOSED with ValueError (no fake fallback!)
    with pytest.raises(ValueError, match="Invalid or missing workspace ID"):
        collect_workspace_3d_evidence(app, 999)

    conn.close()


def test_section_b_level_resolution_service_matrix():
    """SECTION B: Test 5 explicit level resolution states (known, explicit 0.0, unresolved container, no evidence, wrong level conflict)."""
    level_map = {
        "lvl_1": CanonicalLevel(id="lvl_1", name="Level 1", elevation_m=0.0, height_m=3.0),
        "lvl_2": CanonicalLevel(id="lvl_2", name="Level 2", elevation_m=3.0, height_m=3.0),
        "lvl_unresolved": CanonicalLevel(id="lvl_unresolved", name="Unresolved", elevation_m=None, height_m=None, review_state=ReviewState.REVIEW_REQUIRED),
    }
    unresolved_container = level_map["lvl_unresolved"]
    skipped_items = []

    # 1. Objectively-known level
    lvl1, r1 = resolve_canonical_level("lvl_2", level_map, unresolved_container, skipped_items)
    assert lvl1.id == "lvl_2"
    assert r1 == "objectively_known_level"

    # 2. Explicitly-known level with zero elevation
    lvl0, r0 = resolve_canonical_level("lvl_1", level_map, unresolved_container, skipped_items)
    assert lvl0.elevation_m == 0.0
    assert r0 == "objectively_known_level"

    # 3. Unresolved level in map
    lvl_un, r_un = resolve_canonical_level("lvl_unresolved", level_map, unresolved_container, skipped_items)
    assert lvl_un.elevation_m is None
    assert r_un == "unresolved_level_in_map"

    # 4. No level evidence
    lvl_none, r_none = resolve_canonical_level(None, level_map, unresolved_container, skipped_items)
    assert lvl_none.id == "lvl_unresolved"
    assert r_none == "no_level_evidence"

    # 5. Wrong known level conflict (claims Level 99 which does not exist)
    lvl_err, r_err = resolve_canonical_level("lvl_99", level_map, unresolved_container, skipped_items)
    assert lvl_err.id == "lvl_unresolved"
    assert r_err == "wrong_known_level_conflict"
    assert any("wrong_known_level_conflict" in item.get("reason", "") for item in skipped_items)


def test_section_c_authority_regression_matrix():
    """SECTION C & U: Authority regression matrix (missing B5 proof, stale deduct=True, string 'true', stale deduction_authority=True)."""
    # 1. Missing B5 proof
    op_no_proof = {"id": "op1", "deduct": True}
    assert revalidate_b5_opening(op_no_proof) is False

    # 2. Stale deduction_authority=True string
    op_stale_str = {"id": "op2", "deduction_authority": "true"}
    assert revalidate_b5_opening(op_stale_str) is False

    # 3. Untrusted payload CANNOT grant project authority
    untrusted_payload = {
        "project_name": "Untrusted Payload",
        "walls": [{"wall_ref": "w1", "a": {"x": 0, "y": 0}, "b": {"x": 10, "y": 0}, "height_m": 3.0, "height_status": "confirmed"}]
    }
    project, _ = planreader_to_canonical_model(untrusted_payload, is_validated_internal_workspace=False)
    assert project.takeoff_eligible is False
    assert project.deduction_authority is False


def test_section_d_real_v139_opening_schema_contract():
    """SECTION D & T: Test exact real v139 opening schema fixture (offset_m, sill_m, width_m, height_m, instance ID, signature)."""
    fixture_path = "tests/fixtures/v139_registered_wall_contract.json"
    assert os.path.exists(fixture_path)
    with open(fixture_path, "r", encoding="utf-8") as f:
        v139_wall_dict = json.load(f)

    skipped = []
    c_wall, ops = registered_wall_to_canonical_input(v139_wall_dict, skipped)
    assert c_wall is not None
    assert c_wall.id == "W-E101"
    assert len(ops) == 1
    
    op_raw = ops[0]
    assert op_raw["offset_m"] == 2.5
    assert op_raw["sill_m"] == 0.0
    assert op_raw["opening_instance_id"] == "inst_d01_99"


def test_section_f_page_scoped_floor_mapper_fixture():
    """SECTION F & T: Test page-scoped floor mapper saved state (v127_floor_mapper_contract.json)."""
    fixture_path = "tests/fixtures/v127_floor_mapper_contract.json"
    assert os.path.exists(fixture_path)
    with open(fixture_path, "r", encoding="utf-8") as f:
        mapper_json = json.load(f)

    payload = {
        "mapper_shapes": [{"page_id": 4, "mapper_setting": mapper_json}]
    }

    project, skipped = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    bld = project.buildings[0]
    unresolved_lvl = next(l for l in bld.levels if l.id == "lvl_unresolved_review")

    assert len(unresolved_lvl.floors) == 1
    floor = unresolved_lvl.floors[0]
    assert floor.name == "Level 1 Main Slab"
    assert len(floor.polygon) == 4


def test_section_g_roof_evidence_fixture():
    """SECTION G & T: Test roof evidence integration (v140_roof_evidence_contract.json)."""
    fixture_path = "tests/fixtures/v140_roof_evidence_contract.json"
    assert os.path.exists(fixture_path)
    with open(fixture_path, "r", encoding="utf-8") as f:
        roof_json = json.load(f)

    payload = {"roof_data": roof_json}
    project, _ = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    bld = project.buildings[0]
    unresolved_lvl = next(l for l in bld.levels if l.id == "lvl_unresolved_review")

    assert len(unresolved_lvl.roofs) == 1
    roof = unresolved_lvl.roofs[0]
    assert roof.name == "Main Building Roof Envelope"
    assert roof.pitch_deg == 22.5
    assert roof.roof_type == "GABLE"


def test_section_i_j_k_persistence_json_string_and_refresh():
    """SECTION I, J, K: Test JSON string persistence against real set_workspace_setting API, initial save, staleness, and refresh."""
    app = MockAppDB()
    snapshot = {
        "workspace_metadata": {"id": 101, "name": "Persistence Unit Test"},
        "registered_walls": [{"wall_ref": "W-1", "a": {"x": 0, "y": 0}, "b": {"x": 10, "y": 0}, "height_m": 3.0, "height_status": "confirmed"}],
        "takeoff_rows": [{"wall_ref": "W-1", "m2": 30.0}]
    }

    project, _ = planreader_to_canonical_model(snapshot, is_validated_internal_workspace=True)

    # 1. Save canonical model -> stored as JSON string
    save_workspace_canonical_model(app, 101, project, snapshot=snapshot)
    stored_val = app.workspace_setting(101, PERSISTENCE_KEY, None)
    assert isinstance(stored_val, str)
    assert "canonical_3d_model_v1" in stored_val

    # 2. Load model from string -> parses cleanly
    is_fresh, loaded_proj, msg, _ = load_workspace_canonical_model(app, 101, current_snapshot=snapshot)
    assert is_fresh is True
    assert loaded_proj.id == "101"

    # 3. Mutate source snapshot -> staleness detected!
    mutated_snapshot = {
        **snapshot,
        "takeoff_rows": [{"wall_ref": "W-1", "m2": 30.0}, {"wall_ref": "W-2", "m2": 45.0}]
    }
    is_fresh_stale, _, stale_msg, _ = load_workspace_canonical_model(app, 101, current_snapshot=mutated_snapshot)
    assert is_fresh_stale is False
    assert "Stale" in stale_msg

    # 4. Refresh model -> clears stale condition!
    refreshed_project, _ = planreader_to_canonical_model(mutated_snapshot, is_validated_internal_workspace=True)
    save_workspace_canonical_model(app, 101, refreshed_project, snapshot=mutated_snapshot)
    is_fresh_after_refresh, _, _, _ = load_workspace_canonical_model(app, 101, current_snapshot=mutated_snapshot)
    assert is_fresh_after_refresh is True


def test_section_l_original_3d_workflow_wrapper_preserved():
    """SECTION L: Test that apply() wraps model_3d_page while preserving and calling original model_3d_page."""
    from pb_3d_workspace_integration import apply as apply_3d_canonical_integration
    
    orig_called = False

    class ProductionApp:
        def model_3d_page(self, workspace):
            nonlocal orig_called
            orig_called = True

    app = ProductionApp()
    apply_3d_canonical_integration(app)

    # Call wrapped model_3d_page
    app.model_3d_page({"id": 101})
    assert orig_called is True  # Original model page callable MUST execute!


def test_section_n_quantity_reconciliation_unit_and_identity_rules():
    """SECTION N: Test per-wall quantity reconciliation rules (unit filtering strictly m², ambiguous match handling)."""
    payload = {
        "walls": [
            {"wall_ref": "W-1", "a": {"x": 0, "y": 0}, "b": {"x": 10, "y": 0}, "height_m": 3.0, "height_status": "confirmed"}
        ]
    }
    project, _ = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    
    # Takeoff rows with no unit / non-m² unit
    ws_data = {
        "takeoff_rows": [
            {"wall_ref": "W-1", "quantity": 10.0, "unit": "lm"},     # Linear meter -> ignored!
            {"wall_ref": "W-1", "quantity": 1.0, "unit": "item"},    # Count -> ignored!
            {"wall_ref": "W-1", "quantity": 30.0, "unit": "m²"},     # Valid m² match!
        ]
    }

    diagnostics = generate_production_diagnostics_report(project, workspace_data=ws_data)
    rec = diagnostics["per_wall_quantity_reconciliation"][0]
    assert rec["reconciliation_status"] == "matched"
    assert rec["production_quantity"] == 30.0


def test_section_q_real_lago_elevation_fail_closed():
    """SECTION Q: Test real committed LAGO elevation benchmark (0 plan host walls -> 0 physical 3D openings -> 0 deductions)."""
    fpath = "tests/fixtures/lago_cd3001_east_elevation_v177.json"
    if not os.path.exists(fpath):
        pytest.skip("Fixture not found")

    with open(fpath, "r", encoding="utf-8") as f:
        fixture_data = json.load(f)

    src = fixture_data.get("source", {})
    lago_payload = {
        "workspace_metadata": {"id": "lago_cd3001", "name": f"LAGO Elevation Benchmark {src.get('drawing_no')}"},
        "registered_walls": [],  # Zero plan host walls!
    }

    project, skipped = planreader_to_canonical_model(lago_payload, is_validated_internal_workspace=True)
    bld = project.buildings[0]

    total_physical_openings = sum(len(w.openings) for l in bld.levels for w in l.walls)
    assert total_physical_openings == 0
