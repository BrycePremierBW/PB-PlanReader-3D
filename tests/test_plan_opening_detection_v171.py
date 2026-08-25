"""Tests for Priority 5 Phase B1 — plan-vector opening candidate detection.

Covers all ChatGPT review corrections through round 1:
  1. Authoritative PDF-point calibration (scale_info with render_zoom)
  2. True discontinuity-based wall gaps (continuous wall = 0 gaps)
  3. Three confidence channels (tag ≠ geometry ≠ association)
  4. Downgraded straight-line door evidence (no fake swing)
  5. Wall-local + hatch-aware false-positive filtering
"""
from __future__ import annotations

import math
import unittest

from pb_opening_evidence_v170 import (
    DEDUCTION_REVIEW,
    DIMENSION_BASIS_UNKNOWN,
    OPENING_TYPE_DOOR,
    OPENING_TYPE_OTHER,
    OPENING_TYPE_WINDOW,
    OpeningEvidence,
)
from pb_plan_opening_detection_v171 import (
    DoorCandidate,
    GapCandidate,
    PlanOpeningDetectionResult,
    Segment,
    TextWord,
    WallLine,
    WindowCandidate,
    _classify_tag,
    _line_perp_distance,
    _point_segment_distance,
    _resolve_scale,
    _segments_parallel,
    _segments_perpendicular,
    door_to_opening_evidence,
    detect_door_candidates,
    detect_gap_candidates,
    detect_wall_lines,
    detect_window_candidates,
    gap_to_opening_evidence,
    plan_opening_candidates,
    window_to_opening_evidence,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _horiz_seg(x1: float, y: float, x2: float, y2: float = None) -> Segment:
    if y2 is None:
        y2 = y
    return Segment(x1=x1, y1=y, x2=x2, y2=y2)


def _vert_seg(x: float, y1: float, y2: float) -> Segment:
    return Segment(x1=x, y1=y1, x2=x, y2=y2)


def _word(text: str, cx: float, cy: float) -> TextWord:
    return TextWord(text=text, x0=cx - 10, y0=cy - 5, x1=cx + 10, y1=cy + 5)


# Typical floor plan: 50 px/m with render_zoom=1.0 → 50 PDF pt/m
SCALE_INFO_1X = {"px_per_m": 50.0, "render_zoom": 1.0}
SCALE_INFO_2X = {"px_per_m": 100.0, "render_zoom": 2.0}  # zoomed → 50 pt/m
SCALE_PX_PER_M = 50.0  # legacy fallback


# ---------------------------------------------------------------------------
# 1. Geometry primitives
# ---------------------------------------------------------------------------

class TestSegmentPrimitives(unittest.TestCase):

    def test_length_horizontal(self):
        s = _horiz_seg(0, 0, 100)
        self.assertAlmostEqual(s.length, 100.0)

    def test_length_diagonal(self):
        s = Segment(x1=0, y1=0, x2=3, y2=4)
        self.assertAlmostEqual(s.length, 5.0)

    def test_angle_horizontal(self):
        s = _horiz_seg(0, 0, 100)
        self.assertAlmostEqual(s.angle_deg, 0.0)

    def test_angle_vertical(self):
        s = _vert_seg(0, 0, 100)
        self.assertAlmostEqual(s.angle_deg, 90.0)

    def test_midpoint(self):
        s = Segment(x1=10, y1=20, x2=30, y2=40)
        self.assertAlmostEqual(s.cx, 20.0)
        self.assertAlmostEqual(s.cy, 30.0)


class TestTextWord(unittest.TestCase):

    def test_midpoint(self):
        w = TextWord(text="D01", x0=90, y0=45, x1=110, y1=55)
        self.assertAlmostEqual(w.cx, 100.0)
        self.assertAlmostEqual(w.cy, 50.0)

    def test_bbox(self):
        w = TextWord(text="W01", x0=10, y0=20, x1=30, y1=40)
        self.assertEqual(w.bbox, (10, 20, 30, 40))


class TestTagClassification(unittest.TestCase):

    def test_door_tag(self):
        self.assertEqual(_classify_tag("D01"), "door")
        self.assertEqual(_classify_tag("D1"), "door")
        self.assertEqual(_classify_tag("d12"), "door")

    def test_window_tag(self):
        self.assertEqual(_classify_tag("W01"), "window")
        self.assertEqual(_classify_tag("W1"), "window")
        self.assertEqual(_classify_tag("w99"), "window")

    def test_unknown_tag(self):
        self.assertEqual(_classify_tag("R01"), "")
        self.assertEqual(_classify_tag("hello"), "")
        self.assertEqual(_classify_tag(""), "")


class TestSegmentRelationships(unittest.TestCase):

    def test_parallel_horizontal(self):
        a = _horiz_seg(0, 0, 100, 0)
        b = _horiz_seg(0, 50, 100, 50)
        self.assertTrue(_segments_parallel(a, b))

    def test_not_parallel(self):
        a = _horiz_seg(0, 0, 100, 0)
        b = _vert_seg(50, 0, 100)
        self.assertFalse(_segments_parallel(a, b))

    def test_perpendicular(self):
        a = _horiz_seg(0, 0, 100, 0)
        b = _vert_seg(50, 0, 100)
        self.assertTrue(_segments_perpendicular(a, b))

    def test_not_perpendicular(self):
        a = _horiz_seg(0, 0, 100, 0)
        b = _horiz_seg(0, 50, 100, 50)
        self.assertFalse(_segments_perpendicular(a, b))

    def test_point_segment_distance(self):
        s = _horiz_seg(0, 0, 100, 0)
        d = _point_segment_distance(50, 10, s)
        self.assertAlmostEqual(d, 10.0)

    def test_point_segment_distance_at_endpoint(self):
        s = _horiz_seg(0, 0, 100, 0)
        d = _point_segment_distance(150, 0, s)
        self.assertAlmostEqual(d, 50.0)

    def test_line_perp_distance_along_extension(self):
        """_line_perp_distance gives perpendicular-only distance, even past endpoint."""
        s = _horiz_seg(0, 0, 100, 0)
        # Point 250pt along wall direction, 10pt perpendicular
        d_perp = _line_perp_distance(250, 10, s)
        self.assertAlmostEqual(d_perp, 10.0)  # perpendicular only
        # Verify it differs from _point_segment_distance (which includes along-wall)
        d_euclid = _point_segment_distance(250, 10, s)
        self.assertGreater(d_euclid, 100.0)  # Euclidean is much larger


# ---------------------------------------------------------------------------
# 2. Calibration (BLOCKER 1 — scale_info with render_zoom)
# ---------------------------------------------------------------------------

class TestCalibration(unittest.TestCase):

    def test_scale_info_1x(self):
        pt_per_m, m_per_pt = _resolve_scale({"px_per_m": 50.0, "render_zoom": 1.0})
        self.assertAlmostEqual(pt_per_m, 50.0)
        self.assertAlmostEqual(m_per_pt, 0.02)

    def test_scale_info_2x_render_zoom(self):
        """render_zoom=2.0 means rendered at 100 px/m but PDF pt/m = 50."""
        pt_per_m, m_per_pt = _resolve_scale({"px_per_m": 100.0, "render_zoom": 2.0})
        self.assertAlmostEqual(pt_per_m, 50.0)
        self.assertAlmostEqual(m_per_pt, 0.02)

    def test_scale_info_4x_render_zoom(self):
        """render_zoom=4.0: px_per_m=200 → 50 PDF pt/m."""
        pt_per_m, m_per_pt = _resolve_scale({"px_per_m": 200.0, "render_zoom": 4.0})
        self.assertAlmostEqual(pt_per_m, 50.0)

    def test_scale_info_missing_zoom(self):
        """Missing render_zoom defaults to 1.0."""
        pt_per_m, m_per_pt = _resolve_scale({"px_per_m": 50.0})
        self.assertAlmostEqual(pt_per_m, 50.0)

    def test_legacy_scale_px_per_m(self):
        """Legacy scale_px_per_m used when no scale_info."""
        pt_per_m, m_per_pt = _resolve_scale(None, scale_px_per_m=50.0)
        self.assertAlmostEqual(pt_per_m, 50.0)

    def test_no_scale(self):
        pt_per_m, m_per_pt = _resolve_scale(None, 0.0)
        self.assertAlmostEqual(pt_per_m, 0.0)

    def test_render_zoom_2x_produces_correct_widths(self):
        """With render_zoom=2.0, a 50pt segment = 1.0m, not 2.0m."""
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        leaf = _vert_seg(200, 85, 115)  # 30pt long
        words = [_word("D01", 200, 70)]

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], words,
            scale_info={"px_per_m": 100.0, "render_zoom": 2.0},
        )
        self.assertEqual(len(doors), 1)
        # 30pt / (100/2.0 pt/m) = 30 / 50 = 0.6m
        self.assertAlmostEqual(doors[0].width_m, 0.6, places=2)
        # Position: 200pt / 50 pt/m = 4.0m
        self.assertAlmostEqual(doors[0].position_along_wall_m, 4.0, places=2)


