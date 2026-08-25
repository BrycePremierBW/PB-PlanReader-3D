"""Tests for pb_opening_reconciliation_v173 — B4 cross-source reconciliation.

Covers:
  - Source detection from structured observations (not free-text)
  - Conflict detection: dimension mismatch, mark mismatch, basis ambiguity
  - Reconciliation confidence as separate field (not overwriting per-source)
  - Conflicts force deduction_status = review
  - Rejected observations retained for audit
  - Main reconcile_opening_evidence() pipeline
  - Safety rules: no creation, no deletion, no deduct=True
  - Edge cases: empty input, single source, all sources
"""
from __future__ import annotations

import unittest

from pb_opening_evidence_v170 import (
    OpeningEvidence,
    DIMENSION_BASIS_UNKNOWN,
    DIMENSION_BASIS_ROUGH_OPENING,
    DEDUCTION_REVIEW,
    DEDUCTION_AUTO_ELIGIBLE,
    CONFIDENCE_AUTO_DEDUCT,
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
    _obs_sources,
    _SOURCE_CONFIDENCE,
    SOURCE_PLAN,
    SOURCE_SCHEDULE,
    SOURCE_ELEVATION,
    DIMENSION_CONFLICT_THRESHOLD_M,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _obs(source, width=None, height=None, basis=DIMENSION_BASIS_UNKNOWN,
         conf=0.5, mark="", page=None, accepted=True):
    """Create a source observation dict."""
    return {
        "source": source,
        "width_m": width,
        "height_m": height,
        "dimension_basis": basis,
        "dimension_confidence": conf,
        "type_mark": mark,
        "page_no": page,
        "accepted": accepted,
    }


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
    observations=None,
):
    """Create a minimal OpeningEvidence with optional source observations."""
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
    )
    if observations:
        ev.source_observations = list(observations)
    return ev


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------
class TestVersion(unittest.TestCase):
    def test_version(self):
        self.assertEqual(VERSION, "1.7.3")


class TestSourceObservationsField(unittest.TestCase):
    """OpeningEvidence.source_observations field."""

    def test_default_empty(self):
        inst = _inst()
        self.assertEqual(inst.source_observations, [])

    def test_stores_observations(self):
        obs = [_obs(SOURCE_PLAN, width=0.82)]
        inst = _inst(observations=obs)
        self.assertEqual(len(inst.source_observations), 1)
        self.assertEqual(inst.source_observations[0]["source"], SOURCE_PLAN)


class TestSourceDetection(unittest.TestCase):
    """Source detection from structured observations."""

    def test_plan_detected(self):
        inst = _inst(observations=[_obs(SOURCE_PLAN, width=0.82)])
        self.assertTrue(_has_plan_evidence(inst))
        self.assertFalse(_has_schedule_evidence(inst))
        self.assertFalse(_has_elevation_evidence(inst))

    def test_schedule_detected(self):
        inst = _inst(observations=[_obs(SOURCE_SCHEDULE, width=0.82)])
        self.assertFalse(_has_plan_evidence(inst))
        self.assertTrue(_has_schedule_evidence(inst))
        self.assertFalse(_has_elevation_evidence(inst))

    def test_elevation_detected(self):
        inst = _inst(observations=[_obs(SOURCE_ELEVATION, height=2.1)])
        self.assertFalse(_has_plan_evidence(inst))
        self.assertFalse(_has_schedule_evidence(inst))
        self.assertTrue(_has_elevation_evidence(inst))

    def test_multiple_sources_detected(self):
        inst = _inst(observations=[
            _obs(SOURCE_PLAN, width=0.82),
            _obs(SOURCE_SCHEDULE, width=0.82),
            _obs(SOURCE_ELEVATION, height=2.1),
        ])
        self.assertTrue(_has_plan_evidence(inst))
        self.assertTrue(_has_schedule_evidence(inst))
        self.assertTrue(_has_elevation_evidence(inst))

    def test_no_observations_no_sources(self):
        inst = _inst()
        self.assertFalse(_has_plan_evidence(inst))
        self.assertFalse(_has_schedule_evidence(inst))
        self.assertFalse(_has_elevation_evidence(inst))

    def test_evidence_note_with_schedule_keyword_no_obs(self):
        """Evidence containing 'schedule' word but no schedule observation
        → has_schedule = false."""
        inst = _inst()
        inst.evidence = ["schedule_parse page=5 D01 820x2100"]
        self.assertFalse(_has_schedule_evidence(inst))

    def test_obs_sources_returns_dict(self):
        inst = _inst(observations=[
            _obs(SOURCE_PLAN, width=0.82),
            _obs(SOURCE_ELEVATION, height=2.1),
        ])
        srcs = _obs_sources(inst)
        self.assertTrue(srcs[SOURCE_PLAN])
        self.assertFalse(srcs[SOURCE_SCHEDULE])
        self.assertTrue(srcs[SOURCE_ELEVATION])


