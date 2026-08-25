"""
Tests for pb_opening_deduction_v174 — B5: Deduction integration.

Covers:
  - Eligibility gate (all criteria)
  - Safety dedup (cross-detector duplicates)
  - Deduct assignment (end-to-end gate → deduct)
  - Area computation
  - Safety rules (B5 never creates instances, never overrides review)
"""
import copy
import unittest

from pb_opening_evidence_v170 import (
    DEDUCTION_AUTO_ELIGIBLE,
    DEDUCTION_DERIVED_ELIGIBLE,
    DEDUCTION_DEDUCTED,
    DEDUCTION_NOT_DEDUCTED,
    DEDUCTION_REVIEW,
    DIMENSION_BASIS_ROUGH_OPENING,
    DIMENSION_BASIS_UNKNOWN,
    DIMENSION_BASIS_FRAME,
    CONFIDENCE_AUTO_DEDUCT,
    CONFIDENCE_DERIVED_DEDUCT,
    OPENING_TYPE_DOOR,
    OPENING_TYPE_WINDOW,
    TOLERANCE_POSITION_M,
    OpeningEvidence,
    record_plan_observation,
)
from pb_opening_deduction_v174 import (
    VERSION,
    passes_eligibility_gate,
    safety_deduplicate,
    apply_deductions,
    deducted_total_area_m2,
    net_wall_area_after_deductions,
)


def _make_instance(
    *,
    mark: str = "D01",
    wall: str = "W01",
    width: float = 0.82,
    height: float = 2.10,
    basis: str = DIMENSION_BASIS_ROUGH_OPENING,
    status: str = DEDUCTION_AUTO_ELIGIBLE,
    geom_conf: float = 0.95,
    dim_conf: float = 0.95,
    assoc_conf: float = 0.95,
    opening_type: str = OPENING_TYPE_DOOR,
    position: float = 1.5,
    method: str = "plan_vector",
) -> OpeningEvidence:
    """Create a fully-eligible OpeningEvidence for testing."""
    inst = OpeningEvidence(
        type_mark=mark,
        wall_ref=wall,
        opening_type=opening_type,
        width_m=width,
        height_m=height,
        dimension_basis=basis,
        dimension_source="schedule_parse",
        position_along_wall_m=position,
        extraction_method=method,
        geometry_confidence=geom_conf,
        dimension_confidence=dim_conf,
        association_confidence=assoc_conf,
        deduction_status=status,
    )
    record_plan_observation(inst)
    inst.compute_area()
    return inst


# ============================================================================
# Eligibility gate tests
# ============================================================================
class TestEligibilityGate(unittest.TestCase):
    """All eligibility criteria must pass for deduct=True."""

    def test_full_eligible_auto(self):
        """All criteria met + auto_eligible → passes."""
        inst = _make_instance(status=DEDUCTION_AUTO_ELIGIBLE)
        self.assertTrue(passes_eligibility_gate(inst))

    def test_full_eligible_derived(self):
        """All criteria met + derived_eligible → passes."""
        inst = _make_instance(status=DEDUCTION_DERIVED_ELIGIBLE)
        self.assertTrue(passes_eligibility_gate(inst))

    def test_rejects_review_status(self):
        """deduction_status=review → fails."""
        inst = _make_instance(status=DEDUCTION_REVIEW)
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_none_status(self):
        """deduction_status=none → fails."""
        inst = _make_instance(status="none")
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_not_deducted_status(self):
        """deduction_status=not_deducted → fails."""
        inst = _make_instance(status=DEDUCTION_NOT_DEDUCTED)
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_no_width(self):
        """width_m=None → fails."""
        inst = _make_instance()
        inst.width_m = None
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_no_height(self):
        """height_m=None → fails."""
        inst = _make_instance()
        inst.height_m = None
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_zero_width(self):
        """width_m=0 → fails."""
        inst = _make_instance(width=0.0)
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_negative_height(self):
        """height_m=-1 → fails."""
        inst = _make_instance(height=-1.0)
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_wrong_basis(self):
        """dimension_basis=frame → fails (only rough_opening eligible)."""
        inst = _make_instance(basis=DIMENSION_BASIS_FRAME)
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_unknown_basis(self):
        """dimension_basis=unknown → fails."""
        inst = _make_instance(basis=DIMENSION_BASIS_UNKNOWN)
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_low_confidence(self):
        """min(geometry,dimension,association) < 0.70 → fails."""
        inst = _make_instance(geom_conf=0.69, dim_conf=0.95, assoc_conf=0.95)
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_low_dim_confidence(self):
        """dimension_confidence=0.69 → fails."""
        inst = _make_instance(geom_conf=0.95, dim_conf=0.69, assoc_conf=0.95)
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_low_assoc_confidence(self):
        """association_confidence=0.69 → fails."""
        inst = _make_instance(geom_conf=0.95, dim_conf=0.95, assoc_conf=0.69)
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_no_wall_ref(self):
        """wall_ref="" → fails."""
        inst = _make_instance(wall="")
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_no_area(self):
        """area_m2=None → fails."""
        inst = _make_instance()
        inst.area_m2 = None
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_zero_area(self):
        """area_m2=0 → fails."""
        inst = _make_instance()
        inst.area_m2 = 0.0
        self.assertFalse(passes_eligibility_gate(inst))

    def test_boundary_confidence_070(self):
        """Confidence exactly 0.70 → passes (>= threshold)."""
        inst = _make_instance(
            geom_conf=0.70, dim_conf=0.70, assoc_conf=0.70
        )
        self.assertTrue(passes_eligibility_gate(inst))

    def test_boundary_confidence_069(self):
        """Confidence 0.69 → fails (< 0.70 threshold)."""
        inst = _make_instance(
            geom_conf=0.69, dim_conf=0.69, assoc_conf=0.69
        )
        self.assertFalse(passes_eligibility_gate(inst))

    def test_boundary_width_001(self):
        """Width 0.001 → passes (> 0)."""
        inst = _make_instance(width=0.001)
        self.assertTrue(passes_eligibility_gate(inst))

    def test_rejects_multiple_failures(self):
        """Multiple failures → still returns False (not a crash)."""
        inst = _make_instance(
            status=DEDUCTION_REVIEW,
            width=None,
            height=None,
            wall="",
        )
        self.assertFalse(passes_eligibility_gate(inst))


