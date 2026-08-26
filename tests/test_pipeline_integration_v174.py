"""
B6: Opening pipeline integration / acceptance tests (v1.7.4)

Synthetic integration tests exercising the full pipeline
(B1→dedup→B2→B3→B4→B5) with realistic mocked B1 output.  Each test
asserts the expected final state of the pipeline output — deduction
decisions, net wall area, conflict records, and reconciliation status.

These are NOT benchmark validations against real drawings.  Real
benchmark fixtures require actual PDF/vector data from authoritative
sources (e.g. the LAGO revised tender basis) and should exercise the
full unmocked extraction chain.

These tests validate the pipeline's internal logic contracts:
  - Schedule basis must be explicit for deduction eligibility
  - Generic schedule dims must NOT enable deduction
  - Conflicts block deduction
  - Wall-scoped net area is isolated per wall

Total: 33 tests
"""
import unittest
from unittest.mock import patch, MagicMock

from pb_opening_evidence_v170 import (
    OPENING_TYPE_DOOR,
    OPENING_TYPE_WINDOW,
    DEDUCTION_REVIEW,
    DEDUCTION_AUTO_ELIGIBLE,
    DEDUCTION_NOT_DEDUCTED,
    OpeningEvidence,
)
from pb_opening_deduction_v174 import (
    run_opening_pipeline,
    apply_deductions,
    resolve_physical_duplicates,
    net_wall_area_after_deductions,
)


# ---------------------------------------------------------------------------
# Helper: build a realistic B1 candidate
# ---------------------------------------------------------------------------
def _b1_candidate(
    *,
    mark: str = "D01",
    wall: str = "W01",
    opening_type: str = OPENING_TYPE_DOOR,
    position: float = 1.5,
    width: float = 0.82,
    page_no: int = 1,
    geom_conf: float = 0.90,
    dim_conf: float = 0.0,
    assoc_conf: float = 0.85,
    sig: tuple = None,
) -> OpeningEvidence:
    """Create an OpeningEvidence that mimics B1 output."""
    ev = OpeningEvidence(
        type_mark=mark,
        page_no=page_no,
        wall_ref=wall,
        opening_type=opening_type,
        width_m=width,
        height_m=None,
        dimension_basis="unknown",
        dimension_source="plan_vector",
        sill_m=0.0 if opening_type == OPENING_TYPE_DOOR else 0.9,
        position_along_wall_m=position,
        extraction_method="plan_vector",
        geometry_confidence=geom_conf,
        dimension_confidence=dim_conf,
        association_confidence=assoc_conf,
        deduction_status=DEDUCTION_REVIEW,
        evidence=["plan_vector_jamb_detection"],
    )
    ev.set_quantity(1, source="geometric")
    ev.compute_area()
    ev.compute_deduction_status()
    from pb_opening_evidence_v170 import record_plan_observation
    record_plan_observation(ev)
    if sig is not None:
        ev.plan_geometry_signature = sig
    return ev


def _mock_b1(candidates, door_count=None, window_count=None, gap_count=None):
    """Create a mock PlanOpeningDetectionResult."""
    result = MagicMock()
    result.candidates = candidates
    result.door_count = door_count or sum(
        1 for c in candidates if c.opening_type == OPENING_TYPE_DOOR
    )
    result.window_count = window_count or sum(
        1 for c in candidates if c.opening_type == OPENING_TYPE_WINDOW
    )
    result.gap_count = gap_count or 0
    return result


# ============================================================================
# Benchmark 1: Single eligible door — full deduction
# ============================================================================
class TestBenchmark_SingleDoor(unittest.TestCase):
    """A single door with schedule-enriched dims should deduct."""

    def test_eligible_door_deducts(self):
        from pb_opening_schedule_v171 import ScheduleEntry

        door = _b1_candidate(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
        )
        schedule = [ScheduleEntry(
            type_mark="D01", width_mm=820, height_mm=2100,
            description="Standard door", count=4, page_no=2,
            parse_source="header_separate",
            dimension_basis="rough_opening",
            basis_source="Rough Opening Width",
        )]

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([door]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                schedule_entries=schedule,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        self.assertEqual(len(result["instances"]), 1)
        inst = result["instances"][0]

        # B4 completed
        self.assertTrue(inst.reconciliation_complete)
        # Schedule enriched dims
        self.assertAlmostEqual(inst.width_m, 0.82, places=2)
        self.assertAlmostEqual(inst.height_m, 2.10, places=2)
        self.assertEqual(inst.dimension_basis, "rough_opening")
        # B5 deducted
        self.assertTrue(inst.deduct)
        self.assertEqual(inst.deduction_decision, "deducted")
        # Net wall = 20.0 - (0.82 × 2.10)
        expected_net = round(20.0 - 0.82 * 2.10, 4)
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], expected_net, places=4
        )
        self.assertTrue(result["net_wall"]["valid"])
        # deducted_area_m2 is the area subtracted, not the net
        expected_deducted = round(0.82 * 2.10, 4)
        self.assertAlmostEqual(
            result["deducted_area_m2"], expected_deducted, places=4
        )