# ---------------------------------------------------------------------------
# 3. Wall line detection
# ---------------------------------------------------------------------------

class TestWallLineDetection(unittest.TestCase):

    def test_long_segments_are_walls(self):
        segs = [
            _horiz_seg(0, 100, 500, 100),
            _horiz_seg(100, 100, 120, 100),
            _vert_seg(0, 0, 400),
        ]
        walls = detect_wall_lines(segs, min_length_pt=200.0)
        self.assertEqual(len(walls), 2)

    def test_empty_segments(self):
        walls = detect_wall_lines([], min_length_pt=200.0)
        self.assertEqual(len(walls), 0)

    def test_no_long_segments(self):
        segs = [_horiz_seg(0, 0, 10, 0), _horiz_seg(0, 10, 15, 10)]
        walls = detect_wall_lines(segs, min_length_pt=200.0)
        self.assertEqual(len(walls), 0)


# ---------------------------------------------------------------------------
# 4. Door detection (BLOCKER 4 — no fake swing, lower base confidence)
# ---------------------------------------------------------------------------

class TestDoorDetection(unittest.TestCase):

    def test_door_jamb_with_tag(self):
        """Perpendicular segment near wall + D01 tag → door candidate."""
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        leaf = _vert_seg(200, 85, 115)
        words = [_word("D01", 200, 70)]

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], words,
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(doors), 1)
        self.assertEqual(doors[0].tag, "D01")

    def test_door_base_geometry_confidence_is_low(self):
        """A single perpendicular line is weak geometry — not 0.95."""
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        leaf = _vert_seg(200, 85, 115)
        words = [_word("D01", 200, 70)]

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], words,
            scale_info=SCALE_INFO_1X,
        )
        # Tag provides SEMANTIC confidence, not geometry confidence
        self.assertAlmostEqual(doors[0].geometry_confidence, 0.60)  # reasonable width
        self.assertAlmostEqual(doors[0].semantic_confidence, 0.95)  # D tag
        # No wall_ref → low association
        self.assertAlmostEqual(doors[0].association_confidence, 0.30)

    def test_door_without_tag_lower_confidence(self):
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        leaf = _vert_seg(200, 85, 115)

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(doors), 1)
        self.assertAlmostEqual(doors[0].geometry_confidence, 0.60)
        self.assertAlmostEqual(doors[0].semantic_confidence, 0.0)

    def test_no_door_when_too_far_from_wall(self):
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        leaf = _vert_seg(200, 20, 50)

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(doors), 0)

    def test_no_door_when_wrong_angle(self):
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        diag = Segment(x1=190, y1=90, x2=210, y2=110)

        doors = detect_door_candidates(
            [wall.segment, diag], [wall], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(doors), 0)

    def test_position_along_wall(self):
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        leaf = _vert_seg(250, 85, 115)

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(doors), 1)
        self.assertAlmostEqual(doors[0].position_along_wall_m, 5.0, places=2)

    def test_no_wall_ref_low_association(self):
        """Without wall_ref, association confidence stays low."""
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        leaf = _vert_seg(200, 85, 115)

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], [],
            scale_info=SCALE_INFO_1X,
        )
        # WallLine has no wall_ref → low association
        self.assertAlmostEqual(doors[0].association_confidence, 0.30)