class TestConflictDetection(unittest.TestCase):
    """Conflict detection from structured observations."""

    def test_no_conflict_agreeing_sources(self):
        """Plan 0.82 + schedule 0.82 → no dimension conflict."""
        inst = _inst(observations=[
            _obs(SOURCE_PLAN, width=0.82, height=2.1),
            _obs(SOURCE_SCHEDULE, width=0.82, height=2.1),
        ])
        conflicts = _detect_conflicts(inst)
        dim = [c for c in conflicts if c.conflict_type == "dimension_mismatch"]
        self.assertEqual(len(dim), 0)

    def test_width_conflict(self):
        """Plan 0.82 vs schedule 0.90 → width conflict."""
        inst = _inst(width=0.90, dim_source="schedule_parse", observations=[
            _obs(SOURCE_PLAN, width=0.82, height=2.1, accepted=False),
            _obs(SOURCE_SCHEDULE, width=0.90, height=2.1, accepted=True),
        ])
        conflicts = _detect_conflicts(inst)
        dim = [c for c in conflicts if c.conflict_type == "dimension_mismatch"
               and c.field_name == "width_m"]
        self.assertEqual(len(dim), 1)

    def test_height_conflict(self):
        """Plan height 2.1 vs elevation height 2.5 → height conflict."""
        inst = _inst(observations=[
            _obs(SOURCE_PLAN, width=0.82, height=2.1),
            _obs(SOURCE_ELEVATION, width=0.82, height=2.5),
        ])
        conflicts = _detect_conflicts(inst)
        dim = [c for c in conflicts if c.conflict_type == "dimension_mismatch"
               and c.field_name == "height_m"]
        self.assertEqual(len(dim), 1)

    def test_mark_conflict(self):
        """Plan D01 vs schedule W01 → mark conflict."""
        inst = _inst(mark="D01", observations=[
            _obs(SOURCE_PLAN, width=0.82, mark="D01"),
            _obs(SOURCE_SCHEDULE, width=0.82, mark="W01"),
        ])
        conflicts = _detect_conflicts(inst)
        mark = [c for c in conflicts if c.conflict_type == "mark_mismatch"]
        self.assertEqual(len(mark), 1)

    def test_basis_ambiguous(self):
        """Multiple sources but unknown basis → basis_ambiguous."""
        inst = _inst(basis=DIMENSION_BASIS_UNKNOWN, observations=[
            _obs(SOURCE_PLAN, width=0.82),
            _obs(SOURCE_SCHEDULE, width=0.82),
        ])
        conflicts = _detect_conflicts(inst)
        basis = [c for c in conflicts if c.conflict_type == "basis_ambiguous"]
        self.assertEqual(len(basis), 1)

    def test_no_basis_ambiguous_single_source(self):
        """Single source with unknown basis → no conflict."""
        inst = _inst(observations=[_obs(SOURCE_PLAN, width=0.82)])
        conflicts = _detect_conflicts(inst)
        basis = [c for c in conflicts if c.conflict_type == "basis_ambiguous"]
        self.assertEqual(len(basis), 0)

    def test_basis_disagreement(self):
        """Plan says rough_opening, schedule says frame → basis disagreement."""
        inst = _inst(basis=DIMENSION_BASIS_ROUGH_OPENING, observations=[
            _obs(SOURCE_PLAN, width=0.82, basis=DIMENSION_BASIS_ROUGH_OPENING),
            _obs(SOURCE_SCHEDULE, width=0.82, basis="frame"),
        ])
        conflicts = _detect_conflicts(inst)
        basis = [c for c in conflicts if c.conflict_type == "basis_disagreement"]
        self.assertEqual(len(basis), 1)

    def test_no_comparison_when_source_missing(self):
        """Plan present but no schedule → no plan-vs-schedule comparison."""
        inst = _inst(observations=[_obs(SOURCE_PLAN, width=0.82)])
        conflicts = _detect_conflicts(inst)
        self.assertEqual(len(conflicts), 0)

    def test_multiple_conflict_types(self):
        """Width mismatch + mark mismatch → two conflicts."""
        inst = _inst(mark="D01", observations=[
            _obs(SOURCE_PLAN, width=0.82, mark="D01"),
            _obs(SOURCE_SCHEDULE, width=0.90, mark="W01"),
        ])
        conflicts = _detect_conflicts(inst)
        types = {c.conflict_type for c in conflicts}
        self.assertIn("dimension_mismatch", types)
        self.assertIn("mark_mismatch", types)

    def test_within_threshold_no_conflict(self):
        """Widths within 50mm → no conflict."""
        inst = _inst(observations=[
            _obs(SOURCE_PLAN, width=0.82),
            _obs(SOURCE_SCHEDULE, width=0.86),
        ])
        conflicts = _detect_conflicts(inst)
        dim = [c for c in conflicts if c.conflict_type == "dimension_mismatch"]
        self.assertEqual(len(dim), 0)

    def test_rejected_observation_retained(self):
        """Rejected schedule observation is still in source_observations."""
        obs = [
            _obs(SOURCE_PLAN, width=0.82, accepted=True),
            _obs(SOURCE_SCHEDULE, width=0.90, accepted=False),
        ]
        inst = _inst(observations=obs)
        # Rejected obs is still there for B4 comparison
        sched_obs = [o for o in inst.source_observations
                     if o["source"] == SOURCE_SCHEDULE]
        self.assertEqual(len(sched_obs), 1)
        self.assertFalse(sched_obs[0]["accepted"])

    def test_conflict_record_frozen(self):
        cr = ConflictRecord(
            opening_instance_id="abc",
            conflict_type="dimension_mismatch",
            source_a="plan_vector",
            source_b="schedule_parse",
            field_name="width_m",
            value_a="0.8200",
            value_b="0.9000",
            severity="warning",
            description="test",
        )
        with self.assertRaises(AttributeError):
            cr.severity = "error"  # type: ignore[misc]


