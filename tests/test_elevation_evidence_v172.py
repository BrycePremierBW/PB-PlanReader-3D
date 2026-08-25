"""Tests for pb_elevation_evidence_v172 — B3 elevation evidence correlation.

Covers:
  - ElevationOpening dataclass (sill/head optional)
  - Opening detection from elevation rectangles (sizing, scale-aware labels)
  - Correlation scoring (conflicting marks, strong signal requirement)
  - Global order-independent assignment
  - Enrichment via merge_opening_evidence (basis=unknown, not rough_opening)
  - Safety rules: no new instances, no deductions, no mark assignment
  - Edge cases: empty inputs, unmatched, conflicting data
"""
from __future__ import annotations

import unittest

from pb_opening_evidence_v170 import (
    OpeningEvidence,
    merge_opening_evidence,
    DIMENSION_BASIS_UNKNOWN,
    DEDUCTION_REVIEW,
    NON_INSTANCE_SOURCES,
    OPENING_TYPE_DOOR,
    OPENING_TYPE_WINDOW,
)
from pb_elevation_evidence_v172 import (
    VERSION,
    ElevationOpening,
    detect_elevation_openings,
    correlate_elevation_to_plan,
    _enrich_from_elevation,
    _is_opening_sized,
    _width_compatible,
    _mark_compatible,
    _marks_conflict,
    _opening_type_conflicts,
    _correlation_score,
    _extract_label_near_rect,
    ELEVATION_WIDTH_TOLERANCE_M,
    ELEVATION_HEIGHT_CONFIDENCE,
    _LABEL_SEARCH_RADIUS_M,
    _MIN_STRONG_SIGNAL,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _rect(x0=100, y0=100, x1=300, y1=500):
    """Create a rect dict with bbox."""
    return {"bbox": [x0, y0, x1, y1], "confidence": 0.6}


def _word(text, x0=0, y0=0, x1=50, y1=20):
    """Create a word dict matching PDF extraction format."""
    return {"0": x0, "1": y0, "2": x1, "3": y1, "4": text}


def _inst(
    mark="D01",
    wall_ref="N01",
    width=0.82,
    height=None,
    side="North",
    pos=2.5,
    method="plan_vector",
    dim_source="plan_vector",
    basis=DIMENSION_BASIS_UNKNOWN,
):
    """Create a minimal B1-style OpeningEvidence for testing."""
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
        geometry_confidence=0.6,
        dimension_confidence=0.0 if height is None else 0.4,
    )
    return ev


def _elev(
    side="North",
    page=5,
    w=0.82,
    h=2.1,
    label="",
    bbox=(100, 100, 300, 500),
    conf=0.6,
):
    """Create a minimal ElevationOpening for testing."""
    return ElevationOpening(
        elevation_page_no=page,
        elevation_side=side,
        bbox_px=bbox,
        width_m=w,
        height_m=h,
        label=label,
        confidence=conf,
    )


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------
class TestVersion(unittest.TestCase):
    def test_version(self):
        self.assertEqual(VERSION, "1.7.2")


class TestElevationOpeningDataclass(unittest.TestCase):
    """ElevationOpening frozen dataclass."""

    def test_frozen(self):
        e = _elev()
        with self.assertRaises(AttributeError):
            e.width_m = 1.0  # type: ignore[misc]

    def test_defaults(self):
        e = ElevationOpening(
            elevation_page_no=1,
            elevation_side="North",
            bbox_px=(0, 0, 100, 200),
            width_m=0.8,
            height_m=2.0,
        )
        self.assertEqual(e.label, "")
        self.assertIsNone(e.sill_m)
        self.assertIsNone(e.head_m)
        self.assertAlmostEqual(e.confidence, 0.5, places=1)


