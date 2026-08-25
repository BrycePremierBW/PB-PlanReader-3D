"""Tests for Priority 5 Phase B1 — plan-vector opening candidate detection.

Covers all ChatGPT review corrections through round 2:
  1. Authoritative PDF-point calibration (scale_info with render_zoom)
  2. True discontinuity-based wall gaps (continuous wall = 0 gaps)
  3. Three confidence channels (tag ≠ geometry ≠ association)
  4. Downgraded straight-line door evidence (no fake swing)
  5. Wall-local + hatch-aware false-positive filtering
  6. Multi-window wall detection (cluster-based, not whole-wall count)
  7. D/W tag/type compatibility (no cross-type type_mark assignment)
  8. Canonical, orientation-independent wall position
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
    _canonical_wall_direction,
    _classify_tag,
    _line_perp_distance,
    _point_segment_distance,
    _position_along_wall,
    _resolve_scale,
    _resolve_wall_ref,
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
        d_perp = _line_perp_distance(250, 10, s)
        self.assertAlmostEqual(d_perp, 10.0)  # perpendicular only
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
        pt_per_m, m_per_pt = _resolve_scale({"px_per_m": 100.0, "render_zoom": 2.0})
        self.assertAlmostEqual(pt_per_m, 50.0)
        self.assertAlmostEqual(m_per_pt, 0.02)

    def test_scale_info_4x_render_zoom(self):
        pt_per_m, m_per_pt = _resolve_scale({"px_per_m": 200.0, "render_zoom": 4.0})
        self.assertAlmostEqual(pt_per_m, 50.0)

    def test_scale_info_missing_zoom(self):
        pt_per_m, m_per_pt = _resolve_scale({"px_per_m": 50.0})
        self.assertAlmostEqual(pt_per_m, 50.0)

    def test_legacy_scale_px_per_m(self):
        pt_per_m, m_per_pt = _resolve_scale(None, scale_px_per_m=50.0)
        self.assertAlmostEqual(pt_per_m, 50.0)

    def test_no_scale(self):
        pt_per_m, m_per_pt = _resolve_scale(None, 0.0)
        self.assertAlmostEqual(pt_per_m, 0.0)

    def test_render_zoom_2x_produces_correct_widths(self):
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        leaf = _vert_seg(200, 85, 115)  # 30pt long
        words = [_word("D01", 200, 70)]

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], words,
            scale_info={"px_per_m": 100.0, "render_zoom": 2.0},
        )
        self.assertEqual(len(doors), 1)
        self.assertAlmostEqual(doors[0].width_m, 0.6, places=2)
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
# 4. Canonical wall direction (BLOCKER 3 — orientation-independent position)
# ---------------------------------------------------------------------------

class TestCanonicalWallDirection(unittest.TestCase):

    def test_horizontal_left_to_right(self):
        seg = _horiz_seg(0, 100, 500, 100)
        dx, dy, ox, oy = _canonical_wall_direction(seg)
        self.assertGreater(dx, 0)  # left to right
        self.assertAlmostEqual(ox, 0.0)

    def test_horizontal_reversed(self):
        """Reversing segment endpoints produces the same canonical direction."""
        seg_fwd = _horiz_seg(0, 100, 500, 100)
        seg_rev = _horiz_seg(500, 100, 0, 100)
        dx1, dy1, ox1, oy1 = _canonical_wall_direction(seg_fwd)
        dx2, dy2, ox2, oy2 = _canonical_wall_direction(seg_rev)
        self.assertAlmostEqual(dx1, dx2)
        self.assertAlmostEqual(dy1, dy2)
        self.assertAlmostEqual(ox1, ox2)
        self.assertAlmostEqual(oy1, oy2)

    def test_vertical_bottom_to_top(self):
        seg = _vert_seg(100, 0, 500)
        dx, dy, ox, oy = _canonical_wall_direction(seg)
        self.assertGreater(dy, 0)  # bottom to top
        self.assertAlmostEqual(oy, 0.0)

    def test_vertical_reversed(self):
        seg_fwd = _vert_seg(100, 0, 500)
        seg_rev = _vert_seg(100, 500, 0)
        dx1, dy1, ox1, oy1 = _canonical_wall_direction(seg_fwd)
        dx2, dy2, ox2, oy2 = _canonical_wall_direction(seg_rev)
        self.assertAlmostEqual(dx1, dx2)
        self.assertAlmostEqual(dy1, dy2)
        self.assertAlmostEqual(ox1, ox2)
        self.assertAlmostEqual(oy1, oy2)

    def test_position_along_wall_non_negative(self):
        wall = _horiz_seg(0, 100, 500, 100)
        pos = _position_along_wall(250, 100, wall)
        self.assertAlmostEqual(pos, 250.0)
        self.assertGreaterEqual(pos, 0)

    def test_position_reversed_segment_same(self):
        """Reversed segment gives the same position for the same point."""
        wall_fwd = _horiz_seg(0, 100, 500, 100)
        wall_rev = _horiz_seg(500, 100, 0, 100)
        pos_fwd = _position_along_wall(250, 110, wall_fwd)
        pos_rev = _position_along_wall(250, 110, wall_rev)
        self.assertAlmostEqual(pos_fwd, pos_rev)


class TestDoorPositionOrientationStability(unittest.TestCase):

    def test_reversed_wall_gives_same_position(self):
        """Door position is stable when wall segment is reversed."""
        wall_fwd = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        wall_rev = WallLine(segment=_horiz_seg(500, 100, 0, 100))
        leaf = _vert_seg(200, 85, 115)

        doors_fwd = detect_door_candidates(
            [wall_fwd.segment, leaf], [wall_fwd], [],
            scale_info=SCALE_INFO_1X,
        )
        doors_rev = detect_door_candidates(
            [wall_rev.segment, leaf], [wall_rev], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(doors_fwd), 1)
        self.assertEqual(len(doors_rev), 1)
        self.assertAlmostEqual(
            doors_fwd[0].position_along_wall_m,
            doors_rev[0].position_along_wall_m,
            places=2,
        )


class TestWindowPositionOrientationStability(unittest.TestCase):

    def test_reversed_wall_gives_same_position(self):
        wall_fwd = WallLine(segment=_horiz_seg(0, 100, 600, 100))
        wall_rev = WallLine(segment=_horiz_seg(600, 100, 0, 100))
        jamb1 = _vert_seg(290, 85, 115)
        jamb2 = _vert_seg(310, 85, 115)

        wins_fwd = detect_window_candidates(
            [wall_fwd.segment, jamb1, jamb2], [wall_fwd], [],
            scale_info=SCALE_INFO_1X,
        )
        wins_rev = detect_window_candidates(
            [wall_rev.segment, jamb1, jamb2], [wall_rev], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(wins_fwd), 1)
        self.assertEqual(len(wins_rev), 1)
        self.assertAlmostEqual(
            wins_fwd[0].position_along_wall_m,
            wins_rev[0].position_along_wall_m,
            places=2,
        )


class TestGapPositionOrientationStability(unittest.TestCase):

    def test_reversed_wall_gives_same_position(self):
        """Gap position is stable when either wall segment is reversed."""
        wall_a = WallLine(segment=_horiz_seg(0, 100, 200, 100))
        wall_b = WallLine(segment=_horiz_seg(300, 100, 600, 100))

        gaps_fwd = detect_gap_candidates(
            [wall_a.segment, wall_b.segment], [wall_a, wall_b], [],
            scale_info=SCALE_INFO_1X,
        )

        # Reverse both segments
        wall_a_rev = WallLine(segment=_horiz_seg(200, 100, 0, 100))
        wall_b_rev = WallLine(segment=_horiz_seg(600, 100, 300, 100))
        gaps_rev = detect_gap_candidates(
            [wall_a_rev.segment, wall_b_rev.segment], [wall_a_rev, wall_b_rev], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(gaps_fwd), 1)
        self.assertEqual(len(gaps_rev), 1)
        self.assertAlmostEqual(
            gaps_fwd[0].position_along_wall_m,
            gaps_rev[0].position_along_wall_m,
            places=2,
        )

    def test_swapped_segment_order_same_position(self):
        """Swapping input order of gap segments produces the same position."""
        wall_a = WallLine(segment=_horiz_seg(0, 100, 200, 100))
        wall_b = WallLine(segment=_horiz_seg(300, 100, 600, 100))

        gaps_ab = detect_gap_candidates(
            [wall_a.segment, wall_b.segment], [wall_a, wall_b], [],
            scale_info=SCALE_INFO_1X,
        )
        gaps_ba = detect_gap_candidates(
            [wall_b.segment, wall_a.segment], [wall_b, wall_a], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(gaps_ab), 1)
        self.assertEqual(len(gaps_ba), 1)
        self.assertAlmostEqual(
            gaps_ab[0].position_along_wall_m,
            gaps_ba[0].position_along_wall_m,
            places=2,
        )

    def test_gap_position_non_negative(self):
        wall_a = WallLine(segment=_horiz_seg(0, 100, 200, 100))
        wall_b = WallLine(segment=_horiz_seg(300, 100, 600, 100))

        gaps = detect_gap_candidates(
            [wall_a.segment, wall_b.segment], [wall_a, wall_b], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(gaps), 1)
        self.assertGreaterEqual(gaps[0].position_along_wall_m, 0)


# ---------------------------------------------------------------------------
# 5. Door detection (BLOCKER 4 — no fake swing, lower base confidence)
# ---------------------------------------------------------------------------

class TestDoorDetection(unittest.TestCase):

    def test_door_jamb_with_tag(self):
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
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        leaf = _vert_seg(200, 85, 115)
        words = [_word("D01", 200, 70)]

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], words,
            scale_info=SCALE_INFO_1X,
        )
        self.assertAlmostEqual(doors[0].geometry_confidence, 0.60)
        self.assertAlmostEqual(doors[0].semantic_confidence, 0.95)
        self.assertAlmostEqual(doors[0].association_confidence, 0.30)  # no wall_ref

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
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        leaf = _vert_seg(200, 85, 115)

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertAlmostEqual(doors[0].association_confidence, 0.30)


# ---------------------------------------------------------------------------
# 6. Window detection (BLOCKER 5 — wall-local, hatch-aware)
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
        self.assertAlmostEqual(windows[0].geometry_confidence, 0.70)
        self.assertAlmostEqual(windows[0].semantic_confidence, 0.95)

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

    # --- Multi-window wall (BLOCKER 1 — cluster-based, not whole-wall count) ---

    def test_three_windows_on_same_wall(self):
        """Wall with 3 windows (6 jamb segments) → 3 candidates, not 0."""
        wall = WallLine(segment=_horiz_seg(0, 100, 1200, 100))
        # Window 1: jambs at 190, 210 (spacing=20pt)
        jamb1a = _vert_seg(190, 85, 115)
        jamb1b = _vert_seg(210, 85, 115)
        # Window 2: jambs at 590, 610 (spacing=20pt)
        jamb2a = _vert_seg(590, 85, 115)
        jamb2b = _vert_seg(610, 85, 115)
        # Window 3: jambs at 990, 1010 (spacing=20pt)
        jamb3a = _vert_seg(990, 85, 115)
        jamb3b = _vert_seg(1010, 85, 115)

        all_segs = [wall.segment, jamb1a, jamb1b, jamb2a, jamb2b, jamb3a, jamb3b]
        words = [_word("W01", 200, 70), _word("W02", 600, 70), _word("W03", 1000, 70)]

        windows = detect_window_candidates(
            all_segs, [wall], words,
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(windows), 3)
        tags = {w.tag for w in windows}
        self.assertEqual(tags, {"W01", "W02", "W03"})

    def test_two_windows_on_same_wall(self):
        """Wall with 2 windows → 2 candidates."""
        wall = WallLine(segment=_horiz_seg(0, 100, 800, 100))
        jamb1a = _vert_seg(190, 85, 115)
        jamb1b = _vert_seg(210, 85, 115)
        jamb2a = _vert_seg(590, 85, 115)
        jamb2b = _vert_seg(610, 85, 115)

        all_segs = [wall.segment, jamb1a, jamb1b, jamb2a, jamb2b]

        windows = detect_window_candidates(
            all_segs, [wall], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(windows), 2)

    # --- Hatch/grid false-positive tests (BLOCKER 5) ---

    def test_hatch_panel_not_detected_as_window(self):
        """Many parallel perpendicular lines with regular spacing → hatch."""
        wall = WallLine(segment=_horiz_seg(0, 100, 800, 100))
        hatch_segs = [_vert_seg(x, 85, 115) for x in range(100, 400, 30)]

        windows = detect_window_candidates(
            [wall.segment] + hatch_segs, [wall], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(windows), 0)

    def test_batten_repetition_not_window(self):
        wall = WallLine(segment=_horiz_seg(0, 100, 800, 100))
        battens = [_vert_seg(x, 90, 110) for x in range(50, 350, 20)]

        windows = detect_window_candidates(
            [wall.segment] + battens, [wall], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(windows), 0)

    def test_dimension_ticks_not_window(self):
        wall = WallLine(segment=_horiz_seg(0, 100, 800, 100))
        ticks = [_vert_seg(x, 95, 105) for x in range(100, 400, 50)]

        windows = detect_window_candidates(
            [wall.segment] + ticks, [wall], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(windows), 0)

    def test_balustrade_repeated_lines_not_window(self):
        wall = WallLine(segment=_horiz_seg(0, 100, 800, 100))
        rails = [_vert_seg(x, 80, 120) for x in range(100, 400, 25)]

        windows = detect_window_candidates(
            [wall.segment] + rails, [wall], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(windows), 0)

    def test_two_unrelated_parallel_near_wall_not_window(self):
        wall = WallLine(segment=_horiz_seg(0, 100, 800, 100))
        seg1 = _vert_seg(100, 85, 115)
        seg2 = _vert_seg(400, 85, 115)  # 300pt apart > MAX_PAIR_DISTANCE_PT

        windows = detect_window_candidates(
            [wall.segment, seg1, seg2], [wall], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(windows), 0)

    # --- Scale-aware hatch filter (BLOCKER 1 — different scales) ---

    def test_three_windows_close_1_5m_at_50pt_m(self):
        """Three tagged windows ~1.5 m apart at 50 pt/m → 3 candidates.
        W tags override geometric hatch suspicion for close windows."""
        wall = WallLine(segment=_horiz_seg(0, 100, 1200, 100))
        # Window centres at 300, 375, 450 → spacing = 75pt = 1.5m at 50pt/m
        jamb1a = _vert_seg(290, 85, 115)
        jamb1b = _vert_seg(310, 85, 115)
        jamb2a = _vert_seg(365, 85, 115)
        jamb2b = _vert_seg(385, 85, 115)
        jamb3a = _vert_seg(440, 85, 115)
        jamb3b = _vert_seg(460, 85, 115)

        all_segs = [wall.segment, jamb1a, jamb1b, jamb2a, jamb2b, jamb3a, jamb3b]
        words = [_word("W01", 300, 70), _word("W02", 375, 70), _word("W03", 450, 70)]

        windows = detect_window_candidates(
            all_segs, [wall], words,
            scale_info={"px_per_m": 50.0, "render_zoom": 1.0},
        )
        self.assertEqual(len(windows), 3)
        tags = {w.tag for w in windows}
        self.assertEqual(tags, {"W01", "W02", "W03"})

    def test_three_windows_close_1_5m_at_200pt_m(self):
        """Same 3-window layout at 200 pt/m with 0.9 m opening widths → 3 candidates.
        Proves physical scale invariance: same layout at different PDF scales."""
        wall = WallLine(segment=_horiz_seg(0, 100, 4800, 100))
        # Window centres at 1200, 1500, 1800 → spacing = 300pt = 1.5m at 200pt/m
        # Jamb spacing = 180pt = 0.9m opening width (realistic)
        jamb1a = _vert_seg(1110, 85, 115)
        jamb1b = _vert_seg(1290, 85, 115)
        jamb2a = _vert_seg(1410, 85, 115)
        jamb2b = _vert_seg(1590, 85, 115)
        jamb3a = _vert_seg(1710, 85, 115)
        jamb3b = _vert_seg(1890, 85, 115)

        all_segs = [wall.segment, jamb1a, jamb1b, jamb2a, jamb2b, jamb3a, jamb3b]
        words = [_word("W01", 1200, 70), _word("W02", 1500, 70), _word("W03", 1800, 70)]

        windows = detect_window_candidates(
            all_segs, [wall], words,
            scale_info={"px_per_m": 200.0, "render_zoom": 1.0},
        )
        self.assertEqual(len(windows), 3)
        tags = {w.tag for w in windows}
        self.assertEqual(tags, {"W01", "W02", "W03"})

    def test_w_tag_overrides_hatch_filter_for_multiple_pairs(self):
        """Five window pairs at 1.4 m spacing (70pt at 50pt/m) → hatch filter
        rejects untagged pairs but W tags override for tagged pairs.

        Without tags: max_gap=70pt < scale_min=75pt → is_hatch=True
        With W03 tag on middle pair: any_w_tag=True → skip hatch filter"""
        wall = WallLine(segment=_horiz_seg(0, 100, 600, 100))
        # 5 pairs at centres 150, 220, 290, 360, 430 → spacing = 70pt = 1.4m
        # Jamb spacing = 44pt = 0.88m opening width
        # All gaps = 70pt < scale_min=75pt → hatch filter triggers without tags
        pairs_segs = []
        for cx in [150, 220, 290, 360, 430]:
            pairs_segs.append(_vert_seg(cx - 22, 85, 115))
            pairs_segs.append(_vert_seg(cx + 22, 85, 115))

        all_segs = [wall.segment] + pairs_segs
        words_no_tag = []
        words_with_tag = [_word("W03", 290, 70)]

        # Without tags: hatch filter rejects (max_gap=70 < scale_min=75)
        windows_no_tag = detect_window_candidates(
            all_segs, [wall], words_no_tag,
            scale_info={"px_per_m": 50.0, "render_zoom": 1.0},
        )
        self.assertEqual(len(windows_no_tag), 0)

        # With W03 tag: W-tag override bypasses hatch filter → 5 windows
        windows_with_tag = detect_window_candidates(
            all_segs, [wall], words_with_tag,
            scale_info={"px_per_m": 50.0, "render_zoom": 1.0},
        )
        self.assertEqual(len(windows_with_tag), 5)


# ---------------------------------------------------------------------------
# 6b. Segment.bbox regression
# ---------------------------------------------------------------------------

class TestSegmentBbox(unittest.TestCase):

    def test_bbox_normal_endpoints(self):
        s = Segment(x1=10, y1=20, x2=30, y2=40)
        self.assertEqual(s.bbox, (10, 20, 30, 40))

    def test_bbox_reversed_endpoints(self):
        """Reversed endpoints produce the same bbox."""
        s = Segment(x1=30, y1=40, x2=10, y2=20)
        self.assertEqual(s.bbox, (10, 20, 30, 40))

    def test_bbox_mixed_reversal(self):
        s = Segment(x1=30, y1=10, x2=10, y2=40)
        self.assertEqual(s.bbox, (10, 10, 30, 40))


# ---------------------------------------------------------------------------
# 7. Gap detection (BLOCKER 2 — real discontinuities only)
# ---------------------------------------------------------------------------

class TestGapDetection(unittest.TestCase):

    def test_continuous_wall_no_gaps(self):
        """A continuous wall with perpendicular intersections → 0 gaps."""
        wall = WallLine(segment=_horiz_seg(0, 100, 600, 100))
        left = _vert_seg(100, 85, 115)
        right = _vert_seg(200, 85, 115)

        gaps = detect_gap_candidates(
            [wall.segment, left, right], [wall], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(gaps), 0)

    def test_continuous_wall_with_intersections_no_gaps(self):
        wall = WallLine(segment=_horiz_seg(0, 100, 800, 100))
        perps = [_vert_seg(x, 85, 115) for x in [100, 200, 300, 400, 500]]

        gaps = detect_gap_candidates(
            [wall.segment] + perps, [wall], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(gaps), 0)

    def test_two_collinear_wall_segments_with_gap(self):
        wall_a = WallLine(segment=_horiz_seg(0, 100, 200, 100))
        wall_b = WallLine(segment=_horiz_seg(300, 100, 600, 100))

        gaps = detect_gap_candidates(
            [wall_a.segment, wall_b.segment], [wall_a, wall_b], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(gaps), 1)
        self.assertAlmostEqual(gaps[0].width_m, 2.0, places=2)
        self.assertAlmostEqual(gaps[0].geometry_confidence, 0.75)

    def test_gap_width_from_wall_discontinuity(self):
        wall_a = WallLine(segment=_horiz_seg(0, 100, 150, 100))
        wall_b = WallLine(segment=_horiz_seg(250, 100, 500, 100))

        gaps = detect_gap_candidates(
            [wall_a.segment, wall_b.segment], [wall_a, wall_b], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(gaps), 1)
        self.assertAlmostEqual(gaps[0].width_m, 2.0, places=2)

    def test_no_gap_when_segments_overlap(self):
        wall_a = WallLine(segment=_horiz_seg(0, 100, 300, 100))
        wall_b = WallLine(segment=_horiz_seg(200, 100, 600, 100))

        gaps = detect_gap_candidates(
            [wall_a.segment, wall_b.segment], [wall_a, wall_b], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(gaps), 0)

    def test_no_gap_when_fillers_block(self):
        wall_a = WallLine(segment=_horiz_seg(0, 100, 200, 100))
        wall_b = WallLine(segment=_horiz_seg(300, 100, 600, 100))
        filler = _horiz_seg(205, 100, 295, 100)

        gaps = detect_gap_candidates(
            [wall_a.segment, wall_b.segment, filler], [wall_a, wall_b], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(gaps), 0)

    def test_non_collinear_segments_no_gap(self):
        wall_h = WallLine(segment=_horiz_seg(0, 100, 300, 100))
        wall_v = WallLine(segment=_vert_seg(400, 0, 300))

        gaps = detect_gap_candidates(
            [wall_h.segment, wall_v.segment], [wall_h, wall_v], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(gaps), 0)

    def test_gap_with_tag(self):
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
        self.assertAlmostEqual(gaps[0].geometry_confidence, 0.75)


# ---------------------------------------------------------------------------
# 7b. Gap suppression — blank wall_ref safety
# ---------------------------------------------------------------------------

class TestGapSuppression(unittest.TestCase):

    def test_different_walls_blank_ref_not_suppressed(self):
        """Gap on wall B NOT suppressed by door on wall A when both have blank ref.
        Walls are perpendicular to avoid the filler-check bug (parallel segments
        on different wall lines filling each other's gaps)."""
        # Wall A: horizontal, door at x=500 → position 10.0m
        wall_a = WallLine(segment=_horiz_seg(0, 100, 1000, 100))
        door_leaf = _vert_seg(500, 85, 115)
        # Wall B: vertical at x=800 with gap from y=480→520 → position 10.0m
        wall_b1 = WallLine(segment=_vert_seg(800, 0, 480))
        wall_b2 = WallLine(segment=_vert_seg(800, 520, 1000))

        words = [_word("D01", 500, 70)]

        result = plan_opening_candidates(
            [wall_a.segment, door_leaf, wall_b1.segment, wall_b2.segment],
            words,
            scale_info=SCALE_INFO_1X,
        )
        # Door on wall A should NOT suppress gap on wall B (blank ref safety)
        gap_count = sum(1 for c in result.candidates if c.opening_type == OPENING_TYPE_OTHER)
        self.assertGreaterEqual(gap_count, 1)

    def test_same_wall_matching_ref_suppressed(self):
        """Gap at same position on same wall as a door → suppressed.
        Gap is ≥30pt (production MIN_GAP_PT), wall_lines passed explicitly
        to preserve N01 ref. Proves the gap exists before suppression."""
        # Two wall segments on same line with a 50pt gap at x≈500
        gap_a = WallLine(segment=_horiz_seg(0, 100, 475, 100), wall_ref="N01")
        gap_b = WallLine(segment=_horiz_seg(525, 100, 1000, 100), wall_ref="N01")
        # Door leaf at x=500 (on gap_a's extended line, position 10.0m)
        door_leaf = _vert_seg(500, 85, 115)

        words = [_word("D01", 500, 70)]

        # Pass wall_lines explicitly so N01 ref is preserved
        result = plan_opening_candidates(
            [gap_a.segment, door_leaf, gap_b.segment],
            words,
            wall_lines=[gap_a, gap_b],
            scale_info=SCALE_INFO_1X,
        )
        # Verify: gap exists as a candidate (position ≈10.0m on N01)
        all_positions = [c.position_along_wall_m for c in result.candidates
                         if c.position_along_wall_m is not None]
        close_to_10m = [p for p in all_positions if abs(p - 10.0) < 0.2]
        # Door at 10.0m is one candidate; gap at 10.0m should be suppressed
        self.assertLessEqual(len(close_to_10m), 1)

    def test_different_walls_nonempty_ref_not_suppressed(self):
        """Gap on wall S01 NOT suppressed by door on wall N01 (different refs).
        Uses pre-detected wall_lines to preserve wall_ref assignments."""
        # Wall N: horizontal, door at x=500 → position 10.0m
        wall_n = WallLine(segment=_horiz_seg(0, 100, 1000, 100), wall_ref="N01")
        door_leaf = _vert_seg(500, 85, 115)
        # Wall S: vertical at x=800 with gap → position 10.0m, ref S01
        wall_s1 = WallLine(segment=_vert_seg(800, 0, 480), wall_ref="S01")
        wall_s2 = WallLine(segment=_vert_seg(800, 520, 1000), wall_ref="S01")

        words = [_word("D01", 500, 70)]

        # Pass pre-detected wall_lines so wall_ref is preserved
        result = plan_opening_candidates(
            [wall_n.segment, door_leaf, wall_s1.segment, wall_s2.segment],
            words,
            wall_lines=[wall_n, wall_s1, wall_s2],
            scale_info=SCALE_INFO_1X,
        )
        # Gap on S01 should survive — different wall_ref from door on N01
        s01_gaps = [c for c in result.candidates
                    if c.wall_ref == "S01" and c.opening_type == OPENING_TYPE_OTHER]
        self.assertGreaterEqual(len(s01_gaps), 1)


# ---------------------------------------------------------------------------
# 8. Confidence channel separation (BLOCKER 3)
# ---------------------------------------------------------------------------

class TestConfidenceChannels(unittest.TestCase):

    def test_tag_does_not_inflate_geometry_confidence(self):
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100), wall_ref="N01")
        leaf = _vert_seg(200, 85, 115)
        words = [_word("D01", 200, 70)]

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], words,
            scale_info=SCALE_INFO_1X,
        )
        self.assertLessEqual(doors[0].geometry_confidence, 0.65)
        self.assertAlmostEqual(doors[0].semantic_confidence, 0.95)
        self.assertNotAlmostEqual(
            doors[0].geometry_confidence,
            doors[0].semantic_confidence,
        )

    def test_window_tag_does_not_inflate_geometry(self):
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
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        leaf = _vert_seg(200, 85, 115)
        words = [_word("D01", 200, 70)]

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], words,
            scale_info=SCALE_INFO_1X,
        )
        self.assertAlmostEqual(doors[0].association_confidence, 0.30)

    def test_wall_ref_gives_higher_association(self):
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100), wall_ref="N01")
        leaf = _vert_seg(200, 85, 115)
        words = [_word("D01", 200, 70)]

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], words,
            scale_info=SCALE_INFO_1X,
        )
        self.assertAlmostEqual(doors[0].association_confidence, 0.70)

    def test_three_channels_are_independent(self):
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100), wall_ref="N01")
        leaf = _vert_seg(200, 85, 115)
        words = [_word("D01", 200, 70)]

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], words,
            scale_info=SCALE_INFO_1X,
        )
        d = doors[0]
        self.assertGreater(d.geometry_confidence, 0)
        self.assertGreater(d.association_confidence, 0)
        self.assertGreater(d.semantic_confidence, 0)
        self.assertLessEqual(d.geometry_confidence, 0.65)