class TestReconciliationConfidence(unittest.TestCase):
    """Reconciliation confidence as separate field."""

    def test_plan_only(self):
        inst = _inst(observations=[_obs(SOURCE_PLAN, width=0.82)])
        conf = _compute_reconciliation_confidence(inst)
        self.assertAlmostEqual(conf, 0.55, places=2)

    def test_plan_plus_schedule(self):
        inst = _inst(observations=[
            _obs(SOURCE_PLAN, width=0.82),
            _obs(SOURCE_SCHEDULE, width=0.82),
        ])
        conf = _compute_reconciliation_confidence(inst)
        self.assertAlmostEqual(conf, 0.75, places=2)

    def test_plan_plus_schedule_plus_elevation(self):
        inst = _inst(observations=[
            _obs(SOURCE_PLAN, width=0.82),
            _obs(SOURCE_SCHEDULE, width=0.82),
            _obs(SOURCE_ELEVATION, height=2.1),
        ])
        conf = _compute_reconciliation_confidence(inst)
        self.assertAlmostEqual(conf, 0.90, places=2)

    def test_rough_opening_bonus(self):
        inst = _inst(
            basis=DIMENSION_BASIS_ROUGH_OPENING,
            width=0.82,
            height=2.1,
            observations=[_obs(SOURCE_PLAN, width=0.82, basis=DIMENSION_BASIS_ROUGH_OPENING)],
        )
        conf = _compute_reconciliation_confidence(inst)
        self.assertAlmostEqual(conf, 0.60, places=2)  # 0.55 + 0.05

    def test_capped_at_097(self):
        inst = _inst(
            basis=DIMENSION_BASIS_ROUGH_OPENING,
            width=0.82,
            height=2.1,
            observations=[
                _obs(SOURCE_PLAN, width=0.82),
                _obs(SOURCE_SCHEDULE, width=0.82),
                _obs(SOURCE_ELEVATION, height=2.1),
            ],
        )
        conf = _compute_reconciliation_confidence(inst)
        self.assertLessEqual(conf, 0.97)

    def test_per_source_confidence_not_overwritten(self):
        """reconciliation_confidence is separate — per-source confidence unchanged."""
        inst = _inst(
            geom_conf=0.55,
            dim_conf=0.40,
            assoc_conf=0.30,
            observations=[
                _obs(SOURCE_PLAN, width=0.82),
                _obs(SOURCE_SCHEDULE, width=0.82),
                _obs(SOURCE_ELEVATION, height=2.1),
            ],
        )
        reconcile_opening_evidence([inst])
        # Per-source confidence NOT overwritten
        self.assertAlmostEqual(inst.geometry_confidence, 0.55, places=2)
        self.assertAlmostEqual(inst.dimension_confidence, 0.40, places=2)
        self.assertAlmostEqual(inst.association_confidence, 0.30, places=2)
        # Reconciliation confidence is separate
        self.assertAlmostEqual(inst.reconciliation_confidence, 0.90, places=2)