class TestIsOpeningSized(unittest.TestCase):
    """_is_opening_sized() — size filter for elevation rectangles."""

    def test_valid_door(self):
        self.assertTrue(_is_opening_sized(0.82, 2.1))

    def test_valid_window(self):
        self.assertTrue(_is_opening_sized(1.2, 1.5))

    def test_too_narrow(self):
        self.assertFalse(_is_opening_sized(0.15, 2.0))

    def test_too_wide(self):
        self.assertFalse(_is_opening_sized(7.0, 2.0))

    def test_too_short(self):
        self.assertFalse(_is_opening_sized(0.82, 0.15))

    def test_too_tall(self):
        self.assertFalse(_is_opening_sized(0.82, 6.0))


class TestWidthCompatible(unittest.TestCase):
    """_width_compatible() — cross-view width agreement."""

    def test_exact(self):
        self.assertTrue(_width_compatible(0.82, 0.82))

    def test_within_tolerance(self):
        self.assertTrue(_width_compatible(0.82, 0.90))

    def test_outside_tolerance(self):
        self.assertFalse(_width_compatible(0.82, 1.20))

    def test_one_none(self):
        self.assertTrue(_width_compatible(0.82, None))
        self.assertTrue(_width_compatible(None, 0.82))

    def test_both_none(self):
        self.assertTrue(_width_compatible(None, None))


class TestMarkCompatible(unittest.TestCase):
    """_mark_compatible() — type mark agreement."""

    def test_exact(self):
        self.assertTrue(_mark_compatible("D01", "D01"))

    def test_case_insensitive(self):
        self.assertTrue(_mark_compatible("d01", "D01"))

    def test_mismatch(self):
        self.assertFalse(_mark_compatible("D01", "W01"))

    def test_one_empty(self):
        self.assertTrue(_mark_compatible("D01", ""))
        self.assertTrue(_mark_compatible("", "D01"))

    def test_both_empty(self):
        self.assertTrue(_mark_compatible("", ""))


class TestMarksConflict(unittest.TestCase):
    """_marks_conflict() — exact mark mismatch detection."""

    def test_same_mark(self):
        self.assertFalse(_marks_conflict("D01", "D01"))

    def test_different_mark_same_family(self):
        """D01 vs D02 → conflict (marks are type identity)."""
        self.assertTrue(_marks_conflict("D01", "D02"))

    def test_different_mark_cross_family(self):
        self.assertTrue(_marks_conflict("D01", "W01"))

    def test_one_empty(self):
        self.assertFalse(_marks_conflict("D01", ""))
        self.assertFalse(_marks_conflict("", "W01"))

    def test_both_empty(self):
        self.assertFalse(_marks_conflict("", ""))

    def test_case_insensitive(self):
        self.assertFalse(_marks_conflict("d01", "D01"))
        self.assertTrue(_marks_conflict("d01", "D02"))


