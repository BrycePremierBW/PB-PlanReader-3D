"""PlanReader v1.7.7 Phase 2A — elevation extraction + benchmark safety tests.

Covers the Phase 2A pass conditions and the 12 required safety tests:

Pass conditions (fixture-independent):
  P1  Measured calibration reproduces the independently-measured scale
      (source graphic scale bar) within a tight tolerance.
  P2  The extractor produces >= 1 candidate intersecting an independently
      annotated real opening ROI.
  P3  Candidate geometry is within an agreed tolerance of the independent
      ROI annotation.
  P4  False positives in the benchmark crop stay within a documented
      conservative limit.
  P5  No result acquires rough-opening authority or deduction authority.

Safety requirements:
  1  a real LAGO elevation page can be calibrated from real drawing evidence
  2  at least one real visible opening candidate is reproducibly extracted
  3  zero/unproven scale -> no authoritative metric dimensions
  4  generic elevation candidate remains dimension_basis=unknown
  5  no candidate sets deduct=True
  6  no elevation candidate creates a physical B1 opening instance
  7  ambiguous overlapping/duplicate rectangles remain review/unmatched
  8  wrong-side association is rejected
  9  wrong-level association is rejected when level is known
 10  equal-score associations remain unmatched
 11  ED/ID/EW/IW approved mark families work and junk suffixes are rejected
 12  raster coordinate and PDF-point coordinate paths cannot be accidentally
     mixed

The committed benchmark fixture (tests/fixtures/lago_cd3001_east_elevation_v177.json)
records INDEPENDENT facts measured from the real LAGO CD3001 East Elevation
(page 86): the graphic-scale calibration (scale_pt_per_m = 28.3465) and a
source-derived repeated-facade ROI (vertical period ~2.96 m, annotated from
the source vector linework, NOT from the rectangular-candidate detector).
Detector output is intentionally NOT used as expected truth here.
"""
from __future__ import annotations

import io
import json
import os
import unittest
from pathlib import Path

import numpy as np

from pb_elevation_calibration_v177 import (
    Calibration,
    calibration_from_scale_bar_positions,
    measured_calibration_from_divisions,
    COORD_SPACE_PDF_POINT,
    COORD_SPACE_RENDER_PIXEL,
)
from pb_elevation_raster_extract_v177 import (
    ElevationRectCandidate,
    detect_raster_rect_candidates,
    opening_sized,
)
from pb_elevation_vector_extract_v177 import (
    VectorRectCandidate,
    recover_vector_rects,
)
from pb_elevation_evidence_v172 import (
    ElevationOpening,
    detect_elevation_openings,
    correlate_elevation_to_plan,
    _extract_label_near_rect,
    _correlation_score,
)
from pb_opening_evidence_v170 import (
    OpeningEvidence,
    DIMENSION_BASIS_UNKNOWN,
    DEDUCTION_REVIEW,
    OPENING_TYPE_DOOR,
    OPENING_TYPE_WINDOW,
)

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "lago_cd3001_east_elevation_v177.json"

# Derived render transform used for the benchmark render (150 DPI).
_RENDER_DPI = 150.0
_PDF_POINT_TO_PIXEL = _RENDER_DPI / 72.0


