import copy
import json
from dataclasses import asdict

from pb_3d_diagnostics import generate_production_diagnostics_report
from pb_canonical_building import ReviewState
from pb_canonical_persistence import compute_workspace_source_fingerprint
from pb_elevation_evidence_v172 import ElevationOpening
from pb_production_3d_adapter import (
    collect_workspace_3d_evidence,
    planreader_to_canonical_model,
    planreader_workspace_to_canonical,
    resolve_canonical_level,
)
from pb_unified_building_v139 import takeoff_rows


def test_phase5m_zero_made_up_level_matrix():
    cases = [None, {}]
    for raw in cases:
        level, slug = resolve_canonical_level(raw, {})
        assert slug == "unresolved"
        assert level.elevation_m is None
        assert level.review_state == ReviewState.REVIEW_REQUIRED

    ground, _ = resolve_canonical_level("Ground", {})
    assert ground.name == "Ground"
    assert ground.elevation_m is None

    explicit, _ = resolve_canonical_level({"id": "L0", "name": "Ground", "elevation_m": 0.0}, {})
    assert explicit.elevation_m == 0.0
    assert explicit.review_state == ReviewState.CONFIRMED

    unregistered, _ = resolve_canonical_level("Ground / unregistered", {})
    assert unregistered.elevation_m is None
    assert unregistered.review_state == ReviewState.REVIEW_REQUIRED
    assert unregistered.metadata["registered_storey"] is False

    sheet, slug = resolve_canonical_level("A101", {})
    assert slug == "unresolved"
    assert sheet.elevation_m is None

    floor_plan, slug = resolve_canonical_level("Ground Floor Plan", {})
    assert slug == "unresolved"
    assert floor_plan.elevation_m is None


def test_phase5m_v135_source_polygon_stays_distinct_without_elevation():
    payload = {
        "walls": [
            {"wall_ref": "N01", "a": [0, 0], "b": [5, 0], "level_name": "Level 1", "source_polygon": "poly_A", "height_m": 3.0, "height_status": "confirmed"},
            {"wall_ref": "N02", "a": [0, 1], "b": [5, 1], "level_name": "Level 1", "source_polygon": "poly_B", "height_m": 3.0, "height_status": "confirmed"},
        ]
    }
    project, _ = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    levels = project.buildings[0].levels
    assert len(levels) == 2
    assert {lvl.metadata.get("source_polygon") for lvl in levels} == {"poly_A", "poly_B"}
    assert all(lvl.elevation_m is None for lvl in levels)


def test_phase5m_provisional_height_and_missing_thickness_fail_closed():
    payload = {
        "walls": [
            {
                "wall_ref": "N01",
                "a": [0, 0],
                "b": [5, 0],
                "height_m": 2.7,
                "height_status": "Provisional until elevation/section height is registered",
            }
        ]
    }
    project, _ = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    wall = project.buildings[0].levels[0].walls[0]
    assert wall.metadata["observed_height_m"] == 2.7
    assert wall.height_m is None
    assert wall.thickness_m is None
    assert wall.review_state == ReviewState.REVIEW_REQUIRED
    assert wall.deduction_authority is False


def test_phase5m_confirmed_height_and_explicit_thickness_preserved():
    payload = {
        "levels": [{"id": "L0", "name": "Ground", "elevation_m": 0.0}],
        "walls": [
            {
                "wall_ref": "N01",
                "level": {"id": "L0", "name": "Ground", "elevation_m": 0.0},
                "a": [0, 0],
                "b": [5, 0],
                "height_m": 3.2,
                "height_status": "confirmed",
                "thickness_m": 0.2,
            }
        ],
    }
    project, _ = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    wall = project.buildings[0].levels[0].walls[0]
    assert wall.height_m == 3.2
    assert wall.thickness_m == 0.2


def test_phase5m_calibrated_floor_without_storey_keeps_xy_in_unresolved_level():
    payload = {
        "mapper_shapes": [
            {
                "box_id": "floor1",
                "page_id": 7,
                "page_width_px": 1000,
                "page_height_px": 1000,
                "px_per_m": 100,
                "raw_box": {"id": "floor1", "x": 10, "y": 20, "w": 40, "h": 30},
                "_source_level_label": "Ground Floor Plan",
            }
        ]
    }
    project, skipped = planreader_to_canonical_model(payload, is_validated_internal_workspace=True)
    floors = [f for lvl in project.buildings[0].levels for f in lvl.floors]
    assert len(floors) == 1
    floor = floors[0]
    level = next(lvl for lvl in project.buildings[0].levels if lvl.id == floor.level_id)
    assert level.elevation_m is None
    assert level.review_state == ReviewState.REVIEW_REQUIRED
    assert floor.review_state == ReviewState.REVIEW_REQUIRED
    assert floor.takeoff_eligible is False
    assert floor.polygon[0].x == 1.0
    assert floor.polygon[0].y == 2.0
    assert not any(s.get("id") == "floor1" and s.get("type") == "FLOOR" for s in skipped)


