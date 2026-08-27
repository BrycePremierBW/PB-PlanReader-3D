from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pb_accuracy_v13_engines_v145 as accuracy_v145
import pb_opening_deductions_v134 as legacy_v134
import pb_opening_production_v175 as prod

import pytest

from pb_opening_schedule_v171 import ScheduleEntry
from tests.test_pipeline_integration_v174 import _b1_candidate, _mock_b1

from unittest.mock import patch


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


# ============================================================================
# B2 schedule wiring + authoritative B5 publication (review round: complete
# the evidence wiring without weakening any approved B0–B6 rules).
# ============================================================================
def _rough_door_schedule():
    """A genuine rough-opening schedule entry (explicit basis)."""
    return [ScheduleEntry(
        type_mark="D01", width_mm=820, height_mm=2100,
        description="Standard door", count=4, page_no=2,
        parse_source="header_separate",
        dimension_basis="rough_opening",
        basis_source="Rough Opening Width",
    )]


def _generic_schedule():
    """LAGO-style generic WIDTH/HEIGHT schedule — basis unknown, never deducts."""
    return [ScheduleEntry(
        type_mark="D01", width_mm=820, height_mm=2100,
        description="Standard door", count=4, page_no=2,
        parse_source="heuristic", dimension_basis="", basis_source="",
    )]


def _run_payload_with_schedule(schedule_entries, *, candidate, **extra):
    native = {"segments": [], "words": []}
    with patch(
        "pb_plan_opening_detection_v171.plan_opening_candidates",
        return_value=_mock_b1([candidate]),
    ):
        return prod.run_p5_native_payload(
            native, page_no=1, scale_info={"px_per_m": 28.3},
            schedule_entries=schedule_entries, **extra,
        )


def test_b1_plus_rough_schedule_reaches_b5_and_deducts():
    door = _b1_candidate(
        mark="D01", wall="W01", position=1.5, width=0.82,
        geom_conf=0.95, assoc_conf=0.95,
    )
    result = _run_payload_with_schedule(_rough_door_schedule(), candidate=door)
    assert result["status"] == "ok"
    assert result["deducted_count"] == 1
    assert result["deducted_area_m2"] > 0
    inst = result["instances"][0]
    assert inst["reconciliation_complete"] is True
    assert inst["dimension_basis"] == "rough_opening"
    assert inst["deduct"] is True


def test_generic_lago_width_height_cannot_deduct():
    door = _b1_candidate(
        mark="D01", wall="W01", position=1.5, width=0.82,
        geom_conf=0.95, assoc_conf=0.95,
    )
    result = _run_payload_with_schedule(_generic_schedule(), candidate=door)
    assert result["deducted_count"] == 0
    assert result["deducted_area_m2"] == 0.0


def test_ambiguous_repeated_mark_stays_review_no_deduction():
    # Two B1 physical instances share the repeated mark D01, and the schedule
    # carries CONFLICTING dimensions for D01.  B2 must not arbitrarily enrich
    # either instance; both stay review / no-deduction.
    a = _b1_candidate(
        mark="D01", wall="W01", position=1.0, width=0.7,
        geom_conf=0.95, assoc_conf=0.90,
        sig=(1, 100.0, 200.0, 0.7, "door"),
    )
    b = _b1_candidate(
        mark="D01", wall="W01", position=3.0, width=2.2,
        geom_conf=0.95, assoc_conf=0.90,
        sig=(2, 500.0, 200.0, 2.2, "door"),
    )
    # Conflicting schedule dimensions for the same mark D01 -> ambiguous.
    ambiguous_schedule = [
        ScheduleEntry(type_mark="D01", width_mm=700, height_mm=2100,
                      description="Door A", count=1, page_no=2,
                      parse_source="header_separate",
                      dimension_basis="rough_opening", basis_source="Rough Opening Width"),
        ScheduleEntry(type_mark="D01", width_mm=2200, height_mm=2100,
                      description="Door B", count=1, page_no=2,
                      parse_source="header_separate",
                      dimension_basis="rough_opening", basis_source="Rough Opening Width"),
    ]
    native = {"segments": [], "words": []}
    with patch(
        "pb_plan_opening_detection_v171.plan_opening_candidates",
        return_value=_mock_b1([a, b]),
    ):
        result = prod.run_p5_native_payload(
            native, page_no=1, scale_info={"px_per_m": 28.3},
            schedule_entries=ambiguous_schedule,
        )
    instances = result["instances"]
    assert len(instances) == 2
    # neither physical instance was arbitrarily dimensioned or authorised
    assert result["deducted_count"] == 0
    assert result["deducted_area_m2"] == 0.0
    for inst in instances:
        assert inst["deduct"] is False
        assert inst["dimension_basis"] != "rough_opening"


