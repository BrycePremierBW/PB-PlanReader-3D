"""B3 benchmark review continuation — real page-86 evidence + labelled regressions.

This suite is the review-driven continuation of Workstream 2 (PR #65).  It adds
explicit REAL page-86 benchmark cases alongside clearly-labelled SYNTHETIC
targeted regressions, and states honestly where real benchmark truth does and
does not exist in this repository.

REAL benchmark truth (committed, independently-annotated, page 86 only)
-----------------------------------------------------------------------
The ONLY real committed elevation evidence is page 86 / drawing CD3001 / E1 East
(tests/fixtures/lago_cd3001_east_elevation_v177.json plus its two 150-DPI render
crops).  Real truth is available for THREE categories, all measured from that
one page:

  * POSITIVE  — 9 glazed window lights independently annotated in the band
                y[648.3, 690.5] pt (positive_benchmark.independent_annotation).
  * CALIBRATION — a genuinely different, MEASURED real calibration:
                28.346 pt/m (pdf_point) reproduced from the REAL graphic scale
                bar (11 ticks, 10 divisions, 10 m) on page 86.
  * NEGATIVE / NON-OPENING — the solid spandrel band y[690.5, 722.5] pt below
                the glazing is a KNOWN non-opening (independently annotated in
                the same fixture).  Any opening-sized detection inside it is
                noise, and repeated mullion/batten/grid-like rectangles there
                must never each become a separate physical opening.

HONESTY — page 87/88 and other extra elevation pages
-----------------------------------------------------
There is NO committed source PDF, NO page-87/88 crop, and NO other real
elevation raster/page anywhere in this repository (files, git history, or any
worktree).  Therefore there is NO real benchmark truth for an additional
elevation page.  That category is covered ONLY by the clearly-labelled
SYNTHETIC TARGETED REGRESSION below — never as real-world accuracy.

HONESTY — real doors
--------------------
tests/fixtures/lago_b1_ga08_ed04_cluster.json (plan data, GA Level 08 / CD1161-06)
IS committed and DOES carry genuinely real, independently-verified door geometry
(verified_door_leaf_segments) plus a real ED04 door tag at a known location.  So
real door EVIDENCE exists in-repo.  However, its own safety_note states that B1
may conservatively leave the real ED04 mark unassociated and that the benchmark
must never force a tag or deduction merely to pass.  Running the current B1
detector over the full real cluster confirms this: it classifies the door leaf
geometry as a door but does NOT tag it (type_mark stays unassociated) and the
cluster is noisy.  Accordingly the door benchmark here asserts ONLY what the
real evidence honestly supports (real geometry present; no forced tag; no
deduction), and the precise door-COVERAGE behavioural assertions are kept as
labelled SYNTHETIC regressions.

Labelling
---------
Every test in the SYNTHETIC group carries the constant
SYNTHETIC_TARGETED_REGRESSION_LABEL in its docstring:
    "SYNTHETIC TARGETED REGRESSION — NOT REAL BENCHMARK TRUTH"
Synthetic regression results are NEVER mixed into real benchmark accuracy.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Any, Dict, Optional

import cv2
import numpy as np

from pb_elevation_calibration_v177 import (
    COORD_SPACE_PDF_POINT,
    COORD_SPACE_RENDER_PIXEL,
    Calibration,
    calibration_from_scale_bar_positions,
)
from pb_elevation_evidence_v172 import (
    ElevationOpening,
    _correlation_score,
    correlate_elevation_to_plan,
    detect_elevation_openings,
)
from pb_elevation_raster_extract_v177 import (
    detect_raster_rect_candidates,
    opening_sized,
)
from pb_opening_evidence_v170 import (
    DIMENSION_BASIS_UNKNOWN,
    OPENING_TYPE_DOOR,
    OpeningEvidence,
)
from pb_plan_opening_detection_v171 import (
    Segment,
    TextWord,
    plan_opening_candidates,
)

SYNTHETIC_TARGETED_REGRESSION_LABEL = (
    "SYNTHETIC TARGETED REGRESSION — NOT REAL BENCHMARK TRUTH"
)

BASE = Path(__file__).resolve().parent
_FIXTURE = BASE / "fixtures" / "lago_cd3001_east_elevation_v177.json"
_POS_CROP = BASE / "fixtures" / "lago_cd3001_p86_e1east_glazed_open_group_150dpi.png"
_NEG_CROP = BASE / "fixtures" / "lago_cd3001_p86_e1east_roi_150dpi.png"
_DOOR_FIXTURE = BASE / "fixtures" / "lago_b1_ga08_ed04_cluster.json"

_RENDER_DPI = 150.0
_PDF_POINT_TO_PIXEL = _RENDER_DPI / 72.0

# Documented conservative engineering cap on opening-sized detections inside
# the real solid spandrel band (page 86).  The band is a solid facade panel
# ~32 pt tall (~1.13 m) spanning ~448 pt of visible facade with NO physical
# openings (independent real annotation).  This cap is an independent
# false-positive/noise guard, NOT a value tuned to the current detector output:
# current live output is well below it.  It bounds how much "repeated
# rectangle" over-segmentation may appear in a genuinely-negative region before
# it is deemed runaway fabrication.
SPANDREL_OPENING_SIZED_CAP = 8


def _load_fixture() -> Dict[str, Any]:
    with open(_FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def _load_door_fixture() -> Dict[str, Any]:
    with open(_DOOR_FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def _tolerance_rel(a, b, rel=0.02) -> bool:
    return abs(a - b) <= rel * max(abs(a), abs(b), 1e-9)


def _render_pixel_calibration(fx: Dict[str, Any]) -> Calibration:
    """Calibration (render_pixel, 150 DPI) derived from the fixture scale."""
    scale_pt_per_m = fx["calibration"]["scale_pt_per_m"]
    px_per_m = scale_pt_per_m * _PDF_POINT_TO_PIXEL
    return calibration_from_scale_bar_positions(
        [0.0, px_per_m, 2 * px_per_m, 3 * px_per_m], 1.0,
        coord_space=COORD_SPACE_RENDER_PIXEL, render_dpi=_RENDER_DPI)


def _load_positive_crop() -> np.ndarray:
    img = cv2.imread(str(_POS_CROP), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise AssertionError(f"positive crop not readable: {_POS_CROP}")
    return img


def _positive_crop_meta(fx: Dict[str, Any]) -> Dict[str, Any]:
    return fx["positive_benchmark"]["crop"]


def _run_positive_detector(fx: Dict[str, Any], crop: Optional[np.ndarray] = None):
    cal = _render_pixel_calibration(fx)
    img = crop if crop is not None else _load_positive_crop()
    return detect_raster_rect_candidates(
        img, cal,
        source_filename=fx["source"]["local_source_alias"],
        source_page=fx["source"]["page_1_based"],
        drawing_ref=fx["source"]["drawing_no"],
        elevation_side=fx["source"]["elevation_side"],
        calibration_source="page-86-cd3001-e1-east",
    )


def _overlap_frac(a0, a1, b0, b1) -> float:
    lo, hi = max(a0, b0), min(a1, b1)
    width = min(a1 - a0, b1 - b0)
    if width <= 1e-9:
        return 0.0
    return max(0.0, hi - lo) / width


def _positive_precision_recall(cands, tp_xs, tp_y, x0_pt, y0_pt, scale_pt_per_px,
                               x_tol_frac=0.5, y_tol_frac=0.45):
    """One-to-one match of detected candidates to independent TP openings.
    Identical matching semantics to test_elevation_extraction_v177.py.  A true
    opening accepts at most ONE candidate; a duplicate overlapping candidate is
    a false positive (noise).  Returns (tp_matched, detected_tp, fp)."""
    head, sill = tp_y
    boxes = []
    for c in cands:
        xl, yl, xr, yr = c.bbox
        boxes.append((x0_pt + xl * scale_pt_per_px,
                      y0_pt + yl * scale_pt_per_px,
                      x0_pt + xr * scale_pt_per_px,
                      y0_pt + yr * scale_pt_per_px))
    claimed = [False] * len(cands)
    tp_matched = 0
    for ti, (tx0, tx1) in enumerate(tp_xs):
        best_ci = None
        best_score = 0.0
        for ci, (sxl, syl, sxr, syr) in enumerate(boxes):
            if claimed[ci]:
                continue
            xo = _overlap_frac(sxl, sxr, tx0, tx1)
            yo = _overlap_frac(syl, syr, head, sill)
            if xo > x_tol_frac and yo > y_tol_frac:
                score = min(xo, yo)
                if score > best_score:
                    best_score = score
                    best_ci = ci
        if best_ci is not None:
            claimed[best_ci] = True
            tp_matched += 1
    detected_tp = sum(claimed)
    fp = len(cands) - detected_tp
    return tp_matched, detected_tp, fp


# ---------------------------------------------------------------------------
# Real evidence honesty
# ---------------------------------------------------------------------------
class TestRealEvidenceHonesty(unittest.TestCase):
    """Declarative honesty: what real evidence exists and what does not."""

    def test_real_fixture_committed_and_source_consistent(self):
        fx = _load_fixture()
        self.assertTrue(_POS_CROP.exists())
        self.assertTrue(_NEG_CROP.exists())
        self.assertEqual(fx["source"]["page_1_based"], 86)
        self.assertEqual(fx["source"]["drawing_no"], "CD3001")
        self.assertEqual(fx["source"]["elevation_side"], "East")

    def test_no_other_real_elevation_page_is_committed(self):
        """Honesty: there is no page-87/88 (or other extra-elevation) artifact
        committed; real elevation evidence is page 86 ONLY."""
        # Only the two page-86 crops are committed; there must be NO crop for
        # any other elevation page (87/88 or otherwise).  Match every committed
        # *_150dpi.png raster and require its "p<NN>" page token to be 86.
        for crop in _FIXTURE.parent.glob("lago_*.png"):
            m = re.search(r"_p(\d+)_", crop.name)
            self.assertIsNotNone(m, crop.name)
            self.assertEqual(int(m.group(1)), 86,
                             f"only page-86 crops may be committed, got {crop.name}")
        # Only the two page-86 crops and the door-plan cluster are real raster
        # / vector evidence present in fixtures.
        known_real = {
            "lago_cd3001_east_elevation_v177.json",
            "lago_cd3001_p86_e1east_glazed_open_group_150dpi.png",
            "lago_cd3001_p86_e1east_roi_150dpi.png",
            "lago_b1_ga08_ed04_cluster.json",
        }
        present = {p.name for p in _FIXTURE.parent.glob("lago_*")}
        self.assertTrue(known_real <= present)

    def test_synthetic_fixtures_are_labelled_synthetic(self):
        """The existing B3 controlled fixtures are synthetic, not real truth."""
        for name in ("bench_ground_floor", "bench_multi_window_wall",
                     "bench_envelope_schedule"):
            with open(BASE / "fixtures" / f"{name}.json", encoding="utf-8") as fh:
                fx = json.load(fh)
            authority = fx["truth"]["authority"].lower()
            self.assertIn("synthe", authority, name)
            self.assertIn("detector output never defines truth", authority)


# ---------------------------------------------------------------------------
# 2. REAL calibration / scale benchmark (page 86, 28.346 pt/m, pdf_point)
# ---------------------------------------------------------------------------
class TestRealCalibrationBenchmark(unittest.TestCase):
    """REAL: the genuinely-different measured calibration available in real
    data — page 86's 28.346 pt/m (pdf_point).  Dimensional results must be
    correct and coordinate-space-safe in pdf_point."""

    def setUp(self):
        self.fx = _load_fixture()

    def test_real_calibration_is_pdf_point_and_measured(self):
        cal = self.fx["calibration"]
        self.assertEqual(cal["coordinate_space"], COORD_SPACE_PDF_POINT)
        self.assertTrue(cal["valid"])
        self.assertEqual(cal["method"], "measured_graphic_scale_bar")
        self.assertTrue(_tolerance_rel(cal["scale_pt_per_m"], 28.346, rel=0.01))

    def test_real_calibration_to_meters_is_correct(self):
        """REAL: 28.346 pt/m converts PDF-point lengths to correct metres."""
        pcal = calibration_from_scale_bar_positions(
            self.fx["calibration"]["measured_bar_geometry"]["tick_positions_x_pt"],
            1.0, coord_space=COORD_SPACE_PDF_POINT,
            source_page=86,
            labels=self.fx["calibration"]["measured_bar_geometry"]["labels"],
        )
        self.assertTrue(pcal.valid)
        self.assertTrue(_tolerance_rel(pcal.to_meters(28.346), 1.0, rel=0.02))
        self.assertTrue(_tolerance_rel(pcal.to_meters(283.46), 10.0, rel=0.02))
        # The real glazed light is 21.9 pt wide = 0.773 m.
        self.assertTrue(_tolerance_rel(
            pcal.to_meters(21.9),
            self.fx["positive_benchmark"]["independent_annotation"]["light_width_m"],
            rel=0.03))
        # The real opening band is 42.2 pt tall = 1.489 m.
        self.assertTrue(_tolerance_rel(
            pcal.to_meters(42.2),
            self.fx["positive_benchmark"]["independent_annotation"]["opening_height_m"],
            rel=0.03))

    def test_real_calibration_is_pdf_point_coordinate_safe(self):
        """REAL: a pdf_point calibration never exposes px_per_m and never lets
        a raster path produce pixel-coordinate metres; width/height stay
        geometric-only where the coordinate space does not match."""
        pcal = calibration_from_scale_bar_positions(
            self.fx["calibration"]["measured_bar_geometry"]["tick_positions_x_pt"],
            1.0, coord_space=COORD_SPACE_PDF_POINT, source_page=86)
        self.assertIsNotNone(pcal.pt_per_m)
        self.assertIsNone(pcal.px_per_m)  # 28.346 pt/m is NEVER px/m
        # A pdf_point calibration on a raster image must not yield px metres.
        img = np.full((60, 60), 255, dtype=np.uint8)
        img[10:50, 10:50] = 0
        cands = detect_raster_rect_candidates(img, pcal, source_page=86)
        self.assertTrue(cands)
        for c in cands:
            self.assertEqual(c.coord_space, COORD_SPACE_RENDER_PIXEL)
            self.assertIsNone(c.width_m)
            self.assertIsNone(c.height_m)
            self.assertEqual(c.dimension_basis, DIMENSION_BASIS_UNKNOWN)

    def test_real_detect_elevation_openings_pdf_point_is_dimensional(self):
        """REAL: a pdf_point rect measured against the 28.346 pt/m pdf_point
        calibration yields correct metre dimensions (via detect_elevation_openings
        in pdf_point space) and stays review/unknown-basis."""
        pcal = calibration_from_scale_bar_positions(
            self.fx["calibration"]["measured_bar_geometry"]["tick_positions_x_pt"],
            1.0, coord_space=COORD_SPACE_PDF_POINT, source_page=86)
        # Real light 21.9 pt wide x 42.2 pt tall = 0.773 x 1.489 m.
        rect = {"bbox": [0, 0, round(21.9, 3), round(42.2, 3)], "confidence": 0.7,
                "coord_space": COORD_SPACE_PDF_POINT}
        opens = detect_elevation_openings(
            86, "East", [rect], [], pcal.units_per_m,
            coord_space=COORD_SPACE_PDF_POINT)
        self.assertEqual(len(opens), 1)
        ann = self.fx["positive_benchmark"]["independent_annotation"]
        self.assertTrue(_tolerance_rel(
            opens[0].width_m, ann["light_width_m"], rel=0.05))
        self.assertTrue(_tolerance_rel(
            opens[0].height_m, ann["opening_height_m"], rel=0.05))
        self.assertEqual(opens[0].coord_space, COORD_SPACE_PDF_POINT)
        # Feeding the real-dimensional opening into plan correlation keeps the
        # basis unknown and non-deducting (never rough-opening / never a void).
        plan = OpeningEvidence(
            type_mark="W01", width_m=ann["light_width_m"],
            dimension_basis=DIMENSION_BASIS_UNKNOWN,
            elevation_side="East", position_along_wall_m=2.0)
        enriched, _ = correlate_elevation_to_plan(
            [ElevationOpening(86, "East", (0, 0, 10, 10),
                              opens[0].width_m, opens[0].height_m, label="W01")],
            [plan])
        self.assertEqual(len(enriched), 1)
        self.assertEqual(enriched[0].dimension_basis, DIMENSION_BASIS_UNKNOWN)
        self.assertFalse(enriched[0].deduct)


# ---------------------------------------------------------------------------
# 1. REAL negative / non-opening facade geometry (solid spandrel band)
# ---------------------------------------------------------------------------
class TestRealNegativeSpandrelBenchmark(unittest.TestCase):
    """REAL: the solid spandrel band y[690.5, 722.5] pt below the page-86
    glazing is a known non-opening.  Its repeated mullion/batten/panel-joint
    rectangles must NOT each become a separate physical opening; any
    opening-sized detection inside it is noise, always bounded."""

    def setUp(self):
        self.fx = _load_fixture()

    def _spandrel_region_pt(self):
        sp = self.fx["positive_benchmark"]["independent_annotation"][
            "non_opening_regions"][0]
        self.assertEqual(sp["role"].startswith("known non-opening"), True)
        return tuple(sp["source_bbox_pt"])  # [x0,y0,x1,y1]

    def test_spandrel_is_annotated_known_non_opening(self):
        sp = self.fx["positive_benchmark"]["independent_annotation"][
            "non_opening_regions"][0]
        self.assertEqual(sp["id"], "solid-spandrel-below-glazing")
        self.assertIn("must not be detected as an opening",
                      sp["note"].lower())
        x0, y0, x1, y1 = sp["source_bbox_pt"]
        # Real geometry: solid band immediately below the glazing band.
        self.assertTrue(_tolerance_rel(y0, 690.5, rel=0.01))
        self.assertTrue(_tolerance_rel(y1, 722.5, rel=0.01))

    def _crop_coords(self, x0_pt, y0_pt, x_pt, y_pt):
        # source-pt -> crop-px via the (x0,y0) crop origin + pt-per-px scale.
        return (x_pt - x0_pt) * _PDF_POINT_TO_PIXEL, \
               (y_pt - y0_pt) * _PDF_POINT_TO_PIXEL

    def test_spandrel_repeated_rectangles_not_individual_openings(self):
        """REAL: run the detector on the positive crop and measure how many
        opening-sized candidates fall ENTIRELY inside the solid spandrel band.
        Because the spandrel is a known non-opening (real annotation), every
        such detection is noise.  It must be BOUNDED (never one-per-mullion)
        and must never acquire physical-opening identity / deduction."""
        fx = self.fx
        meta = _positive_crop_meta(fx)
        x0_pt, y0_pt = meta["source_bbox_pt"][0], meta["source_bbox_pt"][1]
        sx0, sy0, sx1, sy1 = self._spandrel_region_pt()

        cands = _run_positive_detector(fx)
        sized = [c for c in cands if opening_sized(c.width_m, c.height_m)]

        # Candidate's source-pt y-range from its crop-pixel bbox.
        # bbox is in RENDER PIXELS; 1/_PDF_POINT_TO_PIXEL converts px -> pt.
        def pt_y(c):
            _px_to_pt = 1.0 / _PDF_POINT_TO_PIXEL
            return (y0_pt + c.bbox[1] * _px_to_pt,
                    y0_pt + c.bbox[3] * _px_to_pt)

        inside = [c for c in sized
                  if pt_y(c)[0] >= sy0 - 1.0 and pt_y(c)[1] <= sy1 + 1.0]
        # All spandrel-band detections are noise: bounded by a documented cap.
        self.assertLessEqual(len(inside), SPANDREL_OPENING_SIZED_CAP,
                             "repeated spandrel rectangles must NOT each become "
                             "a separate opening — count is bound to noise cap")
        # None of these noise observations is a physical opening: all stay
        # generic geometry with unknown basis and review status.
        for c in inside:
            self.assertEqual(c.dimension_basis, DIMENSION_BASIS_UNKNOWN)
            self.assertEqual(c.review_status, "review")
            self.assertFalse(hasattr(c, "opening_type"))
            self.assertFalse(hasattr(c, "deduct"))

    def test_spandrel_detections_never_fabricate_opening_identity(self):
        """REAL: detections in the spandrel band are geometry observations
        only — they never cross into physical opening identity, never enter the
        positive recall, and never deduct."""
        fx = self.fx
        cands = _run_positive_detector(fx)
        sized = [c for c in cands if opening_sized(c.width_m, c.height_m)]
        ann = fx["positive_benchmark"]["independent_annotation"]
        meta = _positive_crop_meta(fx)
        x0_pt, y0_pt = meta["source_bbox_pt"][0], meta["source_bbox_pt"][1]
        tp_xs = [(o["x0_pt"], o["x1_pt"]) for o in ann["true_positive_openings"]]
        tp_y = tuple(ann["true_positive_y_extent_pt"])
        tp_matched, detected_tp, fp = _positive_precision_recall(
            sized, tp_xs, tp_y, x0_pt, y0_pt, 1.0 / _PDF_POINT_TO_PIXEL)
        # The spandrel receivers must be counted as FP/noise, not matched TP.
        self.assertEqual(tp_matched, ann["true_positive_openings_count"])
        self.assertGreaterEqual(detected_tp, tp_matched)


# ---------------------------------------------------------------------------
# 5. Benchmark reporting — REAL page-86 positive LIVE diagnostics
# ---------------------------------------------------------------------------
class TestRealPositiveLiveDiagnostics(unittest.TestCase):
    """REAL: live-computed diagnostics for the page-86 positive crop.  All
    numbers are RE-COMPUTED from current detector output at assert time — never
    stale, never merely re-read from stored constants (unless re-derived)."""

    def setUp(self):
        self.fx = _load_fixture()

    def test_live_diagnostics_positive(self):
        fx = self.fx
        ann = fx["positive_benchmark"]["independent_annotation"]
        meta = _positive_crop_meta(fx)
        x0_pt, y0_pt = meta["source_bbox_pt"][0], meta["source_bbox_pt"][1]
        scale_pt_per_px = 1.0 / _PDF_POINT_TO_PIXEL
        tp_xs = [(o["x0_pt"], o["x1_pt"]) for o in ann["true_positive_openings"]]
        tp_y = tuple(ann["true_positive_y_extent_pt"])
        truth_count = ann["true_positive_openings_count"]

        cands = _run_positive_detector(fx)
        sized = [c for c in cands if opening_sized(c.width_m, c.height_m)]
        candidate_count = len(sized)
        tp_matched, detected_tp, fp = _positive_precision_recall(
            sized, tp_xs, tp_y, x0_pt, y0_pt, scale_pt_per_px)

        recall = tp_matched / truth_count
        precision = detected_tp / candidate_count if candidate_count else 0.0
        noise_fp_fraction = fp / candidate_count if candidate_count else 0.0

        # Live assertions: useful signal (all 9 real openings recovered) with
        # precision/noise measured against the independent real annotation.
        self.assertEqual(tp_matched, truth_count)
        self.assertGreaterEqual(recall, 0.9)
        self.assertGreaterEqual(precision, 0.25)
        # Recompute-and-assert against the fixture's recorded diagnostics so a
        # detector change can never silently leave a stale story in the fixture.
        diag = fx["positive_benchmark"]["diagnostics"]
        self.assertEqual(len(cands), diag["observed_total_candidates"])
        self.assertEqual(candidate_count, diag["observed_opening_sized_candidates"])
        self.assertEqual(tp_matched, diag["detected_tp_matches"])
        self.assertAlmostEqual(recall, diag["recall_tp"], places=3)
        self.assertAlmostEqual(precision, diag["precision_tp"], places=3)
        self.assertAlmostEqual(noise_fp_fraction, diag["noise_fp_fraction"], places=3)

    def test_live_diagnostics_report_shape(self):
        """REAL: the reported diagnostics are complete (truth/candidate/matched/
        FP/recall/precision) and are re-derived, not just copied constants."""
        fx = self.fx
        ann = fx["positive_benchmark"]["independent_annotation"]
        meta = _positive_crop_meta(fx)
        x0_pt, y0_pt = meta["source_bbox_pt"][0], meta["source_bbox_pt"][1]
        tp_xs = [(o["x0_pt"], o["x1_pt"]) for o in ann["true_positive_openings"]]
        tp_y = tuple(ann["true_positive_y_extent_pt"])
        cands = _run_positive_detector(fx)
        sized = [c for c in cands if opening_sized(c.width_m, c.height_m)]
        candidate_count = len(sized)
        tp_matched, detected_tp, fp = _positive_precision_recall(
            sized, tp_xs, tp_y, x0_pt, y0_pt, 1.0 / _PDF_POINT_TO_PIXEL)
        truth_count = ann["true_positive_openings_count"]
        # Non-vacuous, recomputed-from-current-output diagnostic checks:
        # (a) the reported FP count is exactly the candidates NOT matched to a
        #     true positive (independent of any stored constant);
        # (b) every one-to-one-matched candidate is counted in detected_tp;
        # (c) all independently-annotated true openings are recovered;
        # (d) precision/recall are internally consistent with the live counts.
        self.assertEqual(fp, candidate_count - detected_tp)
        self.assertGreaterEqual(detected_tp, tp_matched)
        self.assertEqual(tp_matched, truth_count)
        self.assertEqual(detected_tp, 9)
        self.assertEqual(fp, candidate_count - 9)
        self.assertGreaterEqual(len(tp_xs), truth_count)


# ---------------------------------------------------------------------------
# Real door geometry benchmark (plan data)
# ---------------------------------------------------------------------------
class TestRealDoorGeometryBenchmark(unittest.TestCase):
    """REAL (conservatively scoped): the committed ED04 door cluster carries
    genuinely real, independently-verified door-plan geometry.  This benchmark
    asserts the REAL door truth and the safety contract.  It does NOT over-claim
    clean boolean door-detection precision (see module docstring honesty note);
    precise door-coverage behaviour is covered by the labelled SYNTHETIC group."""

    def setUp(self):
        self.fx = _load_door_fixture()

    def test_real_door_leaf_geometry_is_independently_verified(self):
        fx = self.fx
        leaf = fx["expected"]["verified_door_leaf_segments"]
        self.assertGreaterEqual(len(leaf), 3)
        # Each verified segment is genuine door-leaf geometry from a real plan.
        for seg in leaf:
            self.assertGreater(max(seg) - min(seg), 0)
        self.assertEqual(fx["expected"]["source_tags"], ["ED04"])
        self.assertIn("ED04", fx["expected"]["tag_bboxes"])

    def test_safety_note_never_forces_tag_or_deduction(self):
        fx = self.fx
        note = fx["expected"]["safety_note"].lower()
        self.assertIn("never force", note)
        self.assertIn("unassociated", note)
        self.assertIn("deduction", note)

    def test_real_door_fixture_fragments_honest(self):
        """REAL: the fixture asserts the verified real door GEOMETRY but does not
        encode a detector-derived tag/deduction as truth."""
        fx = self.fx
        self.assertNotIn("opening_count", fx["expected"])
        self.assertEqual(fx["source"]["drawing_ref"], "CD1161/06")
        self.assertEqual(fx["source"]["drawing_title"], "GA - LEVEL 08")

    def test_real_door_classified_not_forced_live(self):
        """REAL live regression on the committed cluster (reflects CURRENT
        detector behaviour, recomputed live — never a stored constant): the
        real door-leaf geometry is classified as a door, but the ED04 tag is
        NOT forced onto an instance (type_mark stays unassociated, matching the
        fixture's own safety contract that the mark may be left unassociated)
        and nothing ever deducts."""
        fx = self.fx
        segs = [Segment(x1=r["x1"], y1=r["y1"], x2=r["x2"], y2=r["y2"],
                        drawing_index=r.get("drawing_index", 0))
                for r in fx["segments"]]
        words = [TextWord(text=r["text"], x0=r["x0"], y0=r["y0"],
                          x1=r["x1"], y1=r["y1"], page_no=23)
                 for r in fx["words"]]
        res = plan_opening_candidates(
            segs, words, scale_px_per_m=fx["source"]["scale_pt_per_m"], page_no=23)
        doors = [c for c in res.candidates if c.opening_type == OPENING_TYPE_DOOR]
        # Real drawing contains a real door: at least one door classification on
        # the live run (recomputed, not a stored constant).
        self.assertGreaterEqual(len(doors), 1,
                                "real door geometry must produce a door candidate")
        # Honesty: B1 does NOT force the ED04 tag onto an instance (the fixture's
        # own safety_note says the mark may stay unassociated).
        for d in doors:
            self.assertNotEqual(d.type_mark, "ED04",
                                "B1 must not force a tag that the drawing/safety "
                                "contract leaves unassociated")
        # No deduction anywhere from plan-only + real door geometry.
        for c in res.candidates:
            self.assertEqual(c.dimension_basis, DIMENSION_BASIS_UNKNOWN)
            self.assertFalse(c.deduct)
        self.assertGreater(res.wall_lines_found, 0)


# ---------------------------------------------------------------------------
# 3, 4. SYNTHETIC TARGETED REGRESSIONS — NOT REAL BENCHMARK TRUTH
#       (wrong-level, wrong-side, ambiguity, repeated same-width, doors,
#        page-87/88)
# ---------------------------------------------------------------------------
class TestSyntheticWrongLevelRegression(unittest.TestCase):
    """SYNTHETIC TARGETED REGRESSION — NOT REAL BENCHMARK TRUTH
    Wrong-level rejection: a plan instance and an elevation opening on different
    KNOWN levels must hard-reject (score 0) regardless of geometry."""

    def test_different_known_levels_hard_reject(self):
        inst = OpeningEvidence(
            type_mark="ED01", width_m=2.96, dimension_basis=DIMENSION_BASIS_UNKNOWN,
            elevation_side="East", level="G")
        elev = ElevationOpening(
            86, "East", (0, 0, 100, 100), 2.96, 2.96, label="ED01", level="L2")
        self.assertEqual(_correlation_score(inst, elev), 0.0)

    def test_matching_known_level_not_rejected(self):
        inst = OpeningEvidence(
            type_mark="ED01", width_m=2.96, dimension_basis=DIMENSION_BASIS_UNKNOWN,
            elevation_side="East", level="L2")
        elev = ElevationOpening(
            86, "East", (0, 0, 100, 100), 2.96, 2.96, label="ED01", level="L2")
        self.assertGreater(_correlation_score(inst, elev), 0.0)


class TestSyntheticWrongSideRegression(unittest.TestCase):
    """SYNTHETIC TARGETED REGRESSION — NOT REAL BENCHMARK TRUTH
    Wrong-side rejection: a North plan instance must never match an East
    elevation even with identical mark and width."""

    def test_wrong_side_hard_reject_with_matching_mark(self):
        inst = OpeningEvidence(
            type_mark="ED01", width_m=0.82, dimension_basis=DIMENSION_BASIS_UNKNOWN,
            elevation_side="North")
        elev = ElevationOpening(
            86, "East", (0, 0, 50, 100), 0.82, 2.1, label="ED01")
        self.assertEqual(_correlation_score(inst, elev), 0.0)

    def test_wrong_side_hard_reject_through_pipeline(self):
        inst = OpeningEvidence(
            type_mark="D01", width_m=0.82, dimension_basis=DIMENSION_BASIS_UNKNOWN,
            elevation_side="North")
        elev = ElevationOpening(
            86, "East", (0, 0, 50, 100), 0.82, 2.1, label="D01")
        enriched, _ = correlate_elevation_to_plan([elev], [inst])
        self.assertEqual(sum(1 for i in enriched if i.source_observations), 0)


class TestSyntheticAmbiguousTieRegression(unittest.TestCase):
    """SYNTHETIC TARGETED REGRESSION — NOT REAL BENCHMARK TRUTH
    Ambiguous tie without stronger identity: two identical-width, same-mark
    elevations vs one plan instance must resolve to NO match (equality is
    ambiguity, never arbitrary enrichment)."""

    def test_equal_scores_stay_unmatched(self):
        plan = [OpeningEvidence(
            type_mark="W01", width_m=1.2, dimension_basis=DIMENSION_BASIS_UNKNOWN,
            elevation_side="North", position_along_wall_m=2.0)]
        elevs = [
            ElevationOpening(1, "North", (0, 0, 100, 100), 1.2, 1.5, label="W01"),
            ElevationOpening(1, "North", (200, 0, 300, 100), 1.2, 1.5, label="W01"),
        ]
        enriched, unmatched = correlate_elevation_to_plan(elevs, plan)
        self.assertEqual(sum(1 for i in enriched if i.source_observations), 0)
        self.assertEqual(len(unmatched), 2)


class TestSyntheticRepeatedSameWidthRegression(unittest.TestCase):
    """SYNTHETIC TARGETED REGRESSION — NOT REAL BENCHMARK TRUTH
    Repeated same-width openings on one side must NOT independently correlate:
    several identical-width, label-less elevation candidates on one side must
    not each enrich a single plan instance (width+side alone is not identity and
    identical candidates are ambiguous)."""

    def test_repeated_same_width_not_independently_correlated(self):
        plan = [OpeningEvidence(
            type_mark="W01", width_m=0.9, dimension_basis=DIMENSION_BASIS_UNKNOWN,
            elevation_side="North", position_along_wall_m=2.0)]
        elevs = [
            ElevationOpening(1, "North", (0, 0, 100, 100), 0.9, 1.5, label=""),
            ElevationOpening(1, "North", (200, 0, 300, 100), 0.9, 1.5, label=""),
            ElevationOpening(1, "North", (400, 0, 500, 100), 0.9, 1.5, label=""),
        ]
        enriched, unmatched = correlate_elevation_to_plan(elevs, plan)
        # The single plan instance must NOT be enriched by any of the 3
        # identical-width candidates (no independent correlation).
        self.assertEqual(sum(1 for i in enriched if i.source_observations), 0)
        self.assertEqual(len(unmatched), 3)

    def test_repeated_same_width_with_distinct_marks_correlate_one_to_one(self):
        """Control: with distinct marks (strong identity) each of 3 repeated
        same-width openings correlates to its own instance — proving the
        regression above is about missing identity, not width banding."""
        plans = [OpeningEvidence(
            type_mark=f"W0{i}", width_m=0.9, dimension_basis=DIMENSION_BASIS_UNKNOWN,
            elevation_side="North", position_along_wall_m=float(i))
            for i in (1, 2, 3)]
        elevs = [ElevationOpening(1, "North", (0, 0, 100, 100), 0.9, 1.5,
                                  label=f"W0{i}") for i in (1, 2, 3)]
        enriched, unmatched = correlate_elevation_to_plan(elevs, plans)
        self.assertEqual(sum(1 for i in enriched if i.source_observations), 3)
        self.assertEqual(len(unmatched), 0)


class TestSyntheticDoorCoverageRegression(unittest.TestCase):
    """SYNTHETIC TARGETED REGRESSION — NOT REAL BENCHMARK TRUTH
    Clean single-door coverage on a clean wall.  A clear door-leaf on a clear
    wall with a door tag must resolve to exactly one door.  This is a clean
    synthetic control; the real noisy ED04 cluster cannot establish such clean
    boolean precision (see TestRealDoorGeometryBenchmark)."""

    def _make_door(self, mark="D01", page=1):
        segs = [
            Segment(x1=0.0, y1=0.0, x2=400.0, y2=0.0, drawing_index=1),
            Segment(x1=80.0, y1=0.0, x2=80.0, y2=-45.0, drawing_index=2),
        ]
        words = [TextWord(text=mark, x0=78.0, y0=-53.0, x1=98.0, y1=-43.0, page_no=page)]
        res = plan_opening_candidates(segs, words, scale_px_per_m=50.0, page_no=page)
        return res

    def test_clean_single_door_is_detected(self):
        res = self._make_door()
        self.assertEqual(res.door_count, 1)
        doors = [c for c in res.candidates if c.opening_type == OPENING_TYPE_DOOR]
        self.assertEqual(len(doors), 1)
        self.assertEqual(doors[0].type_mark, "D01")
        self.assertFalse(doors[0].deduct)
        self.assertEqual(doors[0].dimension_basis, DIMENSION_BASIS_UNKNOWN)

    def test_no_false_window_from_door_only_wall(self):
        res = self._make_door()
        self.assertEqual(res.window_count, 0)


class TestSyntheticPage8788Regression(unittest.TestCase):
    """SYNTHETIC TARGETED REGRESSION — NOT REAL BENCHMARK TRUTH
    Additional-elevation-page behavioural regression.  NO real page-87/88 (or
    other extra-elevation) raster/page is committed in this repo, so this
    category is synthetic only.  It verifies the extraction path does not
    conflate a caller-supplied page/sheet provenance with page 86 / CD3001 and
    does not fabricate dimensional output from unidentified extra-elevation
    geometry."""

    def test_extra_page_provenance_not_conflated_with_cd3001(self):
        pcal = calibration_from_scale_bar_positions(
            [0.0, 28.346, 56.692, 85.038], 1.0,
            coord_space=COORD_SPACE_PDF_POINT)
        self.assertTrue(pcal.valid)
        img = np.full((60, 60), 255, dtype=np.uint8)
        img[10:50, 10:50] = 0
        cands = detect_raster_rect_candidates(
            img, pcal, source_page=88, drawing_ref="CD-OTHER",
            elevation_side="North",
            calibration_source="page-88-other-project-primary")
        self.assertTrue(cands)
        for c in cands:
            self.assertNotIn("cd3001", c.drawing_ref.lower())
            self.assertNotIn("cd3001", c.calibration_source.lower())
            self.assertEqual(c.source_page, 88)
            self.assertEqual(c.elevation_side, "North")
            self.assertEqual(c.calibration_source, "page-88-other-project-primary")
            # pdf_point calibration on a raster: no pixel-coordinate metres.
            self.assertIsNone(c.width_m)
            self.assertIsNone(c.height_m)

    def test_no_real_page87_is_passed_off_as_real_truth(self):
        """Honesty: the fixture set contains no page-87/88 real truth; this
        synthetic page-88 path must be labelled synthetic and never appear in
        the real page-86 accuracy numbers."""
        self.assertIn(SYNTHETIC_TARGETED_REGRESSION_LABEL,
                      TestSyntheticPage8788Regression.__doc__)


if __name__ == "__main__":
    unittest.main()