# ============================================================================
# Benchmark 2: Single eligible window — full deduction
# ============================================================================
class TestBenchmark_SingleWindow(unittest.TestCase):
    """A single window with schedule-enriched dims should deduct."""

    def test_eligible_window_deducts(self):
        from pb_opening_schedule_v171 import ScheduleEntry

        window = _b1_candidate(
            mark="W01", wall="W01", position=4.0, width=1.20,
            opening_type=OPENING_TYPE_WINDOW,
            geom_conf=0.90, assoc_conf=0.90,
        )
        schedule = [ScheduleEntry(
            type_mark="W01", width_mm=1200, height_mm=1500,
            description="Standard window", count=6, page_no=2,
            parse_source="header_separate",
            dimension_basis="rough_opening",
            basis_source="Rough Opening",
        )]

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([window]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                schedule_entries=schedule,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        inst = result["instances"][0]
        self.assertTrue(inst.reconciliation_complete)
        self.assertAlmostEqual(inst.height_m, 1.50, places=2)
        self.assertTrue(inst.deduct)
        expected_net = round(20.0 - 1.20 * 1.50, 4)
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], expected_net, places=4
        )


# ============================================================================
# Benchmark 3: Two independent doors — both deduct
# ============================================================================
class TestBenchmark_TwoIndependentDoors(unittest.TestCase):
    """Two doors at different positions, both eligible → both deduct."""

    def test_both_deduct(self):
        from pb_opening_schedule_v171 import ScheduleEntry

        d1 = _b1_candidate(
            mark="D01", wall="W01", position=1.0, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
            sig=(1, 100.0, 200.0, 0.82, "door"),
        )
        d2 = _b1_candidate(
            mark="D01", wall="W01", position=4.0, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
            sig=(1, 400.0, 200.0, 0.82, "door"),
        )
        schedule = [ScheduleEntry(
            type_mark="D01", width_mm=820, height_mm=2100,
            description="Standard door", count=4, page_no=2,
            parse_source="header_separate",
            dimension_basis="rough_opening",
            basis_source="Rough Opening Width",
        )]

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([d1, d2]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                schedule_entries=schedule,
                gross_wall_m2=30.0, wall_ref="W01",
            )

        self.assertEqual(len(result["instances"]), 2)
        for inst in result["instances"]:
            self.assertTrue(inst.reconciliation_complete)
            self.assertTrue(inst.deduct)
        # Both deduct: 2 × (0.82 × 2.10) = 3.444
        expected_net = round(30.0 - 2 * 0.82 * 2.10, 4)
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], expected_net, places=4
        )


# ============================================================================
# Benchmark 4: Door + window at same location — type conflict
# ============================================================================
class TestBenchmark_DoorWindowConflict(unittest.TestCase):
    """Door + window at same position → type conflict → neither deducts."""

    def test_type_conflict_blocks_deduction(self):
        door = _b1_candidate(
            mark="D01", wall="W01", position=1.5, width=0.82,
            opening_type=OPENING_TYPE_DOOR,
            geom_conf=0.95, assoc_conf=0.95,
            sig=(1, 150.0, 200.0, 0.82, "door"),
        )
        window = _b1_candidate(
            mark="W01", wall="W01", position=1.5, width=0.82,
            opening_type=OPENING_TYPE_WINDOW,
            geom_conf=0.95, assoc_conf=0.95,
            sig=(1, 150.0, 200.0, 0.82, "window"),
        )

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([door, window]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        # Both present, both conflict
        self.assertEqual(len(result["instances"]), 2)
        pic = [c for c in result["conflicts"]
               if c.conflict_type == "physical_instance_conflict"]
        self.assertGreaterEqual(len(pic), 2)
        for inst in result["instances"]:
            self.assertEqual(inst.deduction_status, DEDUCTION_REVIEW)
            self.assertFalse(inst.deduct)
        # No deductions → full gross
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], 20.0, places=4
        )


# ============================================================================
# Benchmark 5: D01 + D02 at same position — mark conflict
# ============================================================================
class TestBenchmark_D01D02MarkConflict(unittest.TestCase):
    """D01 + D02 at same position → mark conflict → neither deducts."""

    def test_mark_conflict_blocks_deduction(self):
        a = _b1_candidate(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
            sig=(1, 150.0, 200.0, 0.82, "door"),
        )
        b = _b1_candidate(
            mark="D02", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
            sig=(1, 150.0, 200.0, 0.82, "door"),
        )

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([a, b]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        self.assertEqual(len(result["instances"]), 2)
        for inst in result["instances"]:
            self.assertEqual(inst.deduction_status, DEDUCTION_REVIEW)
            self.assertFalse(inst.deduct)
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], 20.0, places=4
        )


