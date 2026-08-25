"""Tests for Priority 5 Phase B0 — OpeningEvidence contract and safety rules.

Covers all ChatGPT review corrections through round 4:
  1. Physical-instance dedup requires position anchor
  2. Deduction eligibility separated from deduct decision
  3. dimension_basis enforced (rough_opening only)
  4. Dimension merge by basis+confidence, not source type
  5. Garage/roller door deserialization
  6. quantity=1 enforced for geometric sources
  7. Conflicting specific types at same position rejected
  8. Schedule enrichment: matching type_mark required, all instances enriched
  9. Atomic dimension bundle: width+height+basis+confidence+source travel
     together; rejected dims never donate confidence
"""
from __future__ import annotations

import unittest

from pb_opening_evidence_v170 import (
    CONFIDENCE_AUTO_DEDUCT,
    CONFIDENCE_DERIVED_DEDUCT,
    CONFIDENCE_REVIEW,
    DEDUCTION_AUTO_ELIGIBLE,
    DEDUCTION_DERIVED_ELIGIBLE,
    DEDUCTION_DEDUCTED,
    DEDUCTION_NONE,
    DEDUCTION_REVIEW,
    DIMENSION_BASIS_CLEAR_OPENING,
    DIMENSION_BASIS_FRAME,
    DIMENSION_BASIS_LEAF,
    DIMENSION_BASIS_ROUGH_OPENING,
    DIMENSION_BASIS_UNKNOWN,
    OPENING_TYPE_DOOR,
    OPENING_TYPE_GARAGE,
    OPENING_TYPE_GLAZED,
    OPENING_TYPE_OTHER,
    OPENING_TYPE_ROLLER,
    OPENING_TYPE_WINDOW,
    TOLERANCE_HEIGHT_M,
    TOLERANCE_POSITION_M,
    TOLERANCE_WIDTH_M,
    OpeningEvidence,
    deducted_area_m2,
    deduplicate_openings,
    enriches_by_type,
    from_v134_record,
    merge_opening_evidence,
    net_wall_area_m2,
    same_physical_opening,
    to_v134_record,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_window(
    wall_ref="N01",
    level="Ground",
    width=1.2,
    height=1.5,
    mark="W01",
    position=3.0,
    confidence=0.95,
    extraction_method="plan_vector",
    basis=DIMENSION_BASIS_ROUGH_OPENING,
    dim_source="",
):
    ev = OpeningEvidence(
        type_mark=mark,
        wall_ref=wall_ref,
        level=level,
        opening_type=OPENING_TYPE_WINDOW,
        width_m=width,
        height_m=height,
        dimension_basis=basis,
        dimension_source=dim_source or extraction_method,
        sill_m=0.9,
        position_along_wall_m=position,
        extraction_method=extraction_method,
        geometry_confidence=confidence,
        dimension_confidence=confidence,
        association_confidence=confidence,
    )
    ev.compute_area()
    ev.compute_deduction_status()
    return ev


def _make_door(
    wall_ref="N01",
    level="Ground",
    width=0.82,
    height=2.04,
    mark="D01",
    position=1.0,
    confidence=0.95,
    extraction_method="plan_vector",
    basis=DIMENSION_BASIS_ROUGH_OPENING,
    dim_source="",
):
    ev = OpeningEvidence(
        type_mark=mark,
        wall_ref=wall_ref,
        level=level,
        opening_type=OPENING_TYPE_DOOR,
        width_m=width,
        height_m=height,
        dimension_basis=basis,
        dimension_source=dim_source or extraction_method,
        sill_m=0.0,
        position_along_wall_m=position,
        extraction_method=extraction_method,
        geometry_confidence=confidence,
        dimension_confidence=confidence,
        association_confidence=confidence,
    )
    ev.compute_area()
    ev.compute_deduction_status()
    return ev


# ---------------------------------------------------------------------------
# 1. OpeningEvidence dataclass contract
# ---------------------------------------------------------------------------
class TestOpeningEvidenceContract(unittest.TestCase):

    def test_quantity_always_one_for_geometric_evidence(self):
        ev = _make_window()
        self.assertEqual(ev.quantity, 1)

    def test_set_quantity_geometric_forces_one(self):
        ev = OpeningEvidence(extraction_method="plan_vector")
        ev.set_quantity(4, source="geometric")
        self.assertEqual(ev.quantity, 1)

    def test_set_quantity_manual_allows_grouped(self):
        ev = OpeningEvidence(extraction_method="manual")
        ev.set_quantity(4, source="manual")
        self.assertEqual(ev.quantity, 4)

    def test_duct_defaults_false(self):
        ev = OpeningEvidence(opening_type=OPENING_TYPE_WINDOW)
        self.assertFalse(ev.deduct)

    def test_compute_area(self):
        ev = _make_window(width=1.2, height=1.5)
        self.assertAlmostEqual(ev.area_m2, 1.8, places=4)

    def test_compute_area_none_when_missing(self):
        ev = OpeningEvidence(opening_type=OPENING_TYPE_WINDOW, height_m=1.5)
        ev.compute_area()
        self.assertIsNone(ev.area_m2)

    def test_instance_id_unique(self):
        self.assertNotEqual(
            _make_window().opening_instance_id,
            _make_window().opening_instance_id,
        )

    def test_type_mark_not_identity(self):
        a = _make_window(mark="W01", position=3.0)
        b = _make_window(mark="W01", position=6.0)
        self.assertNotEqual(a.opening_instance_id, b.opening_instance_id)


# ---------------------------------------------------------------------------
# 2. Deduction eligibility separated from deduct decision
# ---------------------------------------------------------------------------
class TestDeductionEligibility(unittest.TestCase):

    def test_high_confidence_auto_eligible(self):
        ev = _make_window(confidence=0.95)
        self.assertEqual(ev.deduction_status, DEDUCTION_AUTO_ELIGIBLE)
        self.assertFalse(ev.deduct)

    def test_medium_confidence_derived_eligible(self):
        ev = _make_window(confidence=0.75)
        self.assertEqual(ev.deduction_status, DEDUCTION_DERIVED_ELIGIBLE)
        self.assertFalse(ev.deduct)

    def test_low_confidence_review(self):
        ev = _make_window(confidence=0.6)
        self.assertEqual(ev.deduction_status, DEDUCTION_REVIEW)
        self.assertFalse(ev.deduct)

    def test_very_low_confidence_none(self):
        ev = _make_window(confidence=0.3)
        self.assertEqual(ev.deduction_status, DEDUCTION_NONE)
        self.assertFalse(ev.deduct)

    def test_is_eligible_for_deduction(self):
        ev = _make_window(confidence=0.95)
        self.assertTrue(ev.is_eligible_for_deduction())
        ev2 = _make_window(confidence=0.6)
        self.assertFalse(ev2.is_eligible_for_deduction())

    def test_missing_dims_blocks_eligibility(self):
        ev = OpeningEvidence(
            opening_type=OPENING_TYPE_WINDOW,
            wall_ref="N01",
            dimension_basis=DIMENSION_BASIS_ROUGH_OPENING,
            geometry_confidence=0.95,
            dimension_confidence=0.95,
            association_confidence=0.95,
        )
        ev.compute_area()
        ev.compute_deduction_status()
        self.assertEqual(ev.deduction_status, DEDUCTION_REVIEW)

    def test_missing_wall_blocks_eligibility(self):
        ev = OpeningEvidence(
            opening_type=OPENING_TYPE_WINDOW,
            width_m=1.2, height_m=1.5,
            dimension_basis=DIMENSION_BASIS_ROUGH_OPENING,
            geometry_confidence=0.95,
            dimension_confidence=0.95,
            association_confidence=0.95,
        )
        ev.compute_area()
        ev.compute_deduction_status()
        self.assertEqual(ev.deduction_status, DEDUCTION_REVIEW)

    def test_mixed_confidence_uses_minimum(self):
        ev = OpeningEvidence(
            opening_type=OPENING_TYPE_WINDOW,
            width_m=1.2, height_m=1.5,
            wall_ref="N01",
            dimension_basis=DIMENSION_BASIS_ROUGH_OPENING,
            geometry_confidence=0.95,
            dimension_confidence=0.95,
            association_confidence=0.6,
        )
        ev.compute_area()
        ev.compute_deduction_status()
        self.assertEqual(ev.deduction_status, DEDUCTION_REVIEW)

    def test_eligible_does_not_set_deduct_true(self):
        ev = _make_window(confidence=0.95)
        self.assertTrue(ev.is_eligible_for_deduction())
        self.assertFalse(ev.deduct)


# ---------------------------------------------------------------------------
# 3. dimension_basis enforced
# ---------------------------------------------------------------------------
class TestDimensionBasisEnforcement(unittest.TestCase):

    def test_rough_opening_eligible(self):
        ev = _make_window(basis=DIMENSION_BASIS_ROUGH_OPENING, confidence=0.95)
        self.assertEqual(ev.deduction_status, DEDUCTION_AUTO_ELIGIBLE)

    def test_leaf_not_eligible(self):
        ev = _make_window(basis=DIMENSION_BASIS_LEAF, confidence=0.95)
        self.assertEqual(ev.deduction_status, DEDUCTION_REVIEW)

    def test_frame_not_eligible(self):
        ev = _make_window(basis=DIMENSION_BASIS_FRAME, confidence=0.95)
        self.assertEqual(ev.deduction_status, DEDUCTION_REVIEW)

    def test_clear_opening_not_eligible_for_auto(self):
        ev = _make_window(basis=DIMENSION_BASIS_CLEAR_OPENING, confidence=0.95)
        self.assertEqual(ev.deduction_status, DEDUCTION_REVIEW)

    def test_unknown_not_eligible(self):
        ev = _make_window(basis=DIMENSION_BASIS_UNKNOWN, confidence=0.95)
        self.assertEqual(ev.deduction_status, DEDUCTION_REVIEW)

    def test_leaf_dims_do_not_auto_deduct(self):
        ev = OpeningEvidence(
            opening_type=OPENING_TYPE_DOOR,
            width_m=0.82, height_m=2.04,
            wall_ref="N01",
            position_along_wall_m=1.0,
            dimension_basis=DIMENSION_BASIS_LEAF,
            extraction_method="schedule_parse",
            geometry_confidence=0.95,
            dimension_confidence=0.95,
            association_confidence=0.95,
        )
        ev.compute_area()
        ev.compute_deduction_status()
        self.assertEqual(ev.deduction_status, DEDUCTION_REVIEW)
        self.assertFalse(ev.is_eligible_for_deduction())


# ---------------------------------------------------------------------------
# 4. Physical-instance dedup requires position anchor
# ---------------------------------------------------------------------------
class TestPhysicalDeduplication(unittest.TestCase):

    def test_two_unassigned_records_not_same(self):
        a = OpeningEvidence(opening_type=OPENING_TYPE_WINDOW, width_m=1.2, height_m=1.5)
        b = OpeningEvidence(opening_type=OPENING_TYPE_WINDOW, width_m=1.2, height_m=1.5)
        self.assertFalse(same_physical_opening(a, b))

    def test_same_wall_no_positions_not_same(self):
        a = _make_window(position=None)
        b = _make_window(position=None)
        self.assertFalse(same_physical_opening(a, b))

    def test_four_identical_windows_remain_four(self):
        windows = [
            _make_window(mark="W01", position=1.0, width=1.2, height=1.5)
            for _ in range(4)
        ]
        windows[0].position_along_wall_m = 1.0
        windows[1].position_along_wall_m = 3.0
        windows[2].position_along_wall_m = 5.0
        windows[3].position_along_wall_m = 7.0
        result = deduplicate_openings(windows)
        self.assertEqual(len(result), 4)

    def test_schedule_plus_four_plan_not_collapsed(self):
        plan_windows = [
            _make_window(mark="W01", position=pos, extraction_method="plan_vector")
            for pos in [1.0, 3.0, 5.0, 7.0]
        ]
        schedule = OpeningEvidence(
            type_mark="W01", wall_ref="N01", level="Ground",
            opening_type=OPENING_TYPE_WINDOW,
            width_m=1.2, height_m=1.5,
            dimension_basis=DIMENSION_BASIS_ROUGH_OPENING,
            extraction_method="schedule_parse",
        )
        result = deduplicate_openings(plan_windows + [schedule])
        self.assertEqual(len(result), 4)

    def test_same_position_matches(self):
        a = _make_window(position=3.0, extraction_method="plan_vector")
        b = _make_window(position=3.05, extraction_method="elevation_rect")
        self.assertTrue(same_physical_opening(a, b))

    def test_different_positions_not_same(self):
        a = _make_window(position=3.0)
        b = _make_window(position=6.0)
        self.assertFalse(same_physical_opening(a, b))

    def test_position_boundary(self):
        a = _make_window(position=3.0)
        b = _make_window(position=3.20)
        self.assertTrue(same_physical_opening(a, b))

    def test_position_beyond_boundary(self):
        a = _make_window(position=3.0)
        b = _make_window(position=3.21)
        self.assertFalse(same_physical_opening(a, b))

    def test_different_wall_never_matches(self):
        a = _make_window(wall_ref="N01", position=3.0)
        b = _make_window(wall_ref="N02", position=3.0)
        self.assertFalse(same_physical_opening(a, b))

    def test_different_level_never_matches(self):
        a = _make_window(level="Ground", position=3.0)
        b = _make_window(level="First", position=3.0)
        self.assertFalse(same_physical_opening(a, b))

    def test_conflicting_types_not_same(self):
        door = _make_door(wall_ref="N01", position=3.0)
        window = _make_window(wall_ref="N01", position=3.0)
        self.assertFalse(same_physical_opening(door, window))

    def test_generic_type_matches_specific(self):
        generic = OpeningEvidence(
            opening_type=OPENING_TYPE_OTHER,
            wall_ref="N01", level="Ground",
            width_m=0.82, height_m=2.04,
            position_along_wall_m=3.0,
            dimension_basis=DIMENSION_BASIS_ROUGH_OPENING,
        )
        door = _make_door(wall_ref="N01", position=3.0)
        self.assertTrue(same_physical_opening(generic, door))

    def test_garage_vs_door_not_same(self):
        garage = OpeningEvidence(
            opening_type=OPENING_TYPE_GARAGE,
            wall_ref="N01", level="Ground",
            width_m=3.0, height_m=2.4,
            position_along_wall_m=5.0,
            dimension_basis=DIMENSION_BASIS_ROUGH_OPENING,
            extraction_method="plan_vector",
            geometry_confidence=0.95,
            dimension_confidence=0.95,
            association_confidence=0.95,
        )
        door = _make_door(wall_ref="N01", position=5.0, width=0.82, height=2.04)
        self.assertFalse(same_physical_opening(garage, door))

    def test_width_beyond_tolerance(self):
        a = _make_window(width=1.20, position=3.0)
        b = _make_window(width=1.26, position=3.0)
        self.assertFalse(same_physical_opening(a, b))


# ---------------------------------------------------------------------------
# 5. Type enrichment: matching type_mark required
# ---------------------------------------------------------------------------
class TestTypeEnrichment(unittest.TestCase):

    def test_schedule_enriches_matching_mark(self):
        detected = _make_window(mark="W01", position=3.0)
        schedule = OpeningEvidence(
            type_mark="W01", wall_ref="N01", level="Ground",
            opening_type=OPENING_TYPE_WINDOW,
            extraction_method="schedule_parse",
        )
        self.assertTrue(enriches_by_type(detected, schedule))

    def test_unlabeled_not_enriched(self):
        detected = _make_window(mark="", position=3.0)
        schedule = OpeningEvidence(
            type_mark="W01", wall_ref="N01", level="Ground",
            opening_type=OPENING_TYPE_WINDOW,
            extraction_method="schedule_parse",
        )
        self.assertFalse(enriches_by_type(detected, schedule))

    def test_different_mark_does_not_enrich(self):
        detected = _make_window(mark="W01", position=3.0)
        schedule = OpeningEvidence(
            type_mark="W02", wall_ref="N01", level="Ground",
            extraction_method="schedule_parse",
        )
        self.assertFalse(enriches_by_type(detected, schedule))

    def test_geometric_source_does_not_enrich(self):
        detected = _make_window(mark="W01", position=3.0)
        another_plan = _make_window(mark="W01", position=3.0,
                                    extraction_method="elevation_rect")
        self.assertFalse(enriches_by_type(detected, another_plan))


# ---------------------------------------------------------------------------
# 6. Dimension merge by basis + confidence
# ---------------------------------------------------------------------------
class TestDimensionMerge(unittest.TestCase):

    def test_rough_opening_preferred_over_leaf(self):
        plan = _make_window(width=1.0, height=2.1,
                            basis=DIMENSION_BASIS_ROUGH_OPENING, confidence=0.8)
        schedule = OpeningEvidence(
            type_mark="W01", wall_ref="N01", level="Ground",
            opening_type=OPENING_TYPE_WINDOW,
            width_m=0.92, height_m=2.04,
            dimension_basis=DIMENSION_BASIS_LEAF,
            extraction_method="schedule_parse",
            dimension_confidence=0.95,
        )
        merged = merge_opening_evidence(plan, schedule)
        self.assertEqual(merged.width_m, 1.0)
        self.assertEqual(merged.height_m, 2.1)
        self.assertEqual(merged.dimension_basis, DIMENSION_BASIS_ROUGH_OPENING)

    def test_same_basis_higher_confidence_wins(self):
        low = _make_window(width=1.15, height=1.45,
                           basis=DIMENSION_BASIS_ROUGH_OPENING, confidence=0.75)
        high = _make_window(width=1.20, height=1.50,
                            basis=DIMENSION_BASIS_ROUGH_OPENING, confidence=0.95)
        merged = merge_opening_evidence(low, high)
        self.assertEqual(merged.width_m, 1.20)
        self.assertEqual(merged.height_m, 1.50)

    def test_schedule_preferred_same_basis_same_confidence(self):
        plan = _make_window(width=1.19, height=1.49,
                            basis=DIMENSION_BASIS_ROUGH_OPENING, confidence=0.95)
        plan.dimension_confidence = 0.95
        schedule = OpeningEvidence(
            type_mark="W01", wall_ref="N01", level="Ground",
            opening_type=OPENING_TYPE_WINDOW,
            width_m=1.20, height_m=1.50,
            dimension_basis=DIMENSION_BASIS_ROUGH_OPENING,
            extraction_method="schedule_parse",
            dimension_confidence=0.95,
        )
        merged = merge_opening_evidence(plan, schedule)
        self.assertEqual(merged.width_m, 1.20)

    def test_partial_width_only_no_basis_relabel(self):
        existing = _make_window(width=1.20, height=1.50,
                                basis=DIMENSION_BASIS_LEAF, confidence=0.80)
        schedule_only_width = OpeningEvidence(
            type_mark="W01", wall_ref="N01", level="Ground",
            opening_type=OPENING_TYPE_WINDOW,
            width_m=1.25, height_m=None,
            dimension_basis=DIMENSION_BASIS_ROUGH_OPENING,
            extraction_method="schedule_parse",
            dimension_confidence=0.95,
        )
        merged = merge_opening_evidence(existing, schedule_only_width)
        self.assertEqual(merged.dimension_basis, DIMENSION_BASIS_LEAF)
        self.assertEqual(merged.width_m, 1.20)
        self.assertEqual(merged.height_m, 1.50)


# ---------------------------------------------------------------------------
# 7. Garage/roller door deserialization
# ---------------------------------------------------------------------------
class TestOpeningTypeClassification(unittest.TestCase):

    def test_garage_door(self):
        record = {"kind": "Garage door", "width_m": 3.0, "height_m": 2.4, "quantity": 1}
        ev = from_v134_record(record)
        self.assertEqual(ev.opening_type, OPENING_TYPE_GARAGE)

    def test_roller_door(self):
        record = {"kind": "Roller door", "width_m": 3.0, "height_m": 2.4, "quantity": 1}
        ev = from_v134_record(record)
        self.assertEqual(ev.opening_type, OPENING_TYPE_ROLLER)

    def test_ordinary_door(self):
        record = {"kind": "Door", "width_m": 0.9, "height_m": 2.1, "quantity": 1}
        ev = from_v134_record(record)
        self.assertEqual(ev.opening_type, OPENING_TYPE_DOOR)

    def test_window(self):
        record = {"kind": "Window", "width_m": 1.2, "height_m": 1.5, "quantity": 1}
        ev = from_v134_record(record)
        self.assertEqual(ev.opening_type, OPENING_TYPE_WINDOW)

    def test_glazed_opening(self):
        record = {"kind": "Glazed opening", "width_m": 1.8, "height_m": 2.1, "quantity": 1}
        ev = from_v134_record(record)
        self.assertEqual(ev.opening_type, OPENING_TYPE_GLAZED)

    def test_roundtrip_all_types(self):
        for otype in (OPENING_TYPE_DOOR, OPENING_TYPE_WINDOW, OPENING_TYPE_GLAZED,
                      OPENING_TYPE_GARAGE, OPENING_TYPE_ROLLER):
            record = {"kind": otype.replace("_", " ").title(),
                      "width_m": 1.0, "height_m": 2.0, "quantity": 1}
            ev = from_v134_record(record)
            self.assertEqual(ev.opening_type, otype, f"Roundtrip failed for {otype}")

    def test_garage_door_sill_is_zero(self):
        record = {"kind": "Garage door", "width_m": 3.0, "height_m": 2.4, "quantity": 1}
        ev = from_v134_record(record)
        self.assertEqual(ev.sill_m, 0.0)

    def test_roller_door_sill_is_zero(self):
        record = {"kind": "Roller door", "width_m": 3.0, "height_m": 2.4, "quantity": 1}
        ev = from_v134_record(record)
        self.assertEqual(ev.sill_m, 0.0)


# ---------------------------------------------------------------------------
# 8. Deduction calculation
# ---------------------------------------------------------------------------
class TestDeductionCalculation(unittest.TestCase):

    def test_deducted_area_only_deducted(self):
        d = _make_window(confidence=0.95)
        d.deduct = True
        nd = _make_window(confidence=0.6)
        self.assertFalse(nd.deduct)
        total = deducted_area_m2([d, nd])
        self.assertAlmostEqual(total, d.area_m2, places=4)

    def test_net_never_negative(self):
        o = _make_window(width=10.0, height=10.0, confidence=0.95)
        o.deduct = True
        self.assertEqual(net_wall_area_m2(5.0, [o]), 0.0)

    def test_net_subtracts(self):
        o = _make_window(width=1.2, height=1.5, confidence=0.95)
        o.deduct = True
        self.assertAlmostEqual(net_wall_area_m2(27.0, [o]), 25.2, places=4)

    def test_uncertain_no_effect(self):
        o = _make_window(confidence=0.6)
        self.assertFalse(o.deduct)
        self.assertEqual(net_wall_area_m2(27.0, [o]), 27.0)

    def test_eligible_but_not_yet_deducted(self):
        o = _make_window(confidence=0.95)
        self.assertTrue(o.is_eligible_for_deduction())
        self.assertFalse(o.deduct)
        self.assertEqual(net_wall_area_m2(27.0, [o]), 27.0)


# ---------------------------------------------------------------------------
# 9. v134 conversion
# ---------------------------------------------------------------------------
class TestV134Conversion(unittest.TestCase):

    def test_to_v134(self):
        ev = _make_window(width=1.2, height=1.5)
        r = to_v134_record(ev)
        self.assertEqual(r["width_m"], 1.2)
        self.assertEqual(r["height_m"], 1.5)

    def test_from_v134_grouped_quantity(self):
        record = {"kind": "Window", "width_m": 1.2, "height_m": 1.5,
                  "quantity": 4, "deduct": True}
        ev = from_v134_record(record)
        self.assertEqual(ev.quantity, 4)
        self.assertAlmostEqual(ev.area_m2, 7.2, places=4)

    def test_roundtrip(self):
        original = _make_window(width=1.2, height=1.5)
        r = to_v134_record(original)
        restored = from_v134_record(r)
        self.assertEqual(restored.width_m, 1.2)
        self.assertEqual(restored.height_m, 1.5)


# ---------------------------------------------------------------------------
# 10. Deterministic evidence merge
# ---------------------------------------------------------------------------
class TestDeterministicMerge(unittest.TestCase):

    def test_evidence_order_stable(self):
        a = _make_window()
        a.evidence = ["source_A", "source_B"]
        b = _make_window()
        b.evidence = ["source_B", "source_C"]
        merged = merge_opening_evidence(a, b)
        self.assertEqual(merged.evidence, ["source_A", "source_B", "source_C"])

    def test_three_source_dedup(self):
        plan = _make_window(width=1.2, height=1.5, position=3.0)
        plan.evidence = ["plan_A301"]
        elev = _make_window(width=1.18, height=1.52, position=3.05)
        elev.evidence = ["elev_A201"]
        sched = OpeningEvidence(
            type_mark="W01", wall_ref="N01", level="Ground",
            opening_type=OPENING_TYPE_WINDOW,
            width_m=1.20, height_m=1.50,
            dimension_basis=DIMENSION_BASIS_ROUGH_OPENING,
            extraction_method="schedule_parse",
        )
        result = deduplicate_openings([plan, elev, sched])
        self.assertEqual(len(result), 1)


# ---------------------------------------------------------------------------
# 11. Schedule enrichment safety
# ---------------------------------------------------------------------------
class TestScheduleEnrichmentSafety(unittest.TestCase):

    def test_schedule_enriches_all_matching_instances(self):
        """W01 schedule + four already-tagged W01 physical windows -> all enriched."""
        plan_windows = [
            _make_window(mark="W01", position=pos, extraction_method="plan_vector")
            for pos in [1.0, 3.0, 5.0, 7.0]
        ]
        for w in plan_windows:
            w.evidence = [f"plan_{w.position_along_wall_m}"]

        schedule = OpeningEvidence(
            type_mark="W01", wall_ref="N01", level="Ground",
            opening_type=OPENING_TYPE_WINDOW,
            width_m=1.2, height_m=1.5,
            dimension_basis=DIMENSION_BASIS_ROUGH_OPENING,
            extraction_method="schedule_parse",
            evidence=["schedule_W01"],
        )
        result = deduplicate_openings(plan_windows + [schedule])
        self.assertEqual(len(result), 4)
        for r in result:
            self.assertIn("schedule_W01", r.evidence)

    def test_schedule_no_arbitrary_assignment_to_unlabeled(self):
        """W01 schedule + four unlabeled openings -> none assigned W01."""
        unlabeled = [
            _make_window(mark="", position=pos, extraction_method="plan_vector")
            for pos in [1.0, 3.0, 5.0, 7.0]
        ]
        schedule = OpeningEvidence(
            type_mark="W01", wall_ref="N01", level="Ground",
            opening_type=OPENING_TYPE_WINDOW,
            width_m=1.2, height_m=1.5,
            dimension_basis=DIMENSION_BASIS_ROUGH_OPENING,
            extraction_method="schedule_parse",
        )
        result = deduplicate_openings(unlabeled + [schedule])
        self.assertEqual(len(result), 5)
        unlabeled_results = [r for r in result if r.type_mark == ""]
        self.assertEqual(len(unlabeled_results), 4)
        for r in unlabeled_results:
            self.assertNotIn("schedule_parse", r.evidence)

    def test_quantity_enforced_at_compute_area(self):
        """quantity=1 enforced even if extraction_method set after construction."""
        ev = OpeningEvidence()
        ev.quantity = 4
        ev.extraction_method = "plan_vector"
        ev.width_m = 1.2
        ev.height_m = 1.5
        ev.compute_area()
        self.assertEqual(ev.quantity, 1)
        self.assertAlmostEqual(ev.area_m2, 1.8, places=4)


# ---------------------------------------------------------------------------
# 12. Atomic dimension bundle — BLOCKER FIXES (round 4)
# ---------------------------------------------------------------------------
class TestAtomicDimensionBundle(unittest.TestCase):

    def test_leaf_dims_not_relabelled_rough_via_enrichment(self):
        """Enrichment replaces the full bundle, not just basis.

        Plan: 1.20 x 1.50, basis=leaf, dim_confidence=0.80
        Schedule: 1.25 x 1.55, basis=rough_opening, dim_confidence=0.95

        After enrichment: 1.25 x 1.55, rough_opening, 0.95
        """
        detected = _make_window(
            mark="W01", position=3.0,
            width=1.20, height=1.50,
            basis=DIMENSION_BASIS_LEAF,
            confidence=0.80,
        )
        detected.evidence = ["plan_3.0"]

        schedule = OpeningEvidence(
            type_mark="W01", wall_ref="N01", level="Ground",
            opening_type=OPENING_TYPE_WINDOW,
            width_m=1.25, height_m=1.55,
            dimension_basis=DIMENSION_BASIS_ROUGH_OPENING,
            extraction_method="schedule_parse",
            dimension_confidence=0.95,
            evidence=["schedule_W01"],
        )
        result = deduplicate_openings([detected, schedule])
        self.assertEqual(len(result), 1)
        merged = result[0]
        # Full bundle must be replaced atomically
        self.assertEqual(merged.width_m, 1.25)
        self.assertEqual(merged.height_m, 1.55)
        self.assertEqual(merged.dimension_basis, DIMENSION_BASIS_ROUGH_OPENING)
        self.assertAlmostEqual(merged.dimension_confidence, 0.95, places=4)
        self.assertEqual(merged.dimension_source, "schedule_parse")
        # Area must be recomputed from new dims
        self.assertAlmostEqual(merged.area_m2, 1.25 * 1.55, places=4)

    def test_partial_width_only_enrichment_no_basis_change(self):
        """Width-only enrichment must not change any dimension field."""
        detected = _make_window(
            mark="W01", position=3.0,
            width=1.20, height=1.50,
            basis=DIMENSION_BASIS_LEAF,
            confidence=0.80,
        )
        detected.evidence = ["plan_3.0"]

        schedule = OpeningEvidence(
            type_mark="W01", wall_ref="N01", level="Ground",
            opening_type=OPENING_TYPE_WINDOW,
            width_m=1.25, height_m=None,
            dimension_basis=DIMENSION_BASIS_ROUGH_OPENING,
            extraction_method="schedule_parse",
            dimension_confidence=0.95,
            evidence=["schedule_W01"],
        )
        result = deduplicate_openings([detected, schedule])
        self.assertEqual(len(result), 1)
        merged = result[0]
        # ALL five dimension fields must remain unchanged
        self.assertEqual(merged.width_m, 1.20)
        self.assertEqual(merged.height_m, 1.50)
        self.assertEqual(merged.dimension_basis, DIMENSION_BASIS_LEAF)
        self.assertAlmostEqual(merged.dimension_confidence, 0.80, places=4)
        self.assertEqual(merged.dimension_source, "plan_vector")

    def test_rejected_dims_do_not_donate_confidence(self):
        """Rough dims 0.75 + rejected leaf dims 0.95 -> retained dim_confidence stays 0.75."""
        rough = _make_window(
            mark="W01", position=3.0,
            width=1.20, height=1.50,
            basis=DIMENSION_BASIS_ROUGH_OPENING,
            confidence=0.75,
        )
        leaf = _make_window(
            mark="W01", position=3.05,
            width=1.10, height=1.40,
            basis=DIMENSION_BASIS_LEAF,
            confidence=0.95,
        )
        merged = merge_opening_evidence(rough, leaf)
        # Leaf dims are rejected (lower basis priority), so their
        # confidence must NOT inflate the retained rough dims
        self.assertAlmostEqual(merged.dimension_confidence, 0.75, places=4)
        self.assertEqual(merged.width_m, 1.20)
        self.assertEqual(merged.height_m, 1.50)
        self.assertEqual(merged.dimension_basis, DIMENSION_BASIS_ROUGH_OPENING)

    def test_accepted_replacement_copies_full_bundle(self):
        """Accepted replacement copies width, height, basis, confidence and source."""
        existing = _make_window(
            mark="W01", position=3.0,
            width=1.20, height=1.50,
            basis=DIMENSION_BASIS_LEAF,
            confidence=0.75,
        )
        existing.dimension_source = "plan_vector"

        replacement = _make_window(
            mark="W01", position=3.05,
            width=1.25, height=1.55,
            basis=DIMENSION_BASIS_ROUGH_OPENING,
            confidence=0.90,
            extraction_method="schedule_parse",
            dim_source="schedule_parse",
        )
        merged = merge_opening_evidence(existing, replacement)
        # All five fields must be copied
        self.assertEqual(merged.width_m, 1.25)
        self.assertEqual(merged.height_m, 1.55)
        self.assertEqual(merged.dimension_basis, DIMENSION_BASIS_ROUGH_OPENING)
        self.assertAlmostEqual(merged.dimension_confidence, 0.90, places=4)
        self.assertEqual(merged.dimension_source, "schedule_parse")

    def test_rejected_replacement_leaves_all_five_unchanged(self):
        """Rejected replacement leaves width, height, basis, confidence, source unchanged."""
        existing = _make_window(
            mark="W01", position=3.0,
            width=1.20, height=1.50,
            basis=DIMENSION_BASIS_ROUGH_OPENING,
            confidence=0.90,
        )
        existing.dimension_source = "elevation_rect"

        rejected = _make_window(
            mark="W01", position=3.05,
            width=1.10, height=1.40,
            basis=DIMENSION_BASIS_LEAF,
            confidence=0.95,
            extraction_method="schedule_parse",
        )
        merged = merge_opening_evidence(existing, rejected)
        # All five dimension fields must remain from existing
        self.assertEqual(merged.width_m, 1.20)
        self.assertEqual(merged.height_m, 1.50)
        self.assertEqual(merged.dimension_basis, DIMENSION_BASIS_ROUGH_OPENING)
        self.assertAlmostEqual(merged.dimension_confidence, 0.90, places=4)
        self.assertEqual(merged.dimension_source, "elevation_rect")

    def test_enrichment_replacement_recomputes_area_and_status(self):
        """Enrichment replacement recomputes area_m2 and deduction_status."""
        detected = _make_window(
            mark="W01", position=3.0,
            width=1.20, height=1.50,
            basis=DIMENSION_BASIS_LEAF,
            confidence=0.80,
        )
        # Geometry/association confidence high enough so that the
        # dimension bundle upgrade is the gating factor
        detected.geometry_confidence = 0.95
        detected.association_confidence = 0.95

        schedule = OpeningEvidence(
            type_mark="W01", wall_ref="N01", level="Ground",
            opening_type=OPENING_TYPE_WINDOW,
            width_m=2.00, height_m=2.50,
            dimension_basis=DIMENSION_BASIS_ROUGH_OPENING,
            extraction_method="schedule_parse",
            dimension_confidence=0.95,
        )
        result = deduplicate_openings([detected, schedule])
        merged = result[0]
        # area_m2 must be from new dims (2.00 x 2.50 = 5.0), not old
        self.assertAlmostEqual(merged.area_m2, 5.0, places=4)
        # deduction_status must reflect new basis + confidence
        self.assertEqual(merged.deduction_status, DEDUCTION_AUTO_ELIGIBLE)


if __name__ == "__main__":
    unittest.main()