# ============================================================================
# Safety dedup tests
# ============================================================================
class TestSafetyDeduplicate(unittest.TestCase):
    """Cross-detector duplicate removal before deduction assignment."""

    def test_no_duplicates_unchanged(self):
        """Two distinct instances → no removal."""
        a = _make_instance(mark="D01", position=1.0)
        b = _make_instance(mark="D02", position=3.0)
        result = safety_deduplicate([a, b])
        self.assertEqual(len(result), 2)

    def test_identical_instances_merged(self):
        """Two instances at same position on same wall → merged."""
        a = _make_instance(mark="D01", position=1.5, width=0.82, geom_conf=0.6)
        b = _make_instance(mark="D01", position=1.5, width=0.82, geom_conf=0.9)
        result = safety_deduplicate([a, b])
        self.assertEqual(len(result), 1)
        # Confidence upgraded to max
        self.assertAlmostEqual(result[0].geometry_confidence, 0.9, places=2)

    def test_different_positions_not_merged(self):
        """Different positions → not merged."""
        a = _make_instance(mark="D01", position=1.0)
        b = _make_instance(mark="D01", position=5.0)
        result = safety_deduplicate([a, b])
        self.assertEqual(len(result), 2)

    def test_different_walls_not_merged(self):
        """Different wall_ref → not merged."""
        a = _make_instance(wall="W01", position=1.5)
        b = _make_instance(wall="W02", position=1.5)
        result = safety_deduplicate([a, b])
        self.assertEqual(len(result), 2)

    def test_door_window_same_position_not_merged(self):
        """Door + window at same position → different types, not merged."""
        a = _make_instance(
            mark="D01", opening_type=OPENING_TYPE_DOOR, position=1.5
        )
        b = _make_instance(
            mark="W01", opening_type=OPENING_TYPE_WINDOW, position=1.5
        )
        result = safety_deduplicate([a, b])
        self.assertEqual(len(result), 2)

    def test_empty_list(self):
        """Empty list → empty result."""
        result = safety_deduplicate([])
        self.assertEqual(len(result), 0)

    def test_single_instance(self):
        """Single instance → unchanged."""
        a = _make_instance()
        result = safety_deduplicate([a])
        self.assertEqual(len(result), 1)

    def test_preserves_count(self):
        """Three distinct instances → three in result."""
        instances = [
            _make_instance(mark="D01", position=1.0),
            _make_instance(mark="D02", position=3.0),
            _make_instance(mark="D03", position=5.0),
        ]
        result = safety_deduplicate(instances)
        self.assertEqual(len(result), 3)