# ---------------------------------------------------------------------------
# 5. Window detection (BLOCKER 5 — wall-local, hatch-aware)
# ---------------------------------------------------------------------------

class TestWindowDetection(unittest.TestCase):

    def test_window_with_tag(self):
        wall = WallLine(segment=_horiz_seg(0, 100, 600, 100))
        jamb1 = _vert_seg(290, 85, 115)
        jamb2 = _vert_seg(310, 85, 115)
        words = [_word("W01", 300, 70)]

        windows = detect_window_candidates(
            [wall.segment, jamb1, jamb2], [wall], words,
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].tag, "W01")
        # Tag is semantic, not geometry
        self.assertAlmostEqual(windows[0].geometry_confidence, 0.70)  # reasonable width
        self.assertAlmostEqual(windows[0].semantic_confidence, 0.95)  # W tag

    def test_window_without_tag(self):
        wall = WallLine(segment=_horiz_seg(0, 100, 600, 100))
        jamb1 = _vert_seg(290, 85, 115)
        jamb2 = _vert_seg(310, 85, 115)

        windows = detect_window_candidates(
            [wall.segment, jamb1, jamb2], [wall], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(windows), 1)
        self.assertAlmostEqual(windows[0].geometry_confidence, 0.70)
        self.assertAlmostEqual(windows[0].semantic_confidence, 0.0)

    def test_no_window_when_not_parallel(self):
        wall = WallLine(segment=_horiz_seg(0, 100, 600, 100))
        seg1 = _vert_seg(290, 85, 115)
        seg2 = _horiz_seg(310, 95, 330, 105)

        windows = detect_window_candidates(
            [wall.segment, seg1, seg2], [wall], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(windows), 0)

    def test_no_window_when_too_far_apart(self):
        wall = WallLine(segment=_horiz_seg(0, 100, 600, 100))
        jamb1 = _vert_seg(200, 85, 115)
        jamb2 = _vert_seg(400, 85, 115)

        windows = detect_window_candidates(
            [wall.segment, jamb1, jamb2], [wall], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(windows), 0)

    # --- Hatch/grid false-positive tests (BLOCKER 5) ---

    def test_hatch_panel_not_detected_as_window(self):
        """Many parallel perpendicular lines near a wall → hatch, not window."""
        wall = WallLine(segment=_horiz_seg(0, 100, 800, 100))
        # 6 parallel vertical lines (louvre/batten pattern)
        hatch_segs = [_vert_seg(x, 85, 115) for x in range(100, 400, 30)]

        windows = detect_window_candidates(
            [wall.segment] + hatch_segs, [wall], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(windows), 0)

    def test_batten_repetition_not_window(self):
        """Repeated battens/slabs are not windows."""
        wall = WallLine(segment=_horiz_seg(0, 100, 800, 100))
        battens = [_vert_seg(x, 90, 110) for x in range(50, 350, 20)]

        windows = detect_window_candidates(
            [wall.segment] + battens, [wall], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(windows), 0)

    def test_dimension_ticks_not_window(self):
        """Dimension tick marks are not windows."""
        wall = WallLine(segment=_horiz_seg(0, 100, 800, 100))
        # Short ticks at regular intervals (dimension annotation)
        ticks = [_vert_seg(x, 95, 105) for x in range(100, 400, 50)]

        windows = detect_window_candidates(
            [wall.segment] + ticks, [wall], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(windows), 0)

    def test_balustrade_repeated_lines_not_window(self):
        """Balustrade/stair repeated lines are not windows."""
        wall = WallLine(segment=_horiz_seg(0, 100, 800, 100))
        rails = [_vert_seg(x, 80, 120) for x in range(100, 400, 25)]

        windows = detect_window_candidates(
            [wall.segment] + rails, [wall], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(windows), 0)

    def test_two_unrelated_parallel_near_wall_not_window(self):
        """Two parallel lines near a wall but from different features."""
        wall = WallLine(segment=_horiz_seg(0, 100, 800, 100))
        # One near the wall, one also near but not a pair
        # They happen to be parallel but are 300pt apart (not a pair)
        seg1 = _vert_seg(100, 85, 115)
        seg2 = _vert_seg(400, 85, 115)  # 300pt apart > MAX_PAIR_DISTANCE_PT

        windows = detect_window_candidates(
            [wall.segment, seg1, seg2], [wall], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(windows), 0)