def test_missing_b2_or_b3_evidence_fails_closed():
    door = _b1_candidate(
        mark="D01", wall="W01", position=1.5, width=0.82,
        geom_conf=0.95, assoc_conf=0.95,
    )
    # No schedule (B2) and no elevation (B3) -> plan-only, no deduction.
    result = _run_payload_with_schedule(None, candidate=door)
    assert result["deducted_count"] == 0
    assert result["deducted_area_m2"] == 0.0


def _register_app(register_records=None, *, geom_attach, b5_payload=None):
    store = {}
    if register_records:
        store[(7, legacy_v134.SETTING_KEY)] = json.dumps(register_records, separators=(",", ":"))

    def workspace_setting(wid, key, default=""):
        return store.get((int(wid), key), default)

    def set_workspace_setting(wid, key, value):
        store[(int(wid), key)] = value

    app = SimpleNamespace(
        workspace_setting=workspace_setting,
        set_workspace_setting=set_workspace_setting,
        attach_openings_v137=lambda wid, walls: geom_attach(app, int(wid), walls),
    )
    # Persist a completed B5 payload the way the bridge would, so the consumer
    # (safe_attach) can pick up the authoritative instances.
    if b5_payload:
        store[(7, f"{prod.SETTING_PREFIX}5")] = json.dumps(b5_payload, separators=(",", ":"))
        store[(7, prod.PAGES_INDEX_KEY)] = json.dumps([5], separators=(",", ":"))
    return app, store


def _b5_payload(wall_ref="W01", mark="D01"):
    return {
        "instances": [{
            "deduct": True, "wall_ref": wall_ref, "resolved_wall_ref": wall_ref,
            "type_mark": mark, "width_m": 0.82, "height_m": 2.1,
            "quantity": 1, "page_no": 1,
            "reconciliation_complete": True, "deduction_status": "auto_eligible",
            "deduction_decision": "deducted", "dimension_basis": "rough_opening",
            "geometry_confidence": 0.9, "dimension_confidence": 0.9,
            "association_confidence": 0.9,
        }],
    }


def test_b5_authoritative_reduces_only_assigned_wall_once():
    # A completed B5 decision is merged into the net-area consumer and reduces
    # exactly its own wall once (a legacy no-proof auto record for the same
    # opening is superseded, never double counted).
    from pb_opening_geometry_v137 import attach_openings as _geom_attach
    app, _ = _register_app(
        [
            {"wall_ref": "W01", "kind": "Door", "label": "D01", "width_m": 0.82,
             "height_m": 2.1, "deduct": True},  # legacy no-proof auto record
        ],
        geom_attach=_geom_attach,
        b5_payload=_b5_payload(),
    )
    try:
        prod.install_legacy_safety_fence(app)
        walls = [
            {"wall_ref": "W01", "length_m": 5.0, "side": "North"},
            {"wall_ref": "W02", "length_m": 4.0, "side": "North"},
        ]
        attached = app.attach_openings_v137(7, walls)
        w01 = [o for o in attached if o.get("resolved_wall_ref") == "W01"]
        w02 = [o for o in attached if o.get("resolved_wall_ref") == "W02"]
        # exactly ONE deducting row on W01 (the proven B5 row); the unproven
        # legacy record is retained but carries deduct=False, so never double-deducts
        assert sum(1 for o in w01 if o.get("deduct")) == 1
        assert any(o.get("deduct") for o in w01)
        assert w02 == []
        d_w01 = sum(o["area_m2"] for o in w01 if o.get("deduct"))
        # exactly once: 0.82 * 2.1
        assert abs(d_w01 - 0.82 * 2.1) < 1e-6
    finally:
        importlib.reload(accuracy_v145)
        importlib.reload(legacy_v134)