# ============================================================================
# Deduction assignment tests
# ============================================================================
class TestApplyDeductions(unittest.TestCase):
    """End-to-end gate → deduct assignment."""

    def test_eligible_instance_deducted(self):
        """auto_eligible + all criteria met → deduct=True."""
        inst = _make_instance(status=DEDUCTION_AUTO_ELIGIBLE)
        result = apply_deductions([inst])
        self.assertTrue(result[0].deduct)
        self.assertEqual(result[0].deduction_status, DEDUCTION_DEDUCTED)

    def test_derived_eligible_deducted(self):
        """derived_eligible + all criteria met → deduct=True."""
        inst = _make_instance(status=DEDUCTION_DERIVED_ELIGIBLE)
        result = apply_deductions([inst])
        self.assertTrue(result[0].deduct)
        self.assertEqual(result[0].deduction_status, DEDUCTION_DEDUCTED)

    def test_review_not_deducted(self):
        """review status → deduct=False, status preserved as review."""
        inst = _make_instance(status=DEDUCTION_REVIEW)
        result = apply_deductions([inst])
        self.assertFalse(result[0].deduct)
        self.assertEqual(result[0].deduction_status, DEDUCTION_REVIEW)

    def test_none_not_deducted(self):
        """none status → deduct=False, status preserved as none."""
        inst = _make_instance(status="none")
        result = apply_deductions([inst])
        self.assertFalse(result[0].deduct)
        self.assertEqual(result[0].deduction_status, "none")

    def test_missing_width_not_deducted(self):
        """width_m=None → deduct=False."""
        inst = _make_instance(status=DEDUCTION_AUTO_ELIGIBLE)
        inst.width_m = None
        result = apply_deductions([inst])
        self.assertFalse(result[0].deduct)

    def test_missing_height_not_deducted(self):
        """height_m=None → deduct=False."""
        inst = _make_instance(status=DEDUCTION_AUTO_ELIGIBLE)
        inst.height_m = None
        result = apply_deductions([inst])
        self.assertFalse(result[0].deduct)

    def test_wrong_basis_not_deducted(self):
        """dimension_basis=frame → deduct=False."""
        inst = _make_instance(
            status=DEDUCTION_AUTO_ELIGIBLE, basis=DIMENSION_BASIS_FRAME
        )
        result = apply_deductions([inst])
        self.assertFalse(result[0].deduct)

    def test_low_confidence_not_deducted(self):
        """confidence 0.69 → deduct=False."""
        inst = _make_instance(
            status=DEDUCTION_AUTO_ELIGIBLE,
            geom_conf=0.69,
            dim_conf=0.69,
            assoc_conf=0.69,
        )
        result = apply_deductions([inst])
        self.assertFalse(result[0].deduct)

    def test_no_wall_ref_not_deducted(self):
        """wall_ref="" → deduct=False."""
        inst = _make_instance(status=DEDUCTION_AUTO_ELIGIBLE, wall="")
        result = apply_deductions([inst])
        self.assertFalse(result[0].deduct)

    def test_mixed_eligible_and_ineligible(self):
        """Mix of eligible and ineligible → correct split."""
        eligible = _make_instance(
            mark="D01", status=DEDUCTION_AUTO_ELIGIBLE, position=1.0
        )
        ineligible = _make_instance(
            mark="D02", status=DEDUCTION_REVIEW, position=3.0
        )
        result = apply_deductions([eligible, ineligible])
        self.assertTrue(result[0].deduct)
        self.assertFalse(result[1].deduct)
        self.assertEqual(result[0].deduction_status, DEDUCTION_DEDUCTED)
        self.assertEqual(result[1].deduction_status, DEDUCTION_REVIEW)

    def test_auto_eligible_boundary_confidence(self):
        """Confidence exactly 0.90 → auto_eligible → deduct."""
        inst = _make_instance(
            status=DEDUCTION_AUTO_ELIGIBLE,
            geom_conf=0.90,
            dim_conf=0.90,
            assoc_conf=0.90,
        )
        result = apply_deductions([inst])
        self.assertTrue(result[0].deduct)
        self.assertEqual(result[0].deduction_status, DEDUCTION_DEDUCTED)

    def test_derived_eligible_boundary_confidence(self):
        """Confidence exactly 0.70 → derived_eligible → deduct."""
        inst = _make_instance(
            status=DEDUCTION_DERIVED_ELIGIBLE,
            geom_conf=0.70,
            dim_conf=0.70,
            assoc_conf=0.70,
        )
        result = apply_deductions([inst])
        self.assertTrue(result[0].deduct)


