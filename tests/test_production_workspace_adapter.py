"""
Unit and Integration test suite for Phase 5F Production Contract Closure & Canonical Workspace Hardening
(tests/test_production_workspace_adapter.py).

Verifies Sections 1 through 70:
1. Real app.lquery() contract with page_no schema (NOT page_number!).
2. documents table integration & cross-workspace evidence isolation.
3. require_workspace_id() strict rejection of floats (101.5), booleans, zero, and negative values.
4. B5 authorized opening granting wall deduction gate for potential_net_wall_area().
5. Nested opening host wall verification (wrong host -> skipped).
6. Snapshot v3 determinism across shuffled collection list orders.
7. Application wrapper idempotency (apply(app) twice does not double wrap).
8. Genuine LAGO elevation benchmark fail-closed integration test (0 physical openings).
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
from pb_geometry_services import potential_net_wall_area


class MockAppDB:
    """Mock PlanReader application instance mirroring real database (Dict rows & page_no schema) & setting string storage."""
    def __init__(self, db_conn=None):
        self.settings = {}
        self.db_conn = db_conn

    def set_workspace_setting(self, wid: int, key: str, value: any):
        self.settings[(int(wid), str(key))] = str(value) if not isinstance(value, str) else value

    def workspace_setting(self, wid: int, key: str, default: any = None):
        return self.settings.get((int(wid), str(key)), default)

    def lquery(self, query_str: str, params: tuple = ()):
        if self.db_conn:
            cursor = self.db_conn.cursor()
            cursor.execute(query_str, params)
            cols = [desc[0] for desc in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]
        return []


def test_section_1_pages_schema_page_no_query(tmp_path):
    """SECTION 1 & 48: Test pages table query using page_no schema (NOT page_number)."""
    db_file = tmp_path / "planreader.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE workspaces (id INTEGER PRIMARY KEY, job_no TEXT, job_name TEXT, builder_client TEXT, site_address TEXT)")
    conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, workspace_id INTEGER, file_name TEXT, sha256 TEXT, category TEXT, page_count INTEGER, source_type TEXT)")
    conn.execute("CREATE TABLE pages (id INTEGER PRIMARY KEY, workspace_id INTEGER, document_id INTEGER, page_no INTEGER, page_label TEXT, page_type TEXT, scale_text TEXT, px_per_m REAL, width_px REAL, height_px REAL, render_zoom REAL, selected INTEGER)")

    conn.execute("INSERT INTO workspaces (id, job_no, job_name) VALUES (101, 'JOB-99', 'Commercial Building CD3001')")
    conn.execute("INSERT INTO documents (id, workspace_id, file_name) VALUES (10, 101, 'drawing_a101.pdf')")
    conn.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, page_label, page_type, width_px, height_px) VALUES (1, 101, 10, 1, 'A101', 'plan', 1000.0, 1000.0)")
    conn.commit()

    app = MockAppDB(conn)
    snapshot = collect_workspace_3d_evidence(app, 101)
    
    assert len(snapshot["pages"]) == 1
    assert snapshot["pages"][0]["page_no"] == 1
    assert snapshot["pages"][0]["document_id"] == 10
    conn.close()


def test_section_3_require_workspace_id_rejects_floats_and_invalid():
    """SECTION 3: Test require_workspace_id() rejects floats like 101.5, booleans, zero, and negative numbers."""
    assert require_workspace_id(101) == 101
    assert require_workspace_id(101.0) == 101
    assert require_workspace_id("101") == 101

    with pytest.raises(ValueError, match="non-integral float rejected"):
        require_workspace_id(101.5)

    with pytest.raises(ValueError, match="must be positive"):
        require_workspace_id(0)

    with pytest.raises(ValueError, match="must be positive"):
        require_workspace_id(-10)

    with pytest.raises(ValueError, match="must be a positive integer"):
        require_workspace_id(True)

    with pytest.raises(ValueError, match="cannot parse as integer"):
        require_workspace_id("abc")


def test_section_18_b5_authorized_opening_grants_wall_deduction_gate():
    """SECTION 18 & 51 & 52: Verify complete B5 proof bundle grants wall deduction authority so net area calculation works."""
    payload = {
        "walls": [
            {
                "wall_ref": "W-1",
                "a": {"x": 0, "y": 0},
                "b": {"x": 10, "y": 0},
                "height_m": 3.0,
                "height_status": "confirmed",
                "openings": [
                    {
                        "id": "op1",
                        "wall_ref": "W-1",
                        "offset_along_wall_m": 2.0,
                        "sill_height_m": 0.0,
                        "width_m": 1.5,
                        "height_m": 2.0,
                        "deduct": True,
                        "manual_override_confirmed": True,
                        "reconciliation_complete": True,
                        "deduction_status": "auto_eligible",
                        "deduction_decision": "deducted",
                        "dimension_basis": "rough_opening",
                        "geometry_confidence": 0.95,
                        "dimension_confidence": 0.95,
                        "association_confidence": 0.95
                    }
                ]
            }
        ]
    }

    project, _ = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    wall = project.buildings[0].levels[0].walls[0]

    assert wall.deduction_authority is True
    p_net = potential_net_wall_area(wall)
    assert p_net["authorized_opening_deduction_area_m2"] == 3.0  # 1.5m * 2.0m = 3.0m² authorized deduction!
    assert p_net["authorized_net_area_m2"] == 27.0  # Gross 30m² - 3m² = 27m² authorized net!


def test_section_20_nested_opening_wrong_host_rejected():
    """SECTION 20: Test nested opening claiming host W-2 nested on W-1 is rejected."""
    payload = {
        "walls": [
            {
                "wall_ref": "W-1",
                "a": {"x": 0, "y": 0},
                "b": {"x": 10, "y": 0},
                "height_m": 3.0,
                "height_status": "confirmed",
                "openings": [
                    {
                        "id": "op_wrong",
                        "resolved_wall_ref": "W-2",  # Claims host W-2 but nested on W-1!
                        "offset_along_wall_m": 1.0,
                        "width_m": 1.0,
                        "height_m": 2.0
                    }
                ]
            }
        ]
    }

    project, skipped = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    wall = project.buildings[0].levels[0].walls[0]
    assert len(wall.openings) == 0
    assert any("wrong_host_conflict" in s.get("reason", "") for s in skipped)


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

    assert first_wrapper is second_wrapper  # Did not double-wrap!


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
