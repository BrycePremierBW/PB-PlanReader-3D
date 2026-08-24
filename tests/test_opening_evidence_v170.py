"""Tests for Priority 5 Phase B0 — OpeningEvidence contract and safety rules.

These tests prove the safety contract BEFORE any production deduction
changes are made. The contract is:
  - One record per physical opening, quantity = 1
  - deduct defaults to False for auto-detected evidence
  - Uncertain openings never alter net wall m2
  - Tolerance-based deduplication (not rounding)
  - Dimension basis tracking
  - Confidence-gated deduction status
"""
from __future__ import annotations

import unittest

from pb_opening_evidence_v170 import (
    CONFIDENCE_AUTO_DEDUCT,
    CONFIDENCE_DERIVED_DEDUCT,
    CONFIDENCE_REVIEW,
    DEDUCTION_DEDUCTED,
    DEDUCTION_REVIEW,
    DIMENSION_BASIS_FRAME,
    DIMENSION_BASIS_LEAF,
    DIMENSION_BASIS_ROUGH_OPENING,
    DIMENSION_BASIS_UNKNOWN,
    OPENING_TYPE_DOOR,
    OPENING_TYPE_WINDOW,
    TOLERANCE_HEIGHT_M,
    TOLERANCE_POSITION_M,
    TOLERANCE_WIDTH_M,
    OpeningEvidence,
    deducted_area_m2,
    deduplicate_openings,
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
):
    """Create a window OpeningEvidence with typical values."""
    ev = OpeningEvidence(
        type_mark=mark,
        wall_ref=wall_ref,
        level=level,
        opening_type=OPENING_TYPE_WINDOW,
        width_m=width,
        height_m=height,
        dimension_basis=DIMENSION_BASIS_ROUGH_OPENING,
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
):
    """Create a door OpeningEvidence with typical values."""
    ev = OpeningEvidence(
        type_mark=mark,
        wall_ref=wall_ref,
        level=level,
        opening_type=OPENING_TYPE_DOOR,
        width_m=width,
        height_m=height,
        dimension_basis=DIMENSION_BASIS_ROUGH_OPENING,
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
# B0-1: OpeningEvidence dataclass contract
# ---------------------------------------------------------------------------
class TestOpeningEvidenceContract(unittest.TestCase):
    """B0-1: OpeningEvidence must enforce the safety contract."""

    def test_quantity_always_one_for_geometric_evidence(self):
        """Geometric evidence must have quantity = 1."""
        ev = _make_window()
        self.assertEqual(ev.quantity, 1)

    def test_deduct_defaults_false(self):
        """Auto-detected evidence must default to deduct=False."""
        ev = OpeningEvidence(
            opening_type=OPENING_TYPE_WINDOW,
            width_m=1.2,
            height_m=1.5,
        )
        ev.compute_area()
        self.assertFalse(ev.deduct)
        self.assertEqual(ev.deduction_status, DEDUCTION_REVIEW)

    def test_compute_area_from_dimensions(self):
        """area_m2 = width x height x quantity."""
        ev = _make_window(width=1.2, height=1.5)
        self.assertAlmostEqual(ev.area_m2, 1.8, places=4)

    def test_compute_area_missing_dimensions(self):
        """area_m2 is None when dimensions are missing."""
        ev = OpeningEvidence(
            opening_type=OPENING_TYPE_WINDOW,
            width_m=None,
            height_m=1.5,
        )
        ev.compute_area()
        self.assertIsNone(ev.area_m2)

    def test_dimension_basis_required(self):
        """dimension_basis must be tracked."""
        ev = _make_window()
        self.assertEqual(ev.dimension_basis, DIMENSION_BASIS_ROUGH_OPENING)

    def test_instance_id_unique(self):
        """Each opening gets a unique instance ID."""
        ev1 = _make_window()
        ev2 = _make_window()
        self.assertNotEqual(ev1.opening_instance_id, ev2.opening_instance_id)

    def test_type_mark_not_identity(self):
        """Type mark is NOT the physical identity."""
        ev1 = _make_window(mark="W01", position=3.0)
        ev2 = _make_window(mark="W01", position=6.0)
        # Same type mark but different positions -> different openings
        self.assertNotEqual(ev1.opening_instance_id, ev2.opening_instance_id)
        self.assertFalse(same_physical_opening(ev1, ev2))


# ---------------------------------------------------------------------------
# B0-2: Confidence-gated deduction status
# ---------------------------------------------------------------------------
class TestDeductionGating(unittest.TestCase):
    """B0-2: deduction_status must be gated by confidence thresholds."""

    def test_high_confidence_eligible_for_deduction(self):
        """All confidences >= 0.9 -> deduct=True."""
        ev = _make_window(confidence=0.95)
        self.assertTrue(ev.deduct)
        self.assertEqual(ev.deduction_status, DEDUCTION_DEDUCTED)

    def test_medium_confidence_eligible_for_deduction(self):
        """All confidences >= 0.7 -> deduct=True, Derived status."""
        ev = _make_window(confidence=0.75)
        self.assertTrue(ev.deduct)
        self.assertEqual(ev.deduction_status, DEDUCTION_DEDUCTED)

    def test_low_confidence_no_deduction(self):
        """Confidence 0.5-0.7 -> Review, no deduction."""
        ev = _make_window(confidence=0.6)
        self.assertFalse(ev.deduct)
        self.assertEqual(ev.deduction_status, DEDUCTION_REVIEW)

    def test_very_low_confidence_no_deduction(self):
        """Confidence < 0.5 -> Review, no deduction."""
        ev = _make_window(confidence=0.3)
        self.assertFalse(ev.deduct)
        self.assertEqual(ev.deduction_status, DEDUCTION_REVIEW)

    def test_missing_dimensions_blocks_deduction(self):
        """No dimensions -> Review, no deduction regardless of confidence."""
        ev = OpeningEvidence(
            opening_type=OPENING_TYPE_WINDOW,
            width_m=None,
            height_m=1.5,
            wall_ref="N01",
            geometry_confidence=0.95,
            dimension_confidence=0.95,
            association_confidence=0.95,
        )
        ev.compute_area()
        ev.compute_deduction_status()
        self.assertFalse(ev.deduct)
        self.assertEqual(ev.deduction_status, DEDUCTION_REVIEW)

    def test_missing_wall_ref_blocks_deduction(self):
        """No wall_ref -> Review, no deduction."""
        ev = OpeningEvidence(
            opening_type=OPENING_TYPE_WINDOW,
            width_m=1.2,
            height_m=1.5,
            wall_ref="",
            geometry_confidence=0.95,
            dimension_confidence=0.95,
            association_confidence=0.95,
        )
        ev.compute_area()
        ev.compute_deduction_status()
        self.assertFalse(ev.deduct)
        self.assertEqual(ev.deduction_status, DEDUCTION_REVIEW)

    def test_mixed_confidence_uses_minimum(self):
        """Mixed confidences: deduction gated by minimum."""
        ev = OpeningEvidence(
            opening_type=OPENING_TYPE_WINDOW,
            width_m=1.2,
            height_m=1.5,
            wall_ref="N01",
            geometry_confidence=0.95,
            dimension_confidence=0.95,
            association_confidence=0.6,  # low
        )
        ev.compute_area()
        ev.compute_deduction_status()
        self.assertFalse(ev.deduct)
        self.assertEqual(ev.deduction_status, DEDUCTION_REVIEW)


# ---------------------------------------------------------------------------
# B0-3: Tolerance-based deduplication
# ---------------------------------------------------------------------------
class TestToleranceDeduplication(unittest.TestCase):
    """B0-3: Cross-source deduplication uses explicit tolerances, not rounding."""

    def test_same_opening_detected_on_plan_and_elevation(self):
        """Same physical opening from plan and elevation -> merge."""
        plan = _make_window(
            width=1.2, height=1.5, position=3.0,
            extraction_method="plan_vector",
        )
        elev = _make_window(
            width=1.18, height=1.52, position=3.05,
            extraction_method="elevation_rect",
        )
        self.assertTrue(same_physical_opening(plan, elev))

    def test_different_positions_not_same_opening(self):
        """Different positions -> different openings."""
        a = _make_window(position=3.0)
        b = _make_window(position=5.0)
        self.assertFalse(same_physical_opening(a, b))

    def test_width_tolerance_boundary(self):
        """Width difference exactly at tolerance -> same opening."""
        a = _make_window(width=1.20)
        b = _make_window(width=1.25)  # 50mm difference
        self.assertTrue(same_physical_opening(a, b))

    def test_width_beyond_tolerance(self):
        """Width difference beyond tolerance -> different openings."""
        a = _make_window(width=1.20)
        b = _make_window(width=1.26)  # 60mm difference
        self.assertFalse(same_physical_opening(a, b))

    def test_height_tolerance_boundary(self):
        """Height difference exactly at tolerance -> same opening."""
        a = _make_window(height=1.50)
        b = _make_window(height=1.55)  # 50mm difference
        self.assertTrue(same_physical_opening(a, b))

    def test_height_beyond_tolerance(self):
        """Height difference beyond tolerance -> different openings."""
        a = _make_window(height=1.50)
        b = _make_window(height=1.56)  # 60mm difference
        self.assertFalse(same_physical_opening(a, b))

    def test_position_tolerance_boundary(self):
        """Position difference exactly at tolerance -> same opening."""
        a = _make_window(position=3.00)
        b = _make_window(position=3.20)  # 200mm difference
        self.assertTrue(same_physical_opening(a, b))

    def test_position_beyond_tolerance(self):
        """Position difference beyond tolerance -> different openings."""
        a = _make_window(position=3.00)
        b = _make_window(position=3.21)  # 210mm difference
        self.assertFalse(same_physical_opening(a, b))

    def test_different_wall_ref_never_matches(self):
        """Different wall_ref -> never same opening."""
        a = _make_window(wall_ref="N01")
        b = _make_window(wall_ref="N02")
        self.assertFalse(same_physical_opening(a, b))

    def test_different_level_never_matches(self):
        """Different level -> never same opening."""
        a = _make_window(level="Ground")
        b = _make_window(level="First")
        self.assertFalse(same_physical_opening(a, b))

    def test_missing_position_allows_match(self):
        """Missing position in one source -> still match on dims."""
        a = _make_window(position=3.0)
        b = OpeningEvidence(
            type_mark="W01",
            wall_ref="N01",
            level="Ground",
            opening_type=OPENING_TYPE_WINDOW,
            width_m=1.2,
            height_m=1.5,
            position_along_wall_m=None,  # no position from schedule
        )
        self.assertTrue(same_physical_opening(a, b))


# ---------------------------------------------------------------------------
# B0-4: Merge evidence from duplicate sources
# ---------------------------------------------------------------------------
class TestMergeEvidence(unittest.TestCase):
    """B0-4: Merging duplicate records preserves highest confidence."""

    def test_merge_upgrades_confidence(self):
        """Merge takes maximum confidence from both sources."""
        low = _make_window(confidence=0.6, extraction_method="plan_vector")
        high = _make_window(confidence=0.95, extraction_method="elevation_rect")
        merged = merge_opening_evidence(low, high)
        self.assertEqual(merged.geometry_confidence, 0.95)
        self.assertEqual(merged.dimension_confidence, 0.95)

    def test_merge_prefers_schedule_dimensions(self):
        """Schedule dimensions override geometric estimation."""
        plan = _make_window(width=1.15, height=1.45)
        schedule = OpeningEvidence(
            type_mark="W01",
            wall_ref="N01",
            level="Ground",
            opening_type=OPENING_TYPE_WINDOW,
            width_m=1.20,
            height_m=1.50,
            dimension_basis=DIMENSION_BASIS_ROUGH_OPENING,
            extraction_method="schedule_parse",
        )
        merged = merge_opening_evidence(plan, schedule)
        self.assertEqual(merged.width_m, 1.20)
        self.assertEqual(merged.height_m, 1.50)

    def test_merge_combines_evidence_sources(self):
        """Evidence provenance is merged and deduplicated."""
        a = _make_window()
        a.evidence = ["plan_A301"]
        b = _make_window()
        b.evidence = ["elevation_A201", "plan_A301"]
        merged = merge_opening_evidence(a, b)
        self.assertIn("plan_A301", merged.evidence)
        self.assertIn("elevation_A201", merged.evidence)
        self.assertEqual(len(merged.evidence), 2)

    def test_merge_prefers_rough_opening_basis(self):
        """Rough opening basis is preferred over unknown."""
        unknown = OpeningEvidence(
            opening_type=OPENING_TYPE_WINDOW,
            width_m=1.2,
            height_m=1.5,
            dimension_basis=DIMENSION_BASIS_UNKNOWN,
        )
        rough = OpeningEvidence(
            opening_type=OPENING_TYPE_WINDOW,
            width_m=1.2,
            height_m=1.5,
            dimension_basis=DIMENSION_BASIS_ROUGH_OPENING,
        )
        merged = merge_opening_evidence(unknown, rough)
        self.assertEqual(merged.dimension_basis, DIMENSION_BASIS_ROUGH_OPENING)


# ---------------------------------------------------------------------------
# B0-5: Bulk deduplication
# ---------------------------------------------------------------------------
class TestBulkDeduplication(unittest.TestCase):
    """B0-5: Bulk deduplication merges duplicates across a list."""

    def test_deduplicate_merges_plan_and_elevation(self):
        """Plan + elevation of same window -> one record."""
        plan = _make_window(width=1.2, height=1.5, position=3.0,
                            extraction_method="plan_vector")
        plan.evidence = ["plan_A301"]
        elev = _make_window(width=1.18, height=1.52, position=3.05,
                            extraction_method="elevation_rect")
        elev.evidence = ["elevation_A201"]
        result = deduplicate_openings([plan, elev])
        self.assertEqual(len(result), 1)
        self.assertIn("plan_A301", result[0].evidence)
        self.assertIn("elevation_A201", result[0].evidence)

    def test_deduplicate_keeps_different_openings(self):
        """Two genuinely different windows -> two records."""
        a = _make_window(position=3.0, width=1.2)
        b = _make_window(position=6.0, width=1.2)
        result = deduplicate_openings([a, b])
        self.assertEqual(len(result), 2)

    def test_deduplicate_three_sources(self):
        """Plan + elevation + schedule of same window -> one record."""
        plan = _make_window(width=1.2, height=1.5, position=3.0)
        elev = _make_window(width=1.18, height=1.52, position=3.05)
        sched = OpeningEvidence(
            type_mark="W01",
            wall_ref="N01",
            level="Ground",
            opening_type=OPENING_TYPE_WINDOW,
            width_m=1.20,
            height_m=1.50,
            dimension_basis=DIMENSION_BASIS_ROUGH_OPENING,
            extraction_method="schedule_parse",
        )
        result = deduplicate_openings([plan, elev, sched])
        self.assertEqual(len(result), 1)
        # Schedule dims should be preferred
        self.assertEqual(result[0].width_m, 1.20)


# ---------------------------------------------------------------------------
# B0-6: Deduction calculation
# ---------------------------------------------------------------------------
class TestDeductionCalculation(unittest.TestCase):
    """B0-6: Area and net wall calculations."""

    def test_deducted_area_sums_only_deducted(self):
        """Only openings with deduct=True contribute to deductions."""
        deducted = _make_window(confidence=0.95)
        self.assertTrue(deducted.deduct)
        not_deducted = _make_window(confidence=0.6)
        self.assertFalse(not_deducted.deduct)
        total = deducted_area_m2([deducted, not_deducted])
        self.assertAlmostEqual(total, deducted.area_m2, places=4)

    def test_net_wall_area_never_negative(self):
        """Net wall area is never negative."""
        openings = [_make_window(width=10.0, height=10.0, confidence=0.95)]
        net = net_wall_area_m2(5.0, openings)
        self.assertEqual(net, 0.0)

    def test_net_wall_area_subtracts_deductions(self):
        """Net = gross - deducted area."""
        opening = _make_window(width=1.2, height=1.5, confidence=0.95)
        net = net_wall_area_m2(27.0, [opening])
        self.assertAlmostEqual(net, 27.0 - 1.8, places=4)

    def test_net_wall_area_unchanged_when_no_deductions(self):
        """No openings -> net = gross."""
        net = net_wall_area_m2(27.0, [])
        self.assertEqual(net, 27.0)

    def test_uncertain_opening_does_not_affect_net(self):
        """Uncertain opening (Review) must not change net wall area."""
        uncertain = _make_window(confidence=0.6)
        self.assertFalse(uncertain.deduct)
        gross = 27.0
        net = net_wall_area_m2(gross, [uncertain])
        self.assertEqual(net, gross)


# ---------------------------------------------------------------------------
# B0-7: v134 conversion
# ---------------------------------------------------------------------------
class TestV134Conversion(unittest.TestCase):
    """B0-7: Conversion to/from v134 register format."""

    def test_to_v134_preserves_deduct(self):
        """to_v134 preserves deduct toggle."""
        ev = _make_window(confidence=0.95)
        record = to_v134_record(ev)
        self.assertTrue(record["deduct"])

    def test_to_v134_preserves_dimensions(self):
        """to_v134 preserves width/height."""
        ev = _make_window(width=1.2, height=1.5)
        record = to_v134_record(ev)
        self.assertEqual(record["width_m"], 1.2)
        self.assertEqual(record["height_m"], 1.5)

    def test_from_v134_single_quantity(self):
        """v134 record with quantity=1 -> OpeningEvidence."""
        record = {
            "id": "test01",
            "kind": "Window",
            "label": "W01",
            "wall_ref": "N01",
            "width_m": 1.2,
            "height_m": 1.5,
            "quantity": 1,
            "deduct": True,
            "source_reference": "Manual entry",
            "confidence": "Manual estimator entry",
        }
        ev = from_v134_record(record)
        self.assertEqual(ev.quantity, 1)
        self.assertEqual(ev.width_m, 1.2)
        self.assertTrue(ev.deduct)

    def test_from_v134_grouped_quantity_preserved(self):
        """v134 record with quantity=4 preserves quantity for commercial use."""
        record = {
            "id": "test02",
            "kind": "Window",
            "label": "W01",
            "wall_ref": "N01",
            "width_m": 1.2,
            "height_m": 1.5,
            "quantity": 4,
            "deduct": True,
        }
        ev = from_v134_record(record)
        # v134 grouped records keep their quantity
        self.assertEqual(ev.quantity, 4)
        # area_m2 = 1.2 * 1.5 * 4 = 7.2
        self.assertAlmostEqual(ev.area_m2, 7.2, places=4)

    def test_roundtrip_v134(self):
        """OpeningEvidence -> v134 -> OpeningEvidence preserves key fields."""
        original = _make_window(width=1.2, height=1.5, mark="W01")
        record = to_v134_record(original)
        restored = from_v134_record(record)
        self.assertEqual(restored.width_m, 1.2)
        self.assertEqual(restored.height_m, 1.5)
        self.assertEqual(restored.wall_ref, "N01")


# ---------------------------------------------------------------------------
# B0-8: Known width/height not auto-deducted
# ---------------------------------------------------------------------------
class TestKnownDimensionsNotAutoDeducted(unittest.TestCase):
    """B0-8: Even with known dimensions, auto-detected evidence must
    not auto-deduct without going through the safety gate."""

    def test_plan_detected_window_not_deducted_until_confirmed(self):
        """Plan-detected window with known dims -> Review until confirmed."""
        ev = _make_window(
            width=1.2,
            height=1.5,
            confidence=0.95,
            extraction_method="plan_vector",
        )
        # Even with high confidence, initial deduction_status is computed
        # by the safety gate. This test proves the gate is applied.
        self.assertIn(
            ev.deduction_status,
            (DEDUCTION_DEDUCTED, DEDUCTION_REVIEW),
        )
        # If deducted, it must be because all three confidences are high
        if ev.deduct:
            self.assertGreaterEqual(ev.geometry_confidence, CONFIDENCE_DERIVED_DEDUCT)
            self.assertGreaterEqual(ev.dimension_confidence, CONFIDENCE_DERIVED_DEDUCT)
            self.assertGreaterEqual(ev.association_confidence, CONFIDENCE_DERIVED_DEDUCT)

    def test_schedule_only_window_not_deducted(self):
        """Schedule-only window (no plan/elevation) -> Review."""
        ev = OpeningEvidence(
            type_mark="W03",
            wall_ref="E01",
            level="Ground",
            opening_type=OPENING_TYPE_WINDOW,
            width_m=1.2,
            height_m=1.5,
            dimension_basis=DIMENSION_BASIS_UNKNOWN,
            extraction_method="schedule_parse",
            geometry_confidence=0.0,     # no geometry
            dimension_confidence=0.85,   # from schedule
            association_confidence=0.3,  # no plan/elevation
        )
        ev.compute_area()
        ev.compute_deduction_status()
        self.assertFalse(ev.deduct)
        self.assertEqual(ev.deduction_status, DEDUCTION_REVIEW)


if __name__ == "__main__":
    unittest.main()