class TestCorrelationScore(unittest.TestCase):
    """_correlation_score() — matching quality with identity-signal gate."""

    def test_perfect_match(self):
        """Side + width + exact mark → strong score."""
        inst = _inst(mark="D01", side="North", width=0.82)
        elev = _elev(side="North", w=0.82, label="D01")
        sc = _correlation_score(inst, elev)
        self.assertGreater(sc, 0.6)

    def test_wrong_side(self):
        inst = _inst(side="North")
        elev = _elev(side="South")
        sc = _correlation_score(inst, elev)
        self.assertEqual(sc, 0.0)

    def test_incompatible_width(self):
        inst = _inst(width=0.82)
        elev = _elev(side="North", w=1.50)
        sc = _correlation_score(inst, elev)
        self.assertEqual(sc, 0.0)

    def test_same_family_marks_conflict(self):
        """D01 vs D02 → score 0 (marks are type identity)."""
        inst = _inst(mark="D01", side="North", width=0.82)
        elev = _elev(side="North", w=0.82, label="D02")
        sc = _correlation_score(inst, elev)
        self.assertEqual(sc, 0.0)

    def test_cross_family_marks_conflict(self):
        """D01 vs W01 → score 0."""
        inst = _inst(mark="D01", side="North", width=0.82)
        elev = _elev(side="North", w=0.82, label="W01")
        sc = _correlation_score(inst, elev)
        self.assertEqual(sc, 0.0)

    def test_width_only_rejected(self):
        """Width alone (no side, no mark) → score 0."""
        inst = _inst(width=0.82, side="", mark="")
        elev = _elev(side="", w=0.82, label="")
        sc = _correlation_score(inst, elev)
        self.assertEqual(sc, 0.0)

    def test_width_plus_side_sufficient(self):
        """Width agreement + side match → qualifies."""
        inst = _inst(width=0.82, side="North")
        elev = _elev(side="North", w=0.82)
        sc = _correlation_score(inst, elev)
        self.assertGreater(sc, 0.3)

    def test_width_plus_mark_sufficient(self):
        """Width agreement + exact mark match → qualifies."""
        inst = _inst(mark="D01", width=0.82, side="")
        elev = _elev(side="", w=0.82, label="D01")
        sc = _correlation_score(inst, elev)
        self.assertGreater(sc, 0.3)

    def test_mark_only_no_width_rejected(self):
        """Mark match without width → no match."""
        inst = _inst(mark="D01", width=None, side="")
        elev = _elev(side="", w=0.82, label="D01")
        sc = _correlation_score(inst, elev)
        self.assertEqual(sc, 0.0)

    def test_opening_type_conflict(self):
        """Window instance + door label → rejected."""
        inst = _inst(mark="", side="North", width=0.82,
                     method="plan_vector")
        inst.opening_type = "window"
        elev = _elev(side="North", w=0.82, label="D01")
        sc = _correlation_score(inst, elev)
        self.assertEqual(sc, 0.0)

    def test_label_bonus(self):
        """Same structural match, label agreement adds score."""
        inst1 = _inst(mark="D01", side="North", width=0.82)
        elev1 = _elev(side="North", w=0.82, label="D01")
        inst2 = _inst(mark="D01", side="North", width=0.82)
        elev2 = _elev(side="North", w=0.82, label="")
        sc1 = _correlation_score(inst1, elev1)
        sc2 = _correlation_score(inst2, elev2)
        self.assertGreater(sc1, sc2)


class TestDetectElevationOpenings(unittest.TestCase):
    """detect_elevation_openings() — rectangle detection from elevation."""

    def test_basic_detection(self):
        rects = [_rect(100, 100, 300, 500)]
        result = detect_elevation_openings(5, "North", rects, [], 100.0)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0].width_m, 2.0, places=1)
        self.assertAlmostEqual(result[0].height_m, 4.0, places=1)

    def test_filters_too_small(self):
        rects = [_rect(100, 100, 110, 110)]
        result = detect_elevation_openings(1, "North", rects, [], 100.0)
        self.assertEqual(len(result), 0)

    def test_filters_too_large(self):
        rects = [_rect(0, 0, 800, 600)]
        result = detect_elevation_openings(1, "North", rects, [], 100.0)
        self.assertEqual(len(result), 0)

    def test_sill_head_are_none(self):
        """Sill/head are None without registered elevation datum."""
        rects = [_rect(100, 100, 300, 500)]
        result = detect_elevation_openings(1, "North", rects, [], 100.0)
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0].sill_m)
        self.assertIsNone(result[0].head_m)

    def test_label_near_rect(self):
        rects = [_rect(100, 100, 300, 500)]
        words = [_word("D01", 140, 60, 190, 80)]
        result = detect_elevation_openings(1, "North", rects, words, 100.0)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].label, "D01")

    def test_label_too_far(self):
        rects = [_rect(100, 100, 300, 500)]
        words = [_word("D01", 800, 800, 850, 820)]
        result = detect_elevation_openings(1, "North", rects, words, 100.0)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].label, "")

    def test_zero_scale(self):
        rects = [_rect(100, 100, 300, 500)]
        result = detect_elevation_openings(1, "North", rects, [], 0.0)
        self.assertEqual(len(result), 0)

    def test_no_rects(self):
        result = detect_elevation_openings(1, "North", [], [], 100.0)
        self.assertEqual(len(result), 0)

    def test_missing_bbox(self):
        rects = [{"confidence": 0.5}]
        result = detect_elevation_openings(1, "North", rects, [], 100.0)
        self.assertEqual(len(result), 0)


