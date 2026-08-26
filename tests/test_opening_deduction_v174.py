"""
Tests for pb_opening_deduction_v174 — B5: Deduction integration.

Covers:
  - Eligibility gate (all criteria including reconciliation_complete)
  - Safety dedup with mark conflict rejection (runs BEFORE B4)
  - Deduct assignment (idempotent, preserves deduction_status)
  - Area computation (over-deduction detection)
  - Safety rules (B5 never creates instances, never overrides B4 review)
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
    CONFIDENCE_DERIVED_DEDUCT,
    OPENING_TYPE_DOOR,
    OPENING_TYPE_WINDOW,
    OpeningEvidence,
    record_plan_observation,
    merge_opening_evidence,
)
from pb_opening_deduction_v174 import (
    VERSION,
    passes_eligibility_gate,
    resolve_physical_duplicates,
    apply_deductions,
    deducted_total_area_m2,
    net_wall_area_after_deductions,
    _marks_conflict,
    same_location,
    _types_conflict,
    _same_plan_geometry,
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
    reconciled: bool = True,
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
        reconciliation_complete=reconciled,
    )
    record_plan_observation(inst)
    inst.compute_area()
    return inst


# ============================================================================
# Mark conflict check
# ============================================================================
class TestMarksConflict(unittest.TestCase):
    """D01 + D02 at same location should not silently merge."""

    def test_same_marks_no_conflict(self):
        a = _make_instance(mark="D01")
        b = _make_instance(mark="D01")
        self.assertFalse(_marks_conflict(a, b))

    def test_different_marks_conflict(self):
        a = _make_instance(mark="D01")
        b = _make_instance(mark="D02")
        self.assertTrue(_marks_conflict(a, b))

    def test_one_blank_no_conflict(self):
        a = _make_instance(mark="")
        b = _make_instance(mark="D01")
        self.assertFalse(_marks_conflict(a, b))

    def test_both_blank_no_conflict(self):
        a = _make_instance(mark="")
        b = _make_instance(mark="")
        self.assertFalse(_marks_conflict(a, b))

    def test_case_insensitive(self):
        a = _make_instance(mark="d01")
        b = _make_instance(mark="D01")
        self.assertFalse(_marks_conflict(a, b))


# ============================================================================
# Location and type conflict checks
# ============================================================================
class TestSameLocation(unittest.TestCase):
    """same_location detects physical proximity without type compatibility."""

    def test_same_wall_same_position(self):
        a = _make_instance(wall="W01", position=1.5)
        b = _make_instance(wall="W01", position=1.5)
        self.assertTrue(same_location(a, b))

    def test_different_wall(self):
        a = _make_instance(wall="W01", position=1.5)
        b = _make_instance(wall="W02", position=1.5)
        self.assertFalse(same_location(a, b))

    def test_far_position(self):
        a = _make_instance(wall="W01", position=1.0)
        b = _make_instance(wall="W01", position=5.0)
        self.assertFalse(same_location(a, b))

    def test_no_position(self):
        a = _make_instance(wall="W01", position=1.5)
        b = _make_instance(wall="W01", position=None)
        self.assertFalse(same_location(a, b))


class TestTypesConflict(unittest.TestCase):
    """_types_conflict detects incompatible opening types."""

    def test_door_window_conflicts(self):
        a = _make_instance(opening_type=OPENING_TYPE_DOOR)
        b = _make_instance(opening_type=OPENING_TYPE_WINDOW)
        self.assertTrue(_types_conflict(a, b))

    def test_same_type_no_conflict(self):
        a = _make_instance(opening_type=OPENING_TYPE_DOOR)
        b = _make_instance(opening_type=OPENING_TYPE_DOOR)
        self.assertFalse(_types_conflict(a, b))

    def test_opening_compatible_with_any(self):
        a = _make_instance(opening_type="opening")
        b = _make_instance(opening_type=OPENING_TYPE_DOOR)
        self.assertFalse(_types_conflict(a, b))


# ============================================================================
# Cross-wall duplicate detection
# ============================================================================
class TestCrossWallDuplicate(unittest.TestCase):
    """Same plan-space geometry on different wall refs → conflict."""

    def test_same_geometry_different_walls_conflict(self):
        """Same signature + different wall_ref → physical_instance_conflict."""
        a = _make_instance(
            mark="D01", wall="W01", position=1.5, width=0.82,
        )
        a.plan_geometry_signature = (1, 100.0, 200.0, 0.82, "door")
        b = _make_instance(
            mark="D01", wall="W02", position=1.5, width=0.82,
        )
        b.plan_geometry_signature = (1, 100.0, 200.0, 0.82, "door")
        result = resolve_physical_duplicates([a, b])
        self.assertEqual(len(result), 2)
        for inst in result:
            pic = [o for o in inst.source_observations
                   if o["source"] == "physical_instance_conflict"]
            self.assertEqual(len(pic), 1)

    def test_different_geometry_different_walls_no_conflict(self):
        """Different signature + different wall_ref → independent records."""
        a = _make_instance(
            mark="D01", wall="W01", position=1.0, width=0.82,
        )
        a.plan_geometry_signature = (1, 100.0, 200.0, 0.82, "door")
        b = _make_instance(
            mark="D02", wall="W02", position=5.0, width=0.90,
        )
        b.plan_geometry_signature = (1, 500.0, 600.0, 0.90, "door")
        result = resolve_physical_duplicates([a, b])
        self.assertEqual(len(result), 2)
        for inst in result:
            pic = [o for o in inst.source_observations
                   if o["source"] == "physical_instance_conflict"]
            self.assertEqual(len(pic), 0)

    def test_same_geometry_same_wall_merges(self):
        """Same signature + same wall_ref → normal merge."""
        a = _make_instance(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.6,
        )
        a.plan_geometry_signature = (1, 100.0, 200.0, 0.82, "door")
        b = _make_instance(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.9,
        )
        b.plan_geometry_signature = (1, 100.0, 200.0, 0.82, "door")
        result = resolve_physical_duplicates([a, b])
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0].geometry_confidence, 0.9, places=2)

    def test_cross_wall_conflict_forces_review_via_b4(self):
        """Same geometry, W01 + W02 → B4 forces review → neither deducted."""
        from pb_opening_reconciliation_v173 import reconcile_opening_evidence
        a = _make_instance(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, dim_conf=0.95, assoc_conf=0.95,
        )
        a.plan_geometry_signature = (1, 100.0, 200.0, 0.82, "door")
        b = _make_instance(
            mark="D01", wall="W02", position=1.5, width=0.82,
            geom_conf=0.95, dim_conf=0.95, assoc_conf=0.95,
        )
        b.plan_geometry_signature = (1, 100.0, 200.0, 0.82, "door")
        deduped = resolve_physical_duplicates([a, b])
        reconciled, conflicts = reconcile_opening_evidence(deduped)
        pic = [c for c in conflicts
               if c.conflict_type == "physical_instance_conflict"]
        self.assertGreaterEqual(len(pic), 2)
        for inst in reconciled:
            self.assertEqual(inst.deduction_status, DEDUCTION_REVIEW)
        apply_deductions(reconciled)
        for inst in reconciled:
            self.assertFalse(inst.deduct)

    def test_no_signature_no_cross_wall_check(self):
        """No signature → cross-wall check is skipped (can't detect)."""
        a = _make_instance(wall="W01", position=1.5, width=0.82)
        a.plan_geometry_signature = None
        b = _make_instance(wall="W02", position=1.5, width=0.82)
        b.plan_geometry_signature = None
        result = resolve_physical_duplicates([a, b])
        self.assertEqual(len(result), 2)
        # No conflict observation — can't detect without signature
        for inst in result:
            pic = [o for o in inst.source_observations
                   if o["source"] == "physical_instance_conflict"]
            self.assertEqual(len(pic), 0)


# ============================================================================
# Eligibility gate tests
# ============================================================================
class TestEligibilityGate(unittest.TestCase):
    """All eligibility criteria must pass for deduct=True."""

    def test_full_eligible_auto(self):
        inst = _make_instance(status=DEDUCTION_AUTO_ELIGIBLE)
        self.assertTrue(passes_eligibility_gate(inst))

    def test_full_eligible_derived(self):
        inst = _make_instance(status=DEDUCTION_DERIVED_ELIGIBLE)
        self.assertTrue(passes_eligibility_gate(inst))

    def test_rejects_not_reconciled(self):
        """reconciliation_complete=False → fails (B4 must run first)."""
        inst = _make_instance(reconciled=False)
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_review_status(self):
        inst = _make_instance(status=DEDUCTION_REVIEW)
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_none_status(self):
        inst = _make_instance(status="none")
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_no_width(self):
        inst = _make_instance()
        inst.width_m = None
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_no_height(self):
        inst = _make_instance()
        inst.height_m = None
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_zero_width(self):
        inst = _make_instance(width=0.0)
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_negative_height(self):
        inst = _make_instance(height=-1.0)
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_wrong_basis(self):
        inst = _make_instance(basis=DIMENSION_BASIS_FRAME)
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_unknown_basis(self):
        inst = _make_instance(basis=DIMENSION_BASIS_UNKNOWN)
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_low_confidence(self):
        inst = _make_instance(geom_conf=0.69, dim_conf=0.95, assoc_conf=0.95)
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_low_dim_confidence(self):
        inst = _make_instance(geom_conf=0.95, dim_conf=0.69, assoc_conf=0.95)
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_low_assoc_confidence(self):
        inst = _make_instance(geom_conf=0.95, dim_conf=0.95, assoc_conf=0.69)
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_no_wall_ref(self):
        inst = _make_instance(wall="")
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_no_area(self):
        inst = _make_instance()
        inst.area_m2 = None
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_zero_area(self):
        inst = _make_instance()
        inst.area_m2 = 0.0
        self.assertFalse(passes_eligibility_gate(inst))

    def test_boundary_confidence_070(self):
        inst = _make_instance(geom_conf=0.70, dim_conf=0.70, assoc_conf=0.70)
        self.assertTrue(passes_eligibility_gate(inst))

    def test_boundary_confidence_069(self):
        inst = _make_instance(geom_conf=0.69, dim_conf=0.69, assoc_conf=0.69)
        self.assertFalse(passes_eligibility_gate(inst))

    def test_rejects_multiple_failures(self):
        inst = _make_instance(
            status=DEDUCTION_REVIEW,
            width=None,
            height=None,
            wall="",
            reconciled=False,
        )
        self.assertFalse(passes_eligibility_gate(inst))


# ============================================================================
# Safety dedup tests (runs BEFORE B4)
# ============================================================================
class TestResolvePhysicalDuplicates(unittest.TestCase):
    """Cross-detector duplicate removal before enrichment/reconciliation."""

    def test_no_duplicates_unchanged(self):
        a = _make_instance(mark="D01", position=1.0)
        b = _make_instance(mark="D02", position=3.0)
        result = resolve_physical_duplicates([a, b])
        self.assertEqual(len(result), 2)

    def test_identical_instances_merged(self):
        a = _make_instance(mark="D01", position=1.5, width=0.82, geom_conf=0.6)
        b = _make_instance(mark="D01", position=1.5, width=0.82, geom_conf=0.9)
        result = resolve_physical_duplicates([a, b])
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0].geometry_confidence, 0.9, places=2)

    def test_different_positions_not_merged(self):
        a = _make_instance(mark="D01", position=1.0)
        b = _make_instance(mark="D01", position=5.0)
        result = resolve_physical_duplicates([a, b])
        self.assertEqual(len(result), 2)

    def test_different_walls_not_merged(self):
        a = _make_instance(wall="W01", position=1.5)
        b = _make_instance(wall="W02", position=1.5)
        result = resolve_physical_duplicates([a, b])
        self.assertEqual(len(result), 2)

    def test_door_window_same_position_not_merged(self):
        """Door + window at same position → different types, not merged."""
        a = _make_instance(
            mark="D01", opening_type=OPENING_TYPE_DOOR, position=1.5
        )
        b = _make_instance(
            mark="W01", opening_type=OPENING_TYPE_WINDOW, position=1.5
        )
        result = resolve_physical_duplicates([a, b])
        self.assertEqual(len(result), 2)

    def test_empty_list(self):
        result = resolve_physical_duplicates([])
        self.assertEqual(len(result), 0)

    def test_single_instance(self):
        a = _make_instance()
        result = resolve_physical_duplicates([a])
        self.assertEqual(len(result), 1)

    def test_mark_conflict_rejects_merge(self):
        """D01 + D02 at same position → NOT merged, both get conflict obs."""
        a = _make_instance(mark="D01", position=1.5, width=0.82)
        b = _make_instance(mark="D02", position=1.5, width=0.82)
        result = resolve_physical_duplicates([a, b])
        self.assertEqual(len(result), 2)
        # Both have the physical_instance_conflict observation
        for inst in result:
            conflict_obs = [
                o for o in inst.source_observations
                if o["source"] == "physical_instance_conflict"
            ]
            self.assertEqual(len(conflict_obs), 1)

    def test_blank_plus_mark_merges(self):
        """Blank mark + D01 → merges (blank inherits mark)."""
        a = _make_instance(mark="", position=1.5, width=0.82)
        b = _make_instance(mark="D01", position=1.5, width=0.82)
        result = resolve_physical_duplicates([a, b])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].type_mark, "D01")

    def test_preserves_count(self):
        instances = [
            _make_instance(mark="D01", position=1.0),
            _make_instance(mark="D02", position=3.0),
            _make_instance(mark="D03", position=5.0),
        ]
        result = resolve_physical_duplicates(instances)
        self.assertEqual(len(result), 3)