# ============================================================================
# Benchmark 6: Same geometry, different walls — wall-association conflict
# ============================================================================
class TestBenchmark_CrossWallConflict(unittest.TestCase):
    """Same plan-space geometry on W01 + W02 → neither deducts."""

    def test_wall_association_conflict(self):
        a = _b1_candidate(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
            sig=(1, 150.0, 200.0, 0.82, "door"),
        )
        b = _b1_candidate(
            mark="D01", wall="W02", position=1.5, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
            sig=(1, 150.0, 200.0, 0.82, "door"),
        )

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([a, b]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        self.assertEqual(len(result["instances"]), 2)
        for inst in result["instances"]:
            self.assertEqual(inst.deduction_status, DEDUCTION_REVIEW)
            self.assertFalse(inst.deduct)
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], 20.0, places=4
        )


# ============================================================================
# Benchmark 7: Door with schedule, no elevation — deducts from dims
# ============================================================================
class TestBenchmark_ScheduleOnly(unittest.TestCase):
    """Schedule provides rough-opening dims → deduction proceeds."""

    def test_schedule_enables_deduction(self):
        from pb_opening_schedule_v171 import ScheduleEntry

        door = _b1_candidate(
            mark="D01", wall="W01", position=2.0, width=0.82,
            geom_conf=0.90, assoc_conf=0.85,
        )
        schedule = [ScheduleEntry(
            type_mark="D01", width_mm=820, height_mm=2100,
            description="Standard door", count=4, page_no=2,
            parse_source="header_separate",
            dimension_basis="rough_opening",
            basis_source="Rough Opening Width",
        )]

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([door]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                schedule_entries=schedule,
                gross_wall_m2=15.0, wall_ref="W01",
            )

        inst = result["instances"][0]
        self.assertTrue(inst.reconciliation_complete)
        self.assertEqual(inst.dimension_basis, "rough_opening")
        self.assertTrue(inst.deduct)
        expected_net = round(15.0 - 0.82 * 2.10, 4)
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], expected_net, places=4
        )


# ============================================================================
# Benchmark 8: No schedule, plan only — cannot deduct
# ============================================================================
class TestBenchmark_PlanOnlyNoSchedule(unittest.TestCase):
    """Plan-only dims have dimension_basis=unknown → review, no deduction."""

    def test_plan_only_blocks_deduction(self):
        door = _b1_candidate(
            mark="D01", wall="W01", position=2.0, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
        )

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([door]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                gross_wall_m2=15.0, wall_ref="W01",
            )

        inst = result["instances"][0]
        self.assertTrue(inst.reconciliation_complete)
        # Plan-only: dimension_basis stays unknown, height stays None
        self.assertEqual(inst.dimension_basis, "unknown")
        self.assertIsNone(inst.height_m)
        # Cannot deduct without rough-opening dims
        self.assertFalse(inst.deduct)
        self.assertEqual(inst.deduction_status, DEDUCTION_REVIEW)
        # No deductions → full gross
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], 15.0, places=4
        )


# ============================================================================
# Benchmark 9: Wall-scoped net — W02 deduction does not affect W01
# ============================================================================
class TestBenchmark_WallScopedNet(unittest.TestCase):
    """Opening on W02 should not reduce W01's net area."""

    def test_wall_scoped_isolation(self):
        from pb_opening_schedule_v171 import ScheduleEntry

        d_w01 = _b1_candidate(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
            sig=(1, 100.0, 200.0, 0.82, "door"),
        )
        d_w02 = _b1_candidate(
            mark="D01", wall="W02", position=2.0, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
            sig=(1, 500.0, 200.0, 0.82, "door"),
        )
        schedule = [ScheduleEntry(
            type_mark="D01", width_mm=820, height_mm=2100,
            description="Standard door", count=4, page_no=2,
            parse_source="header_separate",
            dimension_basis="rough_opening",
            basis_source="Rough Opening Width",
        )]

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([d_w01, d_w02]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                schedule_entries=schedule,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        # Both deduct (different walls, different geometry)
        for inst in result["instances"]:
            self.assertTrue(inst.deduct)
        # W01 net only subtracts W01 opening
        expected_net = round(20.0 - 0.82 * 2.10, 4)
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], expected_net, places=4
        )


