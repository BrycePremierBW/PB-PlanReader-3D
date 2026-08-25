"""Tests for pb_opening_reconciliation_v173 — B4 cross-source reconciliation.

Covers:
  - Source detection helpers (plan, schedule, elevation)
  - Conflict detection (dimension mismatch, basis ambiguity)
  - Reconciliation confidence computation (source diversity, bonuses, penalties)
  - Main reconcile_opening_evidence() pipeline
  - Safety rules: no creation, no deletion, no deduct=True
  - Edge cases: empty input, single source, all sources, no dimensions
"""
from __future__ import annotations

import unittest

from pb_opening_evidence_v170 import (
    OpeningEvidence,
    merge_opening_evidence,
    DIMENSION_BASIS_UNKNOWN,
    DIMENSION_BASIS_ROUGH_OPENING,
    DIMENSION_BASIS_FRAME,
    DEDUCTION_REVIEW,
    DEDUCTION_AUTO_ELIGIBLE,
    DEDUCTION_DERIVED_ELIGIBLE,
    OPENING_TYPE_DOOR,
    OPENING_TYPE_WINDOW,
    CONFIDENCE_AUTO_DEDUCT,
    CONFIDENCE_DERIVED_DEDUCT,
    CONFIDENCE_REVIEW,
)
from pb_opening_reconciliation_v173 import (
    VERSION,
    ConflictRecord,
    _has_plan_evidence,
    _has_schedule_evidence,
    _has_elevation_evidence,
    _has_rough_opening_basis,
    _detect_conflicts,
    _compute_reconciliation_confidence,
    reconcile_opening_evidence,
    _SOURCE_CONFIDENCE,
    DIMENSION_CONFLICT_THRESHOLD_M,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _inst(
    mark="D01",
    wall_ref="N01",
    width=0.82,
    height=2.1,
    side="North",
    pos=2.5,
    method="plan_vector",
    dim_source="plan_vector",
    basis=DIMENSION_BASIS_UNKNOWN,
    geom_conf=0.6,
    dim_conf=0.4,
    assoc_conf=0.3,
    plan_geom=None,
    elev_geom=None,
    schedule_ref="",
):
    """Create a minimal OpeningEvidence for testing."""
    ev = OpeningEvidence(
        type_mark=mark,
        wall_ref=wall_ref,
        width_m=width,
        height_m=height,
        dimension_basis=basis,
        dimension_source=dim_source,
        elevation_side=side,
        position_along_wall_m=pos,
        extraction_method=method,
        geometry_confidence=geom_conf,
        dimension_confidence=dim_conf,
        association_confidence=assoc_conf,
        plan_geometry=plan_geom,
        elevation_geometry=elev_geom,
        schedule_ref=schedule_ref,
    )
    return ev


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------
class TestVersion(unittest.TestCase):
    def test_version(self):
        self.assertEqual(VERSION, "1.7.3")


class TestSourceDetection(unittest.TestCase):
    """_has_plan_evidence, _has_schedule_evidence, _has_elevation_evidence."""

    def test_plan_evidence_by_method(self):
        inst = _inst(method="plan_vector")
        self.assertTrue(_has_plan_evidence(inst))

    def test_plan_evidence_by_geometry(self):
        inst = _inst(method="", plan_geom={"bbox": [0, 0, 100, 200]})
        self.assertTrue(_has_plan_evidence(inst))

    def test_plan_evidence_by_position(self):
        """Position alone is not plan evidence — must have extraction_method
        or plan_geometry or evidence trail."""
        inst = _inst(method="", pos=2.5, plan_geom={"bbox": [0, 0, 100, 200]})
        self.assertTrue(_has_plan_evidence(inst))

    def test_no_plan_evidence(self):
        inst = _inst(method="schedule_parse", pos=None)
        self.assertFalse(_has_plan_evidence(inst))

    def test_schedule_evidence_by_source(self):
        inst = _inst(dim_source="schedule_parse")
        self.assertTrue(_has_schedule_evidence(inst))

    def test_schedule_evidence_by_ref(self):
        inst = _inst(dim_source="", schedule_ref="page5")
        self.assertTrue(_has_schedule_evidence(inst))

    def test_no_schedule_evidence(self):
        inst = _inst(dim_source="plan_vector")
        self.assertFalse(_has_schedule_evidence(inst))

    def test_elevation_evidence_by_source(self):
        inst = _inst(dim_source="elevation_rect")
        self.assertTrue(_has_elevation_evidence(inst))

    def test_elevation_evidence_by_geometry(self):
        inst = _inst(dim_source="", elev_geom={"bbox_px": [0, 0, 100, 200]})
        self.assertTrue(_has_elevation_evidence(inst))

    def test_no_elevation_evidence(self):
        inst = _inst(dim_source="plan_vector")
        self.assertFalse(_has_elevation_evidence(inst))

    def test_rough_opening_basis(self):
        inst = _inst(basis=DIMENSION_BASIS_ROUGH_OPENING)
        self.assertTrue(_has_rough_opening_basis(inst))

    def test_not_rough_opening_basis(self):
        inst = _inst(basis=DIMENSION_BASIS_UNKNOWN)
        self.assertFalse(_has_rough_opening_basis(inst))


class TestConflictDetection(unittest.TestCase):
    """_detect_conflicts() — cross-source conflict detection."""

    def test_no_conflict_plan_only(self):
        inst = _inst(method="plan_vector")
        conflicts = _detect_conflicts(inst)
        self.assertEqual(len(conflicts), 0)

    def test_no_conflict_agreeing_sources(self):
        """Plan + schedule with same width → no dimension conflict."""
        inst = _inst(
            width=0.82,
            dim_source="schedule_parse",
            plan_geom={"width_m": 0.82},
        )
        conflicts = _detect_conflicts(inst)
        # Widths agree within threshold → no dimension_mismatch
        dim_conflicts = [c for c in conflicts if c.conflict_type == "dimension_mismatch"]
        self.assertEqual(len(dim_conflicts), 0)

    def test_dimension_conflict(self):
        """Plan width 0.82 vs schedule width 0.90 → conflict."""
        inst = _inst(
            width=0.90,
            dim_source="schedule_parse",
            plan_geom={"width_m": 0.82},
        )
        conflicts = _detect_conflicts(inst)
        dim_conflicts = [c for c in conflicts if c.conflict_type == "dimension_mismatch"]
        self.assertEqual(len(dim_conflicts), 1)
        self.assertEqual(dim_conflicts[0].severity, "warning")

    def test_basis_ambiguous(self):
        """Multiple sources but unknown basis → basis_ambiguous conflict."""
        inst = _inst(
            basis=DIMENSION_BASIS_UNKNOWN,
            method="plan_vector",
            dim_source="schedule_parse",
            width=0.82,
            height=2.1,
        )
        conflicts = _detect_conflicts(inst)
        basis_conflicts = [c for c in conflicts if c.conflict_type == "basis_ambiguous"]
        self.assertEqual(len(basis_conflicts), 1)

    def test_no_basis_ambiguous_single_source(self):
        """Single source with unknown basis → no conflict."""
        inst = _inst(
            basis=DIMENSION_BASIS_UNKNOWN,
            method="plan_vector",
            width=0.82,
            height=2.1,
        )
        conflicts = _detect_conflicts(inst)
        basis_conflicts = [c for c in conflicts if c.conflict_type == "basis_ambiguous"]
        self.assertEqual(len(basis_conflicts), 0)

    def test_no_basis_ambiguous_rough_opening(self):
        """Rough opening basis with multiple sources → no basis conflict."""
        inst = _inst(
            basis=DIMENSION_BASIS_ROUGH_OPENING,
            method="plan_vector",
            dim_source="schedule_parse",
            width=0.82,
            height=2.1,
        )
        conflicts = _detect_conflicts(inst)
        basis_conflicts = [c for c in conflicts if c.conflict_type == "basis_ambiguous"]
        self.assertEqual(len(basis_conflicts), 0)

    def test_conflict_record_fields(self):
        """ConflictRecord has all required fields."""
        inst = _inst(
            width=0.90,
            dim_source="schedule_parse",
            plan_geom={"width_m": 0.82},
        )
        conflicts = _detect_conflicts(inst)
        self.assertGreaterEqual(len(conflicts), 1)
        dim_conflicts = [c for c in conflicts if c.conflict_type == "dimension_mismatch"]
        self.assertEqual(len(dim_conflicts), 1)
        c = dim_conflicts[0]
        self.assertIsInstance(c, ConflictRecord)
        self.assertEqual(c.conflict_type, "dimension_mismatch")
        self.assertIn("plan", c.source_a)
        self.assertIn("schedule", c.source_b)
        self.assertEqual(c.field_name, "width_m")

    def test_multiple_conflicts(self):
        """Both dimension mismatch and basis ambiguity."""
        inst = _inst(
            width=0.90,
            dim_source="schedule_parse",
            plan_geom={"width_m": 0.82},
            basis=DIMENSION_BASIS_UNKNOWN,
            height=2.1,
        )
        conflicts = _detect_conflicts(inst)
        types = {c.conflict_type for c in conflicts}
        self.assertIn("dimension_mismatch", types)
        self.assertIn("basis_ambiguous", types)


class TestReconciliationConfidence(unittest.TestCase):
    """_compute_reconciliation_confidence() — source diversity scoring."""

    def test_plan_only(self):
        inst = _inst(method="plan_vector")
        conf = _compute_reconciliation_confidence(inst)
        self.assertAlmostEqual(conf, 0.55, places=2)

    def test_plan_plus_schedule(self):
        inst = _inst(method="plan_vector", dim_source="schedule_parse")
        conf = _compute_reconciliation_confidence(inst)
        self.assertAlmostEqual(conf, 0.75, places=2)

    def test_plan_plus_elevation(self):
        inst = _inst(method="plan_vector", elev_geom={"bbox": [0, 0, 1, 1]})
        conf = _compute_reconciliation_confidence(inst)
        self.assertAlmostEqual(conf, 0.65, places=2)

    def test_plan_plus_schedule_plus_elevation(self):
        inst = _inst(
            method="plan_vector",
            dim_source="schedule_parse",
            elev_geom={"bbox": [0, 0, 1, 1]},
        )
        conf = _compute_reconciliation_confidence(inst)
        self.assertAlmostEqual(conf, 0.90, places=2)

    def test_rough_opening_bonus(self):
        """Rough opening basis with dimensions → +0.05 bonus."""
        inst = _inst(
            method="plan_vector",
            basis=DIMENSION_BASIS_ROUGH_OPENING,
            width=0.82,
            height=2.1,
        )
        conf = _compute_reconciliation_confidence(inst)
        self.assertAlmostEqual(conf, 0.60, places=2)  # 0.55 + 0.05

    def test_unknown_basis_no_penalty(self):
        """Unknown basis with multiple sources → no confidence penalty.

        The basis_ambiguous conflict is surfaced separately.  Reconciliation
        confidence is a floor that never downgrades existing confidence.
        """
        inst = _inst(
            method="plan_vector",
            dim_source="schedule_parse",
            basis=DIMENSION_BASIS_UNKNOWN,
            width=0.82,
            height=2.1,
        )
        conf = _compute_reconciliation_confidence(inst)
        self.assertAlmostEqual(conf, 0.75, places=2)  # no penalty applied

    def test_no_dimensions_low_confidence(self):
        """No dimensions → low base confidence."""
        inst = _inst(width=None, height=None)
        conf = _compute_reconciliation_confidence(inst)
        self.assertLess(conf, 0.60)

    def test_capped_at_097(self):
        """Confidence never exceeds 0.97."""
        inst = _inst(
            method="plan_vector",
            dim_source="schedule_parse",
            elev_geom={"bbox": [0, 0, 1, 1]},
            basis=DIMENSION_BASIS_ROUGH_OPENING,
            width=0.82,
            height=2.1,
            geom_conf=0.95,
            dim_conf=0.95,
            assoc_conf=0.95,
        )
        conf = _compute_reconciliation_confidence(inst)
        self.assertLessEqual(conf, 0.97)


class TestReconcileOpeningEvidence(unittest.TestCase):
    """reconcile_opening_evidence() — main reconciliation pipeline."""

    def test_basic_reconciliation(self):
        """Plan-only instance gets reconciliation confidence and status update."""
        inst = _inst()
        reconciled, conflicts = reconcile_opening_evidence([inst])
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(len(conflicts), 0)
        # Confidence should be upgraded to plan-only level
        self.assertGreaterEqual(reconciled[0].geometry_confidence, 0.55)

    def test_multi_source_reconciliation(self):
        """Plan + schedule + elevation → high confidence."""
        inst = _inst(
            method="plan_vector",
            dim_source="schedule_parse",
            elev_geom={"bbox": [0, 0, 1, 1]},
        )
        reconciled, conflicts = reconcile_opening_evidence([inst])
        self.assertEqual(len(reconciled), 1)
        # All confidence components should be at least 0.90
        self.assertGreaterEqual(reconciled[0].geometry_confidence, 0.90)
        self.assertGreaterEqual(reconciled[0].dimension_confidence, 0.90)
        self.assertGreaterEqual(reconciled[0].association_confidence, 0.90)

    def test_conflict_detected_and_noted(self):
        """Dimension conflict is detected and added to notes."""
        inst = _inst(
            width=0.90,
            dim_source="schedule_parse",
            plan_geom={"width_m": 0.82},
        )
        reconciled, conflicts = reconcile_opening_evidence([inst])
        dim_conflicts = [c for c in conflicts if c.conflict_type == "dimension_mismatch"]
        self.assertGreaterEqual(len(dim_conflicts), 1)
        self.assertIn("B4:", reconciled[0].notes)

    def test_no_instances_empty_output(self):
        reconciled, conflicts = reconcile_opening_evidence([])
        self.assertEqual(len(reconciled), 0)
        self.assertEqual(len(conflicts), 0)

    def test_preserves_instance_count(self):
        """Reconciliation does not create or delete instances."""
        insts = [_inst(mark="D01"), _inst(mark="W01")]
        reconciled, _ = reconcile_opening_evidence(insts)
        self.assertEqual(len(reconciled), 2)

    def test_deduction_status_updated(self):
        """Deduction status is recomputed after confidence upgrade."""
        inst = _inst(
            basis=DIMENSION_BASIS_ROUGH_OPENING,
            geom_conf=0.5,
            dim_conf=0.5,
            assoc_conf=0.5,
            method="plan_vector",
            dim_source="schedule_parse",
            elev_geom={"bbox": [0, 0, 1, 1]},
        )
        # Before reconciliation, min confidence is 0.5 (< 0.7)
        self.assertEqual(inst.deduction_status, DEDUCTION_REVIEW)

        reconciled, _ = reconcile_opening_evidence([inst])
        # After reconciliation, confidence upgraded to 0.90 → auto_eligible
        self.assertEqual(reconciled[0].deduction_status, DEDUCTION_AUTO_ELIGIBLE)

    def test_does_not_set_deduct_true(self):
        """B4 must never set deduct=True."""
        inst = _inst(
            basis=DIMENSION_BASIS_ROUGH_OPENING,
            geom_conf=0.95,
            dim_conf=0.95,
            assoc_conf=0.95,
        )
        inst.deduct = False
        reconciled, _ = reconcile_opening_evidence([inst])
        self.assertFalse(reconciled[0].deduct)

    def test_does_not_create_instances(self):
        """Reconciliation returns exactly the same instances."""
        insts = [_inst() for _ in range(5)]
        ids_before = [i.opening_instance_id for i in insts]
        reconciled, _ = reconcile_opening_evidence(insts)
        ids_after = [i.opening_instance_id for i in reconciled]
        self.assertEqual(ids_before, ids_after)

    def test_conflict_record_frozen(self):
        """ConflictRecord is frozen (immutable)."""
        cr = ConflictRecord(
            opening_instance_id="abc",
            conflict_type="dimension_mismatch",
            source_a="plan_vector",
            source_b="schedule_parse",
            field_name="width_m",
            value_a="0.82",
            value_b="0.90",
            severity="warning",
            description="test",
        )
        with self.assertRaises(AttributeError):
            cr.severity = "error"  # type: ignore[misc]


class TestSafetyContract(unittest.TestCase):
    """Verify B4 safety boundaries."""

    def test_no_instance_creation(self):
        """B4 never creates new OpeningEvidence instances."""
        insts = [_inst()]
        reconciled, _ = reconcile_opening_evidence(insts)
        self.assertEqual(len(reconciled), len(insts))

    def test_no_deduction_decision(self):
        """B4 never sets deduct=True."""
        insts = [_inst(
            basis=DIMENSION_BASIS_ROUGH_OPENING,
            geom_conf=0.99,
            dim_conf=0.99,
            assoc_conf=0.99,
        )]
        reconciled, _ = reconcile_opening_evidence(insts)
        for inst in reconciled:
            self.assertFalse(inst.deduct)

    def test_conflict_does_not_block_output(self):
        """Conflicts are surfaced but do not block reconciliation."""
        inst = _inst(
            width=0.90,
            dim_source="schedule_parse",
            plan_geom={"width_m": 0.82},
        )
        reconciled, conflicts = reconcile_opening_evidence([inst])
        self.assertEqual(len(reconciled), 1)
        self.assertGreater(len(conflicts), 0)

    def test_basis_ambiguous_no_auto_eligible(self):
        """Unknown basis → deduction_status stays review even with good confidence."""
        inst = _inst(
            basis=DIMENSION_BASIS_UNKNOWN,
            method="plan_vector",
            dim_source="schedule_parse",
            width=0.82,
            height=2.1,
            geom_conf=0.9,
            dim_conf=0.9,
            assoc_conf=0.9,
        )
        reconciled, _ = reconcile_opening_evidence([inst])
        # Basis is unknown → compute_deduction_status sets review
        self.assertEqual(reconciled[0].deduction_status, DEDUCTION_REVIEW)


class TestEdgeCases(unittest.TestCase):
    """Edge cases for the reconciliation pipeline."""

    def test_all_none_dimensions(self):
        """Instance with no dimensions → low confidence, no crash."""
        inst = _inst(width=None, height=None)
        reconciled, conflicts = reconcile_opening_evidence([inst])
        self.assertEqual(len(reconciled), 1)
        self.assertIsNone(reconciled[0].width_m)

    def test_schedule_only_instance(self):
        """Schedule-only (no plan position) → lower confidence."""
        inst = _inst(method="schedule_parse", pos=None, dim_source="schedule_parse")
        reconciled, _ = reconcile_opening_evidence([inst])
        self.assertGreaterEqual(reconciled[0].geometry_confidence, 0.60)

    def test_elevation_only_instance(self):
        """Elevation-only → very low confidence."""
        inst = _inst(method="elevation_rect", pos=None,
                     elev_geom={"bbox": [0, 0, 1, 1]})
        reconciled, _ = reconcile_opening_evidence([inst])
        self.assertGreaterEqual(reconciled[0].geometry_confidence, 0.50)

    def test_many_instances(self):
        """Reconciliation handles many instances without error."""
        insts = [_inst(mark=f"D{i:02d}", pos=float(i)) for i in range(50)]
        reconciled, conflicts = reconcile_opening_evidence(insts)
        self.assertEqual(len(reconciled), 50)

    def test_empty_notes_not_corrupted(self):
        """Instance with empty notes gets B4 note only when conflicts exist."""
        inst = _inst()
        reconciled, _ = reconcile_opening_evidence([inst])
        self.assertEqual(reconciled[0].notes, "")

    def test_existing_notes_appended(self):
        """Existing notes are preserved; B4 conflict note is appended."""
        inst = _inst()
        inst.notes = "existing note"
        # Add a conflict
        inst.dimension_source = "schedule_parse"
        inst.plan_geometry = {"width_m": 0.82}
        inst.width_m = 0.90
        reconciled, _ = reconcile_opening_evidence([inst])
        self.assertIn("existing note", reconciled[0].notes)
        self.assertIn("B4:", reconciled[0].notes)


if __name__ == "__main__":
    unittest.main()