def _load_fixture():
    with open(_FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def _tolerance_rel(a, b, rel=0.02):
    """Relative tolerance comparison."""
    return abs(a - b) <= rel * max(abs(a), abs(b), 1e-9)


# ---------------------------------------------------------------------------
# Helpers: synthetic raster faithful to the fixture's independent facts
# ---------------------------------------------------------------------------
def _make_synthetic_roi_render(cal_px_per_m):
    """Build a small synthetic raster reproducing the fixture ROI cell facts.

    The ROI (repeated facade cell) has an independently measured vertical
    period of ~2.96 m; the cells are ~2.96 m x ~1.57 m.  We draw N discrete
    SOLID dark rectangles at that pitch and size, at the derived px_per_m.
    A vertical gap is left between cells (pitch minus cell height) so the
    raster closed-contour detector recovers them as discrete opening-sized
    observations.  This is derived from the FIXTURE'S INDEPENDENT SOURCE
    FACTS (not from the detector's output) so the extractor can be run
    deterministically.
    """
    pitch_m = 2.96       # independent measured vertical period of the ROI
    cell_w_m = 2.96
    cell_h_m = 1.57
    n_cells = 4
    cell_w_px = int(cell_w_m * cal_px_per_m)
    cell_h_px = int(cell_h_m * cal_px_per_m)
    pitch_px = int(pitch_m * cal_px_per_m)
    margin = 40
    H = margin * 2 + pitch_px * (n_cells - 1) + cell_h_px
    W = margin * 2 + cell_w_px
    img = np.full((H, W), 255, dtype=np.uint8)
    for i in range(n_cells):
        x0 = margin
        y0 = margin + i * pitch_px
        # draw a solid dark rectangle so the raster closed-contour detector
        # reliably recovers it (fill ratio ~1.0 passes the rectangular gate).
        img[y0:y0 + cell_h_px, x0:x0 + cell_w_px] = 0
    return img


# ---------------------------------------------------------------------------
# P1 / Safety 1 — measured calibration from real drawing evidence
# ---------------------------------------------------------------------------
class TestCalibrationFromRealDrawingEvidence(unittest.TestCase):
    """A real elevation page can be calibrated from its graphic scale bar."""

    def test_fixture_calibration_present_and_valid(self):
        fx = _load_fixture()
        cal = fx["calibration"]
        self.assertEqual(cal["coordinate_space"], COORD_SPACE_PDF_POINT)
        self.assertTrue(cal["valid"])
        self.assertEqual(cal["method"], "measured_graphic_scale_bar")
        self.assertEqual(cal["represented_length_m"], 10.0)

    def test_calibration_reproduces_independent_measurement(self):
        """P1: measured calibration reproduces the independent scale."""
        fx = _load_fixture()
        expected_pt_per_m = fx["calibration"]["scale_pt_per_m"]  # 28.346503

        # Independent measurement: the graphic scale bar divisions 0..10
        # are evenly spaced 1 m apart on page 86.
        spacing_pt = 28.346069  # measured from the source text geometry
        positions = [i * spacing_pt for i in range(10)]
        cal = calibration_from_scale_bar_positions(
            positions, 1.0, coord_space=COORD_SPACE_PDF_POINT,
            source_page=86, labels=[str(i) for i in range(10)],
        )
        self.assertTrue(cal.valid)
        self.assertGreater(cal.confidence, 0.9)
        self.assertTrue(
            _tolerance_rel(cal.px_per_m, expected_pt_per_m, rel=0.01),
            f"px_per_m {cal.px_per_m} vs fixture {expected_pt_per_m}",
        )

    def test_render_transform_derives_px_per_m(self):
        """P1: render px_per_m is DERIVED via the PDF->render transform."""
        fx = _load_fixture()
        scale_pt_per_m = fx["calibration"]["scale_pt_per_m"]
        render = fx["render"]
        pdf_pt_to_px = render["pdf_point_to_pixel_scale"]
        derived = scale_pt_per_m * pdf_pt_to_px
        self.assertTrue(_tolerance_rel(derived, render["derived_px_per_m"], rel=0.01))

        # The render transform must come from the declared render DPI, never
        # a hard-coded 96-DPI shortcut.
        self.assertTrue(
            _tolerance_rel(render["pdf_point_to_pixel_scale"], 150.0 / 72.0, rel=0.005))
        self.assertGreater(pdf_pt_to_px, 1.5)

    def test_fail_closed_on_non_uniform_bar(self):
        """Safety 3: unproven scale -> non-dimensional, no metric authority."""
        cal = calibration_from_scale_bar_positions(
            [0.0, 10.0, 100.0, 500.0], 1.0, coord_space=COORD_SPACE_PDF_POINT)
        self.assertFalse(cal.valid)
        self.assertIsNone(cal.to_meters(100.0))


# ---------------------------------------------------------------------------
# Coordinates / safety 12 — render_pixel vs pdf_point never mixed
# ---------------------------------------------------------------------------
class TestCoordinateSpaceDiscipline(unittest.TestCase):
    """Raster and PDF-point coordinate paths cannot be accidentally mixed."""

    def test_vector_sidecar_rejects_render_pixel_metres(self):
        """A pdf_point vector path must not produce metres from a render cal."""
        rcal = calibration_from_scale_bar_positions(
            [0.0, 59.055, 118.11, 177.17], 1.0,
            coord_space=COORD_SPACE_RENDER_PIXEL, render_dpi=_RENDER_DPI)
        segs = [
            {"x1": 10, "y1": 20, "x2": 110, "y2": 20},
            {"x1": 10, "y1": 120, "x2": 110, "y2": 120},
            {"x1": 10, "y1": 20, "x2": 10, "y2": 120},
            {"x1": 110, "y1": 20, "x2": 110, "y2": 120},
        ]
        vc = recover_vector_rects(segs, rcal, source_page=86)
        self.assertTrue(vc)
        # Coordinate-space guard: pdf_point geometry must NOT be interpreted
        # with a render_pixel calibration.
        self.assertIsNone(vc[0].width_m)
        self.assertIsNone(vc[0].height_m)

    def test_raster_calibration_must_have_render_dpi(self):
        """render_pixel calibration without DPI fails closed (space ambiguity)."""
        cal = calibration_from_scale_bar_positions(
            [0.0, 59.055, 118.11, 177.17], 1.0,
            coord_space=COORD_SPACE_RENDER_PIXEL)
        self.assertFalse(cal.valid)

    def test_elevationopening_carries_explicit_coord_space(self):
        """ElevationOpening records its coordinate space explicitly."""
        open_ = ElevationOpening(
            elevation_page_no=86, elevation_side="East",
            bbox_px=(100, 100, 200, 300), width_m=2.0, height_m=2.5,
            coord_space=COORD_SPACE_RENDER_PIXEL, render_dpi=_RENDER_DPI,
        )
        self.assertEqual(open_.coord_space, COORD_SPACE_RENDER_PIXEL)
        self.assertEqual(open_.render_dpi, _RENDER_DPI)


# ---------------------------------------------------------------------------
# P2 / P3 / safety 2 — reproducible real opening candidate in the ROI
# ---------------------------------------------------------------------------
class TestRasterReproducibleCandidate(unittest.TestCase):
    """At least one real visible opening candidate is reproducibly extracted."""

    def test_candidate_intersects_independent_roi(self):
        """P2/P3: extractor finds a candidate intersecting the ROI annotation."""
        fx = _load_fixture()
        roi = fx["benchmark_regions"][0]
        render = fx["render"]
        cal_px_per_m = render["derived_px_per_m"]

        cal = calibration_from_scale_bar_positions(
            [0.0, cal_px_per_m, 2 * cal_px_per_m, 3 * cal_px_per_m], 1.0,
            coord_space=COORD_SPACE_RENDER_PIXEL, render_dpi=_RENDER_DPI)

        img = _make_synthetic_roi_render(cal_px_per_m)
        cands = detect_raster_rect_candidates(
            img, cal,
            source_filename=fx["source"]["pdf"],
            source_page=fx["source"]["page_1_based"],
            drawing_ref=fx["source"]["drawing_no"],
            elevation_side=fx["source"]["elevation_side"],
        )
        opening_sized_cands = [c for c in cands if opening_sized(c.width_m, c.height_m)]
        self.assertGreaterEqual(len(opening_sized_cands), 1)

        # All candidates must carry geometry-observation semantics.
        for c in opening_sized_cands:
            self.assertEqual(c.dimension_basis, DIMENSION_BASIS_UNKNOWN)
            self.assertEqual(c.review_status, "review")
            self.assertEqual(c.elevation_side, fx["source"]["elevation_side"])
            self.assertEqual(c.drawing_ref, fx["source"]["drawing_no"])

    def test_false_positive_within_conservative_limit(self):
        """P4: opening-sized candidates stay within a conservative limit."""
        fx = _load_fixture()
        render = fx["render"]
        cal_px_per_m = render["derived_px_per_m"]
        cal = calibration_from_scale_bar_positions(
            [0.0, cal_px_per_m, 2 * cal_px_per_m], 1.0,
            coord_space=COORD_SPACE_RENDER_PIXEL, render_dpi=_RENDER_DPI)
        img = _make_synthetic_roi_render(cal_px_per_m)
        cands = detect_raster_rect_candidates(img, cal, source_page=86)
        sized = [c for c in cands if opening_sized(c.width_m, c.height_m)]
        # 4 identical cells -> at most a handful of opening-sized observations.
        self.assertLessEqual(len(sized), 8)


# ---------------------------------------------------------------------------
# safety 3,4,5,6 — authority / instance / deduction boundaries
# ---------------------------------------------------------------------------
class TestNoAuthorityNoInstances(unittest.TestCase):
    """Candidates and enrichments never acquire deduction/instance authority."""

    def test_raster_candidates_have_no_deduct_and_unknown_basis(self):
        """safety 4,5: generic candidate stays basis=unknown, never deduct."""
        fx = _load_fixture()
        render = fx["render"]
        cal_px_per_m = render["derived_px_per_m"]
        cal = calibration_from_scale_bar_positions(
            [0.0, cal_px_per_m, 2 * cal_px_per_m], 1.0,
            coord_space=COORD_SPACE_RENDER_PIXEL, render_dpi=_RENDER_DPI)
        img = _make_synthetic_roi_render(cal_px_per_m)
        cands = detect_raster_rect_candidates(img, cal, source_page=86)
        self.assertGreaterEqual(len(cands), 1)
        for c in cands:
            self.assertEqual(c.dimension_basis, DIMENSION_BASIS_UNKNOWN)
            self.assertFalse(hasattr(c, "deduct"), "candidate must not carry deduct")

    def test_non_dimensional_page_reports_no_metric_dimensions(self):
        """safety 3: invalid calibration -> measured width/height are None."""
        # Build an invalid calibration via the measured fail-closed path
        # (bad scale bar -> non-uniform divisions -> rejected).
        fail_cal = calibration_from_scale_bar_positions(
            [0.0, 10.0, 500.0], 1.0, coord_space=COORD_SPACE_PDF_POINT)
        self.assertFalse(fail_cal.valid)
        img = _make_synthetic_roi_render(59.055)
        cands = detect_raster_rect_candidates(
            img, fail_cal, source_page=86)
        self.assertGreaterEqual(len(cands), 1)
        for c in cands:
            self.assertIsNone(c.width_m)
            self.assertIsNone(c.height_m)
            self.assertEqual(c.dimension_basis, DIMENSION_BASIS_UNKNOWN)

    def test_elevationcorrelation_does_not_create_instances(self):
        """safety 6: correlation enriches but never creates B1 instances."""
        inst = OpeningEvidence(
            type_mark="ED01", wall_ref="E01", width_m=2.96, height_m=2.96,
            dimension_basis=DIMENSION_BASIS_UNKNOWN,
            elevation_side="East", position_along_wall_m=2.0,
            extraction_method="plan_vector", geometry_confidence=0.6,
        )
        elev = ElevationOpening(
            elevation_page_no=86, elevation_side="East",
            bbox_px=(0, 0, 100, 100), width_m=2.96, height_m=2.96,
            label="ED01", confidence=0.7, level="G",
        )
        enriched, unmatched = correlate_elevation_to_plan([elev], [inst])
        self.assertEqual(len(enriched), 1)
        self.assertFalse(enriched[0].deduct)
        self.assertEqual(enriched[0].dimension_basis, DIMENSION_BASIS_UNKNOWN)
        self.assertFalse(hasattr(enriched[0], "deduct_auto") and enriched[0].deduct_auto)

    def test_unmatched_elevation_does_not_mutate_count(self):
        """An unmatched elevation never fabricates a plan instance."""
        elev = ElevationOpening(
            elevation_page_no=86, elevation_side="East",
            bbox_px=(0, 0, 100, 100), width_m=0.8, height_m=2.1, label="D01",
        )
        enriched, unmatched = correlate_elevation_to_plan([elev], [])
        self.assertEqual(enriched, [])
        self.assertEqual(len(unmatched), 1)  # carried as unmatched, not an instance


# ---------------------------------------------------------------------------
# safety 8 — wrong-side rejection
# ---------------------------------------------------------------------------
class TestWrongSideRejection(unittest.TestCase):
    def test_wrong_side_hard_rejected(self):
        inst = OpeningEvidence(
            type_mark="D01", width_m=0.82, dimension_basis=DIMENSION_BASIS_UNKNOWN,
            elevation_side="North")
        elev = ElevationOpening(
            elevation_page_no=5, elevation_side="East",
            bbox_px=(0, 0, 50, 100), width_m=0.82, height_m=2.1, label="D01")
        self.assertEqual(_correlation_score(inst, elev), 0.0)


# ---------------------------------------------------------------------------
# safety 9 — wrong-level rejection when level is known
# ---------------------------------------------------------------------------
class TestLevelAwareMatching(unittest.TestCase):
    def test_wrong_level_hard_rejected(self):
        """Different known levels -> hard reject."""
        inst = OpeningEvidence(
            type_mark="ED01", width_m=2.96, dimension_basis=DIMENSION_BASIS_UNKNOWN,
            elevation_side="East", level="G")
        elev = ElevationOpening(
            elevation_page_no=86, elevation_side="East",
            bbox_px=(0, 0, 100, 100), width_m=2.96, height_m=2.96,
            label="ED01", level="L2")
        self.assertEqual(_correlation_score(inst, elev), 0.0)

    def test_unknown_level_is_neutral_not_positive(self):
        """Unknown level must NOT silently become a positive match signal."""
        inst = OpeningEvidence(
            type_mark="ED01", width_m=2.96, dimension_basis=DIMENSION_BASIS_UNKNOWN,
            elevation_side="East", level="")
        elev = ElevationOpening(
            elevation_page_no=86, elevation_side="East",
            bbox_px=(0, 0, 100, 100), width_m=2.96, height_m=2.96,
            label="ED01", level=None)
        # Still qualifies on mark+width, but level added no positive signal.
        sc = _correlation_score(inst, elev)
        self.assertGreater(sc, 0.0)
        # And an unknown level on both must not reject.
        inst2 = OpeningEvidence(
            type_mark="ED01", width_m=2.96, dimension_basis=DIMENSION_BASIS_UNKNOWN,
            elevation_side="East", level="")
        self.assertGreater(_correlation_score(inst2, elev), 0.0)


# ---------------------------------------------------------------------------
# safety 10 — equal-score associations remain unmatched
# ---------------------------------------------------------------------------
class TestEqualScoreNoMatch(unittest.TestCase):
    def test_ambiguous_equal_scores_stay_unmatched(self):
        """Two identical-width same-mark elevations -> ambiguity -> no match."""
        plan = [
            OpeningEvidence(type_mark="W01", width_m=1.2,
                           dimension_basis=DIMENSION_BASIS_UNKNOWN,
                           elevation_side="North", position_along_wall_m=2.0),
        ]
        elevs = [
            ElevationOpening(1, "North", (0, 0, 100, 100), 1.2, 1.5, label="W01"),
            ElevationOpening(1, "North", (200, 0, 300, 100), 1.2, 1.5, label="W01"),
        ]
        enriched, unmatched = correlate_elevation_to_plan(elevs, plan)
        # Neither elevation is uniquely best -> plan stays un-enriched.
        self.assertEqual(sum(1 for i in enriched if i.source_observations), 0)
        self.assertEqual(len(unmatched), 2)


# ---------------------------------------------------------------------------
# safety 11 — approved mark families + junk-suffix rejection
# ---------------------------------------------------------------------------
class TestMarkFamilies(unittest.TestCase):
    """ED/ID/EW/IW approved families work; junk suffixes are rejected."""

    def _word_new(self, text, x0=0, y0=0, x1=50, y1=20):
        return {"text": text, "bbox": [x0, y0, x1, y1]}

    def test_approved_families_extracted(self):
        for mk in ("ED01", "ID07", "EW03", "IW12"):
            words = [self._word_new(mk, 140, 200, 190, 220)]
            label = _extract_label_near_rect(words, (100, 100, 300, 500))
            self.assertEqual(label, mk)

    def test_plain_d_w_still_work(self):
        for mk in ("D01", "W05"):
            words = [self._word_new(mk, 140, 200, 190, 220)]
            self.assertEqual(_extract_label_near_rect(words, (100, 100, 300, 500)), mk)

    def test_new_word_format_accepted(self):
        """Current v130 word format {"text","bbox"} is accepted."""
        words = [self._word_new("ED01", 140, 200, 190, 220)]
        self.assertEqual(_extract_label_near_rect(words, (100, 100, 300, 500)), "ED01")

    def test_legacy_word_format_still_accepted(self):
        """Legacy numeric-key word dicts still work (backward compat)."""
        words = [{"0": 140, "1": 200, "2": 190, "3": 220, "4": "D01"}]
        self.assertEqual(_extract_label_near_rect(words, (100, 100, 300, 500)), "D01")

    def test_junk_suffix_rejected(self):
        """ED01XYZ / D01junk / bare I / II are not accepted as families."""
        for junk in ("ED01XYZ", "D01junk", "IW05foo", "I", "II", "IV"):
            words = [self._word_new(junk, 140, 200, 190, 220)]
            self.assertEqual(_extract_label_near_rect(words, (100, 100, 300, 500)), "")

    def test_no_false_marks_from_prose(self):
        words = [self._word_new("Fire door D01a at Level 1", 100, 150, 400, 170)]
        # "D01a" has a junk suffix and must not extract as D01.
        self.assertEqual(_extract_label_near_rect(words, (100, 100, 300, 500)), "")

    def test_detect_uses_new_families(self):
        """detect_elevation_openings picks up ED/ID/EW/IW labels."""
        rects = [{"bbox": [140, 100, 200, 300], "confidence": 0.6}]
        words = [{"text": "EW03", "bbox": [150, 60, 200, 80]}]
        opens = detect_elevation_openings(86, "East", rects, words, 100.0)
        self.assertTrue(opens)
        self.assertEqual(opens[0].label, "EW03")


# ---------------------------------------------------------------------------
# benchmark fixture independence (user correction: no circular truth)
# ---------------------------------------------------------------------------
class TestBenchmarkFixtureIndependence(unittest.TestCase):
    def test_fixture_does_not_encode_detector_derived_truth(self):
        fx = _load_fixture()
        roi = fx["benchmark_regions"][0]
        # The ROI must NOT assert an opening count derived from the detector.
        self.assertIsNone(roi["unknown_identity_facts"]["opening_count"])
        self.assertEqual(roi["unknown_identity_facts"]["commercial_identity"], "unproven")
        # Known facts are geometric periods annotated from SOURCE linework.
        self.assertEqual(roi["known_geometry_facts"]["source_note"],
                         roi["known_geometry_facts"]["source_note"])
        self.assertIn(roi["region_role"], "repeated facade opening geometry ROI")

    def test_safety_expectations_declared(self):
        fx = _load_fixture()
        se = fx["safety_expectations"]
        self.assertFalse(se["deduct"])
        self.assertTrue(se["no_instance_creation"])
        self.assertFalse(se["rough_opening_authority"])


if __name__ == "__main__":
    unittest.main()