class TestScaleAwareLabel(unittest.TestCase):
    """Label search radius is physically scaled through scale_px_per_m."""

    def test_same_physical_offset_different_scales(self):
        """Same 0.4m label offset at two scales → same label assignment."""
        # 0.4m offset at 100 px/m = 40px; at 200 px/m = 80px
        words_100 = [_word("D01", 140, 60, 190, 80)]  # 20px above rect top
        words_200 = [_word("D01", 140, 20, 190, 60)]  # 40px above rect top (0.2m at 200px/m)
        rects = [_rect(100, 100, 300, 500)]

        r1 = detect_elevation_openings(1, "North", rects, words_100, 100.0)
        r2 = detect_elevation_openings(1, "North", rects, words_200, 200.0)

        # Both should find the label (within 0.5m physical radius)
        self.assertEqual(r1[0].label, "D01")
        self.assertEqual(r2[0].label, "D01")

    def test_label_outside_physical_radius(self):
        """Label 3m away → not found regardless of scale."""
        words = [_word("D01", 800, 800, 850, 820)]  # far away
        rects = [_rect(100, 100, 300, 500)]
        r = detect_elevation_openings(1, "North", rects, words, 100.0)
        self.assertEqual(r[0].label, "")


class TestCorrelateElevationToPlan(unittest.TestCase):
    """correlate_elevation_to_plan() — global matching + enrichment."""

    def test_basic_match(self):
        """Side + compatible width + matching mark → enrichment."""
        inst = _inst(mark="D01", side="North", width=0.82)
        elev = _elev(side="North", w=0.82, h=2.1, label="D01")
        enriched, unmatched = correlate_elevation_to_plan([elev], [inst])
        self.assertEqual(len(enriched), 1)
        self.assertIsNotNone(enriched[0].height_m)
        self.assertAlmostEqual(enriched[0].height_m, 2.1, places=1)
        self.assertEqual(len(unmatched), 0)

    def test_no_match_wrong_side(self):
        inst = _inst(side="North")
        elev = _elev(side="South")
        enriched, unmatched = correlate_elevation_to_plan([elev], [inst])
        self.assertIsNone(enriched[0].height_m)
        self.assertEqual(len(unmatched), 1)

    def test_no_match_width_mismatch(self):
        inst = _inst(width=0.82)
        elev = _elev(side="North", w=1.50)
        enriched, unmatched = correlate_elevation_to_plan([elev], [inst])
        self.assertIsNone(enriched[0].height_m)
        self.assertEqual(len(unmatched), 1)

    def test_conflicting_marks_no_match(self):
        """D01 plan + W01 elevation → no match."""
        inst = _inst(mark="D01", side="North", width=0.82)
        elev = _elev(side="North", w=0.82, label="W01")
        enriched, unmatched = correlate_elevation_to_plan([elev], [inst])
        self.assertIsNone(enriched[0].height_m)
        self.assertEqual(len(unmatched), 1)

    def test_same_family_marks_no_match(self):
        """D01 plan + D02 elevation → no match."""
        inst = _inst(mark="D01", side="North", width=0.82)
        elev = _elev(side="North", w=0.82, label="D02")
        enriched, unmatched = correlate_elevation_to_plan([elev], [inst])
        self.assertIsNone(enriched[0].height_m)
        self.assertEqual(len(unmatched), 1)

    def test_type_mark_not_populated_from_elevation(self):
        """Blank plan type_mark must NOT be filled by elevation label."""
        inst = _inst(mark="", side="North", width=0.82)
        elev = _elev(side="North", w=0.82, h=2.1, label="D01")
        enriched, _ = correlate_elevation_to_plan([elev], [inst])
        # type_mark must remain blank — correlation is not semantic identity
        self.assertEqual(enriched[0].type_mark, "")

    def test_side_only_no_match(self):
        """Side match without width or mark → no match."""
        inst = _inst(width=None, side="North")
        elev = _elev(side="North", w=0.82)
        enriched, unmatched = correlate_elevation_to_plan([elev], [inst])
        self.assertIsNone(enriched[0].height_m)
        self.assertEqual(len(unmatched), 1)

    def test_nearest_assignment(self):
        """Each elevation matches at most one plan instance."""
        inst1 = _inst(mark="D01", side="North", width=0.82, pos=2.0)
        inst2 = _inst(mark="D01", side="North", width=0.82, pos=4.0)
        elev = _elev(side="North", w=0.82, h=2.1, label="D01")
        enriched, unmatched = correlate_elevation_to_plan([elev], [inst1, inst2])
        heights = [e.height_m for e in enriched if e.height_m is not None]
        self.assertEqual(len(heights), 1)

    def test_global_assignment_order_independent(self):
        """Reversing plan-instance order must assign the same physical opening."""
        inst_a = _inst(mark="D01", side="North", width=0.82, pos=2.0)
        inst_b = _inst(mark="D01", side="North", width=0.82, pos=4.0)
        elev = _elev(side="North", w=0.82, h=2.1, label="D01")

        enriched_fwd, _ = correlate_elevation_to_plan([elev], [inst_a, inst_b])
        enriched_rev, _ = correlate_elevation_to_plan([elev], [inst_b, inst_a])

        # The SAME physical instance (by position) must receive the elevation
        pos_fwd = [e.position_along_wall_m for e in enriched_fwd
                   if e.height_m is not None]
        pos_rev = [e.position_along_wall_m for e in enriched_rev
                   if e.height_m is not None]
        self.assertEqual(pos_fwd, pos_rev)

    def test_empty_inputs(self):
        enriched, unmatched = correlate_elevation_to_plan([], [])
        self.assertEqual(len(enriched), 0)
        self.assertEqual(len(unmatched), 0)

    def test_no_elevations(self):
        inst = _inst()
        enriched, unmatched = correlate_elevation_to_plan([], [inst])
        self.assertEqual(len(enriched), 1)
        self.assertIsNone(enriched[0].height_m)

    def test_no_instances(self):
        elev = _elev()
        enriched, unmatched = correlate_elevation_to_plan([elev], [])
        self.assertEqual(len(enriched), 0)
        self.assertEqual(len(unmatched), 1)

    def test_multiple_elevations_multiple_instances(self):
        """Multiple elevations match multiple instances."""
        inst1 = _inst(mark="D01", side="North", width=0.82, pos=2.0)
        inst2 = _inst(mark="W01", side="North", width=1.20, pos=5.0)
        elev1 = _elev(side="North", w=0.82, h=2.1, label="D01")
        elev2 = _elev(side="North", w=1.20, h=1.5, label="W01")
        enriched, unmatched = correlate_elevation_to_plan(
            [elev1, elev2], [inst1, inst2]
        )
        heights = [e.height_m for e in enriched if e.height_m is not None]
        self.assertEqual(len(heights), 2)
        self.assertEqual(len(unmatched), 0)