# ---------------------------------------------------------------------------
# 9. Tag/type compatibility (BLOCKER 2 — no cross-type type_mark)
# ---------------------------------------------------------------------------

class TestTagTypeCompatibility(unittest.TestCase):

    def test_door_with_nearby_w_tag_blank_mark(self):
        """Door geometry + nearby W01 → blank type_mark, W01 recorded as evidence."""
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        leaf = _vert_seg(200, 85, 115)
        words = [_word("W01", 200, 70)]

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], words,
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(doors), 1)
        self.assertEqual(doors[0].tag, "")  # blank — W tag incompatible with door
        self.assertAlmostEqual(doors[0].semantic_confidence, 0.30)  # conflicting tag
        # Evidence should note the conflict
        conflict_ev = [e for e in doors[0].evidence if "conflicting" in e.lower() or "W01" in e]
        self.assertTrue(len(conflict_ev) > 0)

    def test_window_with_nearby_d_tag_blank_mark(self):
        """Window geometry + nearby D01 → blank type_mark."""
        wall = WallLine(segment=_horiz_seg(0, 100, 600, 100))
        jamb1 = _vert_seg(290, 85, 115)
        jamb2 = _vert_seg(310, 85, 115)
        words = [_word("D01", 300, 70)]

        windows = detect_window_candidates(
            [wall.segment, jamb1, jamb2], [wall], words,
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].tag, "")  # blank — D tag incompatible with window
        self.assertAlmostEqual(windows[0].semantic_confidence, 0.30)  # conflicting

    def test_door_with_matching_d_tag(self):
        """Door geometry + D01 → type_mark = D01."""
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        leaf = _vert_seg(200, 85, 115)
        words = [_word("D01", 200, 70)]

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], words,
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(doors[0].tag, "D01")
        self.assertAlmostEqual(doors[0].semantic_confidence, 0.95)

    def test_window_with_matching_w_tag(self):
        """Window geometry + W01 → type_mark = W01."""
        wall = WallLine(segment=_horiz_seg(0, 100, 600, 100))
        jamb1 = _vert_seg(290, 85, 115)
        jamb2 = _vert_seg(310, 85, 115)
        words = [_word("W01", 300, 70)]

        windows = detect_window_candidates(
            [wall.segment, jamb1, jamb2], [wall], words,
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(windows[0].tag, "W01")
        self.assertAlmostEqual(windows[0].semantic_confidence, 0.95)

    def test_gap_with_any_tag_classifies(self):
        """Gap (generic) may use D or W tag to classify the opening type."""
        wall_a = WallLine(segment=_horiz_seg(0, 100, 200, 100))
        wall_b = WallLine(segment=_horiz_seg(300, 100, 600, 100))
        words = [_word("D03", 250, 70)]

        gaps = detect_gap_candidates(
            [wall_a.segment, wall_b.segment], [wall_a, wall_b], words,
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].tag, "D03")

    def test_door_evidence_records_conflicting_w_tag(self):
        """Conflicting W tag is recorded in evidence, not lost."""
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        leaf = _vert_seg(200, 85, 115)
        words = [_word("W01", 200, 70)]

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], words,
            scale_info=SCALE_INFO_1X,
        )
        all_ev = " ".join(doors[0].evidence)
        self.assertIn("W01", all_ev)
        self.assertIn("conflicting", all_ev.lower())

    def test_converted_evidence_has_blank_mark(self):
        """Converted OpeningEvidence for door + W01 has blank type_mark."""
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        leaf = _vert_seg(200, 85, 115)
        words = [_word("W01", 200, 70)]

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], words,
            scale_info=SCALE_INFO_1X,
        )
        ev = door_to_opening_evidence(doors[0])
        self.assertEqual(ev.type_mark, "")
        self.assertEqual(ev.opening_type, OPENING_TYPE_DOOR)


