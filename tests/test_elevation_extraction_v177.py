"""PlanReader v1.7.7 Phase 2A + focused corrections — elevation extraction and
real benchmark safety tests.

Covers the Phase 2A pass conditions, the reviewer's focused corrections
(real, not synthetic, benchmark; corrected 11/10/10 scale bar; coordinate-space
naming and guards; mark-family classifier; gap-safe vector clustering; and the
max_cells return contract) and the 12 required safety tests.

Pass conditions (measured on the REAL committed page-86 crop):
  P1  Measured calibration reproduces the independently-measured scale
      (source graphic scale bar: 11 ticks 0..10, 10 divisions, 10 m).
  P2  The extractor reproduces the independently-measured repeated cell
      geometry (width ~2.96 m AND height ~1.57 m) within tolerance on the
      REAL render crop.
  P3  That reproduced geometry is within the documented tolerance of the
      independent cell annotation.
  P4  False positives in the real benchmark crop stay within a documented
      conservative limit.
  P5  No result acquires rough-opening authority or deduction authority.

Focused correction regressions:
  C   Calibration stores the general units_per_m and exposes typed, strictly
      coordinated pt_per_m / px_per_m accessors (nothing describes 28.346
      pt/m as pixels/metre).
  D   detect_elevation_openings fails closed (no dimensional candidate) when
      a rect's declared coordinate space differs from the calibration space.
  E   ED/ID/EW/IW marks classify by approved mark family (never label[0]);
      window+ED/ID elevation and door+EW/IW elevation are hard-rejected.
  F   _cluster_lines is gap-safe: separated collinear fragments cannot be
      bridged into an invented continuous side / rectangle.
  G   recover_vector_rects ALWAYS returns List[VectorRectCandidate] — never
      raw tuples — and signals truncation/review when the work cap is hit.

Final Phase-2A issues:
  POS  A genuinely-visible POSITIVE opening benchmark (9 independently
       annotated glazed window lights) whose precision/noise is measured
       against the independent annotation — not against a threshold set at
       the detector's current output.
  R1   A raster extractor's bbox/centroid are ALWAYS render_pixel regardless
       of the calibration, and width/height stay None with a non-render cal.
  R2   calibration/source provenance comes from caller arguments — never a
       hard-coded "page-86-cd3001".
  R3   The fixture records original_source_filename AND local_source_alias;
       the renamed alias is not the verbatim canonical source.

Preserved safety requirements:
  1-12 as before (real calibration; unknown dimension basis; no deduct; no
  instance creation; ambiguous/duplicate review; wrong side/level rejected;
  equal scores unmatched; approved mark families + junk rejection; raster vs
  pdf_point never mixed).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import cv2

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
    _mark_family_prefix,
)
from pb_opening_evidence_v170 import (
    OpeningEvidence,
    DIMENSION_BASIS_UNKNOWN,
    DEDUCTION_REVIEW,
    OPENING_TYPE_DOOR,
    OPENING_TYPE_WINDOW,
)

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "lago_cd3001_east_elevation_v177.json"
_CROP = Path(__file__).resolve().parent / "fixtures" / "lago_cd3001_p86_e1east_roi_150dpi.png"
# Positive real-opening benchmark crop (clearly-visible glazed window group).
_POS_CROP = Path(__file__).resolve().parent / "fixtures" / \
    "lago_cd3001_p86_e1east_glazed_open_group_150dpi.png"

# Derived render transform used for the benchmark render (150 DPI).
_RENDER_DPI = 150.0
_PDF_POINT_TO_PIXEL = _RENDER_DPI / 72.0


def _load_fixture():
    with open(_FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def _tolerance_rel(a, b, rel=0.02):
    """Relative tolerance comparison."""
    return abs(a - b) <= rel * max(abs(a), abs(b), 1e-9)


def _render_pixel_calibration(fx):
    """Calibration (render_pixel, 150 DPI) derived from the fixture scale."""
    scale_pt_per_m = fx["calibration"]["scale_pt_per_m"]
    px_per_m = scale_pt_per_m * _PDF_POINT_TO_PIXEL
    return calibration_from_scale_bar_positions(
        [0.0, px_per_m, 2 * px_per_m, 3 * px_per_m], 1.0,
        coord_space=COORD_SPACE_RENDER_PIXEL, render_dpi=_RENDER_DPI)


def _load_real_crop():
    """Load the committed REAL page-86 render crop as an HxW grayscale array."""
    img = cv2.imread(str(_CROP), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise AssertionError(f"real benchmark crop not readable: {_CROP}")
    return img


def _load_positive_crop():
    """Load the committed REAL page-86 glazed-opening-group crop (positive)."""
    img = cv2.imread(str(_POS_CROP), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise AssertionError(f"positive benchmark crop not readable: {_POS_CROP}")
    return img


def _overlap_frac(a0, a1, b0, b1):
    """Fraction of the shorter interval [b0,b1] overlapped by [a0,a1]."""
    lo, hi = max(a0, b0), min(a1, b1)
    width = min(a1 - a0, b1 - b0)
    if width <= 1e-9:
        return 0.0
    return max(0.0, hi - lo) / width


def _positive_precision_recall(cands, tp_xs, tp_y, x0_pt, y0_pt, scale_pt_per_px,
                               x_tol_frac=0.5, y_tol_frac=0.45):
    """Match detected candidates (crop-local pixels) to independent TP openings
    (source PDF-points) with ONE-TO-ONE assignment.  Uses independent
    annotations ONLY, never detector-derived truth.

    A true opening can accept at most ONE candidate; once a candidate is
    assigned to an opening, a second candidate overlapping that same opening
    is counted as a duplicate -> false positive (noise).  This is what makes
    precision a meaningful measure of detector noise.

    Returns (tp_matched_count, detected_tp_count, fp_count).
    """
    head, sill = tp_y
    # Candidate crop-local pixel bb -> source PDF-point bb.
    boxes = []
    for c in cands:
        xl, yl, xr, yr = c.bbox
        boxes.append((x0_pt + xl * scale_pt_per_px,
                      y0_pt + yl * scale_pt_per_px,
                      x0_pt + xr * scale_pt_per_px,
                      y0_pt + yr * scale_pt_per_px))

    claimed = [False] * len(cands)
    tp_matched = 0
    # For each true opening, assign its BEST (most-filled) still-unclaimed
    # candidate.  Greedy-by-opening is deterministic regardless of the order
    # the detector returned its candidates.
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

    detected_tp = sum(claimed)  # 1-to-1: each claimed candidate maps to one opening
    assert detected_tp == tp_matched
    fp = len(cands) - detected_tp
    return tp_matched, detected_tp, fp


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

    def test_scale_bar_is_11_ticks_10_divisions_10m(self):
        """Block B: the scale bar labels 0..10 -> 11 positions, 10 divisions."""
        fx = _load_fixture()
        g = fx["calibration"]["measured_bar_geometry"]
        self.assertEqual(len(g["label_positions_x_pt"]), 11)
        self.assertEqual(len(g["labels"]), 11)
        self.assertEqual(len(g["tick_positions_x_pt"]), 11)
        self.assertEqual(len(g["division_spacings_pt"]), 10)
        self.assertEqual(fx["calibration"]["n_labels"], 11)
        self.assertEqual(fx["calibration"]["n_divisions"], 10)
        # 10 divisions x ~28.346 pt = ~283.46 pt = 10 m.
        self.assertTrue(_tolerance_rel(
            g["spanned_pt"], 10 * fx["calibration"]["scale_pt_per_m"], rel=0.01))

    def test_calibration_reproduces_independent_measurement(self):
        """P1: measured calibration reproduces the independent scale."""
        fx = _load_fixture()
        expected_pt_per_m = fx["calibration"]["scale_pt_per_m"]  # 28.346

        # Independent measurement: graphic scale bar ticks 0..10 are each
        # 1 m apart (10 divisions -> 10 m).
        positions = fx["calibration"]["measured_bar_geometry"]["tick_positions_x_pt"]
        cal = calibration_from_scale_bar_positions(
            positions, 1.0, coord_space=COORD_SPACE_PDF_POINT,
            source_page=86, labels=fx["calibration"]["measured_bar_geometry"]["labels"],
        )
        self.assertTrue(cal.valid)
        self.assertGreater(cal.confidence, 0.9)
        self.assertIsNotNone(cal.pt_per_m)
        self.assertTrue(
            _tolerance_rel(cal.pt_per_m, expected_pt_per_m, rel=0.01),
            f"pt_per_m {cal.pt_per_m} vs fixture {expected_pt_per_m}",
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
# Block C — units_per_m with typed, strictly coordinated accessors
# ---------------------------------------------------------------------------
class TestTypedCoordinateAccessors(unittest.TestCase):
    """units_per_m is general; pt_per_m / px_per_m are space-typed & exclusive."""

    def _pdf_cal(self):
        return calibration_from_scale_bar_positions(
            [0.0, 28.346, 56.692, 85.038], 1.0,
            coord_space=COORD_SPACE_PDF_POINT)

    def _px_cal(self):
        return calibration_from_scale_bar_positions(
            [0.0, 59.055, 118.11, 177.17], 1.0,
            coord_space=COORD_SPACE_RENDER_PIXEL, render_dpi=_RENDER_DPI)

    def test_pdf_calibration_exposes_only_pt_per_m(self):
        cal = self._pdf_cal()
        self.assertIsNotNone(cal.pt_per_m)
        self.assertIsNone(cal.px_per_m)      # 28.346 pt/m is NEVER pixels/metre
        self.assertAlmostEqual(cal.pt_per_m, 28.346, delta=0.1)

    def test_pixel_calibration_exposes_only_px_per_m(self):
        cal = self._px_cal()
        self.assertIsNotNone(cal.px_per_m)
        self.assertIsNone(cal.pt_per_m)
        self.assertAlmostEqual(cal.px_per_m, 59.055, delta=0.1)

    def test_as_dict_uses_units_per_m_and_typed_values(self):
        cal = self._pdf_cal()
        d = cal.as_dict()
        self.assertIn("units_per_m", d)
        self.assertEqual(d["units_per_m"], cal.units_per_m)
        self.assertIsNone(d["px_per_m"])     # typed: not pixels for pdf_point
        self.assertEqual(d["pt_per_m"], cal.pt_per_m)

    def test_to_meters_uses_general_units_per_m(self):
        cal = self._pdf_cal()
        self.assertTrue(_tolerance_rel(cal.to_meters(28.346), 1.0, rel=0.01))
        self.assertTrue(_tolerance_rel(cal.to_meters(283.46), 10.0, rel=0.01))


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

    def test_raster_rejects_pdf_point_calibration_for_metres(self):
        """Raster extractor must NOT produce pixel metres from a pt/m cal."""
        pcal = calibration_from_scale_bar_positions(
            [0.0, 28.346, 56.692, 85.038], 1.0,
            coord_space=COORD_SPACE_PDF_POINT)
        # A synthetic blank-ish image with a dark rectangle (real-path guard).
        img = np.full((60, 60), 255, dtype=np.uint8)
        img[10:50, 10:50] = 0
        cands = detect_raster_rect_candidates(img, pcal, source_page=86)
        # dimensional is False for a pt/m cal on a raster -> no metre widths,
        # and the bbox is STILL correctly labelled render_pixel.
        self.assertTrue(cands)
        for c in cands:
            self.assertIsNone(c.width_m)
            self.assertIsNone(c.height_m)
            self.assertEqual(c.coord_space, COORD_SPACE_RENDER_PIXEL)

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
# P2 / P3 / P4 — REAL committed crop benchmark (not synthetic)
# ---------------------------------------------------------------------------
class TestRealCropBenchmark(unittest.TestCase):
    """Run the raster extractor on the REAL page-86 crop, not a synthetic ROI."""

    def test_real_crop_matches_committed_artifact(self):
        """Block A: the committed crop is present, sized, checksum-proven."""
        fx = _load_fixture()
        crop_meta = fx["benchmark"]["crop"]
        self.assertTrue(_CROP.exists(), "real benchmark crop must be committed")
        img = _load_real_crop()
        self.assertEqual(img.shape[1], crop_meta["width_px"])   # 177
        self.assertEqual(img.shape[0], crop_meta["height_px"])  # 952
        checksum = hashlib.sha256(_CROP.read_bytes()).hexdigest()
        self.assertEqual(checksum, crop_meta["sha256"],
                         "crop checksum must match committed provenance")
        # The crop is derived from the real PDF source bbox + render DPI/matrix.
        self.assertEqual(crop_meta["source_bbox_pt"],
                         fx["benchmark_regions"][0]["source_bbox_pt"])
        self.assertEqual(fx["render"]["dpi"], _RENDER_DPI)

    def test_real_crop_produces_repeated_cell_geometry(self):
        """P2/P3: >=1 candidate reproduces the independent cell geometry."""
        fx = _load_fixture()
        cal = _render_pixel_calibration(fx)
        img = _load_real_crop()
        cands = detect_raster_rect_candidates(
            img, cal,
            source_filename=fx["source"]["pdf"],
            source_page=fx["source"]["page_1_based"],
            drawing_ref=fx["source"]["drawing_no"],
            elevation_side=fx["source"]["elevation_side"],
        )
        self.assertGreaterEqual(len(cands), 1)
        cell = fx["benchmark"]["independently_measured_cell"]
        tol = fx["benchmark"]["expected_on_real_crop"]["geometry_tolerance_m"]
        exp_w, exp_h = cell["width_m"], cell["height_m"]

        sized = [c for c in cands if opening_sized(c.width_m, c.height_m)]
        matches = [c for c in sized
                   if (abs(c.width_m - exp_w) <= tol and abs(c.height_m - exp_h) <= tol)]
        min_matches = fx["benchmark"]["expected_on_real_crop"]["min_dimension_matching_candidates"]
        self.assertGreaterEqual(len(matches), min_matches,
                                "must reproduce the real ~2.96 x 1.57 m cell")

        # All candidates stay pure geometry observations.
        for c in sized:
            self.assertEqual(c.dimension_basis, DIMENSION_BASIS_UNKNOWN)
            self.assertEqual(c.review_status, "review")
            self.assertEqual(c.elevation_side, fx["source"]["elevation_side"])
            self.assertEqual(c.drawing_ref, fx["source"]["drawing_no"])

    def test_real_crop_false_positives_within_conservative_limit(self):
        """P4: opening-sized false positives within a documented cap."""
        fx = _load_fixture()
        cal = _render_pixel_calibration(fx)
        img = _load_real_crop()
        cands = detect_raster_rect_candidates(img, cal, source_page=86)
        sized = [c for c in cands if opening_sized(c.width_m, c.height_m)]
        limit = fx["benchmark"]["expected_on_real_crop"]["false_positive_limit_opening_sized"]
        self.assertLessEqual(len(sized), limit,
                             f"opening-sized candidates {len(sized)} exceed limit {limit}")

    def test_real_crop_does_not_encode_detector_truth(self):
        """Block A: fixture must not assert a detector-derived opening count."""
        fx = _load_fixture()
        roi = fx["benchmark_regions"][0]
        self.assertIsNone(roi["unknown_identity_facts"]["opening_count"])
        self.assertEqual(roi["unknown_identity_facts"]["commercial_identity"], "unproven")
        self.assertEqual(fx["benchmark"]["independently_measured_cell"]["commercial_identity"],
                         "unproven")
        self.assertGreater(len(fx["benchmark"]["crop"]["sha256"]), 40)


# ---------------------------------------------------------------------------
# safety 3,4,5,6 — authority / instance / deduction boundaries
# ---------------------------------------------------------------------------
class TestNoAuthorityNoInstances(unittest.TestCase):
    """Candidates and enrichments never acquire deduction/instance authority."""

    def test_raster_candidates_have_no_deduct_and_unknown_basis(self):
        """safety 4,5: generic candidate stays basis=unknown, never deduct."""
        fx = _load_fixture()
        cal = _render_pixel_calibration(fx)
        img = _load_real_crop()
        cands = detect_raster_rect_candidates(img, cal, source_page=86)
        self.assertGreaterEqual(len(cands), 1)
        for c in cands:
            self.assertEqual(c.dimension_basis, DIMENSION_BASIS_UNKNOWN)
            self.assertFalse(hasattr(c, "deduct"), "candidate must not carry deduct")

    def test_non_dimensional_page_reports_no_metric_dimensions(self):
        """safety 3: invalid calibration -> measured width/height are None."""
        fail_cal = calibration_from_scale_bar_positions(
            [0.0, 10.0, 500.0], 1.0, coord_space=COORD_SPACE_PDF_POINT)
        self.assertFalse(fail_cal.valid)
        img = _load_real_crop()
        cands = detect_raster_rect_candidates(img, fail_cal, source_page=86)
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
# Block D — coordinate-space hard guard in detect_elevation_openings
# ---------------------------------------------------------------------------
class TestCoordinateSpaceGuardInDetect(unittest.TestCase):
    """detect_elevation_openings fails closed when rect/calibration spaces differ."""

    def _rect(self, coord_space, bbox=(100, 100, 260, 260)):
        return {"bbox": bbox, "confidence": 0.6, "coord_space": coord_space}

    def test_pdf_rect_against_render_calibration_fails_closed(self):
        """A pdf_point rect with a render_pixel calibration -> NO candidate."""
        rects = [self._rect(COORD_SPACE_PDF_POINT)]
        opens = detect_elevation_openings(
            86, "East", rects, [], 59.055,
            coord_space=COORD_SPACE_RENDER_PIXEL)
        self.assertEqual(opens, [])  # fail closed, never a dimensional candidate

    def test_render_rect_against_pdf_calibration_fails_closed(self):
        """A render_pixel rect with a pdf_point calibration -> NO candidate."""
        rects = [self._rect(COORD_SPACE_RENDER_PIXEL)]
        opens = detect_elevation_openings(
            86, "East", rects, [], 28.346,
            coord_space=COORD_SPACE_PDF_POINT)
        self.assertEqual(opens, [])

    def test_matching_space_rect_succeeds(self):
        """A rect in the SAME space as the calibration -> candidate produced."""
        rects = [self._rect(COORD_SPACE_RENDER_PIXEL)]
        opens = detect_elevation_openings(
            86, "East", rects, [], 59.055,
            coord_space=COORD_SPACE_RENDER_PIXEL)
        self.assertEqual(len(opens), 1)
        self.assertEqual(opens[0].coord_space, COORD_SPACE_RENDER_PIXEL)
        self.assertTrue(_tolerance_rel(opens[0].width_m, 160.0 / 59.055, rel=0.02))

    def test_mixed_rects_only_matching_space_emits(self):
        """In a mixed set, only same-space rects produce candidates."""
        rects = [
            self._rect(COORD_SPACE_RENDER_PIXEL),
            self._rect(COORD_SPACE_PDF_POINT),
            self._rect(COORD_SPACE_RENDER_PIXEL),
        ]
        opens = detect_elevation_openings(
            86, "East", rects, [], 59.055,
            coord_space=COORD_SPACE_RENDER_PIXEL)
        self.assertEqual(len(opens), 2)
        for o in opens:
            self.assertEqual(o.coord_space, COORD_SPACE_RENDER_PIXEL)


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
        sc = _correlation_score(inst, elev)
        self.assertGreater(sc, 0.0)
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
        self.assertEqual(sum(1 for i in enriched if i.source_observations), 0)
        self.assertEqual(len(unmatched), 2)


# ---------------------------------------------------------------------------
# safety 11 — approved mark families, junk rejection, and
#            Block E — classifier (never label[0]) + hard type rejects
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
        words = [self._word_new("ED01", 140, 200, 190, 220)]
        self.assertEqual(_extract_label_near_rect(words, (100, 100, 300, 500)), "ED01")

    def test_legacy_word_format_still_accepted(self):
        words = [{"0": 140, "1": 200, "2": 190, "3": 220, "4": "D01"}]
        self.assertEqual(_extract_label_near_rect(words, (100, 100, 300, 500)), "D01")

    def test_junk_suffix_rejected(self):
        for junk in ("ED01XYZ", "D01junk", "IW05foo", "I", "II", "IV"):
            words = [self._word_new(junk, 140, 200, 190, 220)]
            self.assertEqual(_extract_label_near_rect(words, (100, 100, 300, 500)), "")

    def test_no_false_marks_from_prose(self):
        words = [self._word_new("Fire door D01a at Level 1", 100, 150, 400, 170)]
        self.assertEqual(_extract_label_near_rect(words, (100, 100, 300, 500)), "")

    def test_detect_uses_new_families(self):
        rects = [{"bbox": [140, 100, 200, 300], "confidence": 0.6}]
        words = [{"text": "EW03", "bbox": [150, 60, 200, 80]}]
        opens = detect_elevation_openings(86, "East", rects, words, 100.0)
        self.assertTrue(opens)
        self.assertEqual(opens[0].label, "EW03")


class TestMarkFamilyClassifier(unittest.TestCase):
    """Block E: ED/ID/EW/IW classify by approved family, never label[0]."""

    def test_family_prefix_classifies_correctly(self):
        self.assertEqual(_mark_family_prefix("D01"), "D")
        self.assertEqual(_mark_family_prefix("ED01"), "D")  # label[0] would say 'E'
        self.assertEqual(_mark_family_prefix("ID01"), "D")  # label[0] would say 'I'
        self.assertEqual(_mark_family_prefix("W02"), "W")
        self.assertEqual(_mark_family_prefix("EW03"), "W")  # label[0] would say 'E'
        self.assertEqual(_mark_family_prefix("IW12"), "W")  # label[0] would say 'I'

    def test_junk_gets_no_family(self):
        for junk in ("I", "II", "IV", "ED01XYZ", ""):
            self.assertEqual(_mark_family_prefix(junk), "")

    def _inst(self, opening_type, mark):
        return OpeningEvidence(
            type_mark=mark, width_m=1.2, dimension_basis=DIMENSION_BASIS_UNKNOWN,
            elevation_side="East")

    def test_window_instance_with_door_elevation_hard_rejected(self):
        """window B1 + ED01 / ID01 elevation -> hard reject."""
        for door_label in ("ED01", "ID01"):
            inst = self._inst(OPENING_TYPE_WINDOW, "W03")
            elev = ElevationOpening(1, "East", (0, 0, 100, 100), 1.2, 1.5,
                                    label=door_label)
            self.assertEqual(_correlation_score(inst, elev), 0.0,
                             f"window must NOT match {door_label}")

    def test_door_instance_with_window_elevation_hard_rejected(self):
        """door B1 + EW01 / IW01 elevation -> hard reject."""
        for window_label in ("EW01", "IW01"):
            inst = self._inst(OPENING_TYPE_DOOR, "D01")
            elev = ElevationOpening(1, "East", (0, 0, 100, 100), 1.2, 1.5,
                                    label=window_label)
            self.assertEqual(_correlation_score(inst, elev), 0.0,
                             f"door must NOT match {window_label}")

    def test_matching_family_is_not_rejected(self):
        """door B1 + ED elevation / window B1 + EW elevation still match."""
        door_inst = self._inst(OPENING_TYPE_DOOR, "ED01")
        door_elev = ElevationOpening(1, "East", (0, 0, 100, 100), 1.2, 1.5,
                                     label="ED01")
        self.assertGreater(_correlation_score(door_inst, door_elev), 0.0)


# ---------------------------------------------------------------------------
# Block F — gap-safe vector line clustering
# ---------------------------------------------------------------------------
class TestGapSafeClustering(unittest.TestCase):
    """Separated collinear fragments must NOT be bridged into a rectangle."""

    def _cal(self):
        return calibration_from_scale_bar_positions(
            [0.0, 28.346, 56.692, 85.038], 1.0,
            coord_space=COORD_SPACE_PDF_POINT)

    def test_separated_fragments_cannot_form_bridging_rectangle(self):
        """Two spatially separated collinear fragments -> no invented side."""
        cal = self._cal()
        # Two horizontal fragments at y=20: x[0,10] and x[100,110] (big gap).
        # Two horizontal fragments at y=120 likewise. Vertical lines at
        # x=0,10,100,110 span y[20,120]. Each fragment pair closes a small
        # rectangle of width 10, but the gap between fragments must NOT be
        # bridged into a wide invented side (width ~110).
        segs = [
            {"x1": 0, "y1": 20, "x2": 10, "y2": 20},
            {"x1": 100, "y1": 20, "x2": 110, "y2": 20},
            {"x1": 0, "y1": 120, "x2": 10, "y2": 120},
            {"x1": 100, "y1": 120, "x2": 110, "y2": 120},
            {"x1": 0, "y1": 20, "x2": 0, "y2": 120},
            {"x1": 10, "y1": 20, "x2": 10, "y2": 120},
            {"x1": 100, "y1": 20, "x2": 100, "y2": 120},
            {"x1": 110, "y1": 20, "x2": 110, "y2": 120},
        ]
        vc = recover_vector_rects(segs, cal, source_page=86)
        # The fragments DO legitimately close two small 10-wide rectangles,
        # but they must NEVER be bridged into a wide invented side spanning
        # the gap (x 0..110).  Assert no candidate spans (bridges) the gap.
        for c in vc:
            self.assertNotAlmostEqual(c.bbox[2] - c.bbox[0], 110.0, delta=2.0,
                                      msg="separated collinear fragments must "
                                          "not be bridged into an invented wide side")
        # Sanity: with a single continuous side the rectangle WOULD close.
        segs_cont = [
            {"x1": 0, "y1": 20, "x2": 110, "y2": 20},
            {"x1": 0, "y1": 120, "x2": 110, "y2": 120},
            {"x1": 0, "y1": 20, "x2": 0, "y2": 120},
            {"x1": 110, "y1": 20, "x2": 110, "y2": 120},
        ]
        vc2 = recover_vector_rects(segs_cont, cal, source_page=86)
        self.assertEqual(len(vc2), 1)
        self.assertAlmostEqual(vc2[0].bbox[2] - vc2[0].bbox[0], 110.0, delta=0.1)


# ---------------------------------------------------------------------------
# Block G — max_cells return contract
# ---------------------------------------------------------------------------
class TestMaxCellsReturnContract(unittest.TestCase):
    """recover_vector_rects always returns List[VectorRectCandidate]."""

    def _cal(self):
        return calibration_from_scale_bar_positions(
            [0.0, 28.346, 56.692, 85.038], 1.0,
            coord_space=COORD_SPACE_PDF_POINT)

    def _grid_segments(self):
        """A 4x4 grid -> many cells; enough to exceed max_cells=1."""
        xs = [0, 40, 80, 120]
        ys = [0, 40, 80, 120]
        segments = []
        for y in ys:
            segments.append({"x1": min(xs), "y1": y, "x2": max(xs), "y2": y})
        for x in xs:
            segments.append({"x1": x, "y1": min(ys), "x2": x, "y2": max(ys)})
        return segments

    def test_max_cells_returns_candidates_not_raw_tuples(self):
        cal = self._cal()
        segs = self._grid_segments()
        result = recover_vector_rects(segs, cal, source_page=86, max_cells=1)
        self.assertIsInstance(result, list)
        # A cap hit must NEVER be an empty, indistinguishable return: because
        # the cap counts accepted cells, at least the first valid cell exists
        # and carries the WORK-CAP truncation note.
        self.assertTrue(result, "cap hit must not return an empty list")
        for item in result:
            self.assertIsInstance(item, VectorRectCandidate,
                                  "must never return raw tuples on cap hit")
            self.assertEqual(item.dimension_basis, DIMENSION_BASIS_UNKNOWN)
            self.assertIn("WORK-CAP", " ".join(item.notes))

    def test_no_cap_return_has_no_truncation_note(self):
        cal = self._cal()
        segs = self._grid_segments()
        result = recover_vector_rects(segs, cal, source_page=86, max_cells=100000)
        self.assertIsInstance(result, list)
        for item in result:
            self.assertIsInstance(item, VectorRectCandidate)
            self.assertNotIn("WORK-CAP", " ".join(item.notes))

    def test_cap_hit_around_first_cell_still_signals_truncation(self):
        """Regression: with max_cells=1 the FIRST accepted cell is collected,
        the cap is hit immediately, and every returned candidate still carries
        a visible WORK-CAP truncation signal (never `[]` silently)."""
        cal = self._cal()
        # A single closed rectangle (one usable cell) plus a large grid so the
        # first accepted cell immediately trips the max_cells=1 cap.
        segs = self._grid_segments()
        result = recover_vector_rects(segs, cal, source_page=86, max_cells=1)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1, "cap=1 must collect exactly one cell")
        self.assertIn("WORK-CAP", " ".join(result[0].notes))
        self.assertIn("TRUNCATED", " ".join(result[0].notes))

    def test_max_cells_lt_one_is_rejected(self):
        """Regression: max_cells < 1 would make a cap hit indistinguishable
        from an ordinary empty return, so it is rejected up front."""
        cal = self._cal()
        segs = self._grid_segments()
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                recover_vector_rects(segs, cal, source_page=86, max_cells=bad)


# ---------------------------------------------------------------------------
# Positive real-opening benchmark (Issues 1 & 2)
# ---------------------------------------------------------------------------
class TestPositiveOpeningBenchmark(unittest.TestCase):
    """AUTHORITATIVE positive benchmark: a clearly-visible glazed opening
    group whose true-positive openings are annotated INDEPENDENTLY from the
    source drawing, and whose detector precision/noise is measured against
    that annotation (not by setting a threshold above detector output)."""

    def _pos_crop_meta(self, fx):
        return fx["positive_benchmark"]["crop"]

    def _run_detector(self, fx):
        cal = _render_pixel_calibration(fx)
        img = _load_positive_crop()
        return detect_raster_rect_candidates(
            img, cal,
            source_filename=fx["source"]["local_source_alias"],
            source_page=fx["source"]["page_1_based"],
            drawing_ref=fx["source"]["drawing_no"],
            elevation_side=fx["source"]["elevation_side"],
            calibration_source="page-86-cd3001-e1-east",
        )

    def test_positive_crop_committed_and_checksum(self):
        """Issue 1: the positive crop is present, sized, checksum-proven, and
        is distinct from the negative/ambiguous repeated-cell crop."""
        fx = _load_fixture()
        meta = self._pos_crop_meta(fx)
        self.assertTrue(_POS_CROP.exists(),
                        "positive benchmark crop must be committed")
        img = _load_positive_crop()
        self.assertEqual(img.shape[1], meta["width_px"])
        self.assertEqual(img.shape[0], meta["height_px"])
        checksum = hashlib.sha256(_POS_CROP.read_bytes()).hexdigest()
        self.assertEqual(checksum, meta["sha256"])
        # Distinct ROI from the negative repeated-cell crop.
        self.assertNotEqual(meta["source_bbox_pt"],
                            fx["benchmark"]["crop"]["source_bbox_pt"])
        # Render transform matches the shared 150-DPI render.
        self.assertEqual(fx["render"]["dpi"], _RENDER_DPI)

    def test_positive_crop_has_independent_truth_ahead_of_detector(self):
        """Issue 2: benchmark truth is an independent annotation, not
        detector output.  The annotation exists with measured geometry and
        does not encode a detector-derived count as truth."""
        fx = _load_fixture()
        ann = fx["positive_benchmark"]["independent_annotation"]
        self.assertEqual(ann["method"], "measured_from_source_vector_geometry")
        tp = ann["true_positive_openings"]
        self.assertEqual(ann["true_positive_openings_count"], 9)
        self.assertEqual(len(tp), 9)
        self.assertEqual(fx["positive_benchmark"]["dimension_basis"], "unknown")
        self.assertFalse(fx["positive_benchmark"]["deduction_authority"])
        self.assertTrue(fx["positive_benchmark"]["no_instance_creation"])
        # Diagnostics are recorded separately from truth, never as truth.
        diag = fx["positive_benchmark"]["diagnostics"]
        self.assertIn("note", diag)
        self.assertIn("DIAGNOSTICS", diag["note"])

    def test_positive_crop_recovers_all_independent_openings(self):
        """Issue 2: on the positive crop the detector recovers EVERY
        independently-annotated glazed light (useful signal), and any
        un-matched candidates are counted as noise against the annotation."""
        fx = _load_fixture()
        ann = fx["positive_benchmark"]["independent_annotation"]
        tp_xs = [(o["x0_pt"], o["x1_pt"]) for o in ann["true_positive_openings"]]
        tp_y = tuple(ann["true_positive_y_extent_pt"])

        cands = self._run_detector(fx)
        sized = [c for c in cands if opening_sized(c.width_m, c.height_m)]
        # All candidates stay pure geometry observations.
        for c in cands:
            self.assertEqual(c.dimension_basis, DIMENSION_BASIS_UNKNOWN)
            self.assertEqual(c.elevation_side, fx["source"]["elevation_side"])
        for c in sized:
            self.assertEqual(c.drawing_ref, fx["source"]["drawing_no"])

        meta = self._pos_crop_meta(fx)
        x0_pt, y0_pt = meta["source_bbox_pt"][0], meta["source_bbox_pt"][1]
        scale_pt_per_px = 1.0 / _PDF_POINT_TO_PIXEL  # pt per px at 150 DPI

        tp_matched, detected_tp, fp = _positive_precision_recall(
            sized, tp_xs, tp_y, x0_pt, y0_pt, scale_pt_per_px)

        # Useful signal: all 9 independent openings are recovered.
        self.assertEqual(tp_matched, ann["true_positive_openings_count"],
                         "every independently-annotated glazed opening must "
                         "be recovered (useful signal above noise)")

        # Precision / noise computed against the independent annotation.
        precision = detected_tp / len(sized) if sized else 0.0
        recall = tp_matched / ann["true_positive_openings_count"]
        self.assertGreaterEqual(recall, 0.9)
        # Phase 2A requires useful signal, not perfect classification.
        self.assertGreaterEqual(precision, 0.25)

    def test_positive_crop_does_not_require_d_or_w_mark(self):
        """Issue 1: annotation records no D/W mark (drawing shows none), and
        does not over-claim a commercial identity from geometry."""
        fx = _load_fixture()
        ann = fx["positive_benchmark"]["independent_annotation"]
        self.assertIn("No D/W door/window mark", ann["opening_type_note"])
        self.assertEqual(ann["commercial_identity"], "unproven")
        reg = fx["positive_regions"][0]
        self.assertEqual(reg["opening_type"],
                         "glazed window lights (visible glazing); no D/W mark required")

    def test_positive_precision_is_one_to_one(self):
        """Issue 2 blocker: a true opening accepts at most ONE candidate; a
        second candidate overlapping the SAME opening is a duplicate -> FP."""
        # One annotated opening at source x[0,10] y[0,10].  Using origin 0 and
        # scale_pt_per_px=1 makes crop-px bbox == source-pt bbox.
        tp_xs = [(0.0, 10.0)]
        tp_y = (0.0, 10.0)
        # Two candidates BOTH fully overlapping the single opening.
        cand_a = SimpleNamespace(bbox=(0, 0, 10, 10))
        cand_b = SimpleNamespace(bbox=(2, 0, 8, 10))
        tp_matched, detected_tp, fp = _positive_precision_recall(
            [cand_a, cand_b], tp_xs, tp_y, 0.0, 0.0, 1.0)
        self.assertEqual(tp_matched, 1,
                         "only one candidate may claim a given opening")
        self.assertEqual(detected_tp, 1)
        self.assertEqual(fp, 1,
                         "the duplicate overlapping candidate must count as noise/FP")

    def test_positive_diagnostics_not_stale(self):
        """Issue 3: the recorded diagnostic counts must match CURRENT detector
        output on the committed crop, so a detector change can never silently
        leave the fixture telling an outdated story."""
        fx = _load_fixture()
        diag = fx["positive_benchmark"]["diagnostics"]
        self.assertIn("DIAGNOSTICS", diag["note"])

        ann = fx["positive_benchmark"]["independent_annotation"]
        tp_xs = [(o["x0_pt"], o["x1_pt"]) for o in ann["true_positive_openings"]]
        tp_y = tuple(ann["true_positive_y_extent_pt"])

        cands = self._run_detector(fx)
        sized = [c for c in cands if opening_sized(c.width_m, c.height_m)]
        meta = self._pos_crop_meta(fx)
        x0_pt, y0_pt = meta["source_bbox_pt"][0], meta["source_bbox_pt"][1]
        scale_pt_per_px = 1.0 / _PDF_POINT_TO_PIXEL

        tp_matched, detected_tp, fp = _positive_precision_recall(
            sized, tp_xs, tp_y, x0_pt, y0_pt, scale_pt_per_px)
        precision = detected_tp / len(sized) if sized else 0.0
        recall = tp_matched / ann["true_positive_openings_count"]
        noise = fp / len(sized) if sized else 0.0

        # Recompute-and-assert: recorded diagnostics must equal actual output.
        self.assertEqual(len(cands), diag["observed_total_candidates"])
        self.assertEqual(len(sized), diag["observed_opening_sized_candidates"])
        self.assertEqual(tp_matched, diag["detected_tp_matches"])
        self.assertAlmostEqual(recall, diag["recall_tp"], places=3)
        self.assertAlmostEqual(precision, diag["precision_tp"], places=3)
        self.assertAlmostEqual(noise, diag["noise_fp_fraction"], places=3)


# ---------------------------------------------------------------------------
# Issue 3 — raster coordinate/provenance semantics
# ---------------------------------------------------------------------------
class TestRasterProvenanceSemantics(unittest.TestCase):
    """A raster extractor's bbox/centroid are ALWAYS render_pixel, regardless
    of the calibration object; calibration/source provenance comes from the
    caller, never hard-coded to a specific sheet/project."""

    def test_raster_bbox_is_always_render_pixel_with_pdf_calibration(self):
        """raster image + PDF-point calibration -> coord_space==render_pixel,
        width_m/height_m None (no metres for a pt/m cal on a raster)."""
        pcal = calibration_from_scale_bar_positions(
            [0.0, 28.346, 56.692, 85.038], 1.0,
            coord_space=COORD_SPACE_PDF_POINT)
        img = np.full((60, 60), 255, dtype=np.uint8)
        img[10:50, 10:50] = 0
        cands = detect_raster_rect_candidates(img, pcal, source_page=86)
        self.assertTrue(cands)
        for c in cands:
            self.assertEqual(c.coord_space, COORD_SPACE_RENDER_PIXEL)
            self.assertIsNone(c.width_m)
            self.assertIsNone(c.height_m)
            # geometric (non-dimensional) observation still emitted
            self.assertGreater(c.bbox[2] - c.bbox[0], 0)

    def test_raster_bbox_is_always_render_pixel_with_render_calibration(self):
        """A render_pixel calibration keeps coord_space==render_pixel."""
        rcal = calibration_from_scale_bar_positions(
            [0.0, 59.055, 118.11, 177.17], 1.0,
            coord_space=COORD_SPACE_RENDER_PIXEL, render_dpi=_RENDER_DPI)
        img = np.full((60, 60), 255, dtype=np.uint8)
        img[10:50, 10:50] = 0
        cands = detect_raster_rect_candidates(img, rcal, source_page=86)
        self.assertTrue(cands)
        for c in cands:
            self.assertEqual(c.coord_space, COORD_SPACE_RENDER_PIXEL)
            self.assertIsNotNone(c.width_m)

    def test_provenance_comes_from_caller_not_hardcoded_cd3001(self):
        """Issue 3: a page-87 / other-project call does NOT receive CD3001
        provenance; calibration_source is propagated from the caller."""
        pcal = calibration_from_scale_bar_positions(
            [0.0, 28.346, 56.692, 85.038], 1.0,
            coord_space=COORD_SPACE_PDF_POINT)
        img = np.full((60, 60), 255, dtype=np.uint8)
        img[10:50, 10:50] = 0
        cands = detect_raster_rect_candidates(
            img, pcal, source_page=87, drawing_ref="CD-OTHER-PROJECT",
            elevation_side="North",
            calibration_source="page-87-other-project-primary",
        )
        self.assertTrue(cands)
        for c in cands:
            self.assertNotIn("cd3001", c.calibration_source.lower())
            self.assertNotIn("cd3001", c.drawing_ref.lower())
            self.assertEqual(c.calibration_source,
                             "page-87-other-project-primary")
            self.assertEqual(c.source_page, 87)
            self.assertEqual(c.elevation_side, "North")
            # bbox is still render_pixel for a raster even on this other page.
            self.assertEqual(c.coord_space, COORD_SPACE_RENDER_PIXEL)


# ---------------------------------------------------------------------------
# Issue 4 — source filename provenance
# ---------------------------------------------------------------------------
class TestSourceFilenameProvenance(unittest.TestCase):
    """The local renamed alias is recorded alongside the original issued name;
    the alias is never presented as the verbatim canonical source."""

    def test_original_and_local_alias_both_recorded(self):
        fx = _load_fixture()
        src = fx["source"]
        self.assertEqual(src["original_source_filename"],
                         "260617_004-LAGO-BRITINYA_ARCH-DRAWINGS_COMBINED 2.pdf")
        self.assertEqual(src["local_source_alias"],
                         "260617_004-LAGO-BRITINYA_ARCH-DRAWINGS_COMBINED.pdf")
        self.assertNotEqual(src["original_source_filename"],
                            src["local_source_alias"])
        self.assertIn("renamed", src["filename_provenance"].lower())

    def test_alias_not_presented_as_original(self):
        fx = _load_fixture()
        prov = fx["source"]["filename_provenance"].lower()
        self.assertIn("original_source_filename", prov)
        self.assertIn("not the verbatim canonical source name", prov)


# ---------------------------------------------------------------------------
# benchmark fixture integrity (no circular/tautological truth)
# ---------------------------------------------------------------------------
class TestBenchmarkFixtureIntegrity(unittest.TestCase):
    def test_fixture_does_not_encode_detector_derived_truth(self):
        fx = _load_fixture()
        roi = fx["benchmark_regions"][0]
        self.assertIsNone(roi["unknown_identity_facts"]["opening_count"])
        self.assertEqual(roi["unknown_identity_facts"]["commercial_identity"], "unproven")
        self.assertTrue(roi["region_role"].startswith("repeated facade opening geometry ROI"))
        # Independently measured scales/pitches are present and non-tautological.
        known = roi["known_geometry_facts"]
        self.assertAlmostEqual(known["observed_vertical_cell_period_m"], 2.96, delta=0.05)
        self.assertGreater(len(known["source_note"]), 30)

    def test_safety_expectations_declared(self):
        fx = _load_fixture()
        se = fx["safety_expectations"]
        self.assertFalse(se["deduct"])
        self.assertTrue(se["no_instance_creation"])
        self.assertFalse(se["rough_opening_authority"])


if __name__ == "__main__":
    unittest.main()
