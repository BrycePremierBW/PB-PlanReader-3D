"""Integration safety regressions for the legacy opening-deduction fence."""
from __future__ import annotations

from pb_opening_safety_fence_v175 import (
    deducted_area_m2,
    is_safe_deduction,
    net_wall_area_m2,
    _preserving_normaliser,
)


def _base(**overrides):
    row = {
        "id": "O1",
        "kind": "Door",
        "wall_ref": "W01",
        "width_m": 1.0,
        "height_m": 2.0,
        "quantity": 1,
        "deduct": True,
        "confidence": "To review",
    }
    row.update(overrides)
    return row


def _p5(**overrides):
    row = _base(
        confidence="Derived",
        reconciliation_complete=True,
        deduction_status="derived_eligible",
        deduction_decision="deducted",
        dimension_basis="rough_opening",
        geometry_confidence=0.82,
        dimension_confidence=0.90,
        association_confidence=0.84,
    )
    row.update(overrides)
    return row


def test_missing_deduct_flag_fails_closed():
    row = _base()
    row.pop("deduct")
    assert is_safe_deduction(row) is False


def test_legacy_review_record_cannot_deduct_even_if_flag_true():
    assert is_safe_deduction(_base(deduct=True, confidence="To review")) is False


def test_valid_manual_estimator_entry_can_deduct():
    assert is_safe_deduction(_base(confidence="Manual estimator entry")) is True


def test_manual_entry_requires_real_wall_assignment():
    assert is_safe_deduction(_base(confidence="Manual estimator entry", wall_ref="Unassigned wall")) is False


def test_manual_entry_requires_positive_dimensions():
    assert is_safe_deduction(_base(confidence="Manual estimator entry", width_m=0.0)) is False


def test_p5_eligible_reconciled_rough_opening_can_deduct():
    assert is_safe_deduction(_p5()) is True


def test_p5_unknown_dimension_basis_cannot_deduct():
    assert is_safe_deduction(_p5(dimension_basis="unknown")) is False


def test_p5_incomplete_reconciliation_cannot_deduct():
    assert is_safe_deduction(_p5(reconciliation_complete=False)) is False


def test_p5_low_confidence_cannot_deduct():
    assert is_safe_deduction(_p5(association_confidence=0.69)) is False


def test_area_helpers_only_apply_safe_records():
    rows = [
        _base(confidence="To review", width_m=3.0, height_m=2.0),
        _base(id="M1", confidence="Manual estimator entry", width_m=1.0, height_m=2.0),
        _p5(id="P1", width_m=0.9, height_m=2.1),
    ]
    assert deducted_area_m2(rows) == 3.89
    assert net_wall_area_m2(10.0, rows) == 6.11


def test_preserving_normaliser_keeps_p5_provenance_and_defaults_false():
    def legacy(raw):
        return {
            "id": raw.get("id", "X"),
            "wall_ref": raw.get("wall_ref", "Unassigned wall"),
            "width_m": raw.get("width_m", 0.0),
            "height_m": raw.get("height_m", 0.0),
            "quantity": raw.get("quantity", 1),
            "deduct": bool(raw.get("deduct", True)),
            "confidence": raw.get("confidence", "To review"),
        }

    normalise = _preserving_normaliser(legacy)
    row = normalise({
        "id": "P5",
        "wall_ref": "W02",
        "width_m": 1.0,
        "height_m": 2.0,
        "dimension_basis": "rough_opening",
        "deduction_status": "derived_eligible",
        "deduction_decision": "deducted",
        "reconciliation_complete": True,
    })
    assert row["deduct"] is False
    assert row["dimension_basis"] == "rough_opening"
    assert row["deduction_status"] == "derived_eligible"
    assert row["deduction_decision"] == "deducted"
    assert row["reconciliation_complete"] is True