class TestEnrichFromElevation(unittest.TestCase):
    """_enrich_from_elevation() — enrichment via merge."""

    def test_sets_height(self):
        inst = _inst(height=None)
        elev = _elev(h=2.1)
        result = _enrich_from_elevation(inst, elev)
        self.assertAlmostEqual(result.height_m, 2.1, places=1)

    def test_sets_dimension_source(self):
        inst = _inst()
        elev = _elev()
        result = _enrich_from_elevation(inst, elev)
        self.assertEqual(result.dimension_source, "elevation_rect")

    def test_basis_stays_unknown(self):
        """Generic elevation_rect does NOT upgrade to rough_opening."""
        inst = _inst(basis=DIMENSION_BASIS_UNKNOWN)
        elev = _elev()
        result = _enrich_from_elevation(inst, elev)
        self.assertEqual(result.dimension_basis, DIMENSION_BASIS_UNKNOWN)

    def test_type_mark_preserved(self):
        """Enrichment preserves existing plan type_mark."""
        inst = _inst(mark="D01")
        elev = _elev(label="D01")
        result = _enrich_from_elevation(inst, elev)
        self.assertEqual(result.type_mark, "D01")

    def test_blank_type_mark_not_filled(self):
        """Blank plan type_mark stays blank after elevation enrichment."""
        inst = _inst(mark="")
        elev = _elev(label="D01")
        result = _enrich_from_elevation(inst, elev)
        self.assertEqual(result.type_mark, "")
        inst = _inst()
        elev = _elev()
        result = _enrich_from_elevation(inst, elev)
        self.assertIsNotNone(result.elevation_geometry)
        self.assertIn("sill_m", result.elevation_geometry)
        self.assertIsNone(result.elevation_geometry["sill_m"])

    def test_sets_elevation_side(self):
        inst = _inst(side="")
        elev = _elev(side="North")
        result = _enrich_from_elevation(inst, elev)
        self.assertEqual(result.elevation_side, "North")

    def test_does_not_mutate_original(self):
        inst = _inst(height=None)
        elev = _elev(h=2.1)
        _enrich_from_elevation(inst, elev)
        self.assertIsNone(inst.height_m)

    def test_uses_merge_opening_evidence(self):
        """Enrichment goes through B0's merge contract."""
        inst = _inst(width=0.82, height=None)
        elev = _elev(w=0.82, h=2.1)
        result = _enrich_from_elevation(inst, elev)
        self.assertIsInstance(result, OpeningEvidence)
        self.assertIsNone(inst.height_m)