# ---------------------------------------------------------------------------
# 6. Gap detection (BLOCKER 2 — real discontinuities only)
# ---------------------------------------------------------------------------

class TestGapDetection(unittest.TestCase):

    def test_continuous_wall_no_gaps(self):
        """A continuous wall with perpendicular intersections → 0 gaps.

        Perpendicular lines crossing a continuous wall are partition
        returns or intersections, NOT openings.
        """
        wall = WallLine(segment=_horiz_seg(0, 100, 600, 100))
        left = _vert_seg(100, 85, 115)
        right = _vert_seg(200, 85, 115)

        gaps = detect_gap_candidates(
            [wall.segment, left, right], [wall], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(gaps), 0)

    def test_continuous_wall_with_intersections_no_gaps(self):
        """Multiple perpendicular lines along continuous wall → 0 gaps."""
        wall = WallLine(segment=_horiz_seg(0, 100, 800, 100))
        perps = [_vert_seg(x, 85, 115) for x in [100, 200, 300, 400, 500]]

        gaps = detect_gap_candidates(
            [wall.segment] + perps, [wall], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(gaps), 0)

    def test_two_collinear_wall_segments_with_gap(self):
        """Two collinear wall segments with a gap → 1 gap detected."""
        wall_a = WallLine(segment=_horiz_seg(0, 100, 200, 100))
        wall_b = WallLine(segment=_horiz_seg(300, 100, 600, 100))
        # Gap: 200→300 = 100pt = 2.0m

        gaps = detect_gap_candidates(
            [wall_a.segment, wall_b.segment], [wall_a, wall_b], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(gaps), 1)
        # Gap width: 100pt / 50 pt/m = 2.0m
        self.assertAlmostEqual(gaps[0].width_m, 2.0, places=2)
        self.assertAlmostEqual(gaps[0].geometry_confidence, 0.75)

    def test_gap_width_from_wall_discontinuity(self):
        """Gap width is measured from wall segment endpoints, not perp lines."""
        wall_a = WallLine(segment=_horiz_seg(0, 100, 150, 100))
        wall_b = WallLine(segment=_horiz_seg(250, 100, 500, 100))
        # Gap: 150→250 = 100pt

        gaps = detect_gap_candidates(
            [wall_a.segment, wall_b.segment], [wall_a, wall_b], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(gaps), 1)
        self.assertAlmostEqual(gaps[0].width_m, 2.0, places=2)

    def test_no_gap_when_segments_overlap(self):
        """Overlapping collinear segments → no gap."""
        wall_a = WallLine(segment=_horiz_seg(0, 100, 300, 100))
        wall_b = WallLine(segment=_horiz_seg(200, 100, 600, 100))

        gaps = detect_gap_candidates(
            [wall_a.segment, wall_b.segment], [wall_a, wall_b], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(gaps), 0)

    def test_no_gap_when_fillers_block(self):
        """Gap filled by another wall segment → no gap."""
        wall_a = WallLine(segment=_horiz_seg(0, 100, 200, 100))
        wall_b = WallLine(segment=_horiz_seg(300, 100, 600, 100))
        filler = _horiz_seg(205, 100, 295, 100)  # bridges the 200→300 gap

        gaps = detect_gap_candidates(
            [wall_a.segment, wall_b.segment, filler], [wall_a, wall_b], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(gaps), 0)

    def test_non_collinear_segments_no_gap(self):
        """Non-parallel wall segments → no gap (different walls)."""
        wall_h = WallLine(segment=_horiz_seg(0, 100, 300, 100))
        wall_v = WallLine(segment=_vert_seg(400, 0, 300))

        gaps = detect_gap_candidates(
            [wall_h.segment, wall_v.segment], [wall_h, wall_v], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(gaps), 0)

    def test_gap_with_tag(self):
        """Wall discontinuity with nearby tag → higher semantic confidence."""
        wall_a = WallLine(segment=_horiz_seg(0, 100, 200, 100))
        wall_b = WallLine(segment=_horiz_seg(300, 100, 600, 100))
        words = [_word("D03", 250, 70)]

        gaps = detect_gap_candidates(
            [wall_a.segment, wall_b.segment], [wall_a, wall_b], words,
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].tag, "D03")
        self.assertAlmostEqual(gaps[0].semantic_confidence, 0.80)
        # Geometry confidence stays at 0.75 (from wall discontinuity)
        self.assertAlmostEqual(gaps[0].geometry_confidence, 0.75)


