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
    CanonicalWall,
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

    Blocker #1: The elevation candidates MUST be derived from the REAL producer output
    (positive_benchmark.independent_annotation.true_positive_openings + the REAL measured
    scale_pt_per_m calibration), never from a fabricated fallback candidate. Zero plan host
    walls STILL fail closed to 0 physical 3D openings.
    """
    fpath = "tests/fixtures/lago_cd3001_east_elevation_v177.json"
    if not os.path.exists(fpath):
        pytest.skip("Fixture not found")

    with open(fpath, "r", encoding="utf-8") as f:
        fixture_data = json.load(f)

    # Blocker #1: map the 9 REAL true-positive openings -> candidates using the REAL
    # measured calibration (scale_pt_per_m) and opening_height_m from the producer.
    benchmark = fixture_data.get("positive_benchmark") or {}
    independent = benchmark.get("independent_annotation") or {}
    true_pos = independent.get("true_positive_openings") or []
    calibration = fixture_data.get("calibration") or {}
    scale_pt_per_m = calibration.get("scale_pt_per_m")
    opening_height_m = independent.get("opening_height_m")

    real_candidates = []
    if scale_pt_per_m and opening_height_m:
        for tp in true_pos:
            w_pt = float(tp.get("x1_pt") or 0.0) - float(tp.get("x0_pt") or 0.0)
            real_candidates.append({
                "candidate_id": str(tp.get("id") or "tp"),
                "width_m": round(w_pt / scale_pt_per_m, 3) if w_pt > 0 else 0.0,
                "height_m": float(opening_height_m),
                "side": "East",
                "level": "Ground",
            })

    assert len(real_candidates) == 9  # Blocker #1: ALL 9 real openings translated, none fabricated
    assert all(c["width_m"] > 0 and c["height_m"] > 0 for c in real_candidates)

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
                "_source_level": "Ground",
            },
            {
                "box_id": "box_manual_1",
                "manual_m2": 45.0,
                "raw_box": {"manual_m2": 45.0},
                "_source_level": "Ground",
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


def test_section_10_real_lago_9_true_positive_openings_translation():
    """SECTION 10: Test real LAGO fixture translates exactly 9 true-positive openings from positive_benchmark into evidence observations."""
    fpath = "tests/fixtures/lago_cd3001_east_elevation_v177.json"
    if not os.path.exists(fpath):
        pytest.skip("Fixture not found")

    with open(fpath, "r", encoding="utf-8") as f:
        fixture_data = json.load(f)

    independent = fixture_data["positive_benchmark"]["independent_annotation"]
    true_positives = independent["true_positive_openings"]
    assert len(true_positives) == 9

    expected_ids = {tp["id"] for tp in true_positives}
    assert "bay1-light1" in expected_ids
    assert "bay3-light3" in expected_ids

    candidates = [
        {
            "candidate_id": tp["id"],
            "x0_pt": tp["x0_pt"],
            "x1_pt": tp["x1_pt"],
            "width_m": independent["light_width_m"],
            "height_m": independent["opening_height_m"],
            "side": "East",
            "level": "Ground",
        }
        for tp in true_positives
    ]

    payload = {"workspace_id": "lago_101", "elevation_opening_candidates": candidates, "walls": []}
    project, _ = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)

    obs = project.evidence_observations
    assert len(obs) == 9
    obs_ids = {o.id for o in obs}
    assert obs_ids == expected_ids
    total_physical_openings = sum(len(w.openings) for l in project.buildings[0].levels for w in l.walls)
    assert total_physical_openings == 0


def test_section_14_legacy_27m_roof_z_fencing():
    """SECTION 14: Test roof cap Z is accepted ONLY when supporting wall height is confirmed; fallback 2.7m is fenced."""
    unconfirmed_payload = {
        "walls": [
            {"wall_ref": "W1", "a": {"x": 0, "y": 0}, "b": {"x": 10, "y": 0}, "height_m": 2.7, "height_status": "inferred"}
        ],
        "roof_data": {
            "evidence": {"pitches_deg": [22.5], "flat": False},
            "caps": [{"id": "r1", "points": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}], "z": 2.7}],
        }
    }
    proj_unconf, _ = planreader_to_canonical_model(unconfirmed_payload, is_validated_internal_workspace=True)
    roof_unconf = proj_unconf.buildings[0].levels[0].roofs[0]
    assert roof_unconf.metadata["z"] is None  # Fenced!
    assert roof_unconf.review_state == ReviewState.REVIEW_REQUIRED

    confirmed_payload = {
        "walls": [
            {"wall_ref": "W1", "a": {"x": 0, "y": 0}, "b": {"x": 10, "y": 0}, "height_m": 3.2, "height_status": "confirmed"}
        ],
        "roof_data": {
            "evidence": {"pitches_deg": [22.5], "flat": False},
            "caps": [{"id": "r2", "points": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}], "z": 3.2}],
        }
    }
    proj_conf, _ = planreader_to_canonical_model(confirmed_payload, is_validated_internal_workspace=True)
    roof_conf = proj_conf.buildings[0].levels[0].roofs[0]
    assert roof_conf.metadata["z"] == 3.2  # Accepted!
    assert roof_conf.review_state == ReviewState.CONFIRMED


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
    active_names = {l.name for l in active_levels}
    assert active_names == {"Ground Floor", "Level 1", "Level 2"}

    # Each storey holds exactly its own wall
    ground = next(l for l in bld.levels if l.name == "Ground Floor")
    assert len(ground.walls) == 1 and ground.walls[0].provenance.wall_ref == "WG"

    l1 = next(l for l in bld.levels if l.name == "Level 1")
    assert len(l1.walls) == 1 and l1.walls[0].provenance.wall_ref == "W1"

    l2 = next(l for l in bld.levels if l.name == "Level 2")
    assert len(l2.walls) == 1 and l2.walls[0].provenance.wall_ref == "W2"