# ============================================================================
# Deduction assignment tests
# ============================================================================
class TestApplyDeductions(unittest.TestCase):
    """End-to-end gate → deduct assignment."""

    def test_eligible_instance_deducted(self):
        inst = _make_instance(status=DEDUCTION_AUTO_ELIGIBLE)
        result = apply_deductions([inst])
        self.assertTrue(result[0].deduct)
        self.assertEqual(result[0].deduction_decision, DEDUCTION_DEDUCTED)

    def test_derived_eligible_deducted(self):
        inst = _make_instance(status=DEDUCTION_DERIVED_ELIGIBLE)
        result = apply_deductions([inst])
        self.assertTrue(result[0].deduct)
        self.assertEqual(result[0].deduction_decision, DEDUCTION_DEDUCTED)

    def test_review_not_deducted(self):
        inst = _make_instance(status=DEDUCTION_REVIEW)
        result = apply_deductions([inst])
        self.assertFalse(result[0].deduct)
        self.assertEqual(result[0].deduction_decision, DEDUCTION_NOT_DEDUCTED)

    def test_none_not_deducted(self):
        inst = _make_instance(status="none")
        result = apply_deductions([inst])
        self.assertFalse(result[0].deduct)
        self.assertEqual(result[0].deduction_decision, DEDUCTION_NOT_DEDUCTED)

    def test_missing_width_not_deducted(self):
        inst = _make_instance(status=DEDUCTION_AUTO_ELIGIBLE)
        inst.width_m = None
        result = apply_deductions([inst])
        self.assertFalse(result[0].deduct)

    def test_missing_height_not_deducted(self):
        inst = _make_instance(status=DEDUCTION_AUTO_ELIGIBLE)
        inst.height_m = None
        result = apply_deductions([inst])
        self.assertFalse(result[0].deduct)

    def test_wrong_basis_not_deducted(self):
        inst = _make_instance(
            status=DEDUCTION_AUTO_ELIGIBLE, basis=DIMENSION_BASIS_FRAME
        )
        result = apply_deductions([inst])
        self.assertFalse(result[0].deduct)

    def test_low_confidence_not_deducted(self):
        inst = _make_instance(
            status=DEDUCTION_AUTO_ELIGIBLE,
            geom_conf=0.69, dim_conf=0.69, assoc_conf=0.69,
        )
        result = apply_deductions([inst])
        self.assertFalse(result[0].deduct)

    def test_no_wall_ref_not_deducted(self):
        inst = _make_instance(status=DEDUCTION_AUTO_ELIGIBLE, wall="")
        result = apply_deductions([inst])
        self.assertFalse(result[0].deduct)

    def test_not_reconciled_not_deducted(self):
        """reconciliation_complete=False → not deducted."""
        inst = _make_instance(status=DEDUCTION_AUTO_ELIGIBLE, reconciled=False)
        result = apply_deductions([inst])
        self.assertFalse(result[0].deduct)

    def test_mixed_eligible_and_ineligible(self):
        eligible = _make_instance(
            mark="D01", status=DEDUCTION_AUTO_ELIGIBLE, position=1.0
        )
        ineligible = _make_instance(
            mark="D02", status=DEDUCTION_REVIEW, position=3.0
        )
        result = apply_deductions([eligible, ineligible])
        self.assertTrue(result[0].deduct)
        self.assertFalse(result[1].deduct)

    def test_deduction_status_preserved(self):
        """deduction_status is NEVER modified by B5 — only deduct + decision."""
        inst = _make_instance(status=DEDUCTION_AUTO_ELIGIBLE)
        apply_deductions([inst])
        self.assertEqual(inst.deduction_status, DEDUCTION_AUTO_ELIGIBLE)
        self.assertTrue(inst.deduct)
        self.assertEqual(inst.deduction_decision, DEDUCTION_DEDUCTED)

    def test_review_status_preserved(self):
        """B5 respects B4-forced review status."""
        inst = _make_instance(status=DEDUCTION_REVIEW)
        inst.notes = "B4: conflict detected"
        apply_deductions([inst])
        self.assertFalse(inst.deduct)
        self.assertEqual(inst.deduction_status, DEDUCTION_REVIEW)

    def test_idempotent_deducted(self):
        """Running apply_deductions twice on eligible instance → stable."""
        inst = _make_instance(status=DEDUCTION_AUTO_ELIGIBLE)
        apply_deductions([inst])
        self.assertTrue(inst.deduct)
        self.assertEqual(inst.deduction_decision, DEDUCTION_DEDUCTED)
        # Run again — still deducted
        apply_deductions([inst])
        self.assertTrue(inst.deduct)
        self.assertEqual(inst.deduction_decision, DEDUCTION_DEDUCTED)
        self.assertEqual(inst.deduction_status, DEDUCTION_AUTO_ELIGIBLE)

    def test_idempotent_review(self):
        """Running apply_deductions twice on review instance → stable."""
        inst = _make_instance(status=DEDUCTION_REVIEW)
        apply_deductions([inst])
        self.assertFalse(inst.deduct)
        self.assertEqual(inst.deduction_status, DEDUCTION_REVIEW)
        apply_deductions([inst])
        self.assertFalse(inst.deduct)
        self.assertEqual(inst.deduction_status, DEDUCTION_REVIEW)

    def test_boundary_confidence_090(self):
        inst = _make_instance(
            status=DEDUCTION_AUTO_ELIGIBLE,
            geom_conf=0.90, dim_conf=0.90, assoc_conf=0.90,
        )
        result = apply_deductions([inst])
        self.assertTrue(result[0].deduct)

    def test_boundary_confidence_070(self):
        inst = _make_instance(
            status=DEDUCTION_DERIVED_ELIGIBLE,
            geom_conf=0.70, dim_conf=0.70, assoc_conf=0.70,
        )
        result = apply_deductions([inst])
        self.assertTrue(result[0].deduct)