# ---------------------------------------------------------------------------
# 7. Confidence channel separation (BLOCKER 3)
# ---------------------------------------------------------------------------

class TestConfidenceChannels(unittest.TestCase):

    def test_tag_does_not_inflate_geometry_confidence(self):
        """D01 tag near a jamb sets semantic, NOT geometry confidence."""
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100), wall_ref="N01")
        leaf = _vert_seg(200, 85, 115)
        words = [_word("D01", 200, 70)]

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], words,
            scale_info=SCALE_INFO_1X,
        )
        # Geometry from a single perpendicular line is moderate
        self.assertLessEqual(doors[0].geometry_confidence, 0.65)
        # Semantic from D tag is high
        self.assertAlmostEqual(doors[0].semantic_confidence, 0.95)
        # These are independent channels
        self.assertNotAlmostEqual(
            doors[0].geometry_confidence,
            doors[0].semantic_confidence,
        )

    def test_window_tag_does_not_inflate_geometry(self):
        """W01 tag near jamb pair sets semantic, NOT geometry confidence."""
        wall = WallLine(segment=_horiz_seg(0, 100, 600, 100), wall_ref="S01")
        jamb1 = _vert_seg(290, 85, 115)
        jamb2 = _vert_seg(310, 85, 115)
        words = [_word("W01", 300, 70)]

        windows = detect_window_candidates(
            [wall.segment, jamb1, jamb2], [wall], words,
            scale_info=SCALE_INFO_1X,
        )
        self.assertLessEqual(windows[0].geometry_confidence, 0.75)
        self.assertAlmostEqual(windows[0].semantic_confidence, 0.95)

    def test_no_wall_ref_low_association(self):
        """Without resolved wall_ref, association confidence stays low."""
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))  # no wall_ref
        leaf = _vert_seg(200, 85, 115)
        words = [_word("D01", 200, 70)]

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], words,
            scale_info=SCALE_INFO_1X,
        )
        self.assertAlmostEqual(doors[0].association_confidence, 0.30)

    def test_wall_ref_gives_higher_association(self):
        """With resolved wall_ref, association confidence is higher."""
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100), wall_ref="N01")
        leaf = _vert_seg(200, 85, 115)
        words = [_word("D01", 200, 70)]

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], words,
            scale_info=SCALE_INFO_1X,
        )
        self.assertAlmostEqual(doors[0].association_confidence, 0.70)

    def test_three_channels_are_independent(self):
        """geometry, association, and semantic are three separate values."""
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100), wall_ref="N01")
        leaf = _vert_seg(200, 85, 115)
        words = [_word("D01", 200, 70)]

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], words,
            scale_info=SCALE_INFO_1X,
        )
        d = doors[0]
        # All three are set and can be different
        self.assertGreater(d.geometry_confidence, 0)
        self.assertGreater(d.association_confidence, 0)
        self.assertGreater(d.semantic_confidence, 0)
        # Geometry is not inflated by tag
        self.assertLessEqual(d.geometry_confidence, 0.65)