# ============================================================================
# Benchmark 10: Over-detection — deduction exceeds gross → invalid
# ============================================================================
class TestBenchmark_OverDeductionDetection(unittest.TestCase):
    """If deducted area > gross, net_wall reports invalid."""

    def test_over_deduction_flagged(self):
        from pb_opening_schedule_v171 import ScheduleEntry

        # Two doors on a very small wall
        d1 = _b1_candidate(
            mark="D01", wall="W01", position=0.5, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
            sig=(1, 50.0, 100.0, 0.82, "door"),
        )
        d2 = _b1_candidate(
            mark="D02", wall="W01", position=2.0, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
            sig=(1, 200.0, 100.0, 0.82, "door"),
        )
        # Same marks → merge, not conflict. Use different marks.
        # Actually D01+D02 at different positions won't conflict.
        # But same mark at different positions will merge. Let's use
        # two different eligible marks at different positions.
        schedule = [
            ScheduleEntry(
                type_mark="D01", width_mm=820, height_mm=2100,
                count=1, page_no=2,
                parse_source="header_separate",
                dimension_basis="rough_opening",
                basis_source="Rough Opening",
            ),
            ScheduleEntry(
                type_mark="D02", width_mm=820, height_mm=2100,
                count=1, page_no=2,
                parse_source="header_separate",
                dimension_basis="rough_opening",
                basis_source="Rough Opening",
            ),
        ]

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([d1, d2]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                schedule_entries=schedule,
                gross_wall_m2=1.0, wall_ref="W01",  # tiny wall
            )

        # Both deduct
        for inst in result["instances"]:
            self.assertTrue(inst.deduct)
        # Net wall is invalid: 2 × (0.82 × 2.10) = 3.444 > 1.0
        self.assertFalse(result["net_wall"]["valid"])
        self.assertIn("exceeds", result["net_wall"]["error"])


# ============================================================================
# Benchmark 11: Empty pipeline — no instances
# ============================================================================
class TestBenchmark_EmptyPipeline(unittest.TestCase):
    """No B1 candidates → empty result, full gross."""

    def test_empty_result(self):
        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        self.assertEqual(len(result["instances"]), 0)
        self.assertEqual(result["deducted_area_m2"], 0.0)
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], 20.0, places=4
        )
        self.assertTrue(result["net_wall"]["valid"])


# ============================================================================
# Benchmark 12: Duplicate B1 candidates — dedup merges
# ============================================================================
class TestBenchmark_DedupMerge(unittest.TestCase):
    """Two identical B1 candidates (same physical opening) → merged."""

    def test_duplicates_merged(self):
        from pb_opening_schedule_v171 import ScheduleEntry

        d1 = _b1_candidate(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.60, assoc_conf=0.70,
            sig=(1, 150.0, 200.0, 0.82, "door"),
        )
        d2 = _b1_candidate(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.90, assoc_conf=0.80,
            sig=(1, 150.0, 200.0, 0.82, "door"),
        )
        schedule = [ScheduleEntry(
            type_mark="D01", width_mm=820, height_mm=2100,
            count=4, page_no=2,
            parse_source="header_separate",
            dimension_basis="rough_opening",
            basis_source="Rough Opening",
        )]

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([d1, d2]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                schedule_entries=schedule,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        # Two candidates merged into one
        self.assertEqual(len(result["instances"]), 1)
        inst = result["instances"][0]
        self.assertTrue(inst.deduct)
        # Only one deduction
        expected_net = round(20.0 - 0.82 * 2.10, 4)
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], expected_net, places=4
        )