def test_b5_merges_without_double_counting_legacy_same_openings():
    from pb_opening_geometry_v137 import attach_openings as _geom_attach
    # The same physical opening must never be deducted twice, whether it appears
    # once via B5 or once via a legacy record for the same wall+mark+dims.
    app, _ = _register_app(
        [
            {"wall_ref": "W01", "kind": "Door", "label": "D01", "width_m": 0.82,
             "height_m": 2.1, "deduct": True},  # same physical opening, no-proof
        ],
        geom_attach=_geom_attach,
        b5_payload=_b5_payload(),
    )
    try:
        prod.install_legacy_safety_fence(app)
        walls = [{"wall_ref": "W01", "length_m": 5.0, "side": "North"},
                 {"wall_ref": "W02", "length_m": 4.0, "side": "North"}]
        attached = app.attach_openings_v137(7, walls)
        w01 = [o for o in attached
               if o.get("resolved_wall_ref") == "W01"
               and o.get("type_mark") == "D01"]
        # one deducting row, area counted once
        assert len(w01) == 1 and w01[0]["deduct"] is True
        assert abs(sum(o["area_m2"] for o in w01 if o.get("deduct")) - 0.82 * 2.1) < 1e-6
    finally:
        importlib.reload(accuracy_v145)
        importlib.reload(legacy_v134)


def test_two_distinct_same_sized_doors_each_deduct_once():
    # Fix 3: two SEPARATE physical 820x2100 doors on the same wall must NOT be
    # collapsed into one deduction.  Distinct instance identity -> 2x area.
    from pb_opening_geometry_v137 import attach_openings as _geom_attach

    def two_door_payload():
        base = dict(_b5_payload(wall_ref="W01", mark="D01")["instances"][0])
        d1 = dict(base, opening_instance_id="inst-aaaa",
                  plan_geometry_signature="sig-aaaa", position_along_wall_m=1.0)
        d2 = dict(base, opening_instance_id="inst-bbbb",
                  plan_geometry_signature="sig-bbbb", position_along_wall_m=3.0)
        return {"instances": [d1, d2]}

    app, _ = _register_app([], geom_attach=_geom_attach,
                           b5_payload=two_door_payload())
    try:
        prod.install_legacy_safety_fence(app)
        walls = [{"wall_ref": "W01", "length_m": 9.0, "side": "North"}]
        attached = app.attach_openings_v137(7, walls)
        w01 = [o for o in attached if o.get("resolved_wall_ref") == "W01"
               and o.get("deduct")]
        # two distinct physical openings survive and both deduct
        assert len(w01) == 2
        total = sum(o["area_m2"] for o in w01)
        assert abs(total - 2 * (0.82 * 2.1)) < 1e-6
        # each deducts exactly once
        assert abs(sum(o["area_m2"] for o in w01) - 2 * 0.82 * 2.1) < 1e-6
    finally:
        importlib.reload(accuracy_v145)
        importlib.reload(legacy_v134)


def test_persisted_invalid_b5_proof_is_never_deducted():
    # Fix 1: a persisted payload carrying deduct=True but INVALID/incomplete B5
    # proof must reach the wall consumer as ZERO deduction (re-authorised at
    # consumption, not trusted by the deduct flag alone).
    from pb_opening_geometry_v137 import attach_openings as _geom_attach

    # deduct=True but reconciliation never completed -> must be refused.
    broken = dict(_b5_payload()["instances"][0])
    broken.pop("reconciliation_complete")
    broken["deduct"] = True
    app, _ = _register_app([], geom_attach=_geom_attach,
                           b5_payload={"instances": [broken]})
    try:
        prod.install_legacy_safety_fence(app)
        walls = [{"wall_ref": "W01", "length_m": 5.0, "side": "North"}]
        attached = app.attach_openings_v137(7, walls)
        w01 = [o for o in attached if o.get("resolved_wall_ref") == "W01"]
        # the persisted deduct flag is NOT trusted: no deduction reaches the wall
        assert not any(o.get("deduct") for o in w01)
        assert all(not o.get("deduct") for o in w01)
    finally:
        importlib.reload(accuracy_v145)
        importlib.reload(legacy_v134)


