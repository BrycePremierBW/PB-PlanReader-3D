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
        """D01 + D02 at same position → NOT merged."""
        a = _make_instance(mark="D01", position=1.5, width=0.82)
        b = _make_instance(mark="D02", position=1.5, width=0.82)
        result = resolve_physical_duplicates([a, b])
        self.assertEqual(len(result), 2)
        # B5 note explains why not merged
        b_note = [i for i in result if i.type_mark == "D02"][0]
        self.assertIn("conflicting marks", b_note.notes)

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
        result = net_wall_area_after_deductions(20.0, [inst])
        expected = round(20.0 - 0.82 * 2.10, 4)
        self.assertAlmostEqual(result["net_area_m2"], expected, places=4)
        self.assertTrue(result["valid"])

    def test_over_deduction_detected(self):
        """Gross 1.0 - deducted 5.0 → valid=False, error message."""
        inst = _make_instance(width=2.0, height=2.50, position=1.0)
        apply_deductions([inst])
        result = net_wall_area_after_deductions(1.0, [inst])
        self.assertFalse(result["valid"])
        self.assertIn("exceeds gross wall area", result["error"])
        self.assertEqual(result["net_area_m2"], 0.0)

    def test_no_deductions_full_gross(self):
        inst = _make_instance(status=DEDUCTION_REVIEW, position=1.0)
        apply_deductions([inst])
        result = net_wall_area_after_deductions(20.0, [inst])
        self.assertAlmostEqual(result["net_area_m2"], 20.0, places=4)
        self.assertTrue(result["valid"])

    def test_empty_instances_zero_area(self):
        result = net_wall_area_after_deductions(20.0, [])
        self.assertEqual(result["net_area_m2"], 20.0)
        self.assertTrue(result["valid"])
        self.assertEqual(deducted_total_area_m2([]), 0.0)

    def test_over_deduction_within_tolerance(self):
        """Deduction slightly within tolerance → valid."""
        inst = _make_instance(width=0.82, height=2.10, position=1.0)
        apply_deductions([inst])
        # gross = exact deduction + 0.0005 (within tolerance)
        gross = 0.82 * 2.10 + 0.0005
        result = net_wall_area_after_deductions(gross, [inst])
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


if __name__ == "__main__":
    unittest.main()
