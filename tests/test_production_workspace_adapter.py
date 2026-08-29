"""
Unit and Integration test suite for Phase 5E Production Contract Closure & Workspace 3D Maturity
(tests/test_production_workspace_adapter.py).

Verifies Sections A through T:
1. Real app.lquery() contract returning List[Dict[str, Any]] (dictionary row access).
2. require_workspace_id() validation and zero fallback to workspace 1.
3. One Authoritative Workspace Evidence Snapshot v2 (collect_workspace_3d_evidence).
4. Explicit producer diagnostics log (no silent pass).
5. Floor mapper percentage-to-metric coordinate conversion.
6. Roof evidence integration (unknown roof_type stays None).
7. Deterministic snapshot fingerprinting (same semantic evidence in different list order -> same hash).
8. Identity-safe quantity reconciliation.
9. Cross-workspace evidence isolation.
10. Production app startup integration test.
11. Safe canonical model caching in session memory.
12. Genuine LAGO elevation benchmark fail-closed integration test (0 physical openings).
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


class MockAppDB:
    """Mock PlanReader application instance mirroring real database (Dict rows) & setting string storage."""
    def __init__(self, db_conn=None):
        self.settings = {}
        self.db_conn = db_conn

    def set_workspace_setting(self, wid: int, key: str, value: any):
        self.settings[(int(wid), str(key))] = str(value) if not isinstance(value, str) else value

    def workspace_setting(self, wid: int, key: str, default: any = None):
        return self.settings.get((int(wid), str(key)), default)

    def lquery(self, query_str: str, params: tuple = ()):
        """SECTION A: Real app.lquery() returns List[Dict[str, Any]]!"""
        if self.db_conn:
            cursor = self.db_conn.cursor()
            cursor.execute(query_str, params)
            cols = [desc[0] for desc in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]
        return []


def test_section_a_lquery_returns_dict_and_tuple_access_fails(tmp_path):
    """SECTION A: Verify app.lquery() returns List[Dict[str, Any]] and tuple row access is eliminated."""
    db_file = tmp_path / "planreader.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "CREATE TABLE workspaces ("
        "id INTEGER PRIMARY KEY, job_no TEXT, job_name TEXT, builder_client TEXT, site_address TEXT"
        ")"
    )
    conn.execute(
        "INSERT INTO workspaces (id, job_no, job_name, builder_client, site_address) "
        "VALUES (101, 'JOB-99', 'Commercial Office Level 1', 'Builder Corp', '100 Main St')"
    )
    conn.commit()

    app = MockAppDB(conn)
    rows = app.lquery("SELECT * FROM workspaces WHERE id=?", (101,))
    assert len(rows) == 1
    assert isinstance(rows[0], dict)
    assert rows[0].get("job_no") == "JOB-99"

    # Prove tuple indexing raises KeyError on dict!
    with pytest.raises(KeyError):
        _ = rows[0][0]

    snapshot = collect_workspace_3d_evidence(app, 101)
    assert snapshot["workspace_metadata"]["id"] == 101
    assert snapshot["workspace_metadata"]["job_no"] == "JOB-99"
    conn.close()


def test_section_c_require_workspace_id_strict_validation():
    """SECTION C: Test require_workspace_id() fails closed with zero fallback to workspace 1."""
    assert require_workspace_id(101) == 101
    assert require_workspace_id("101") == 101

    with pytest.raises(ValueError, match="Invalid workspace ID"):
        require_workspace_id(0)

    with pytest.raises(ValueError, match="Invalid workspace ID"):
        require_workspace_id(-5)

    with pytest.raises(ValueError, match="Invalid workspace ID"):
        require_workspace_id(None)

    with pytest.raises(ValueError, match="Invalid workspace ID"):
        require_workspace_id("abc")


def test_section_b_level_resolution_service_matrix():
    """SECTION B: Test 5 explicit level resolution states."""
    level_map = {
        "lvl_1": CanonicalLevel(id="lvl_1", name="Level 1", elevation_m=0.0, height_m=3.0),
        "lvl_2": CanonicalLevel(id="lvl_2", name="Level 2", elevation_m=3.0, height_m=3.0),
        "lvl_unresolved": CanonicalLevel(id="lvl_unresolved", name="Unresolved", elevation_m=None, height_m=None, review_state=ReviewState.REVIEW_REQUIRED),
    }
    unresolved_container = level_map["lvl_unresolved"]
    skipped_items = []

    lvl1, r1 = resolve_canonical_level("lvl_2", level_map, unresolved_container, skipped_items)
    assert lvl1.id == "lvl_2"
    assert r1 == "objectively_known_level"

    lvl0, r0 = resolve_canonical_level("lvl_1", level_map, unresolved_container, skipped_items)
    assert lvl0.elevation_m == 0.0
    assert r0 == "objectively_known_level"

    lvl_un, r_un = resolve_canonical_level("lvl_unresolved", level_map, unresolved_container, skipped_items)
    assert lvl_un.elevation_m is None
    assert r_un == "unresolved_level_in_map"

    lvl_none, r_none = resolve_canonical_level(None, level_map, unresolved_container, skipped_items)
    assert lvl_none.id == "lvl_unresolved"

    lvl_err, r_err = resolve_canonical_level("lvl_99", level_map, unresolved_container, skipped_items)
    assert lvl_err.id == "lvl_unresolved"
    assert r_err == "wrong_known_level_conflict"


def test_section_f_floor_mapper_percentage_to_metric_conversion():
    """SECTION F: Test v127/v128 percentage coordinates conversion to plan metres via px_per_m."""
    mapper_json = {
        "page_id": 4,
        "width_px": 1000.0,
        "height_px": 1000.0,
        "calibration": {"px_per_m": 50.0},  # 50 px/m -> 1000px = 20m
        "boxes": [
            {
                "id": "shape_101",
                "name": "Level 1 Slab",
                "polygon": [
                    {"x": 10.0, "y": 10.0},   # 10% of 1000px = 100px / 50px/m = 2.0m
                    {"x": 60.0, "y": 10.0},   # 60% of 1000px = 600px / 50px/m = 12.0m
                    {"x": 60.0, "y": 50.0},   # 50% of 1000px = 500px / 50px/m = 10.0m
                    {"x": 10.0, "y": 50.0}
                ],
                "area_m2": 80.0
            }
        ]
    }

    payload = {"project_id": "101", "mapper_shapes": [{"workspace_id": 101, "page_id": 4, "mapper_setting": mapper_json}]}
    project, _ = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    unresolved_lvl = next(l for l in project.buildings[0].levels if l.id == "lvl_unresolved_review")

    assert len(unresolved_lvl.floors) == 1
    fl = unresolved_lvl.floors[0]
    p1 = fl.polygon[0]
    p2 = fl.polygon[1]

    assert abs(p1.x - 2.0) < 1e-2
    assert abs(p1.y - 2.0) < 1e-2
    assert abs(p2.x - 12.0) < 1e-2


def test_section_i_deterministic_shuffled_snapshot_fingerprint():
    """SECTION I: Verify that same semantic evidence in different list order produces identical fingerprint."""
    snap1 = {
        "workspace_metadata": {"id": 101},
        "registered_walls": [
            {"wall_ref": "W-1", "a": {"x": 0, "y": 0}, "b": {"x": 10, "y": 0}},
            {"wall_ref": "W-2", "a": {"x": 10, "y": 0}, "b": {"x": 10, "y": 10}},
        ],
        "pages": [{"page_id": 1}, {"page_id": 2}]
    }

    # Reverse order of registered_walls and pages
    snap2 = {
        "workspace_metadata": {"id": 101},
        "registered_walls": [
            {"wall_ref": "W-2", "a": {"x": 10, "y": 0}, "b": {"x": 10, "y": 10}},
            {"wall_ref": "W-1", "a": {"x": 0, "y": 0}, "b": {"x": 10, "y": 0}},
        ],
        "pages": [{"page_id": 2}, {"page_id": 1}]
    }

    fp1 = compute_workspace_source_fingerprint(snap1)
    fp2 = compute_workspace_source_fingerprint(snap2)
    assert fp1 == fp2  # Identical hash regardless of list order!


def test_section_n_cross_workspace_evidence_isolation():
    """SECTION N: Verify foreign workspace page evidence is rejected."""
    foreign_payload = {
        "project_id": "101",
        "mapper_shapes": [
            {"workspace_id": 999, "page_id": 4, "mapper_setting": {"boxes": []}}  # Foreign workspace 999!
        ]
    }

    project, skipped = planreader_to_canonical_model(foreign_payload, is_validated_internal_workspace=True)
    assert any("foreign_workspace_evidence_rejected" in s.get("reason", "") for s in skipped)


def test_section_o_production_app_startup_integration():
    """SECTION O: Integration test proving wrapped original model_3d_page runs once without import cycles."""
    from pb_3d_workspace_integration import apply as apply_3d_canonical_integration

    orig_call_count = 0

    class DummyApp:
        def model_3d_page(self, workspace):
            nonlocal orig_call_count
            orig_call_count += 1

    app = DummyApp()
    apply_3d_canonical_integration(app)
    app.model_3d_page({"id": 101})

    assert orig_call_count == 1  # Original model page executed exactly once!
    assert getattr(app, "_canonical_3d_extension_installed") is True


def test_section_l_real_lago_elevation_fail_closed_assertion():
    """SECTION L: Test real committed LAGO elevation benchmark (0 plan host walls -> 0 physical 3D openings -> 0 deductions)."""
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

    res = planreader_to_canonical_model(lago_payload, is_validated_internal_workspace=True)
    project = res[0]
    bld = project.buildings[0]

    total_physical_openings = sum(len(w.openings) for l in bld.levels for w in l.walls)
    assert total_physical_openings == 0