def test_manual_estimator_override_preserved():

    from pb_opening_geometry_v137 import attach_openings as _geom_attach
    # An explicit estimator override on a separate opening is retained; the
    # B5-authorised opening coexists without being double-counted.
    app, _ = _register_app(
        [
            {"wall_ref": "W02", "kind": "Door", "label": "D02", "width_m": 1.0,
             "height_m": 2.1, "deduct": True, "manual_override_confirmed": True},
        ],
        geom_attach=_geom_attach,
        b5_payload=_b5_payload(wall_ref="W01", mark="D01"),
    )
    try:
        prod.install_legacy_safety_fence(app)
        walls = [
            {"wall_ref": "W01", "length_m": 5.0, "side": "North"},
            {"wall_ref": "W02", "length_m": 4.0, "side": "North"},
        ]
        attached = app.attach_openings_v137(7, walls)
        w01 = [o for o in attached if o.get("resolved_wall_ref") == "W01"]
        w02 = [o for o in attached if o.get("resolved_wall_ref") == "W02"]
        assert len(w01) == 1 and w01[0]["deduct"] is True  # B5 authoritative
        assert len(w02) == 1 and w02[0]["deduct"] is True  # estimator override kept
    finally:
        importlib.reload(accuracy_v145)
        importlib.reload(legacy_v134)


def test_manual_override_is_not_overridden_by_b5():
    from pb_opening_geometry_v137 import attach_openings as _geom_attach
    # The estimator explicitly excluded this opening; a completed B5 decision
    # must not re-enable it on the same physical opening.
    app, _ = _register_app(
        [
            {"wall_ref": "W01", "kind": "Door", "label": "D01", "width_m": 0.82,
             "height_m": 2.1, "deduct": False, "manual_override_confirmed": True},
        ],
        geom_attach=_geom_attach,
        b5_payload=_b5_payload(wall_ref="W01", mark="D01"),
    )
    try:
        prod.install_legacy_safety_fence(app)
        walls = [{"wall_ref": "W01", "length_m": 5.0, "side": "North"}]
        attached = app.attach_openings_v137(7, walls)
        w01 = [o for o in attached if o.get("resolved_wall_ref") == "W01"]
        assert len(w01) == 1
        assert w01[0]["deduct"] is False
    finally:
        importlib.reload(accuracy_v145)
        importlib.reload(legacy_v134)


def test_explicit_manual_exclude_persists_and_blocks_b5():
    # End-to-end: the estimator explicitly saves an opening as DO-NOT-DEDUCT
    # through the REAL patched save path (legacy._save -> safe_save after
    # fencing).  That confirmed exclusion must persist, and a later completed B5
    # result for the same physical opening must NOT re-enable it -> 0 m2.
    from pb_opening_geometry_v137 import attach_openings as _geom_attach
    app, _ = _register_app([], geom_attach=_geom_attach,
                           b5_payload=_b5_payload(wall_ref="W01", mark="D01"))
    try:
        prod.install_legacy_safety_fence(app)
        # The estimator saves this opening with Deduct unchecked.
        legacy_v134._save(app, 7, [{
            "kind": "Door", "label": "D01", "wall_ref": "W01",
            "width_m": 0.82, "height_m": 2.1, "deduct": False,
        }])
        # Reload from the register (real load path): the exclusion persisted and
        # is remembered as an explicit estimator decision.
        loaded = legacy_v134._load(app, 7)
        assert len(loaded) == 1
        assert loaded[0]["deduct"] is False
        assert loaded[0]["manual_override_confirmed"] is True
        # Completed B5 evidence for the same physical opening becomes available.
        walls = [{"wall_ref": "W01", "length_m": 5.0, "side": "North"}]
        attached = app.attach_openings_v137(7, walls)
        w01 = [o for o in attached if o.get("resolved_wall_ref") == "W01"]
        # still zero deduction: the explicit estimator exclusion is honoured
        assert not any(o.get("deduct") for o in w01)
        assert sum(o["area_m2"] for o in w01 if o.get("deduct")) == 0.0
    finally:
        importlib.reload(accuracy_v145)
        importlib.reload(legacy_v134)