# ============================================================================
# Benchmark 13: Schedule + elevation both enrich — dims come from schedule
# ============================================================================
class TestBenchmark_ScheduleAndElevation(unittest.TestCase):
    """Schedule provides dims, elevation provides correlation evidence."""

    def test_both_sources_enrich(self):
        from pb_opening_schedule_v171 import ScheduleEntry
        from pb_elevation_evidence_v172 import ElevationOpening

        door = _b1_candidate(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.90, assoc_conf=0.85,
        )
        schedule = [ScheduleEntry(
            type_mark="D01", width_mm=820, height_mm=2100,
            count=4, page_no=2,
            parse_source="header_separate",
            dimension_basis="rough_opening",
            basis_source="Rough Opening",
        )]
        elev = [ElevationOpening(
            elevation_page_no=3,
            elevation_side="North",
            bbox_px=(100, 100, 200, 300),
            width_m=0.82,
            height_m=2.10,
            label="D01",
            confidence=0.90,
        )]

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([door]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                schedule_entries=schedule,
                elevation_openings=elev,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        inst = result["instances"][0]
        self.assertTrue(inst.reconciliation_complete)
        # Schedule dims take precedence
        self.assertEqual(inst.dimension_basis, "rough_opening")
        self.assertTrue(inst.deduct)


# ============================================================================
# Benchmark 14: Reconciliation cannot be bypassed
# ============================================================================
class TestBenchmark_NoBypass(unittest.TestCase):
    """No path from B1 candidate to deduct=True that skips B4."""

    def test_reconciliation_required(self):
        door = _b1_candidate(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
        )

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([door]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        inst = result["instances"][0]
        # reconciliation_complete must be True before any deduction
        self.assertTrue(inst.reconciliation_complete)
        # Without schedule, no rough dims → review, not deducted
        self.assertFalse(inst.deduct)


# ============================================================================
# Benchmark 15: Deduction is idempotent
# ============================================================================
class TestBenchmark_IdempotentDeduction(unittest.TestCase):
    """Running apply_deductions twice produces the same result."""

    def test_idempotent(self):
        from pb_opening_schedule_v171 import ScheduleEntry

        door = _b1_candidate(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
        )
        schedule = [ScheduleEntry(
            type_mark="D01", width_mm=820, height_mm=2100,
            count=4, page_no=2,
            parse_source="header_separate",
            dimension_basis="rough_opening",
            basis_source="Rough Opening",
        )]

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([door]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                schedule_entries=schedule,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        inst = result["instances"][0]
        first_deduct = inst.deduct
        first_decision = inst.deduction_decision
        first_area = result["deducted_area_m2"]

        # Apply again
        apply_deductions(result["instances"])
        self.assertEqual(inst.deduct, first_deduct)
        self.assertEqual(inst.deduction_decision, first_decision)
        self.assertEqual(result["deducted_area_m2"], first_area)


# ============================================================================
# Benchmark 16: Pipeline notes contain stage information
# ============================================================================
class TestBenchmark_PipelineNotes(unittest.TestCase):
    """Pipeline notes should record B1, B2, B3, B4, B5 stages."""

    def test_notes_record_stages(self):
        from pb_opening_schedule_v171 import ScheduleEntry

        door = _b1_candidate(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
        )
        schedule = [ScheduleEntry(
            type_mark="D01", width_mm=820, height_mm=2100,
            count=4, page_no=2,
            parse_source="header_separate",
            dimension_basis="rough_opening",
            basis_source="Rough Opening",
        )]

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([door]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                schedule_entries=schedule,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        notes = result["pipeline_notes"]
        self.assertTrue(any("B1:" in n for n in notes))
        self.assertTrue(any("B2:" in n for n in notes))
        self.assertTrue(any("B4:" in n for n in notes))
        self.assertTrue(any("B5:" in n for n in notes))


# ============================================================================
# Benchmark 17: Confidence threshold enforcement
# ============================================================================
class TestBenchmark_LowConfidenceNoDeduct(unittest.TestCase):
    """Low confidence → review, no deduction."""

    def test_low_confidence_blocked(self):
        from pb_opening_schedule_v171 import ScheduleEntry

        door = _b1_candidate(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.40, assoc_conf=0.40,
        )
        schedule = [ScheduleEntry(
            type_mark="D01", width_mm=820, height_mm=2100,
            count=4, page_no=2,
            parse_source="header_separate",
            dimension_basis="rough_opening",
            basis_source="Rough Opening",
        )]

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([door]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                schedule_entries=schedule,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        inst = result["instances"][0]
        # Low confidence → not eligible (min(0.40, 0.8, 0.40)=0.40 < 0.5)
        self.assertNotEqual(inst.deduction_status, DEDUCTION_AUTO_ELIGIBLE)
        self.assertFalse(inst.deduct)
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], 20.0, places=4
        )


# ============================================================================
# Benchmark 18: Mixed eligible and review instances
# ============================================================================
class TestBenchmark_MixedStatus(unittest.TestCase):
    """One eligible + one in review → only eligible deducts."""

    def test_mixed_eligibility(self):
        from pb_opening_schedule_v171 import ScheduleEntry

        d_eligible = _b1_candidate(
            mark="D01", wall="W01", position=1.0, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
            sig=(1, 100.0, 200.0, 0.82, "door"),
        )
        d_review = _b1_candidate(
            mark="D02", wall="W01", position=4.0, width=0.90,
            geom_conf=0.40, assoc_conf=0.40,
            sig=(1, 400.0, 200.0, 0.90, "door"),
        )
        schedule = [
            ScheduleEntry(
                type_mark="D01", width_mm=820, height_mm=2100,
                count=4, page_no=2,
                parse_source="header_separate",
                dimension_basis="rough_opening",
                basis_source="Rough Opening",
            ),
            ScheduleEntry(
                type_mark="D02", width_mm=900, height_mm=2100,
                count=1, page_no=2,
                parse_source="header_separate",
                dimension_basis="rough_opening",
                basis_source="Rough Opening",
            ),
        ]

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([d_eligible, d_review]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                schedule_entries=schedule,
                gross_wall_m2=25.0, wall_ref="W01",
            )

        self.assertEqual(len(result["instances"]), 2)
        # Exactly one deducted
        deducted = [i for i in result["instances"] if i.deduct]
        self.assertEqual(len(deducted), 1)
        self.assertEqual(deducted[0].type_mark, "D01")
        # Net = 25.0 - 0.82 × 2.10 (only D01)
        expected_net = round(25.0 - 0.82 * 2.10, 4)
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], expected_net, places=4
        )


# ============================================================================
# Benchmark 19: Different marks, same position, compatible types
# (D01 + D01 at different positions — normal case, both eligible)
# ============================================================================
class TestBenchmark_SameMarkDifferentPositions(unittest.TestCase):
    """Two D01 instances at different positions → both deduct."""

    def test_normal_multiple_instances(self):
        from pb_opening_schedule_v171 import ScheduleEntry

        d1 = _b1_candidate(
            mark="D01", wall="W01", position=1.0, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
            sig=(1, 100.0, 200.0, 0.82, "door"),
        )
        d2 = _b1_candidate(
            mark="D01", wall="W01", position=5.0, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
            sig=(1, 500.0, 200.0, 0.82, "door"),
        )
        schedule = [ScheduleEntry(
            type_mark="D01", width_mm=820, height_mm=2100,
            count=4, page_no=2,
            parse_source="header_separate",
            dimension_basis="rough_opening",
            basis_source="Rough Opening",
        )]

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([d1, d2]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                schedule_entries=schedule,
                gross_wall_m2=30.0, wall_ref="W01",
            )

        self.assertEqual(len(result["instances"]), 2)
        for inst in result["instances"]:
            self.assertTrue(inst.deduct)
        expected_net = round(30.0 - 2 * 0.82 * 2.10, 4)
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], expected_net, places=4
        )