# ============================================================================
# Area computation tests
# ============================================================================
class TestAreaComputation(unittest.TestCase):
    """deducted_total_area_m2 and net_wall_area_after_deductions."""

    def test_deducted_area_sum(self):
        a = _make_instance(mark="D01", width=0.82, height=2.10, position=1.0)
        b = _make_instance(mark="D02", width=0.90, height=2.10, position=3.0)
        apply_deductions([a, b])
        area = deducted_total_area_m2([a, b])
        expected = round(0.82 * 2.10 + 0.90 * 2.10, 4)
        self.assertAlmostEqual(area, expected, places=4)

    def test_non_deducted_excluded(self):
        deducted = _make_instance(
            mark="D01", width=0.82, height=2.10, position=1.0
        )
        not_deducted = _make_instance(
            mark="D02", width=0.90, height=2.10,
            status=DEDUCTION_REVIEW, position=3.0,
        )
        apply_deductions([deducted, not_deducted])
        area = deducted_total_area_m2([deducted, not_deducted])
        expected = round(0.82 * 2.10, 4)
        self.assertAlmostEqual(area, expected, places=4)

    def test_net_wall_area(self):
        inst = _make_instance(width=0.82, height=2.10, position=1.0)
        apply_deductions([inst])
        result = net_wall_area_after_deductions(20.0, [inst], wall_ref="W01")
        expected = round(20.0 - 0.82 * 2.10, 4)
        self.assertAlmostEqual(result["net_area_m2"], expected, places=4)
        self.assertTrue(result["valid"])

    def test_over_deduction_detected(self):
        """Gross 1.0 - deducted 5.0 → valid=False, error message."""
        inst = _make_instance(width=2.0, height=2.50, position=1.0, wall="W01")
        apply_deductions([inst])
        result = net_wall_area_after_deductions(1.0, [inst], wall_ref="W01")
        self.assertFalse(result["valid"])
        self.assertIn("exceeds gross wall area", result["error"])
        self.assertEqual(result["net_area_m2"], 0.0)

    def test_no_deductions_full_gross(self):
        inst = _make_instance(status=DEDUCTION_REVIEW, position=1.0)
        apply_deductions([inst])
        result = net_wall_area_after_deductions(20.0, [inst], wall_ref="W01")
        self.assertAlmostEqual(result["net_area_m2"], 20.0, places=4)
        self.assertTrue(result["valid"])

    def test_empty_instances_zero_area(self):
        result = net_wall_area_after_deductions(20.0, [], wall_ref="W01")
        self.assertEqual(result["net_area_m2"], 20.0)
        self.assertTrue(result["valid"])
        self.assertEqual(deducted_total_area_m2([]), 0.0)

    def test_over_deduction_within_tolerance(self):
        """Deduction slightly within tolerance → valid."""
        inst = _make_instance(width=0.82, height=2.10, position=1.0, wall="W01")
        apply_deductions([inst])
        gross = 0.82 * 2.10 + 0.0005
        result = net_wall_area_after_deductions(gross, [inst], wall_ref="W01")
        self.assertTrue(result["valid"])

    def test_wall_scoped_no_cross_wall_leakage(self):
        """D01 on W02 does not reduce W01 gross area."""
        w02_inst = _make_instance(
            mark="D01", wall="W02", width=2.0, height=2.10, position=1.0
        )
        apply_deductions([w02_inst])
        result = net_wall_area_after_deductions(10.0, [w02_inst], wall_ref="W01")
        self.assertAlmostEqual(result["net_area_m2"], 10.0, places=4)
        self.assertTrue(result["valid"])

    def test_wall_scoped_only_matching_wall(self):
        """Gross W01=10, D01 on W01=2m², D02 on W02=3m² → W01 net=8."""
        d01 = _make_instance(
            mark="D01", wall="W01", width=0.82, height=2.10, position=1.0
        )
        d02 = _make_instance(
            mark="D02", wall="W02", width=2.0, height=1.50, position=3.0
        )
        apply_deductions([d01, d02])
        result = net_wall_area_after_deductions(10.0, [d01, d02], wall_ref="W01")
        expected = round(10.0 - 0.82 * 2.10, 4)
        self.assertAlmostEqual(result["net_area_m2"], expected, places=4)
        self.assertTrue(result["valid"])