class TestSafetyContract(unittest.TestCase):
    """Verify B3 safety boundaries."""

    def test_elevation_source_not_instance(self):
        """elevation_rect is NOT in NON_INSTANCE_SOURCES."""
        self.assertNotIn("elevation_rect", NON_INSTANCE_SOURCES)

    def test_enrichment_never_sets_deduct(self):
        inst = _inst()
        elev = _elev()
        result = _enrich_from_elevation(inst, elev)
        self.assertFalse(result.deduct)

    def test_enrichment_never_creates_instances(self):
        """Enrichment returns same count as input."""
        insts = [_inst(mark="D01"), _inst(mark="W01")]
        elevs = [_elev(side="North", w=0.82, label="D01"),
                 _elev(side="North", w=1.2, label="W01")]
        enriched, _ = correlate_elevation_to_plan(elevs, insts)
        self.assertEqual(len(enriched), 2)

    def test_deduction_status_remains_review(self):
        """B3 enrichment with unknown basis must not change deduction_status."""
        inst = _inst()
        elev = _elev()
        result = _enrich_from_elevation(inst, elev)
        self.assertEqual(result.deduction_status, DEDUCTION_REVIEW)

    def test_unmatched_elevations_not_in_enriched(self):
        inst = _inst(side="North", width=0.82)
        elev_n = _elev(side="North", w=0.82, h=2.1, label="D01")
        elev_s = _elev(side="South", w=0.82, h=2.1)
        enriched, unmatched = correlate_elevation_to_plan(
            [elev_n, elev_s], [inst]
        )
        self.assertEqual(len(enriched), 1)
        self.assertEqual(len(unmatched), 1)
        self.assertEqual(unmatched[0].elevation_side, "South")

    def test_elevation_geometry_carry(self):
        inst = _inst(side="North", width=0.82)
        elev = _elev(side="North", w=0.82, h=2.1, label="D01")
        enriched, _ = correlate_elevation_to_plan([elev], [inst])
        self.assertIsNotNone(enriched[0].elevation_geometry)

    def test_width_compatible_within_tolerance(self):
        self.assertTrue(
            _width_compatible(0.82, 0.82 + ELEVATION_WIDTH_TOLERANCE_M - 0.01)
        )

    def test_width_incompatible_outside_tolerance(self):
        self.assertFalse(
            _width_compatible(0.82, 0.82 + ELEVATION_WIDTH_TOLERANCE_M + 0.01)
        )