# ============================================================================
# Benchmark 20: Gap opening — generic type, no schedule → review
# ============================================================================
class TestBenchmark_GapNoSchedule(unittest.TestCase):
    """Gap candidate without schedule → no dims → review."""

    def test_gap_review(self):
        gap = _b1_candidate(
            mark="", wall="W01", position=2.0, width=0.50,
            opening_type="opening",
            geom_conf=0.75, assoc_conf=0.70,
        )

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([gap], gap_count=1),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        inst = result["instances"][0]
        self.assertTrue(inst.reconciliation_complete)
        self.assertFalse(inst.deduct)
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], 20.0, places=4
        )


# ============================================================================
# Benchmark 21: Three doors, one conflicts — two deduct
# ============================================================================
class TestBenchmark_ThreeDoorsOneConflict(unittest.TestCase):
    """Three doors: two independent + one conflicts → two deduct."""

    def test_partial_deduction(self):
        from pb_opening_schedule_v171 import ScheduleEntry

        d1 = _b1_candidate(
            mark="D01", wall="W01", position=1.0, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
            sig=(1, 100.0, 200.0, 0.82, "door"),
        )
        # d2 and d3 at same position → mark conflict
        d2 = _b1_candidate(
            mark="D01", wall="W01", position=4.0, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
            sig=(1, 400.0, 200.0, 0.82, "door"),
        )
        d3 = _b1_candidate(
            mark="D02", wall="W01", position=4.0, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
            sig=(1, 400.0, 200.0, 0.82, "door"),
        )
        schedule = [
            ScheduleEntry(
                type_mark="D01", width_mm=820, height_mm=2100,
                count=4, page_no=2,
                parse_source="header_separate",
                dimension_basis="rough_opening",
                basis_source="Rough Opening",
            ),
            ScheduleEntry(
                type_mark="D02", width_mm=820, height_mm=2100,
                count=1, page_no=2,
                parse_source="header_separate",
                dimension_basis="rough_opening",
                basis_source="Rough Opening",
            ),
        ]

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([d1, d2, d3]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                schedule_entries=schedule,
                gross_wall_m2=25.0, wall_ref="W01",
            )

        # d1 deducts, d2+d3 in review
        deducted = [i for i in result["instances"] if i.deduct]
        review = [i for i in result["instances"]
                  if i.deduction_status == DEDUCTION_REVIEW]
        self.assertEqual(len(deducted), 1)
        self.assertEqual(len(review), 2)
        expected_net = round(25.0 - 0.82 * 2.10, 4)
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], expected_net, places=4
        )