# ============================================================================
# Safety rule tests
# ============================================================================
class TestSafetyRules(unittest.TestCase):
    """B5 safety invariants."""

    def test_b5_never_creates_instances(self):
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
        inst = _make_instance(status=DEDUCTION_REVIEW)
        inst.notes = "B4: conflict detected"
        apply_deductions([inst])
        self.assertFalse(inst.deduct)
        self.assertEqual(inst.deduction_status, DEDUCTION_REVIEW)

    def test_deduction_status_never_overwritten(self):
        """B5 never modifies deduction_status (evidence eligibility)."""
        inst = _make_instance(status=DEDUCTION_AUTO_ELIGIBLE)
        apply_deductions([inst])
        # deduction_status stays as auto_eligible (not "deducted")
        self.assertEqual(inst.deduction_status, DEDUCTION_AUTO_ELIGIBLE)

    def test_deduction_decision_separate(self):
        """deduction_decision is the separate commercial-decision field."""
        inst = _make_instance(status=DEDUCTION_AUTO_ELIGIBLE)
        apply_deductions([inst])
        self.assertEqual(inst.deduction_decision, DEDUCTION_DEDUCTED)

    def test_version(self):
        self.assertEqual(VERSION, "1.7.4")


# ============================================================================
# Integration: B4-conflicted duplicate + eligible duplicate
# ============================================================================
class TestB4ConflictSurvivesDedup(unittest.TestCase):
    """B4 conflicts must survive the physical dedup pass.

    Pipeline: dedup → B2 → B3 → B4 → B5.
    Dedup merges records but preserves source observations.
    B4 then re-evaluates and may force review from combined observations.
    """

    def test_conflicted_and_eligible_same_location(self):
        """One conflicted instance + one eligible at same location.
        After dedup, source observations from BOTH records are preserved.
        B4 would then re-evaluate from the combined observations."""
        conflicted = _make_instance(
            mark="D01", status=DEDUCTION_REVIEW, position=1.5, geom_conf=0.95,
        )
        # Add a schedule observation to the conflicted record
        conflicted.source_observations = list(conflicted.source_observations) + [{
            "source": "schedule_parse",
            "width_m": 0.90, "height_m": 2.10,
            "dimension_basis": "unknown",
            "dimension_confidence": 0.8,
            "type_mark": "D01", "page_no": 5,
            "accepted": True,
        }]
        eligible = _make_instance(
            mark="D01", status=DEDUCTION_AUTO_ELIGIBLE, position=1.5,
            geom_conf=0.6,
        )
        result = resolve_physical_duplicates([conflicted, eligible])
        # Merged into one (same mark, same position)
        self.assertEqual(len(result), 1)
        # Source observations from BOTH records are preserved
        plan_obs = [o for o in result[0].source_observations
                    if o["source"] == "plan_vector"]
        sched_obs = [o for o in result[0].source_observations
                     if o["source"] == "schedule_parse"]
        self.assertGreaterEqual(len(plan_obs), 1)
        self.assertGreaterEqual(len(sched_obs), 1)

    def test_conflict_survives_full_pipeline(self):
        """B4-conflicted instance → dedup → B4 re-evaluation → review."""
        from pb_opening_reconciliation_v173 import reconcile_opening_evidence
        from pb_opening_schedule_v171 import enrich_opening_evidence, ScheduleEntry
        from pb_opening_evidence_v170 import record_plan_observation

        # Create two instances at same position, one with schedule conflict
        a = _make_instance(
            mark="D01", width=0.82, position=1.5, geom_conf=0.95,
            dim_conf=0.95, assoc_conf=0.95,
        )
        b = _make_instance(
            mark="D01", width=0.82, position=1.5, geom_conf=0.95,
            dim_conf=0.95, assoc_conf=0.95,
        )
        # Enrich b with a conflicting schedule observation
        b.source_observations = list(b.source_observations) + [{
            "source": "schedule_parse",
            "width_m": 0.90, "height_m": 2.10,
            "dimension_basis": "unknown",
            "dimension_confidence": 0.8,
            "type_mark": "D01", "page_no": 5,
            "accepted": True,
        }]
        # Dedup BEFORE B4 — merges (same mark, same position)
        deduped = resolve_physical_duplicates([a, b])
        self.assertEqual(len(deduped), 1)
        # Both source observations preserved
        plan_obs = [o for o in deduped[0].source_observations
                    if o["source"] == "plan_vector"]
        sched_obs = [o for o in deduped[0].source_observations
                     if o["source"] == "schedule_parse"]
        self.assertGreaterEqual(len(plan_obs), 1)
        self.assertGreaterEqual(len(sched_obs), 1)
        # B4 re-evaluates — finds width conflict (0.82 plan vs 0.90 schedule)
        reconciled, conflicts = reconcile_opening_evidence(deduped)
        dim = [c for c in conflicts if c.conflict_type == "dimension_mismatch"]
        self.assertGreaterEqual(len(dim), 1)
        self.assertEqual(reconciled[0].deduction_status, DEDUCTION_REVIEW)

    def test_ambiguous_schedule_survives_dedup(self):
        """Ambiguous schedule observation on a duplicate → survives dedup."""
        a = _make_instance(mark="D01", position=1.5, width=0.82)
        b = _make_instance(mark="D01", position=1.5, width=0.82)
        # Add ambiguous observation to one
        b.source_observations = list(b.source_observations) + [{
            "source": "schedule_parse",
            "width_m": None, "height_m": None,
            "dimension_basis": "unknown",
            "dimension_confidence": 0.0,
            "type_mark": "D01", "page_no": None,
            "accepted": False, "status": "ambiguous",
            "alternatives": [
                {"width_mm": 820, "height_mm": 2100},
                {"width_mm": 920, "height_mm": 2100},
            ],
        }]
        result = resolve_physical_duplicates([a, b])
        # Merged into one; ambiguous observation preserved
        self.assertEqual(len(result), 1)
        ambig = [o for o in result[0].source_observations
                 if o.get("status") == "ambiguous"]
        self.assertEqual(len(ambig), 1)
        # B4 would then see the ambiguity → source_ambiguous → review
        from pb_opening_reconciliation_v173 import reconcile_opening_evidence
        reconciled, conflicts = reconcile_opening_evidence(result)
        ambig_conflicts = [c for c in conflicts
                           if c.conflict_type == "source_ambiguous"]
        self.assertGreaterEqual(len(ambig_conflicts), 1)
        self.assertEqual(reconciled[0].deduction_status, DEDUCTION_REVIEW)

    def test_d01_d02_conflict_forces_review_via_b4(self):
        """D01 + D02 at same location → B4 forces both to review → no deduct."""
        from pb_opening_reconciliation_v173 import reconcile_opening_evidence
        a = _make_instance(
            mark="D01", position=1.5, width=0.82,
            geom_conf=0.95, dim_conf=0.95, assoc_conf=0.95,
        )
        b = _make_instance(
            mark="D02", position=1.5, width=0.82,
            geom_conf=0.95, dim_conf=0.95, assoc_conf=0.95,
        )
        # Dedup records conflict observation on both
        deduped = resolve_physical_duplicates([a, b])
        self.assertEqual(len(deduped), 2)
        # B4 re-evaluates each independently
        reconciled, conflicts = reconcile_opening_evidence(deduped)
        pic = [c for c in conflicts
               if c.conflict_type == "physical_instance_conflict"]
        self.assertGreaterEqual(len(pic), 2)  # one per candidate
        # Both forced to review
        for inst in reconciled:
            self.assertEqual(inst.deduction_status, DEDUCTION_REVIEW)
            self.assertTrue(inst.reconciliation_complete)
        # B5 gate rejects both
        apply_deductions(reconciled)
        for inst in reconciled:
            self.assertFalse(inst.deduct)

    def test_door_window_same_position_forces_review(self):
        """Door + window at same position → same_location detects type conflict
        → physical_instance_conflict on both → B4 forces review → B5 rejects."""
        from pb_opening_reconciliation_v173 import reconcile_opening_evidence
        door = _make_instance(
            mark="D01", opening_type=OPENING_TYPE_DOOR,
            position=1.5, width=0.82,
            geom_conf=0.95, dim_conf=0.95, assoc_conf=0.95,
        )
        window = _make_instance(
            mark="W01", opening_type=OPENING_TYPE_WINDOW,
            position=1.5, width=0.82,
            geom_conf=0.95, dim_conf=0.95, assoc_conf=0.95,
        )
        # same_location detects type conflict → conflict on both
        deduped = resolve_physical_duplicates([door, window])
        self.assertEqual(len(deduped), 2)
        for inst in deduped:
            pic = [o for o in inst.source_observations
                   if o["source"] == "physical_instance_conflict"]
            self.assertEqual(len(pic), 1)
        # B4 forces both to review
        reconciled, conflicts = reconcile_opening_evidence(deduped)
        pic_conflicts = [c for c in conflicts
                         if c.conflict_type == "physical_instance_conflict"]
        self.assertGreaterEqual(len(pic_conflicts), 2)
        for inst in reconciled:
            self.assertEqual(inst.deduction_status, DEDUCTION_REVIEW)
        # B5 rejects both
        apply_deductions(reconciled)
        for inst in reconciled:
            self.assertFalse(inst.deduct)