# ---------------------------------------------------------------------------
# 10. Wall-ref conflict resolution
# ---------------------------------------------------------------------------

class TestWallRefConflict(unittest.TestCase):

    def test_conflicting_wall_ref_gap_blank(self):
        """Two collinear segments with different non-empty wall_refs → blank."""
        wall_a = WallLine(segment=_horiz_seg(0, 100, 200, 100), wall_ref="N01")
        wall_b = WallLine(segment=_horiz_seg(300, 100, 600, 100), wall_ref="S02")

        gaps = detect_gap_candidates(
            [wall_a.segment, wall_b.segment], [wall_a, wall_b], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].wall_ref, "")  # conflict → blank
        self.assertAlmostEqual(gaps[0].association_confidence, 0.30)  # low

    def test_matching_wall_ref_gap_preserved(self):
        """Same wall_ref on both segments → preserved."""
        wall_a = WallLine(segment=_horiz_seg(0, 100, 200, 100), wall_ref="N01")
        wall_b = WallLine(segment=_horiz_seg(300, 100, 600, 100), wall_ref="N01")

        gaps = detect_gap_candidates(
            [wall_a.segment, wall_b.segment], [wall_a, wall_b], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].wall_ref, "N01")

    def test_one_empty_one_populated_wall_ref(self):
        """One segment has wall_ref, other is empty → use the populated one."""
        wall_a = WallLine(segment=_horiz_seg(0, 100, 200, 100))
        wall_b = WallLine(segment=_horiz_seg(300, 100, 600, 100), wall_ref="N01")

        gaps = detect_gap_candidates(
            [wall_a.segment, wall_b.segment], [wall_a, wall_b], [],
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].wall_ref, "N01")