# ============================================================================
# Benchmark 22: Evidence confidence field preserved
# ============================================================================
class TestBenchmark_ConfidencePreserved(unittest.TestCase):
    """Confidence values survive the full pipeline."""

    def test_confidence_preserved(self):
        door = _b1_candidate(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.88, assoc_conf=0.77,
        )

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([door]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        inst = result["instances"][0]
        self.assertAlmostEqual(inst.geometry_confidence, 0.88, places=2)
        self.assertAlmostEqual(inst.association_confidence, 0.77, places=2)


# ============================================================================
# Benchmark 23: No gross wall → no net_wall in result
# ============================================================================
class TestBenchmark_NoGrossWall(unittest.TestCase):
    """Without gross_wall_m2, net_wall is absent from result."""

    def test_no_gross(self):
        door = _b1_candidate(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
        )

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([door]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
            )

        self.assertNotIn("net_wall", result)


# ============================================================================
# Benchmark 24: Multiple wall refs — each wall independent
# ============================================================================
class TestBenchmark_MultiWall(unittest.TestCase):
    """Doors on different walls are independent deductions."""

    def test_multi_wall(self):
        from pb_opening_schedule_v171 import ScheduleEntry

        d1 = _b1_candidate(
            mark="D01", wall="W01", position=1.0, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
            sig=(1, 100.0, 200.0, 0.82, "door"),
        )
        d2 = _b1_candidate(
            mark="D02", wall="W02", position=2.0, width=0.90,
            geom_conf=0.95, assoc_conf=0.95,
            sig=(1, 600.0, 200.0, 0.90, "door"),
        )
        schedule = [
            ScheduleEntry(
                type_mark="D01", width_mm=820, height_mm=2100,
                count=4, page_no=2,
                parse_source="header_separate",
                dimension_basis="rough_opening",
                basis_source="Rough Opening",
            ),
            ScheduleEntry(
                type_mark="D02", width_mm=900, height_mm=2100,
                count=1, page_no=2,
                parse_source="header_separate",
                dimension_basis="rough_opening",
                basis_source="Rough Opening",
            ),
        ]

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([d1, d2]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                schedule_entries=schedule,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        # Both should deduct
        for inst in result["instances"]:
            self.assertTrue(inst.deduct)
        # W01 net only subtracts W01 opening (D01: 0.82 × 2.10)
        expected_net = round(20.0 - 0.82 * 2.10, 4)
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], expected_net, places=4
        )