# ============================================================================
# Pipeline: never-reconciled record rejected
# ============================================================================
class TestNeverReconciled(unittest.TestCase):
    """B5 gate requires reconciliation_complete=True."""

    def test_auto_eligible_not_reconciled_rejected(self):
        """auto_eligible but reconciliation_complete=False → rejected."""
        inst = _make_instance(
            status=DEDUCTION_AUTO_ELIGIBLE, reconciled=False
        )
        self.assertFalse(passes_eligibility_gate(inst))
        result = apply_deductions([inst])
        self.assertFalse(result[0].deduct)

    def test_reconciled_eligible_accepted(self):
        """auto_eligible + reconciliation_complete=True → accepted."""
        inst = _make_instance(
            status=DEDUCTION_AUTO_ELIGIBLE, reconciled=True
        )
        self.assertTrue(passes_eligibility_gate(inst))
        result = apply_deductions([inst])
        self.assertTrue(result[0].deduct)


class TestPipelineOrchestration(unittest.TestCase):
    """Integration test exercising run_opening_pipeline()."""

    def test_no_path_around_reconciliation(self):
        """Pipeline must not allow deduction without B4 reconciliation.

        Constructs a scenario where a never-reconciled auto_eligible
        instance would pass the gate if reconciliation_complete were
        not checked.  The pipeline orchestrator always runs B4, so
        reconciliation_complete is always set.
        """
        from pb_opening_evidence_v170 import record_plan_observation
        from pb_opening_reconciliation_v173 import reconcile_opening_evidence

        # Manually create an auto_eligible instance WITHOUT reconciliation
        inst = _make_instance(
            status=DEDUCTION_AUTO_ELIGIBLE, reconciled=False
        )
        # Without B4: gate rejects
        self.assertFalse(passes_eligibility_gate(inst))
        self.assertFalse(inst.reconciliation_complete)

        # After B4: reconciliation_complete = True, gate can proceed
        reconciled, _ = reconcile_opening_evidence([inst])
        self.assertTrue(reconciled[0].reconciliation_complete)
        # (status may change based on source_observations conflicts,
        # but the point is B4 has run)

    def test_d01_d02_never_both_deducted(self):
        """D01 + D02 at same location through full pipeline → neither deducted."""
        from pb_opening_reconciliation_v173 import reconcile_opening_evidence
        # Create two instances at same position with conflicting marks
        a = _make_instance(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, dim_conf=0.95, assoc_conf=0.95,
        )
        b = _make_instance(
            mark="D02", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, dim_conf=0.95, assoc_conf=0.95,
        )
        # Physical dedup → conflict observation on both
        instances = resolve_physical_duplicates([a, b])
        self.assertEqual(len(instances), 2)
        # B4 reconciliation → both get physical_instance_conflict → review
        instances, conflicts = reconcile_opening_evidence(instances)
        pic = [c for c in conflicts
               if c.conflict_type == "physical_instance_conflict"]
        self.assertGreaterEqual(len(pic), 2)
        # B5 gate → neither deducted
        instances = apply_deductions(instances)
        for inst in instances:
            self.assertFalse(inst.deduct)
            self.assertEqual(inst.deduction_status, DEDUCTION_REVIEW)

    def test_run_opening_pipeline_with_mocked_b1(self):
        """Actual run_opening_pipeline() call with mocked B1 result.

        Exercises the full pipeline order:
        B1 → dedup → B2 → B3 → B4 → B5

        and asserts reconciliation_complete=True before any deduct=True.
        """
        from unittest.mock import patch, MagicMock
        from pb_opening_deduction_v174 import run_opening_pipeline

        # Mock B1 result with D01 + D02 at SAME position → identity conflict
        inst_a = _make_instance(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, dim_conf=0.95, assoc_conf=0.95,
        )
        inst_b = _make_instance(
            mark="D02", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, dim_conf=0.95, assoc_conf=0.95,
        )
        mock_result = MagicMock()
        mock_result.candidates = [inst_a, inst_b]
        mock_result.door_count = 2
        mock_result.window_count = 0
        mock_result.gap_count = 0

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=mock_result,
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        # Pipeline produced instances
        self.assertEqual(len(result["instances"]), 2)
        # B4 ran on every instance
        for inst in result["instances"]:
            self.assertTrue(inst.reconciliation_complete)
        # D01 + D02 at same position → physical_instance_conflict → review
        for inst in result["instances"]:
            self.assertEqual(inst.deduction_status, DEDUCTION_REVIEW)
            self.assertFalse(inst.deduct)
        # Net wall area: no deductions → full gross
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], 20.0, places=4
        )
        self.assertTrue(result["net_wall"]["valid"])

    def test_run_opening_pipeline_eligible_single(self):
        """run_opening_pipeline() with a single eligible instance → deduct."""
        from unittest.mock import patch, MagicMock
        from pb_opening_deduction_v174 import run_opening_pipeline

        inst = _make_instance(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, dim_conf=0.95, assoc_conf=0.95,
        )
        mock_result = MagicMock()
        mock_result.candidates = [inst]
        mock_result.door_count = 1
        mock_result.window_count = 0
        mock_result.gap_count = 0

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=mock_result,
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        self.assertEqual(len(result["instances"]), 1)
        self.assertTrue(result["instances"][0].reconciliation_complete)
        self.assertTrue(result["instances"][0].deduct)
        self.assertEqual(result["instances"][0].deduction_decision, "deducted")
        expected_net = round(20.0 - 0.82 * 2.10, 4)
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], expected_net, places=4
        )

    def test_run_opening_pipeline_cross_wall_duplicate(self):
        """run_opening_pipeline() with same geometry on W01 + W02.

        Both candidates have the same plan_geometry_signature but
        different wall_ref. B5 dedup detects the conflict, B4 forces
        review on both, neither deducts. This is a regression for the
        cross-wall duplicate case that was deferred from B1.
        """
        from unittest.mock import patch, MagicMock
        from pb_opening_deduction_v174 import run_opening_pipeline

        # Mock two candidates with same geometry signature, different walls
        inst_a = _make_instance(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, dim_conf=0.95, assoc_conf=0.95,
        )
        inst_a.plan_geometry_signature = (1, 100.0, 200.0, 0.82, "door")
        inst_b = _make_instance(
            mark="D01", wall="W02", position=1.5, width=0.82,
            geom_conf=0.95, dim_conf=0.95, assoc_conf=0.95,
        )
        inst_b.plan_geometry_signature = (1, 100.0, 200.0, 0.82, "door")

        mock_result = MagicMock()
        mock_result.candidates = [inst_a, inst_b]
        mock_result.door_count = 2
        mock_result.window_count = 0
        mock_result.gap_count = 0

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=mock_result,
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        # Both instances present
        self.assertEqual(len(result["instances"]), 2)
        # B4 ran on every instance
        for inst in result["instances"]:
            self.assertTrue(inst.reconciliation_complete)
        # Cross-wall conflict → review on both
        for inst in result["instances"]:
            self.assertEqual(inst.deduction_status, DEDUCTION_REVIEW)
            self.assertFalse(inst.deduct)
        # Net wall: no deductions → full gross
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], 20.0, places=4
        )


if __name__ == "__main__":
    unittest.main()