class TestReconcileOpeningEvidence(unittest.TestCase):
    """Main reconciliation pipeline."""

    def test_no_conflict_full_sources(self):
        """Plan + schedule + elevation, all agreeing, known basis → no conflict."""
        inst = _inst(
            basis=DIMENSION_BASIS_ROUGH_OPENING,
            geom_conf=0.9,
            dim_conf=0.9,
            assoc_conf=0.9,
            observations=[
                _obs(SOURCE_PLAN, width=0.82, height=2.1,
                     basis=DIMENSION_BASIS_ROUGH_OPENING),
                _obs(SOURCE_SCHEDULE, width=0.82, height=2.1,
                     basis=DIMENSION_BASIS_ROUGH_OPENING),
                _obs(SOURCE_ELEVATION, width=0.82, height=2.1),
            ],
        )
        reconciled, conflicts = reconcile_opening_evidence([inst])
        self.assertEqual(len(conflicts), 0)
        # No conflict → compute_deduction_status runs normally
        # min(0.9, 0.9, 0.9) = 0.9 >= 0.9 → auto_eligible
        self.assertEqual(reconciled[0].deduction_status, DEDUCTION_AUTO_ELIGIBLE)

    def test_width_conflict_forces_review(self):
        """Plan 0.82 vs schedule 0.90 → conflict → deduction_status = review."""
        inst = _inst(
            width=0.90,
            dim_source="schedule_parse",
            basis=DIMENSION_BASIS_ROUGH_OPENING,
            geom_conf=0.95,
            dim_conf=0.95,
            assoc_conf=0.95,
            observations=[
                _obs(SOURCE_PLAN, width=0.82, accepted=False),
                _obs(SOURCE_SCHEDULE, width=0.90, accepted=True),
            ],
        )
        reconciled, conflicts = reconcile_opening_evidence([inst])
        dim = [c for c in conflicts if c.conflict_type == "dimension_mismatch"]
        self.assertGreaterEqual(len(dim), 1)
        # Conflict forces review despite high confidence
        self.assertEqual(reconciled[0].deduction_status, DEDUCTION_REVIEW)

    def test_source_diversity_does_not_manufacture_eligibility(self):
        """Three sources present but geometry_confidence = 0.55
        → geometry confidence remains 0.55
        → source diversity does NOT manufacture auto eligibility."""
        inst = _inst(
            basis=DIMENSION_BASIS_ROUGH_OPENING,
            geom_conf=0.55,
            dim_conf=0.55,
            assoc_conf=0.55,
            observations=[
                _obs(SOURCE_PLAN, width=0.82),
                _obs(SOURCE_SCHEDULE, width=0.82),
                _obs(SOURCE_ELEVATION, height=2.1),
            ],
        )
        reconciled, _ = reconcile_opening_evidence([inst])
        # Per-source confidence NOT changed
        self.assertAlmostEqual(reconciled[0].geometry_confidence, 0.55, places=2)
        self.assertAlmostEqual(reconciled[0].dimension_confidence, 0.55, places=2)
        self.assertAlmostEqual(reconciled[0].association_confidence, 0.55, places=2)
        # Min confidence 0.55 < 0.70 → not derived_eligible
        self.assertNotEqual(reconciled[0].deduction_status, DEDUCTION_AUTO_ELIGIBLE)
        self.assertNotEqual(reconciled[0].deduction_status, "derived_eligible")

    def test_agreeing_sources_eligible_when_confidence_sufficient(self):
        """All sources agree, all confidence ≥ 0.90 → auto_eligible."""
        inst = _inst(
            basis=DIMENSION_BASIS_ROUGH_OPENING,
            geom_conf=0.92,
            dim_conf=0.91,
            assoc_conf=0.90,
            observations=[
                _obs(SOURCE_PLAN, width=0.82),
                _obs(SOURCE_SCHEDULE, width=0.82),
                _obs(SOURCE_ELEVATION, height=2.1),
            ],
        )
        reconciled, conflicts = reconcile_opening_evidence([inst])
        self.assertEqual(len(conflicts), 0)
        self.assertEqual(reconciled[0].deduction_status, DEDUCTION_AUTO_ELIGIBLE)

    def test_no_instances_empty_output(self):
        reconciled, conflicts = reconcile_opening_evidence([])
        self.assertEqual(len(reconciled), 0)
        self.assertEqual(len(conflicts), 0)

    def test_preserves_instance_count(self):
        insts = [_inst(mark="D01"), _inst(mark="W01")]
        reconciled, _ = reconcile_opening_evidence(insts)
        self.assertEqual(len(reconciled), 2)

    def test_does_not_set_deduct_true(self):
        inst = _inst(
            basis=DIMENSION_BASIS_ROUGH_OPENING,
            geom_conf=0.99,
            dim_conf=0.99,
            assoc_conf=0.99,
            observations=[_obs(SOURCE_PLAN, width=0.82)],
        )
        reconciled, _ = reconcile_opening_evidence([inst])
        self.assertFalse(reconciled[0].deduct)

    def test_does_not_create_instances(self):
        insts = [_inst() for _ in range(5)]
        ids_before = [i.opening_instance_id for i in insts]
        reconciled, _ = reconcile_opening_evidence(insts)
        ids_after = [i.opening_instance_id for i in reconciled]
        self.assertEqual(ids_before, ids_after)

    def test_conflict_appended_to_notes(self):
        inst = _inst(observations=[
            _obs(SOURCE_PLAN, width=0.82),
            _obs(SOURCE_SCHEDULE, width=0.90),
        ])
        reconciled, _ = reconcile_opening_evidence([inst])
        self.assertIn("B4:", reconciled[0].notes)

    def test_no_conflict_no_notes(self):
        inst = _inst(observations=[_obs(SOURCE_PLAN, width=0.82)])
        reconciled, _ = reconcile_opening_evidence([inst])
        self.assertEqual(reconciled[0].notes, "")