def test_explicit_manual_include_persists_and_stays_authorised():
    # Positive case: an opening explicitly saved as DEDUCT via the real patched
    # save path persists as a confirmed manual decision and remains a deduction.
    from pb_opening_geometry_v137 import attach_openings as _geom_attach
    app, _ = _register_app([], geom_attach=_geom_attach,
                           b5_payload=_b5_payload(wall_ref="W01", mark="D01"))
    try:
        prod.install_legacy_safety_fence(app)
        legacy_v134._save(app, 7, [{
            "kind": "Door", "label": "D01", "wall_ref": "W01",
            "width_m": 0.82, "height_m": 2.1, "deduct": True,
        }])
        loaded = legacy_v134._load(app, 7)
        assert len(loaded) == 1
        assert loaded[0]["deduct"] is True
        assert loaded[0]["manual_override_confirmed"] is True
        walls = [{"wall_ref": "W01", "length_m": 5.0, "side": "North"}]
        attached = app.attach_openings_v137(7, walls)
        w01 = [o for o in attached if o.get("resolved_wall_ref") == "W01"]
        # the confirmed manual include remains a single authorised deduction
        assert sum(1 for o in w01 if o.get("deduct")) == 1
        assert abs(sum(o["area_m2"] for o in w01 if o.get("deduct")) - 0.82 * 2.1) < 1e-6
    finally:
        importlib.reload(accuracy_v145)
        importlib.reload(legacy_v134)


def test_end_to_end_b5_reaches_wall_net_area_exactly_once():
    # Real end-to-end authoritative trace: a completed B5 decision is merged at
    # the registered-wall consumer (attach_openings_v137) and reduces the net
    # area of exactly its assigned wall, exactly once.  This is what proves the
    # B5 result is genuinely CONSUMED by the quantity calculation rather than
    # merely persisted/displayed.
    from pb_unified_building_v139 import build_registered_walls
    from pb_opening_geometry_v137 import attach_openings as _geom_attach
    app, _ = _register_app([], geom_attach=_geom_attach, b5_payload=_b5_payload())
    app.register_elevations_v135 = lambda wid: {"facades": {}}
    app.registered_wall_records_v135 = lambda wid: [
        {"wall_ref": "W01", "side": "North", "length_m": 5.0, "height_m": 2.7},
        {"wall_ref": "W02", "side": "East", "length_m": 4.0, "height_m": 2.7},
    ]
    try:
        prod.install_legacy_safety_fence(app)
        walls = build_registered_walls(app, 7)
        w01 = next(w for w in walls if w["wall_ref"] == "W01")
        w02 = next(w for w in walls if w["wall_ref"] == "W02")
        gross_w01 = 5.0 * 2.7
        assert w01["gross_m2"] == round(gross_w01, 3)
        # exactly the single B5 opening subtracted, on W01 once
        assert abs(w01["opening_deduction_m2"] - 0.82 * 2.1) < 1e-3
        assert abs(w01["net_m2"] - (gross_w01 - 0.82 * 2.1)) < 1e-3
        # nothing on the other wall
        assert abs(w02["opening_deduction_m2"]) < 1e-9
    finally:
        importlib.reload(accuracy_v145)
        importlib.reload(legacy_v134)


def test_end_to_end_b5_not_double_deducted_with_legacy_register():
    # Both the legacy register AND a completed B5 decision describe the same
    # physical opening on W01: the proven B5 instance supersedes the legacy
    # no-proof record, so it is subtracted exactly once.
    from pb_unified_building_v139 import build_registered_walls
    from pb_opening_geometry_v137 import attach_openings as _geom_attach
    app, _ = _register_app(
        [{"wall_ref": "W01", "kind": "Door", "label": "D01",
          "width_m": 0.82, "height_m": 2.1, "deduct": True}],
        geom_attach=_geom_attach,
        b5_payload=_b5_payload(),
    )
    app.register_elevations_v135 = lambda wid: {"facades": {}}
    app.registered_wall_records_v135 = lambda wid: [
        {"wall_ref": "W01", "side": "North", "length_m": 5.0, "height_m": 2.7},
    ]
    try:
        prod.install_legacy_safety_fence(app)
        walls = build_registered_walls(app, 7)
        w01 = next(w for w in walls if w["wall_ref"] == "W01")
        # subtracted once, not twice
        assert abs(w01["opening_deduction_m2"] - 0.82 * 2.1) < 1e-3
    finally:
        importlib.reload(accuracy_v145)
        importlib.reload(legacy_v134)


