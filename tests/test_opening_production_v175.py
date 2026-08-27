from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pb_accuracy_v13_engines_v145 as accuracy_v145
import pb_opening_deductions_v134 as legacy_v134
import pb_opening_production_v175 as prod


def _unsafe_opening(**extra):
    row = {
        "kind": "door",
        "wall_ref": "W01",
        "width_m": 1.0,
        "height_m": 2.0,
        "quantity": 1,
        "deduct": True,
    }
    row.update(extra)
    return row


def _safe_p5_opening(**extra):
    row = _unsafe_opening(
        reconciliation_complete=True,
        deduction_status="derived_eligible",
        deduction_decision="deducted",
        dimension_basis="rough_opening",
        geometry_confidence=0.8,
        dimension_confidence=0.8,
        association_confidence=0.8,
    )
    row.update(extra)
    return row


def test_legacy_true_default_is_not_authority():
    assert prod.is_authorised_deduction(_unsafe_opening()) is False


def test_manual_estimator_entry_is_authorised():
    assert prod.is_authorised_deduction(
        _unsafe_opening(confidence="Manual estimator entry")
    ) is True


def test_explicit_manual_override_is_authorised():
    assert prod.is_authorised_deduction(
        _unsafe_opening(manual_override_confirmed=True)
    ) is True


def test_p5_complete_proof_is_authorised():
    assert prod.is_authorised_deduction(_safe_p5_opening()) is True


def test_p5_missing_reconciliation_is_blocked():
    assert prod.is_authorised_deduction(
        _safe_p5_opening(reconciliation_complete=False)
    ) is False


def test_p5_non_rough_basis_is_blocked():
    assert prod.is_authorised_deduction(
        _safe_p5_opening(dimension_basis="frame")
    ) is False


def test_p5_low_confidence_is_blocked():
    assert prod.is_authorised_deduction(
        _safe_p5_opening(association_confidence=0.69)
    ) is False


def test_fence_replaces_already_bound_v145_app_aliases():
    app = SimpleNamespace()
    accuracy_v145.apply(app)
    original_bound = app.detect_openings_v145
    try:
        prod.install_legacy_safety_fence(app)
        assert app.detect_openings_v145 is not original_bound
        detected = app.detect_openings_v145([
            {"wall_ref": "W01", "width_m": 1.0, "height_m": 2.0, "deduct": True}
        ])
        assert detected[0]["deduct"] is False
        assert detected[0]["reconciliation_complete"] is False
        assert detected[0]["deduction_status"] == "review"

        rooms = [{"floor_area_m2": 10, "ceiling_reference_m2": 10, "perimeter_m": 12}]
        unsafe = _unsafe_opening(area_m2=2.0)
        summary = app.room_quantity_summary_v145(rooms, [unsafe])
        assert summary["opening_deduction_m2"] == 0.0

        safe = _safe_p5_opening(area_m2=2.0)
        summary = app.room_quantity_summary_v145(rooms, [safe])
        assert summary["opening_deduction_m2"] == 2.0
    finally:
        importlib.reload(accuracy_v145)
        importlib.reload(legacy_v134)


def test_legacy_normaliser_fails_old_true_default_closed():
    app = SimpleNamespace(model_3d_page=lambda *a, **k: None)
    legacy_v134.apply(app)
    try:
        prod.install_legacy_safety_fence(app)
        old_auto = legacy_v134.normalise_opening(_unsafe_opening())
        assert old_auto["deduct"] is False
        manual = legacy_v134.normalise_opening(
            _unsafe_opening(confidence="Manual estimator entry")
        )
        assert manual["deduct"] is True
    finally:
        importlib.reload(accuracy_v145)
        importlib.reload(legacy_v134)


def test_real_lago_native_fixture_runs_b1_to_b5_and_stays_non_deducting():
    fixture_path = Path(__file__).parent / "fixtures" / "lago_b1_ga08_ed04_cluster.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    native = {
        "segments": fixture["segments"],
        "words": fixture["words"],
    }
    result = prod.run_p5_native_payload(
        native,
        page_no=fixture["source"]["pdf_page_1based"],
        page_id=23,
        workspace_id=7,
        scale_info={"px_per_m": fixture["source"]["scale_pt_per_m"], "render_zoom": 1.0},
    )
    assert result["status"] == "ok"
    assert result["candidate_count"] > 0
    assert result["deducted_count"] == 0
    assert result["deducted_area_m2"] == 0.0
    assert any(str(note).startswith("B1:") for note in result["pipeline_notes"])
    assert any(str(note).startswith("B4:") for note in result["pipeline_notes"])
    assert any(str(note).startswith("B5:") for note in result["pipeline_notes"])
    assert all(row["reconciliation_complete"] is True for row in result["instances"])
    assert all(row["deduct"] is False for row in result["instances"])


def test_native_vector_bridge_persists_fail_closed_result():
    saved = {}

    def lquery(sql, params=()):
        if "workspace_id FROM pages" in sql:
            return [{"workspace_id": 9}]
        if "JOIN documents" in sql:
            return [{"workspace_id": 9, "page_no": 1, "path": "/missing/source.pdf"}]
        return []

    app = SimpleNamespace(
        analyse_stored_page_v130=lambda page_id: {"page_id": page_id, "scale": {"px_per_m": 28.3}},
        lquery=lquery,
        fitz=None,
        set_workspace_setting=lambda wid, key, value: saved.update({(wid, key): value}),
    )
    prod.install_native_vector_bridge(app)
    result = app.analyse_stored_page_v130(5)
    assert result["p5_openings"]["status"] == "error"
    assert result["p5_openings"]["deducted_count"] == 0
    assert result["p5_openings"]["deducted_area_m2"] == 0.0
    key = (9, f"{prod.SETTING_PREFIX}5")
    assert key in saved
    payload = json.loads(saved[key])
    assert payload["status"] == "error"
    assert payload["instances"] == []