# ---------------------------------------------------------------------------
# 8. Conversion to OpeningEvidence
# ---------------------------------------------------------------------------

class TestConversionToEvidence(unittest.TestCase):

    def test_door_candidate_to_evidence(self):
        cand = DoorCandidate(
            wall_ref="N01",
            position_along_wall_m=3.0,
            width_m=0.82,
            tag="D01",
            geometry_confidence=0.60,
            association_confidence=0.70,
            semantic_confidence=0.95,
            evidence=["test_evidence"],
            page_no=1,
        )
        ev = door_to_opening_evidence(cand)

        self.assertIsInstance(ev, OpeningEvidence)
        self.assertEqual(ev.opening_type, OPENING_TYPE_DOOR)
        self.assertEqual(ev.type_mark, "D01")
        self.assertEqual(ev.wall_ref, "N01")
        self.assertAlmostEqual(ev.position_along_wall_m, 3.0)
        self.assertAlmostEqual(ev.width_m, 0.82)
        self.assertIsNone(ev.height_m)
        self.assertEqual(ev.extraction_method, "plan_vector")
        self.assertEqual(ev.dimension_source, "plan_vector")
        self.assertEqual(ev.dimension_basis, DIMENSION_BASIS_UNKNOWN)
        self.assertEqual(ev.quantity, 1)
        self.assertFalse(ev.deduct)
        self.assertEqual(ev.deduction_status, DEDUCTION_REVIEW)
        # geometry_confidence comes from candidate, not inflated by tag
        self.assertAlmostEqual(ev.geometry_confidence, 0.60)
        self.assertAlmostEqual(ev.association_confidence, 0.70)

    def test_window_candidate_to_evidence(self):
        cand = WindowCandidate(
            wall_ref="N01",
            position_along_wall_m=5.0,
            width_m=1.2,
            tag="W01",
            geometry_confidence=0.70,
            association_confidence=0.70,
            semantic_confidence=0.95,
            evidence=["test_evidence"],
            page_no=2,
        )
        ev = window_to_opening_evidence(cand)

        self.assertIsInstance(ev, OpeningEvidence)
        self.assertEqual(ev.opening_type, OPENING_TYPE_WINDOW)
        self.assertEqual(ev.type_mark, "W01")
        self.assertAlmostEqual(ev.width_m, 1.2)
        self.assertEqual(ev.sill_m, 0.9)
        self.assertEqual(ev.quantity, 1)
        self.assertFalse(ev.deduct)
        self.assertAlmostEqual(ev.geometry_confidence, 0.70)

    def test_gap_candidate_to_evidence(self):
        cand = GapCandidate(
            wall_ref="N01",
            position_along_wall_m=4.0,
            width_m=1.5,
            tag="",
            geometry_confidence=0.75,
            association_confidence=0.70,
            semantic_confidence=0.0,
            evidence=["test_evidence"],
            page_no=3,
        )
        ev = gap_to_opening_evidence(cand)

        self.assertIsInstance(ev, OpeningEvidence)
        self.assertEqual(ev.opening_type, OPENING_TYPE_OTHER)
        self.assertEqual(ev.type_mark, "")
        self.assertEqual(ev.quantity, 1)
        self.assertFalse(ev.deduct)
        self.assertAlmostEqual(ev.geometry_confidence, 0.75)

    def test_gap_with_door_tag_classifies_as_door(self):
        cand = GapCandidate(
            wall_ref="N01",
            position_along_wall_m=2.0,
            width_m=0.9,
            tag="D03",
            geometry_confidence=0.75,
            association_confidence=0.70,
            semantic_confidence=0.80,
            evidence=[],
            page_no=1,
        )
        ev = gap_to_opening_evidence(cand)
        self.assertEqual(ev.opening_type, OPENING_TYPE_DOOR)
        self.assertEqual(ev.type_mark, "D03")