class _EvidenceApp:
    def lquery(self, sql, args=()):
        if "FROM workspaces" in sql:
            return [{"id": 99, "job_no": "J1", "job_name": "Job 1", "builder_client": "Client", "site_address": "Addr"}]
        if "FROM documents" in sql:
            return [{"id": 55, "workspace_id": 99, "file_name": "elevations.pdf", "sha256": "abc", "category": "elevation", "page_count": 1, "source_type": "pdf"}]
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
                level=None,
                wall_ref="W1",
                coord_space="render_pixel",
                source_page_id=101,
                source_page_no=1,
                calibration={"px_per_m": 100.0, "method": "registered"},
            )
            prov = {
                "source_filename": "elevations.pdf",
                "source_page": 1,
                "drawing_ref": "CD3001",
                "drawing_title": "E1 EAST ELEVATION",
                "elevation_side": "East",
                "coord_space": "render_pixel",
                "level": None,
                "wall_ref": "W1",
                "calibration_source": "registered_reference_line",
                "calibration_state": {"status": "valid", "px_per_m": 100.0},
            }
            return json.dumps({
                "elevation_openings": [asdict(op)],
                "elevation_provenance": [prov],
                "elevation_diagnostics": [{"unrelated": "not-index-authority"}],
            })
        return default


def test_phase5m_normalized_v178_observation_reaches_final_project_once():
    app = _EvidenceApp()
    snap = collect_workspace_3d_evidence(app, 99)
    assert len(snap["evidence_observations"]) == 1
    raw = snap["evidence_observations"][0]
    assert raw["level_name"] is None
    assert raw["source_coords"]["bbox_px"] == [100.0, 200.0, 300.0, 400.0]
    assert raw["calibration_state"] == {"status": "valid", "px_per_m": 100.0}

    result = planreader_workspace_to_canonical(app, 99)
    assert len(result.project.evidence_observations) == 1
    obs = result.project.evidence_observations[0]
    assert obs.level_name is None
    assert obs.deduction_authority is False
    assert obs.no_instance_creation is True
    assert obs.source_coords["source_filename"] == "elevations.pdf"
    assert obs.source_coords["drawing_title"] == "E1 EAST ELEVATION"
    assert obs.source_coords["bbox_px"] == [100.0, 200.0, 300.0, 400.0]
    assert obs.source_coords["calibration_state"] == {"status": "valid", "px_per_m": 100.0}


def test_phase5m_evidence_mutation_changes_fingerprint_but_reorder_does_not():
    snap = collect_workspace_3d_evidence(_EvidenceApp(), 99)
    fp = compute_workspace_source_fingerprint(snap)
    mutated = copy.deepcopy(snap)
    mutated["evidence_observations"][0]["drawing_reference"] = "CD3002"
    assert compute_workspace_source_fingerprint(mutated) != fp

    reordered = copy.deepcopy(snap)
    reordered["evidence_observations"] = list(reversed(reordered["evidence_observations"]))
    assert compute_workspace_source_fingerprint(reordered) == fp


def _canonical_wall_project(ref: str = "N01"):
    payload = {
        "levels": [{"id": "L0", "name": "Ground", "elevation_m": 0.0}],
        "walls": [{
            "wall_ref": ref,
            "level": {"id": "L0", "name": "Ground", "elevation_m": 0.0},
            "a": [0, 0], "b": [5, 0],
            "height_m": 3.0,
            "height_status": "confirmed",
        }],
    }
    return planreader_to_canonical_model(payload, is_validated_internal_workspace=True)[0]


def test_phase5m_actual_v139_rows_reconcile_without_row_role():
    project = _canonical_wall_project("N01")
    producer_rows = takeoff_rows([{
        "wall_ref": "N01",
        "side": "North",
        "substrate": "FC",
        "gross_m2": 15.0,
        "opening_deduction_m2": 0.0,
        "net_m2": 15.0,
        "height_confidence": "Verified",
        "height_status": "confirmed",
    }])
    assert "row_role" not in producer_rows[0]
    report = generate_production_diagnostics_report(project, {"takeoff_rows": producer_rows})
    rec = report["per_wall_quantity_reconciliation"][0]
    assert rec["wall_ref"] == "N01"
    assert rec["reconciliation_status"] == "matched"
    assert rec["production_quantity"] == 15.0
    assert rec["identity_source"] == "dedicated_v139"


def test_phase5m_v139_unknown_ref_and_generic_weak_identity_fail_closed():
    project = _canonical_wall_project("N01")
    rows = [
        {"unit": "m²", "quantity": 15.0, "source_reference": "PB Unified Building v1.3.9 · E99"},
        {"unit": "m²", "quantity": 15.0, "location": "N01", "id": "N01"},
    ]
    report = generate_production_diagnostics_report(project, {"takeoff_rows": rows})
    assert report["per_wall_quantity_reconciliation"][0]["reconciliation_status"] == "unresolved"


def test_phase5m_explicit_zero_quantity_is_valid_not_missing():
    project = _canonical_wall_project("N01")
    rows = [{"unit": "m²", "quantity": 0.0, "source_reference": "PB Unified Building v1.3.9 · N01"}]
    report = generate_production_diagnostics_report(project, {"takeoff_rows": rows})
    rec = report["per_wall_quantity_reconciliation"][0]
    assert rec["production_quantity"] == 0.0
    assert rec["reconciliation_status"] == "variance_detected"