# ---------------------------------------------------------------------------
# 11. Conversion to OpeningEvidence
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
# 12. Main pipeline integration
# ---------------------------------------------------------------------------

class TestPlanOpeningCandidates(unittest.TestCase):

    def test_full_pipeline_door_and_window(self):
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

    def test_pipeline_three_windows(self):
        """Full pipeline: wall with 3 windows produces 3 candidates."""
        wall = _horiz_seg(0, 100, 1200, 100)
        pairs = [
            (_vert_seg(190, 85, 115), _vert_seg(210, 85, 115)),
            (_vert_seg(590, 85, 115), _vert_seg(610, 85, 115)),
            (_vert_seg(990, 85, 115), _vert_seg(1010, 85, 115)),
        ]
        words = [_word("W01", 200, 70), _word("W02", 600, 70), _word("W03", 1000, 70)]

        all_segs = [wall]
        for a, b in pairs:
            all_segs.extend([a, b])

        result = plan_opening_candidates(
            all_segs, words,
            scale_info=SCALE_INFO_1X,
        )
        self.assertEqual(result.window_count, 3)
        self.assertEqual(len(result.candidates), 3)
        marks = {c.type_mark for c in result.candidates}
        self.assertEqual(marks, {"W01", "W02", "W03"})


# ---------------------------------------------------------------------------
# 13. Contract compliance
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