def test_door_window_schedule_page_classifier():
    # The bridge only treats clear door/window schedule pages as B2 evidence,
    # so it never invents schedule data from plan or material/finish pages.
    assert prod._looks_like_door_window_schedule("DOOR SCHEDULE CD6307/05 D01") is True
    assert prod._looks_like_door_window_schedule("WINDOW SCHEDULE 01 EW03") is True
    assert prod._looks_like_door_window_schedule("GA LEVEL 08 FLOOR PLAN") is False
    assert prod._looks_like_door_window_schedule("FINISHES MATERIAL SCHEDULE PT1") is False


def test_extract_schedule_entries_empty_when_no_schedule_page():
    # Missing B2 evidence fails closed: a document with no door/window schedule
    # page yields no ScheduleEntry objects (bridge fabricates nothing).
    class _FakeDoc:
        page_count = 3

        def load_page(self, idx):
            words = [(0, 0, 100, 20, "GA LEVEL 08 FLOOR PLAN")]
            return SimpleNamespace(get_text=lambda mode: words)

    entries = prod.extract_schedule_entries(_FakeDoc())
    assert entries == []


def test_startup_guard_detects_unfenced_legacy_path():
    # Regression: the startup guard must DETECT an unfenced legacy path (no
    # `_pb_opening_legacy_safety_v175` marker) and refuse to proceed, rather than
    # silently allowing legacy auto-deduction defaults.
    import pb_opening_production_guard_v175 as guard

    app = SimpleNamespace(
        detect_openings_v145=lambda rows: [dict(r, deduct=False) for r in rows],
        room_quantity_summary_v145=lambda rooms, openings: {"opening_deduction_m2": 0.0},
        facade_net_area_v145=lambda regions, openings: {},
        normalise_opening=lambda row: dict(row, deduct=False),
        deducted_opening_area_m2=lambda openings: 0.0,
        analyse_stored_page_v130=lambda page_id: {},
        run_p5_opening_native_payload_v175=lambda *a, **k: {},
        is_authorised_opening_deduction_v175=lambda row: False,
        # NOTE: `_pb_opening_legacy_safety_v175` intentionally ABSENT -> unfenced.
    )
    with pytest.raises(RuntimeError) as exc:
        guard.verify(app)
    assert "safety fence was not installed" in str(exc.value)

    # The canonical marker must be set once fencing is installed (install_legacy_
    # safety_fence is called by the bridge install path), and the misspelled
    # `_bp_pb_opening_legacy_safety_v175` variant must never appear.
    fenced = SimpleNamespace(
        detect_openings_v145=lambda rows: [dict(r, deduct=False) for r in rows],
        room_quantity_summary_v145=lambda rooms, openings: {"opening_deduction_m2": 0.0},
        facade_net_area_v145=lambda regions, openings: {},
        normalise_opening=lambda row: dict(row, deduct=False),
        deducted_opening_area_m2=lambda openings: 0.0,
        analyse_stored_page_v130=lambda page_id: {},
        run_p5_opening_native_payload_v175=lambda *a, **k: {},
        is_authorised_opening_deduction_v175=lambda row: False,
        attach_openings_v137=lambda wid, walls: [],
    )
    prod.install_legacy_safety_fence(fenced)
    prod.install_native_vector_bridge(fenced)
    assert getattr(fenced, "_pb_opening_legacy_safety_v175", False) is True
    assert getattr(fenced, "_pb_opening_consumer_attach_v175", False) is True
    assert getattr(fenced, "_pb_opening_native_bridge_v175", False) is True
    assert not hasattr(fenced, "_bp_pb_opening_legacy_safety_v175")
    guard.verify(fenced)  # must NOT raise once fully fenced