# ---------------------------------------------------------------------------
# 9. Main pipeline integration
# ---------------------------------------------------------------------------

class TestPlanOpeningCandidates(unittest.TestCase):

    def test_full_pipeline_door_and_window(self):
        """Complete pipeline with a door and a window on one wall."""
        wall = _horiz_seg(0, 100, 800, 100)
        door_leaf = _vert_seg(150, 85, 115)
        jamb1 = _vert_seg(490, 85, 115)
        jamb2 = _vert_seg(510, 85, 115)

        words = [_word("D01", 150, 70), _word("W01", 500, 70)]
        all_segs = [wall, door_leaf, jamb1, jamb2]

        result = plan_opening_candidates(
            all_segs, words,
            scale_info=SCALE_INFO_1X,
            page_no=1,
        )

        self.assertIsInstance(result, PlanOpeningDetectionResult)
        self.assertEqual(result.door_count, 1)
        self.assertEqual(result.window_count, 1)
        self.assertGreaterEqual(len(result.candidates), 2)

        types = {c.opening_type for c in result.candidates}
        self.assertIn(OPENING_TYPE_DOOR, types)
        self.assertIn(OPENING_TYPE_WINDOW, types)

        for c in result.candidates:
            self.assertEqual(c.extraction_method, "plan_vector")
            self.assertFalse(c.deduct)
            self.assertEqual(c.quantity, 1)

    def test_empty_segments(self):
        result = plan_opening_candidates([], [], scale_info=SCALE_INFO_1X)
        self.assertEqual(len(result.candidates), 0)

    def test_no_wall_lines(self):
        segs = [_horiz_seg(0, 0, 10, 0), _horiz_seg(0, 10, 15, 10)]
        result = plan_opening_candidates(segs, [], scale_info=SCALE_INFO_1X)
        self.assertEqual(len(result.candidates), 0)

    def test_candidate_has_instance_id(self):
        wall = _horiz_seg(0, 100, 500, 100)
        leaf = _vert_seg(150, 85, 115)
        leaf2 = _vert_seg(350, 85, 115)

        result = plan_opening_candidates(
            [wall, leaf, leaf2], [],
            scale_info=SCALE_INFO_1X,
        )
        ids = {c.opening_instance_id for c in result.candidates}
        self.assertEqual(len(ids), len(result.candidates))

    def test_no_scale_still_produces_candidates(self):
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        leaf = _vert_seg(200, 85, 115)

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], [],
            scale_info=None, scale_px_per_m=0.0,
        )
        self.assertEqual(len(doors), 1)
        self.assertIsNone(doors[0].width_m)