# ============================================================================
# Area computation tests
# ============================================================================
class TestAreaComputation(unittest.TestCase):
    """deducted_total_area_m2 and net_wall_area_after_deductions."""

    def test_deducted_area_sum(self):
        """Two deducted instances → sum of areas."""
        a = _make_instance(
            mark="D01", width=0.82, height=2.10, position=1.0
        )
        b = _make_instance(
            mark="D02", width=0.90, height=2.10, position=3.0
        )
        apply_deductions([a, b])
        area = deducted_total_area_m2([a, b])
        expected = round(0.82 * 2.10 + 0.90 * 2.10, 4)
        self.assertAlmostEqual(area, expected, places=4)

    def test_non_deducted_excluded(self):
        """Non-deducted instance excluded from area sum."""
        deducted = _make_instance(
            mark="D01", width=0.82, height=2.10, position=1.0
        )
        not_deducted = _make_instance(
            mark="D02",
            width=0.90,
            height=2.10,
            status=DEDUCTION_REVIEW,
            position=3.0,
        )
        apply_deductions([deducted, not_deducted])
        area = deducted_total_area_m2([deducted, not_deducted])
        expected = round(0.82 * 2.10, 4)
        self.assertAlmostEqual(area, expected, places=4)

    def test_net_wall_area(self):
        """Gross 20.0 - deducted 1.722 → net 18.278."""
        inst = _make_instance(width=0.82, height=2.10, position=1.0)
        apply_deductions([inst])
        net = net_wall_area_after_deductions(20.0, [inst])
        expected = round(20.0 - 0.82 * 2.10, 4)
        self.assertAlmostEqual(net, expected, places=4)

    def test_net_wall_area_never_negative(self):
        """Gross 1.0 - deducted 5.0 → net 0.0 (clamped)."""
        inst = _make_instance(width=2.0, height=2.50, position=1.0)
        apply_deductions([inst])
        net = net_wall_area_after_deductions(1.0, [inst])
        self.assertEqual(net, 0.0)

    def test_no_deductions_full_gross(self):
        """No deductions → net = gross."""
        inst = _make_instance(status=DEDUCTION_REVIEW, position=1.0)
        apply_deductions([inst])
        net = net_wall_area_after_deductions(20.0, [inst])
        self.assertAlmostEqual(net, 20.0, places=4)

    def test_empty_instances_zero_area(self):
        """Empty instance list → 0 deducted, net = gross."""
        net = net_wall_area_after_deductions(20.0, [])
        self.assertEqual(net, 20.0)
        self.assertEqual(deducted_total_area_m2([]), 0.0)


# ============================================================================
# Safety rule tests
# ============================================================================
class TestSafetyRules(unittest.TestCase):
    """B5 safety invariants."""

    def test_b5_never_creates_instances(self):
        """apply_deductions never adds or removes instances."""
        instances = [
            _make_instance(mark="D01", position=1.0),
            _make_instance(mark="D02", position=3.0),
        ]
        original_ids = {i.opening_instance_id for i in instances}
        result = apply_deductions(instances)
        result_ids = {i.opening_instance_id for i in result}
        self.assertEqual(original_ids, result_ids)
        self.assertEqual(len(result), 2)

    def test_b5_never_overrides_b4_review(self):
        """B5 respects B4-forced review status."""
        inst = _make_instance(status=DEDUCTION_REVIEW)
        # Simulate B4 forcing review
        inst.deduction_status = DEDUCTION_REVIEW
        inst.notes = "B4: conflict detected"
        apply_deductions([inst])
        self.assertFalse(inst.deduct)
        # Review status preserved
        self.assertEqual(inst.deduction_status, DEDUCTION_REVIEW)

    def test_b5_only_sets_deduct_true(self):
        """B5 never sets deduct=True on instances that fail the gate."""
        inst = _make_instance(status=DEDUCTION_AUTO_ELIGIBLE)
        inst.width_m = None  # Fail gate
        apply_deductions([inst])
        self.assertFalse(inst.deduct)

    def test_deduction_status_values(self):
        """B5 uses only defined deduction status constants."""
        # eligible → deducted
        a = _make_instance(status=DEDUCTION_AUTO_ELIGIBLE, position=1.0)
        apply_deductions([a])
        self.assertEqual(a.deduction_status, DEDUCTION_DEDUCTED)

        # review → review (preserved)
        b = _make_instance(status=DEDUCTION_REVIEW, position=3.0)
        apply_deductions([b])
        self.assertEqual(b.deduction_status, DEDUCTION_REVIEW)

    def test_b5_is_idempotent_on_review(self):
        """Running apply_deductions twice on review instance → still not deducted."""
        inst = _make_instance(status=DEDUCTION_REVIEW)
        apply_deductions([inst])
        self.assertFalse(inst.deduct)
        self.assertEqual(inst.deduction_status, DEDUCTION_REVIEW)
        # Run again — still not deducted, review preserved
        apply_deductions([inst])
        self.assertFalse(inst.deduct)
        self.assertEqual(inst.deduction_status, DEDUCTION_REVIEW)


# ============================================================================
# Version check
# ============================================================================
class TestVersion(unittest.TestCase):
    def test_version(self):
        self.assertEqual(VERSION, "1.7.4")


if __name__ == "__main__":
    unittest.main()
