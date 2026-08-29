"""B3 benchmark expansion — independent-truth fixture suite (v1.7.8).

Benchmarks the opening-evidence pipeline (B1 detection -> B5 deduction
gate) against committed, independent synthetic-plan truth workspaces
under tests/fixtures/.  The fixtures are generated deterministically by
the committed helper script `benchmark_fixtures.py` at the repository
root; detector output NEVER defines what truth is.

Scenarios:
  1. bench_ground_floor       — one door + one window on a ground-floor
                                plan; positions/widths/marks must resolve
                                and reconcile against the committed truth.
  2. bench_multi_window_wall  — three independently-tagged windows on one
                                wall (no tie-ambiguity) PLUS a deliberately
                                ambiguous hatch-like batten region that
                                must be conservatively rejected (zero
                                candidates, never a false positive).
  3. bench_envelope_schedule  — plan-derived W01/W02 evidence enriched by
                                a door/window schedule with GENERIC
                                width/height headings.  Enrichment upgrades
                                dimensions but must NOT upgrade the basis
                                to rough_opening, and `deduct` must stay
                                False — exactly the LAGO safety authority:
                                generic schedule dimensions alone never
                                create an automatic wall-void deduction.

Safety contract verified:
  - Ambiguity stays review; conservative rejection is acceptable.
  - A confident false positive / false subtraction is NEVER acceptable.
  - No B1-B4 stage ever sets deduct=True; only the B5 gate may decide,
    and the committed fixtures never cross that gate.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from pb_opening_deduction_v174 import apply_deductions
from pb_opening_evidence_v170 import (
    DEDUCTION_DERIVED_ELIGIBLE,
    DEDUCTION_REVIEW,
    DIMENSION_BASIS_ROUGH_OPENING,
    DIMENSION_BASIS_UNKNOWN,
    OpeningEvidence,
)
from pb_opening_reconciliation_v173 import (
    ConflictRecord,
    reconcile_opening_evidence,
)
from pb_opening_schedule_v171 import (
    ScheduleEntry,
    enrich_opening_evidence,
    parse_schedule_rows,
)
from pb_plan_opening_detection_v171 import (
    Segment,
    TextWord,
    WallLine,
    plan_opening_candidates,
)

POSITION_TOLERANCE_M = 0.20    # same as TOLERANCE_POSITION_M in B0
WIDTH_TOLERANCE_M = 0.01       # detection rounds to 1 mm

FIXTURES = [
    "bench_ground_floor",
    "bench_multi_window_wall",
    "bench_envelope_schedule",
]


def _fixture_path(name: str) -> Path:
    return Path(__file__).resolve().parent / "fixtures" / f"{name}.json"


def _load_fixture(name: str) -> Dict[str, Any]:
    with _fixture_path(name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _segments(fixture: Dict[str, Any]) -> List[Segment]:
    return [
        Segment(
            x1=row["x1"], y1=row["y1"], x2=row["x2"], y2=row["y2"],
            drawing_index=row.get("drawing_index", 0),
        )
        for row in fixture["geometry"]["segments"]
    ]


def _words(fixture: Dict[str, Any]) -> List[TextWord]:
    return [
        TextWord(
            text=row["text"], x0=row["x0"], y0=row["y0"],
            x1=row["x1"], y1=row["y1"], page_no=row.get("page_no", 0),
        )
        for row in fixture["geometry"]["words"]
    ]


def _wall_lines(fixture: Dict[str, Any]) -> List[WallLine]:
    return [
        WallLine(
            segment=Segment(x1=r["x1"], y1=r["y1"], x2=r["x2"], y2=r["y2"]),
            wall_ref=r["wall_ref"],
        )
        for r in fixture["geometry"]["wall_lines"]
    ]


def _run_b1(fixture: Dict[str, Any]):
    return plan_opening_candidates(
        segments=_segments(fixture),
        words=_words(fixture),
        wall_lines=_wall_lines(fixture),
        scale_px_per_m=fixture["workspace"]["scale_pt_per_m"],
        page_no=fixture["workspace"]["page_no"],
    )


def _schedule_entries_from_fixture(fixture: Dict[str, Any]) -> List[ScheduleEntry]:
    schedule = fixture["session"]["schedule"]
    rows = [
        {"text": "\t".join(schedule["header"]), "bbox": (0, 0, 100, 20)},
    ]
    for cells in schedule["rows"]:
        rows.append({"text": "\t".join(cells), "bbox": (0, 0, 100, 20)})
    return parse_schedule_rows(rows, page_no=schedule["page_no"])


def _run_pipeline(fixture: Dict[str, Any], schedule_entries: Optional[List[ScheduleEntry]] = None):
    """B1 detection -> B2 schedule enrichment -> B4 reconciliation -> B5 gate."""
    b1 = _run_b1(fixture)
    enriched = enrich_opening_evidence(list(b1.candidates), schedule_entries or [])
    reconciled, conflicts = reconcile_opening_evidence(enriched)
    for inst in reconciled:
        inst.compute_deduction_status()
    deducted = apply_deductions(reconciled)
    return b1, enriched, reconciled, deducted, conflicts


def _matching_candidates(candidates: List[OpeningEvidence], mark: str) -> List[OpeningEvidence]:
    return [c for c in candidates if c.type_mark == mark]


def _assert_truth_openings_satisfied(testcase: unittest.TestCase, candidates: List[OpeningEvidence],
                                     truth_openings: List[Dict[str, Any]]) -> None:
    """Every committed-truth opening must be matched by EXACTLY ONE candidate.

    Expected values are read from the fixture's independent truth only;
    no detector-derived quantity feeds the expectation.
    """
    for truth in truth_openings:
        mark = truth["mark"]
        matched = _matching_candidates(candidates, mark)
        testcase.assertEqual(len(matched), 1, f"truth mark {mark} must resolve to exactly one candidate")
        inst = matched[0]
        testcase.assertEqual(inst.opening_type, truth["opening_type"], mark)
        testcase.assertEqual(inst.wall_ref, truth["wall_ref"], mark)
        if truth.get("width_m") is not None:
            testcase.assertAlmostEqual(inst.width_m or 0.0, truth["width_m"],
                                       delta=WIDTH_TOLERANCE_M, msg=mark)
        testcase.assertAlmostEqual(inst.position_along_wall_m or 0.0,
                                   truth["position_along_wall_m"],
                                   delta=POSITION_TOLERANCE_M, msg=mark)


class TestB3_FixturePersistence(unittest.TestCase):
    """The committed fixtures must exist on disk and be canonical JSON.

    Canonical form is exactly what benchmark_fixtures.py writes
    (sort_keys=True, indent=2, LF-terminated), which is what makes the
    generation deterministic and byte-identical across runs.
    """

    def test_all_fixture_files_exist_on_disk(self):
        for name in FIXTURES:
            self.assertTrue(_fixture_path(name).exists(), name)

    def test_fixture_files_are_canonical_deterministic_json(self):
        for name in FIXTURES:
            with _fixture_path(name).open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            canonical = json.dumps(data, sort_keys=True, indent=2) + "\n"
            on_disk = _fixture_path(name).read_text(encoding="utf-8")
            self.assertEqual(on_disk, canonical, name)

    def test_fixtures_carry_independent_truth(self):
        for name in FIXTURES:
            fixture = _load_fixture(name)
            self.assertEqual(fixture["workspace"]["id"], name)
            truth = fixture["truth"]
            self.assertIn("authority", truth)
            self.assertIn(fixture["workspace"]["scale_pt_per_m"],
                          (50.0,), msg=name)


class TestB3_Scenario1_GroundFloor(unittest.TestCase):
    """One door + one window on a ground-floor plan."""

    def setUp(self):
        self.fixture = _load_fixture("bench_ground_floor")

    def test_detection_counts_match_committed_truth(self):
        result = _run_b1(self.fixture)
        counts = self.fixture["truth"]["expected_candidate_counts"]
        self.assertEqual(result.door_count, counts["door"])
        self.assertEqual(result.window_count, counts["window"])
        self.assertEqual(result.gap_count, counts["gap"])
        self.assertEqual(len(result.candidates),
                         counts["door"] + counts["window"] + counts["gap"])
        self.assertGreater(result.wall_lines_found, 0)

    def test_door_and_window_match_committed_truth(self):
        result = _run_b1(self.fixture)
        truth = self.fixture["truth"]
        _assert_truth_openings_satisfied(self, result.candidates, truth["doors"])
        _assert_truth_openings_satisfied(self, result.candidates, truth["windows"])
        self.assertEqual(len(truth["gaps"]), 0)

    def test_candidates_are_plan_vector_and_unknown_basis(self):
        result = _run_b1(self.fixture)
        for candidate in result.candidates:
            self.assertEqual(candidate.dimension_basis, DIMENSION_BASIS_UNKNOWN)
            self.assertEqual(candidate.dimension_source, "plan_vector")
            self.assertEqual(candidate.page_no, self.fixture["workspace"]["page_no"])
            self.assertFalse(candidate.deduct)

    def test_pipeline_stays_review_and_never_deducts(self):
        _, _, reconciled, deducted, conflicts = _run_pipeline(self.fixture)
        self.assertEqual(len(reconciled), 2)
        self.assertEqual(conflicts, [])
        for inst in deducted:
            self.assertTrue(inst.reconciliation_complete)
            self.assertEqual(inst.deduction_status, DEDUCTION_REVIEW)
            self.assertFalse(inst.deduct)
            self.assertEqual(inst.deduction_decision, "not_deducted")


class TestB3_Scenario2_MultiWindowWall(unittest.TestCase):
    """Three windows on one wall, resolved without tie-ambiguity, plus a
    hatch-like batten region that must be conservatively rejected."""

    def setUp(self):
        self.fixture = _load_fixture("bench_multi_window_wall")

    def test_three_window_detection_counts(self):
        result = _run_b1(self.fixture)
        counts = self.fixture["truth"]["expected_candidate_counts"]
        self.assertEqual(result.window_count, counts["window"])
        self.assertEqual(result.door_count, counts["door"])
        self.assertEqual(result.gap_count, counts["gap"])

    def test_each_window_matches_committed_truth_without_tie_ambiguity(self):
        result = _run_b1(self.fixture)
        truth = self.fixture["truth"]
        _assert_truth_openings_satisfied(self, result.candidates, truth["windows"])
        # Every detected window carries its own distinct mark — no two
        # physical windows collapse onto one mark and no mark is shared.
        marks = [c.type_mark for c in result.candidates]
        self.assertEqual(len(marks), len(set(marks)))
        self.assertEqual(set(marks), {"W01", "W02", "W03"})

    def test_hatch_like_region_is_conservatively_rejected(self):
        result = _run_b1(self.fixture)
        for region in self.fixture["truth"]["rejected_regions"]:
            on_region = [c for c in result.candidates
                         if c.wall_ref == region["wall_ref"]]
            self.assertEqual(len(on_region), region["expected_candidates"],
                             region["wall_ref"])
            self.assertEqual(region["expected_candidates"], 0)
        # Geometry alone (batten repetition) must not fabricate an opening.
        self.assertEqual(result.window_count, 3)

    def test_no_false_doors_or_gaps_from_hatch_wall(self):
        result = _run_b1(self.fixture)
        self.assertEqual(result.door_count, 0)
        self.assertEqual(result.gap_count, 0)

    def test_pipeline_stays_review_and_never_deducts(self):
        _, _, reconciled, deducted, conflicts = _run_pipeline(self.fixture)
        self.assertEqual(len(reconciled), 3)
        self.assertEqual(conflicts, [])
        for inst in deducted:
            self.assertEqual(inst.deduction_status, DEDUCTION_REVIEW)
            self.assertFalse(inst.deduct)


class TestB3_Scenario3_EnvelopeSchedule(unittest.TestCase):
    """Plan-derived W01/W02 evidence enriched by a door/window schedule.

    The committed schedule uses GENERIC width/height headings.  Per the
    LAGO safety authority, enrichment may populate dimensions but must
    never upgrade the basis to rough_opening, and `deduct` must stay
    False — no automatic void deduction from generic schedule dimensions.
    """

    def setUp(self):
        self.fixture = _load_fixture("bench_envelope_schedule")
        self.entries = _schedule_entries_from_fixture(self.fixture)

    def test_plan_detects_w01_and_w02(self):
        result = _run_b1(self.fixture)
        counts = self.fixture["truth"]["expected_candidate_counts"]
        self.assertEqual(result.window_count, counts["window"])
        _assert_truth_openings_satisfied(self, result.candidates,
                                         self.fixture["truth"]["windows"])

    def test_generic_schedule_parses_with_unknown_basis(self):
        self.assertEqual([(e.type_mark, e.width_mm, e.height_mm) for e in self.entries],
                         [("W01", 900, 1500), ("W02", 900, 1200)])
        for entry in self.entries:
            self.assertEqual(entry.dimension_basis, "")

    def test_schedule_enrichment_populates_dimensions_not_basis(self):
        b1 = _run_b1(self.fixture)
        enriched = enrich_opening_evidence(list(b1.candidates), self.entries)
        self.assertEqual(len(enriched), 2)
        for inst in enriched:
            self.assertEqual(inst.dimension_source, "schedule_parse")
            self.assertAlmostEqual(inst.width_m, 0.9, delta=WIDTH_TOLERANCE_M)
            if inst.type_mark == "W01":
                self.assertAlmostEqual(inst.height_m, 1.5, delta=0.01)
            else:
                self.assertAlmostEqual(inst.height_m, 1.2, delta=0.01)
            # LAGO safety authority: generic headings prove nothing about
            # the wall void, so the basis must stay unknown.
            self.assertEqual(inst.dimension_basis, DIMENSION_BASIS_UNKNOWN)

    def test_reconciliation_flags_basis_ambiguous(self):
        b1 = _run_b1(self.fixture)
        enriched = enrich_opening_evidence(list(b1.candidates), self.entries)
        reconciled, conflicts = reconcile_opening_evidence(enriched)
        self.assertEqual(len(conflicts), 2)
        for conflict in conflicts:
            self.assertEqual(conflict.conflict_type, "basis_ambiguous")
        for inst in reconciled:
            inst.compute_deduction_status()
            self.assertEqual(inst.deduction_status, DEDUCTION_REVIEW)

    def test_deduct_stays_false_with_generic_schedule(self):
        _, _, _, deducted, _ = _run_pipeline(self.fixture, self.entries)
        self.assertEqual(len(deducted), 2)
        for inst in deducted:
            self.assertFalse(inst.deduct)
            self.assertEqual(inst.deduction_decision, "not_deducted")
        self.assertEqual(sum(i.area_m2 for i in deducted if i.deduct), 0.0)

    def test_explicit_rough_opening_headings_can_raise_eligibility(self):
        """Capability proof: an EXPLICIT 'rough opening width/height'
        schedule upgrades the basis to rough_opening and reaches
        derived_eligible.  The committed generic-heading fixture never
        takes this path, and eligibility is still NOT a deduction — the
        B5/estimator decision stays False here (no B5 approval applied).
        """
        rows = [
            {"text": "\t".join(["mark", "RO Width", "RO Height"]), "bbox": (0, 0, 100, 20)},
            {"text": "W01\t900\t1500", "bbox": (0, 0, 100, 20)},
            {"text": "W02\t900\t1200", "bbox": (0, 0, 100, 20)},
        ]
        ro_entries = parse_schedule_rows(rows, page_no=self.fixture["workspace"]["page_no"])
        self.assertEqual([e.dimension_basis for e in ro_entries],
                         [DIMENSION_BASIS_ROUGH_OPENING, DIMENSION_BASIS_ROUGH_OPENING])

        b1 = _run_b1(self.fixture)
        enriched = enrich_opening_evidence(list(b1.candidates), ro_entries)
        for inst in enriched:
            self.assertEqual(inst.dimension_basis, DIMENSION_BASIS_ROUGH_OPENING)
            self.assertEqual(inst.dimension_source, "schedule_parse")

        reconciled, conflicts = reconcile_opening_evidence(enriched)
        self.assertEqual(conflicts, [])
        for inst in reconciled:
            inst.compute_deduction_status()
            self.assertEqual(inst.deduction_status, DEDUCTION_DERIVED_ELIGIBLE)
            self.assertGreaterEqual(inst.reconciliation_confidence, 0.75)
            # Eligibility is a gate, not a decision: B5/estimator approval
            # has not been applied, so deduct remains False.
            self.assertFalse(inst.deduct)


class TestB3_RejectedRegionNeverDeducts(unittest.TestCase):
    """Cross-scenario safety: hatch-only evidence must never yield a
    deduction through the full B1->B5 pipeline."""

    def test_all_pipeline_instances_across_committed_fixtures_are_non_deducting(self):
        for name in FIXTURES:
            fixture = _load_fixture(name)
            entries = _schedule_entries_from_fixture(fixture) if "session" in fixture else None
            b1, _, _, deducted, _ = _run_pipeline(fixture, entries)
            self.assertGreater(len(b1.candidates), 0, name)
            for inst in deducted:
                self.assertFalse(inst.deduct, f"{name}:{inst.type_mark}")


if __name__ == "__main__":
    unittest.main()