# ---------------------------------------------------------------------------
# 10. Contract compliance
# ---------------------------------------------------------------------------

class TestContractCompliance(unittest.TestCase):

    def test_all_candidates_are_opening_evidence(self):
        wall = _horiz_seg(0, 100, 800, 100)
        leaf = _vert_seg(150, 85, 115)
        jamb1 = _vert_seg(490, 85, 115)
        jamb2 = _vert_seg(510, 85, 115)
        words = [_word("D01", 150, 70), _word("W01", 500, 70)]

        result = plan_opening_candidates(
            [wall, leaf, jamb1, jamb2], words,
            scale_info=SCALE_INFO_1X,
        )
        for c in result.candidates:
            self.assertIsInstance(c, OpeningEvidence)

    def test_quantity_always_one(self):
        wall = _horiz_seg(0, 100, 500, 100)
        leaf = _vert_seg(200, 85, 115)

        result = plan_opening_candidates(
            [wall, leaf], [],
            scale_info=SCALE_INFO_1X,
        )
        for c in result.candidates:
            self.assertEqual(c.quantity, 1)

    def test_deduct_always_false(self):
        wall = _horiz_seg(0, 100, 500, 100)
        leaf = _vert_seg(200, 85, 115)
        words = [_word("D01", 200, 70)]

        result = plan_opening_candidates(
            [wall, leaf], words,
            scale_info=SCALE_INFO_1X,
        )
        for c in result.candidates:
            self.assertFalse(c.deduct)

    def test_extraction_method_always_plan_vector(self):
        wall = _horiz_seg(0, 100, 500, 100)
        leaf = _vert_seg(200, 85, 115)

        result = plan_opening_candidates(
            [wall, leaf], [],
            scale_info=SCALE_INFO_1X,
        )
        for c in result.candidates:
            self.assertEqual(c.extraction_method, "plan_vector")

    def test_dimension_basis_always_unknown(self):
        wall = _horiz_seg(0, 100, 500, 100)
        leaf = _vert_seg(200, 85, 115)

        result = plan_opening_candidates(
            [wall, leaf], [],
            scale_info=SCALE_INFO_1X,
        )
        for c in result.candidates:
            self.assertEqual(c.dimension_basis, DIMENSION_BASIS_UNKNOWN)

    def test_height_always_none(self):
        wall = _horiz_seg(0, 100, 500, 100)
        leaf = _vert_seg(200, 85, 115)

        result = plan_opening_candidates(
            [wall, leaf], [],
            scale_info=SCALE_INFO_1X,
        )
        for c in result.candidates:
            self.assertIsNone(c.height_m)

    def test_area_always_none_without_height(self):
        wall = _horiz_seg(0, 100, 500, 100)
        leaf = _vert_seg(200, 85, 115)

        result = plan_opening_candidates(
            [wall, leaf], [],
            scale_info=SCALE_INFO_1X,
        )
        for c in result.candidates:
            self.assertIsNone(c.area_m2)

    def test_deduction_status_always_review(self):
        wall = _horiz_seg(0, 100, 500, 100)
        leaf = _vert_seg(200, 85, 115)
        words = [_word("D01", 200, 70)]

        result = plan_opening_candidates(
            [wall, leaf], words,
            scale_info=SCALE_INFO_1X,
        )
        for c in result.candidates:
            self.assertEqual(c.deduction_status, DEDUCTION_REVIEW)


if __name__ == "__main__":
    unittest.main()