# ============================================================================
# Benchmark 25: B4 conflicts recorded
# ============================================================================
class TestBenchmark_ConflictsRecorded(unittest.TestCase):
    """Physical instance conflicts appear in result['conflicts']."""

    def test_conflicts_in_result(self):
        a = _b1_candidate(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
            sig=(1, 150.0, 200.0, 0.82, "door"),
        )
        b = _b1_candidate(
            mark="D02", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
            sig=(1, 150.0, 200.0, 0.82, "door"),
        )

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([a, b]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        self.assertGreater(len(result["conflicts"]), 0)
        pic = [c for c in result["conflicts"]
               if c.conflict_type == "physical_instance_conflict"]
        self.assertGreaterEqual(len(pic), 2)


# ============================================================================
# Benchmark 26: Deducted count in notes
# ============================================================================
class TestBenchmark_DeductedCountInNotes(unittest.TestCase):
    """Pipeline notes include deducted count."""

    def test_notes_count(self):
        from pb_opening_schedule_v171 import ScheduleEntry

        door = _b1_candidate(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
        )
        schedule = [ScheduleEntry(
            type_mark="D01", width_mm=820, height_mm=2100,
            count=4, page_no=2,
            parse_source="header_separate",
            dimension_basis="rough_opening",
            basis_source="Rough Opening",
        )]

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([door]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                schedule_entries=schedule,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        b5_note = [n for n in result["pipeline_notes"] if n.startswith("B5:")]
        self.assertEqual(len(b5_note), 1)
        self.assertIn("1/1", b5_note[0])


# ============================================================================
# Benchmark 27: Door with schedule dims matching B1 width
# ============================================================================
class TestBenchmark_WidthConsistency(unittest.TestCase):
    """Schedule width matches B1 width → consistent dimensions."""

    def test_width_consistent(self):
        from pb_opening_schedule_v171 import ScheduleEntry

        door = _b1_candidate(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
        )
        schedule = [ScheduleEntry(
            type_mark="D01", width_mm=820, height_mm=2100,
            count=4, page_no=2,
            parse_source="header_separate",
            dimension_basis="rough_opening",
            basis_source="Rough Opening",
        )]

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([door]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                schedule_entries=schedule,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        inst = result["instances"][0]
        self.assertAlmostEqual(inst.width_m, 0.82, places=2)


# ============================================================================
# Benchmark 28: End-to-end scoring — deduction_decision separate field
# ============================================================================
class TestBenchmark_DeductionDecisionField(unittest.TestCase):
    """deduction_decision is "deducted" or "not_deducted", never empty after B5."""

    def test_decision_field_populated(self):
        from pb_opening_schedule_v171 import ScheduleEntry

        door = _b1_candidate(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
        )
        schedule = [ScheduleEntry(
            type_mark="D01", width_mm=820, height_mm=2100,
            count=4, page_no=2,
            parse_source="header_separate",
            dimension_basis="rough_opening",
            basis_source="Rough Opening",
        )]

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([door]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                schedule_entries=schedule,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        inst = result["instances"][0]
        self.assertIn(inst.deduction_decision, ("deducted", "not_deducted"))


# ============================================================================
# Basis-differentiation tests — the core safety property
# ============================================================================
class TestBenchmark_GenericScheduleNoDeduct(unittest.TestCase):
    """Generic schedule dims (Width/Height) → basis unknown → no deduction.

    Knowing an opening's width and height is NOT the same thing as knowing
    those dimensions represent the wall void.
    """

    def test_generic_width_height_no_deduction(self):
        from pb_opening_schedule_v171 import ScheduleEntry

        door = _b1_candidate(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
        )
        # Generic "Width/Height" heading — no basis provenance
        schedule = [ScheduleEntry(
            type_mark="D01", width_mm=820, height_mm=2100,
            count=4, page_no=2,
            parse_source="header_separate",
            dimension_basis="",  # unknown — generic heading
            basis_source="",
        )]

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([door]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                schedule_entries=schedule,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        inst = result["instances"][0]
        self.assertTrue(inst.reconciliation_complete)
        # Basis stays unknown — dims enriched but not eligible
        self.assertEqual(inst.dimension_basis, "unknown")
        self.assertFalse(inst.deduct)
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], 20.0, places=4
        )


class TestBenchmark_RoughOpeningScheduleDeducts(unittest.TestCase):
    """Explicit 'Rough Opening' schedule heading → basis rough_opening → eligible."""

    def test_rough_opening_enables_deduction(self):
        from pb_opening_schedule_v171 import ScheduleEntry

        door = _b1_candidate(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
        )
        schedule = [ScheduleEntry(
            type_mark="D01", width_mm=820, height_mm=2100,
            count=4, page_no=2,
            parse_source="header_separate",
            dimension_basis="rough_opening",
            basis_source="Rough Opening Width",
        )]

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([door]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                schedule_entries=schedule,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        inst = result["instances"][0]
        self.assertTrue(inst.reconciliation_complete)
        self.assertEqual(inst.dimension_basis, "rough_opening")
        self.assertTrue(inst.deduct)
        expected_net = round(20.0 - 0.82 * 2.10, 4)
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], expected_net, places=4
        )


class TestBenchmark_FrameScheduleNoDeduct(unittest.TestCase):
    """Frame size schedule → basis frame → no wall-void deduction."""

    def test_frame_basis_blocks_deduction(self):
        from pb_opening_schedule_v171 import ScheduleEntry

        door = _b1_candidate(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
        )
        schedule = [ScheduleEntry(
            type_mark="D01", width_mm=820, height_mm=2100,
            count=4, page_no=2,
            parse_source="header_separate",
            dimension_basis="frame",
            basis_source="Frame Size",
        )]

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([door]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                schedule_entries=schedule,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        inst = result["instances"][0]
        self.assertTrue(inst.reconciliation_complete)
        # Frame dims are NOT wall-void dims
        self.assertEqual(inst.dimension_basis, "frame")
        self.assertFalse(inst.deduct)
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], 20.0, places=4
        )


class TestBenchmark_LeafScheduleNoDeduct(unittest.TestCase):
    """Leaf size schedule → basis leaf → no wall-void deduction."""

    def test_leaf_basis_blocks_deduction(self):
        from pb_opening_schedule_v171 import ScheduleEntry

        door = _b1_candidate(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
        )
        schedule = [ScheduleEntry(
            type_mark="D01", width_mm=820, height_mm=2100,
            count=4, page_no=2,
            parse_source="header_separate",
            dimension_basis="leaf",
            basis_source="Leaf Width",
        )]

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([door]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                schedule_entries=schedule,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        inst = result["instances"][0]
        self.assertTrue(inst.reconciliation_complete)
        self.assertEqual(inst.dimension_basis, "leaf")
        self.assertFalse(inst.deduct)
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], 20.0, places=4
        )


class TestBenchmark_ClearOpeningScheduleNoDeduct(unittest.TestCase):
    """Clear opening schedule → basis clear_opening → no wall-void deduction."""

    def test_clear_opening_basis_blocks_deduction(self):
        from pb_opening_schedule_v171 import ScheduleEntry

        door = _b1_candidate(
            mark="D01", wall="W01", position=1.5, width=0.82,
            geom_conf=0.95, assoc_conf=0.95,
        )
        schedule = [ScheduleEntry(
            type_mark="D01", width_mm=820, height_mm=2100,
            count=4, page_no=2,
            parse_source="header_separate",
            dimension_basis="clear_opening",
            basis_source="Clear Opening Width",
        )]

        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([door]),
        ):
            result = run_opening_pipeline(
                segments=[], words=[], page_no=1,
                schedule_entries=schedule,
                gross_wall_m2=20.0, wall_ref="W01",
            )

        inst = result["instances"][0]
        self.assertTrue(inst.reconciliation_complete)
        self.assertEqual(inst.dimension_basis, "clear_opening")
        self.assertFalse(inst.deduct)
        self.assertAlmostEqual(
            result["net_wall"]["net_area_m2"], 20.0, places=4
        )


if __name__ == "__main__":
    unittest.main()