class TestEdgeCases(unittest.TestCase):
    """Edge cases for the reconciliation pipeline."""

    def test_no_observations(self):
        """Instance with no source observations → no conflicts."""
        inst = _inst()
        reconciled, conflicts = reconcile_opening_evidence([inst])
        self.assertEqual(len(conflicts), 0)
        self.assertEqual(reconciled[0].reconciliation_confidence, 0.30)

    def test_single_observation(self):
        """One observation → no conflicts possible."""
        inst = _inst(observations=[_obs(SOURCE_PLAN, width=0.82)])
        reconciled, conflicts = reconcile_opening_evidence([inst])
        self.assertEqual(len(conflicts), 0)

    def test_many_instances(self):
        insts = [_inst(mark=f"D{i:02d}", pos=float(i)) for i in range(50)]
        reconciled, conflicts = reconcile_opening_evidence(insts)
        self.assertEqual(len(reconciled), 50)

    def test_existing_notes_appended(self):
        inst = _inst()
        inst.notes = "existing"
        inst.source_observations = [
            _obs(SOURCE_PLAN, width=0.82),
            _obs(SOURCE_SCHEDULE, width=0.90),
        ]
        reconciled, _ = reconcile_opening_evidence([inst])
        self.assertIn("existing", reconciled[0].notes)
        self.assertIn("B4:", reconciled[0].notes)

    def test_reconciliation_confidence_written(self):
        inst = _inst(observations=[
            _obs(SOURCE_PLAN, width=0.82),
            _obs(SOURCE_SCHEDULE, width=0.82),
        ])
        reconciled, _ = reconcile_opening_evidence([inst])
        self.assertGreater(reconciled[0].reconciliation_confidence, 0.0)


if __name__ == "__main__":
    unittest.main()