def test_startup_guard_detects_missing_consumer_wrapper():
    # Regression: production integration must not report itself installed if the
    # authoritative attach_openings_v137 B5 consumer wrapper was never installed.
    import pb_opening_production_guard_v175 as guard

    app = SimpleNamespace(
        detect_openings_v145=lambda rows: [dict(r, deduct=False) for r in rows],
        room_quantity_summary_v145=lambda rooms, openings: {"opening_deduction_m2": 0.0},
        facade_net_area_v145=lambda regions, openings: {},
        normalise_opening=lambda row: dict(row, deduct=False),
        deducted_opening_area_m2=lambda openings: 0.0,
        analyse_stored_page_v130=lambda page_id: {},
        run_p5_opening_native_payload_v175=lambda *a, **k: {},
        is_authorised_opening_deduction_v175=lambda row: False,
        _pb_opening_legacy_safety_v175=True,
        _pb_opening_native_bridge_v175=True,
        # NOTE: `_pb_opening_consumer_attach_v175` ABSENT; attach_openings_v137
        # never bound, so the authoritative B5 consumer is not installed.
    )
    with pytest.raises(RuntimeError) as exc:
        guard.verify(app)
    assert "consumer wrapper was not installed" in str(exc.value)


def test_schedule_alone_creates_no_physical_instance():
    # B2 only enriches an ALREADY-EXISTING physical B1 instance.  A schedule
    # mark alone must never create a physical opening / deduction on its own,
    # even when it carries explicit rough-opening dimensions.
    native = {"segments": [], "words": []}
    with patch(
        "pb_plan_opening_detection_v171.plan_opening_candidates",
        return_value=_mock_b1([]),
    ):
        result = prod.run_p5_native_payload(
            native, page_no=1, scale_info={"px_per_m": 28.3},
            schedule_entries=_rough_door_schedule(),
        )
    assert result["instances"] == []
    assert result["deducted_count"] == 0
    assert result["deducted_area_m2"] == 0.0


def test_mark_family_classification_ed_id_ew_iw():
    # Fix 2: the anchored classifier must read full approved mark families, not
    # the first character. ED/ID are DOORS, EW/IW are WINDOWS.
    from pb_opening_deductions_v134 import normalise_opening

    for mark, expected in (
        ("D01", "door"), ("ED01", "door"), ("ID02", "door"),
        ("W01", "window"), ("EW03", "window"), ("IW05", "window"),
        ("D0", "door"), ("W0", "window"), ("D12", "door"), ("EW120", "window"),
    ):
        assert prod._opening_category({"type_mark": mark}) == expected, mark
    # Fix 2 cleanup: true full-match rejects trailing junk and bare prefixes.
    for junk, not_expected in (
        ("ED01XYZ", "door"), ("ID02junk", "door"),
        ("IW05junk", "window"), ("EW03abc", "window"),
        ("I", "door"), ("II", "door"), ("E", "window"),
        ("DD01", "door"), ("WW01", "window"), ("", "door"), ("D-01", "door"),
        ("D01 1", "door"), ("W01/1", "window"),
    ):
        assert prod._opening_category({"type_mark": junk}) != not_expected, junk
    # bare I/II must not be classified as any family
    for bare in ("I", "II", "E"):
        assert prod._opening_category({"type_mark": bare}) == "other", bare
    # A genuine opening_type field takes precedence over the mark fallback.
    assert prod._opening_category(
        {"type_mark": "ED01", "opening_type": "window"}) == "window"
    # Legacy kind form:
    legacy = normalise_opening({"wall_ref": "W01", "kind": "Door",
                                "width_m": 0.82, "height_m": 2.1})
    assert prod._opening_category(legacy) == "door"
    # Same-opening: D01 mark and legacy Door record on the same wall+dims.
    b5 = {"wall_ref": "W01", "type_mark": "D01", "width_m": 0.82, "height_m": 2.1,
          "opening_instance_id": "x1"}
    other = {"wall_ref": "W01", "kind": "Door", "label": "D01",
             "width_m": 0.82, "height_m": 2.1}
    assert prod._same_opening(b5, other) is True
    # A door and a window on the same wall are NOT the same opening.
    window_b5 = {"wall_ref": "W01", "type_mark": "W01",
                 "width_m": 1.2, "height_m": 1.2, "opening_instance_id": "x2"}
    assert prod._same_opening(b5, window_b5) is False