class TestLabelExtraction(unittest.TestCase):
    """_extract_label_near_rect() — finding D/W marks near rectangles."""

    def test_label_inside_rect(self):
        words = [_word("D01", 140, 200, 190, 220)]
        label = _extract_label_near_rect(words, (100, 100, 300, 500))
        self.assertEqual(label, "D01")

    def test_label_near_rect(self):
        words = [_word("W01", 140, 60, 190, 80)]
        label = _extract_label_near_rect(words, (100, 100, 300, 500))
        self.assertEqual(label, "W01")

    def test_label_too_far(self):
        words = [_word("D01", 800, 800, 850, 820)]
        label = _extract_label_near_rect(words, (100, 100, 300, 500))
        self.assertEqual(label, "")

    def test_no_marks(self):
        words = [_word("Fire Door", 150, 200, 250, 220)]
        label = _extract_label_near_rect(words, (100, 100, 300, 500))
        self.assertEqual(label, "")

    def test_empty_words(self):
        label = _extract_label_near_rect([], (100, 100, 300, 500))
        self.assertEqual(label, "")

    def test_best_distance(self):
        """Closer label wins over farther one."""
        words = [
            _word("D01", 140, 70, 190, 90),     # 10px above rect top edge
            _word("D02", 140, 550, 190, 570),   # 50px below rect bottom edge
        ]
        label = _extract_label_near_rect(words, (100, 100, 300, 500))
        self.assertEqual(label, "D01")


class TestEdgeCases(unittest.TestCase):
    """Edge cases for the full pipeline."""

    def test_correlation_with_no_width_on_instance(self):
        """Instance with no width + no mark → no match (insufficient signal)."""
        inst = _inst(width=None, side="North")
        elev = _elev(side="North", w=0.82, h=2.1)
        enriched, unmatched = correlate_elevation_to_plan([elev], [inst])
        self.assertIsNone(enriched[0].height_m)

    def test_correlation_preserves_existing_height(self):
        """If instance already has height, merge still works."""
        inst = _inst(height=2.0, basis=DIMENSION_BASIS_UNKNOWN)
        elev = _elev(h=2.1, label="D01")
        enriched, _ = correlate_elevation_to_plan([elev], [inst])
        self.assertIsNotNone(enriched[0].height_m)

    def test_elevation_side_set_on_result(self):
        inst = _inst(side="")
        elev = _elev(side="East")
        result = _enrich_from_elevation(inst, elev)
        self.assertEqual(result.elevation_side, "East")

    def test_multiple_sides(self):
        """Elevations from different sides match their respective instances."""
        inst_n = _inst(mark="D01", side="North", width=0.82, wall_ref="N01")
        inst_s = _inst(mark="D01", side="South", width=0.82, wall_ref="S01")
        elev_n = _elev(side="North", w=0.82, h=2.1, label="D01")
        elev_s = _elev(side="South", w=0.82, h=2.4, label="D01")
        enriched, unmatched = correlate_elevation_to_plan(
            [elev_n, elev_s], [inst_n, inst_s]
        )
        heights_list = [e.height_m for e in enriched if e.height_m is not None]
        self.assertEqual(len(heights_list), 2)

    def test_strong_signal_minimum_threshold(self):
        """Score must meet minimum to qualify."""
        self.assertGreater(_MIN_STRONG_SIGNAL, 0.0)
        self.assertLessEqual(_MIN_STRONG_SIGNAL, 0.5)


if __name__ == "__main__":
    unittest.main()
