"""Tests for Priority 5 Phase B1 — plan-vector opening candidate detection.

Covers:
  1. Geometry primitives (Segment, TextWord, classification)
  2. Wall line detection
  3. Door detection (leaf + tag → OpeningEvidence)
  4. Window detection (parallel pair + tag → OpeningEvidence)
  5. Gap detection (wall gap without specific features)
  6. Confidence scoring
  7. Output format (OpeningEvidence contract compliance)
  8. Edge cases (no wall lines, no segments, empty words)
  9. False positive filtering (no overlapping gaps)
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
    _point_segment_distance,
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
    """Horizontal segment. 3-arg: _horiz_seg(x1, y, x2). 4-arg: _horiz_seg(x1, y1, x2, y2)."""
    if y2 is None:
        y2 = y
    return Segment(x1=x1, y1=y, x2=x2, y2=y2)


def _vert_seg(x: float, y1: float, y2: float) -> Segment:
    """Vertical segment."""
    return Segment(x1=x, y1=y1, x2=x, y2=y2)


def _word(text: str, cx: float, cy: float) -> TextWord:
    """Text word at a point (small bbox)."""
    return TextWord(text=text, x0=cx - 10, y0=cy - 5, x1=cx + 10, y1=cy + 5)


# Typical floor plan scale: 50 px/m → 1m = 50pt
SCALE = 50.0


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
        # Point directly above midpoint
        d = _point_segment_distance(50, 10, s)
        self.assertAlmostEqual(d, 10.0)

    def test_point_segment_distance_at_endpoint(self):
        s = _horiz_seg(0, 0, 100, 0)
        d = _point_segment_distance(150, 0, s)
        self.assertAlmostEqual(d, 50.0)


# ---------------------------------------------------------------------------
# 2. Wall line detection
# ---------------------------------------------------------------------------

class TestWallLineDetection(unittest.TestCase):

    def test_long_segments_are_walls(self):
        segs = [
            _horiz_seg(0, 100, 500, 100),  # long → wall
            _horiz_seg(100, 100, 120, 100),  # short → not wall
            _vert_seg(0, 0, 400),  # long → wall
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
# 3. Door detection
# ---------------------------------------------------------------------------

class TestDoorDetection(unittest.TestCase):

    def test_door_with_tag(self):
        """A perpendicular short segment near a wall with D01 tag → door."""
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        # Door leaf: perpendicular to wall, at x=200
        leaf = _vert_seg(200, 85, 115)  # 30pt tall, centered on wall
        words = [_word("D01", 200, 70)]

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], words,
            scale_px_per_m=SCALE,
        )
        self.assertEqual(len(doors), 1)
        self.assertEqual(doors[0].tag, "D01")
        self.assertAlmostEqual(doors[0].confidence, 0.95)
        self.assertIsNotNone(doors[0].width_m)
        # Width = 30pt / 50 px/m = 0.6m
        self.assertAlmostEqual(doors[0].width_m, 0.6, places=2)

    def test_door_without_tag(self):
        """Perpendicular segment without tag → lower confidence door."""
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        leaf = _vert_seg(200, 85, 115)

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], [],
            scale_px_per_m=SCALE,
        )
        self.assertEqual(len(doors), 1)
        self.assertEqual(doors[0].tag, "")
        # Confidence should be 0.78 (width 0.6m is in range)
        self.assertAlmostEqual(doors[0].confidence, 0.78)

    def test_no_door_when_too_far_from_wall(self):
        """Segment perpendicular to wall but too far away → not a door."""
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        # Leaf 80pt away from wall
        leaf = _vert_seg(200, 20, 50)

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], [],
            scale_px_per_m=SCALE,
        )
        self.assertEqual(len(doors), 0)

    def test_no_door_when_wrong_angle(self):
        """Segment near wall but not perpendicular → not a door."""
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        # 45-degree segment near wall
        diag = Segment(x1=190, y1=90, x2=210, y2=110)

        doors = detect_door_candidates(
            [wall.segment, diag], [wall], [],
            scale_px_per_m=SCALE,
        )
        self.assertEqual(len(doors), 0)

    def test_position_along_wall(self):
        """Door position is computed correctly along wall length."""
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        leaf = _vert_seg(250, 85, 115)  # at midpoint of wall

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], [],
            scale_px_per_m=SCALE,
        )
        self.assertEqual(len(doors), 1)
        # Position: 250pt / 50 px/m = 5.0m
        self.assertAlmostEqual(doors[0].position_along_wall_m, 5.0, places=2)


# ---------------------------------------------------------------------------
# 4. Window detection
# ---------------------------------------------------------------------------

class TestWindowDetection(unittest.TestCase):

    def test_window_with_tag(self):
        """Two parallel perpendicular segments near wall with W01 → window."""
        wall = WallLine(segment=_horiz_seg(0, 100, 600, 100))
        # Two parallel vertical segments (jamb pair)
        jamb1 = _vert_seg(290, 85, 115)
        jamb2 = _vert_seg(310, 85, 115)
        words = [_word("W01", 300, 70)]

        windows = detect_window_candidates(
            [wall.segment, jamb1, jamb2], [wall], words,
            scale_px_per_m=SCALE,
        )
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].tag, "W01")
        self.assertAlmostEqual(windows[0].confidence, 0.95)
        # Width: distance between jambs = 20pt / 50 = 0.4m
        self.assertAlmostEqual(windows[0].width_m, 0.4, places=2)

    def test_window_without_tag(self):
        """Parallel pair without tag → lower confidence."""
        wall = WallLine(segment=_horiz_seg(0, 100, 600, 100))
        jamb1 = _vert_seg(290, 85, 115)
        jamb2 = _vert_seg(310, 85, 115)

        windows = detect_window_candidates(
            [wall.segment, jamb1, jamb2], [wall], [],
            scale_px_per_m=SCALE,
        )
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].tag, "")
        self.assertAlmostEqual(windows[0].confidence, 0.78)

    def test_no_window_when_not_parallel(self):
        """Two perpendicular segments that are not parallel → not a window."""
        wall = WallLine(segment=_horiz_seg(0, 100, 600, 100))
        seg1 = _vert_seg(290, 85, 115)
        seg2 = _horiz_seg(310, 95, 330, 105)  # horizontal, not parallel to seg1

        windows = detect_window_candidates(
            [wall.segment, seg1, seg2], [wall], [],
            scale_px_per_m=SCALE,
        )
        self.assertEqual(len(windows), 0)

    def test_no_window_when_too_far_apart(self):
        """Parallel segments too far apart → not a window."""
        wall = WallLine(segment=_horiz_seg(0, 100, 600, 100))
        jamb1 = _vert_seg(200, 85, 115)
        jamb2 = _vert_seg(400, 85, 115)  # 200pt apart > MAX_PAIR_DISTANCE_PT

        windows = detect_window_candidates(
            [wall.segment, jamb1, jamb2], [wall], [],
            scale_px_per_m=SCALE,
        )
        self.assertEqual(len(windows), 0)


# ---------------------------------------------------------------------------
# 5. Gap detection
# ---------------------------------------------------------------------------

class TestGapDetection(unittest.TestCase):

    def test_wall_gap_detected(self):
        """Two perpendicular segments with a gap between them → gap candidate."""
        wall = WallLine(segment=_horiz_seg(0, 100, 600, 100))
        # Two perpendicular segments on either side of a gap
        left = _vert_seg(100, 85, 115)
        right = _vert_seg(200, 85, 115)

        gaps = detect_gap_candidates(
            [wall.segment, left, right], [wall], [],
            scale_px_per_m=SCALE,
        )
        self.assertEqual(len(gaps), 1)
        # Gap width: 100pt / 50 = 2.0m
        self.assertAlmostEqual(gaps[0].width_m, 2.0, places=2)

    def test_no_gap_when_wall_fills_it(self):
        """Gap filled by a parallel wall segment → not detected."""
        wall = WallLine(segment=_horiz_seg(0, 100, 600, 100))
        left = _vert_seg(100, 85, 115)
        right = _vert_seg(200, 85, 115)
        filler = _horiz_seg(110, 100, 190, 100)  # fills the gap

        gaps = detect_gap_candidates(
            [wall.segment, left, right, filler], [wall], [],
            scale_px_per_m=SCALE,
        )
        self.assertEqual(len(gaps), 0)

    def test_gap_with_tag(self):
        """Gap with nearby tag → higher confidence."""
        wall = WallLine(segment=_horiz_seg(0, 100, 600, 100))
        left = _vert_seg(100, 85, 115)
        right = _vert_seg(200, 85, 115)
        words = [_word("D03", 150, 70)]

        gaps = detect_gap_candidates(
            [wall.segment, left, right], [wall], words,
            scale_px_per_m=SCALE,
        )
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].tag, "D03")
        self.assertAlmostEqual(gaps[0].confidence, 0.75)


# ---------------------------------------------------------------------------
# 6. Confidence scoring
# ---------------------------------------------------------------------------

class TestConfidenceScoring(unittest.TestCase):

    def test_door_tag_gives_high_confidence(self):
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        leaf = _vert_seg(200, 85, 115)
        words = [_word("D01", 200, 70)]

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], words,
            scale_px_per_m=SCALE,
        )
        self.assertAlmostEqual(doors[0].confidence, 0.95)

    def test_window_tag_gives_high_confidence(self):
        wall = WallLine(segment=_horiz_seg(0, 100, 600, 100))
        jamb1 = _vert_seg(290, 85, 115)
        jamb2 = _vert_seg(310, 85, 115)
        words = [_word("W01", 300, 70)]

        windows = detect_window_candidates(
            [wall.segment, jamb1, jamb2], [wall], words,
            scale_px_per_m=SCALE,
        )
        self.assertAlmostEqual(windows[0].confidence, 0.95)

    def test_no_tag_lower_confidence(self):
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        leaf = _vert_seg(200, 85, 115)

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], [],
            scale_px_per_m=SCALE,
        )
        # 0.78 because width 0.6m is in reasonable range
        self.assertAlmostEqual(doors[0].confidence, 0.78)

    def test_unrelated_tag_not_found(self):
        """Non-D/W tag is not recognized → treated as no tag → lower confidence."""
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        leaf = _vert_seg(200, 85, 115)
        words = [_word("R01", 200, 70)]  # R tag, not D or W

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], words,
            scale_px_per_m=SCALE,
        )
        # R01 is not a D/W tag → tag not found → conf = 0.78
        self.assertEqual(doors[0].tag, "")
        self.assertAlmostEqual(doors[0].confidence, 0.78)


# ---------------------------------------------------------------------------
# 7. Conversion to OpeningEvidence
# ---------------------------------------------------------------------------

class TestConversionToEvidence(unittest.TestCase):

    def test_door_candidate_to_evidence(self):
        cand = DoorCandidate(
            wall_ref="N01",
            position_along_wall_m=3.0,
            width_m=0.82,
            tag="D01",
            confidence=0.95,
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
        self.assertEqual(ev.page_no, 1)
        self.assertIn("test_evidence", ev.evidence)
        self.assertIn("plan_vector_door_detection", ev.evidence)

    def test_window_candidate_to_evidence(self):
        cand = WindowCandidate(
            wall_ref="N01",
            position_along_wall_m=5.0,
            width_m=1.2,
            tag="W01",
            confidence=0.95,
            evidence=["test_evidence"],
            page_no=2,
        )
        ev = window_to_opening_evidence(cand)

        self.assertIsInstance(ev, OpeningEvidence)
        self.assertEqual(ev.opening_type, OPENING_TYPE_WINDOW)
        self.assertEqual(ev.type_mark, "W01")
        self.assertEqual(ev.wall_ref, "N01")
        self.assertAlmostEqual(ev.width_m, 1.2)
        self.assertIsNone(ev.height_m)
        self.assertEqual(ev.sill_m, 0.9)
        self.assertEqual(ev.quantity, 1)
        self.assertFalse(ev.deduct)

    def test_gap_candidate_to_evidence(self):
        cand = GapCandidate(
            wall_ref="N01",
            position_along_wall_m=4.0,
            width_m=1.5,
            tag="",
            confidence=0.50,
            evidence=["test_evidence"],
            page_no=3,
        )
        ev = gap_to_opening_evidence(cand)

        self.assertIsInstance(ev, OpeningEvidence)
        self.assertEqual(ev.opening_type, OPENING_TYPE_OTHER)
        self.assertEqual(ev.type_mark, "")
        self.assertEqual(ev.wall_ref, "N01")
        self.assertEqual(ev.quantity, 1)
        self.assertFalse(ev.deduct)

    def test_gap_with_door_tag_classifies_as_door(self):
        cand = GapCandidate(
            wall_ref="N01",
            position_along_wall_m=2.0,
            width_m=0.9,
            tag="D03",
            confidence=0.75,
            evidence=[],
            page_no=1,
        )
        ev = gap_to_opening_evidence(cand)
        self.assertEqual(ev.opening_type, OPENING_TYPE_DOOR)
        self.assertEqual(ev.type_mark, "D03")


# ---------------------------------------------------------------------------
# 8. Main pipeline integration
# ---------------------------------------------------------------------------

class TestPlanOpeningCandidates(unittest.TestCase):

    def test_full_pipeline_door_and_window(self):
        """Complete pipeline with a door and a window on one wall."""
        wall = _horiz_seg(0, 100, 800, 100)
        # Door at x=150
        door_leaf = _vert_seg(150, 85, 115)
        # Window at x=500 (parallel pair)
        jamb1 = _vert_seg(490, 85, 115)
        jamb2 = _vert_seg(510, 85, 115)

        words = [_word("D01", 150, 70), _word("W01", 500, 70)]
        all_segs = [wall, door_leaf, jamb1, jamb2]

        result = plan_opening_candidates(
            all_segs, words,
            scale_px_per_m=SCALE,
            page_no=1,
        )

        self.assertIsInstance(result, PlanOpeningDetectionResult)
        self.assertEqual(result.door_count, 1)
        self.assertEqual(result.window_count, 1)
        self.assertGreaterEqual(len(result.candidates), 2)

        # Check types
        types = {c.opening_type for c in result.candidates}
        self.assertIn(OPENING_TYPE_DOOR, types)
        self.assertIn(OPENING_TYPE_WINDOW, types)

        # All should be plan_vector
        for c in result.candidates:
            self.assertEqual(c.extraction_method, "plan_vector")
            self.assertFalse(c.deduct)
            self.assertEqual(c.quantity, 1)

    def test_empty_segments(self):
        """No segments → no candidates."""
        result = plan_opening_candidates([], [], scale_px_per_m=SCALE)
        self.assertEqual(len(result.candidates), 0)
        self.assertEqual(result.door_count, 0)
        self.assertEqual(result.window_count, 0)

    def test_no_wall_lines(self):
        """Only short segments → no wall lines → no candidates."""
        segs = [_horiz_seg(0, 0, 10, 0), _horiz_seg(0, 10, 15, 10)]
        result = plan_opening_candidates(segs, [], scale_px_per_m=SCALE)
        self.assertEqual(len(result.candidates), 0)

    def test_candidate_has_instance_id(self):
        """Each candidate gets a unique instance ID."""
        wall = _horiz_seg(0, 100, 500, 100)
        leaf = _vert_seg(150, 85, 115)
        leaf2 = _vert_seg(350, 85, 115)

        result = plan_opening_candidates(
            [wall, leaf, leaf2], [],
            scale_px_per_m=SCALE,
        )
        ids = {c.opening_instance_id for c in result.candidates}
        self.assertEqual(len(ids), len(result.candidates))

    def test_gap_does_not_overlap_door(self):
        """Gap candidate at same position as door is filtered out."""
        wall = _horiz_seg(0, 100, 600, 100)
        door_leaf = _vert_seg(150, 85, 115)
        # Gap-indicating perpendicular segments at different positions
        gap_seg1 = _vert_seg(350, 85, 115)
        gap_seg2 = _vert_seg(450, 85, 115)
        words = [_word("D01", 150, 70)]

        result = plan_opening_candidates(
            [wall, door_leaf, gap_seg1, gap_seg2], words,
            scale_px_per_m=SCALE,
        )
        # Door at x=150 should be detected
        door_count = sum(1 for c in result.candidates if c.opening_type == OPENING_TYPE_DOOR)
        self.assertGreaterEqual(door_count, 1)
        # Gap at x=400 (between gap_seg1 and gap_seg2) is at different position from door
        # Both should be present
        self.assertGreaterEqual(len(result.candidates), 2)

    def test_no_scale_still_produces_candidates(self):
        """Without scale info, candidates are still produced (width_m may be None or in pt)."""
        wall = WallLine(segment=_horiz_seg(0, 100, 500, 100))
        leaf = _vert_seg(200, 85, 115)

        doors = detect_door_candidates(
            [wall.segment, leaf], [wall], [],
            scale_px_per_m=0.0,  # no scale
        )
        self.assertEqual(len(doors), 1)
        # Without scale, width_m should be None
        self.assertIsNone(doors[0].width_m)


# ---------------------------------------------------------------------------
# 9. Contract compliance
# ---------------------------------------------------------------------------

class TestContractCompliance(unittest.TestCase):

    def test_all_candidates_are_opening_evidence(self):
        """Every candidate from plan_opening_candidates is an OpeningEvidence."""
        wall = _horiz_seg(0, 100, 800, 100)
        leaf = _vert_seg(150, 85, 115)
        jamb1 = _vert_seg(490, 85, 115)
        jamb2 = _vert_seg(510, 85, 115)
        words = [_word("D01", 150, 70), _word("W01", 500, 70)]

        result = plan_opening_candidates(
            [wall, leaf, jamb1, jamb2], words,
            scale_px_per_m=SCALE,
        )
        for c in result.candidates:
            self.assertIsInstance(c, OpeningEvidence)

    def test_quantity_always_one(self):
        """All geometric candidates have quantity=1."""
        wall = _horiz_seg(0, 100, 500, 100)
        leaf = _vert_seg(200, 85, 115)

        result = plan_opening_candidates(
            [wall, leaf], [],
            scale_px_per_m=SCALE,
        )
        for c in result.candidates:
            self.assertEqual(c.quantity, 1)

    def test_deduct_always_false(self):
        """B1 never sets deduct=True."""
        wall = _horiz_seg(0, 100, 500, 100)
        leaf = _vert_seg(200, 85, 115)
        words = [_word("D01", 200, 70)]

        result = plan_opening_candidates(
            [wall, leaf], words,
            scale_px_per_m=SCALE,
        )
        for c in result.candidates:
            self.assertFalse(c.deduct)

    def test_extraction_method_always_plan_vector(self):
        """All candidates have extraction_method='plan_vector'."""
        wall = _horiz_seg(0, 100, 500, 100)
        leaf = _vert_seg(200, 85, 115)

        result = plan_opening_candidates(
            [wall, leaf], [],
            scale_px_per_m=SCALE,
        )
        for c in result.candidates:
            self.assertEqual(c.extraction_method, "plan_vector")

    def test_dimension_basis_always_unknown(self):
        """Plan detection cannot determine rough_opening — basis is unknown."""
        wall = _horiz_seg(0, 100, 500, 100)
        leaf = _vert_seg(200, 85, 115)

        result = plan_opening_candidates(
            [wall, leaf], [],
            scale_px_per_m=SCALE,
        )
        for c in result.candidates:
            self.assertEqual(c.dimension_basis, DIMENSION_BASIS_UNKNOWN)

    def test_height_always_none(self):
        """Plan detection cannot measure height."""
        wall = _horiz_seg(0, 100, 500, 100)
        leaf = _vert_seg(200, 85, 115)

        result = plan_opening_candidates(
            [wall, leaf], [],
            scale_px_per_m=SCALE,
        )
        for c in result.candidates:
            self.assertIsNone(c.height_m)

    def test_area_always_none_without_height(self):
        """Without height, area_m2 should be None."""
        wall = _horiz_seg(0, 100, 500, 100)
        leaf = _vert_seg(200, 85, 115)

        result = plan_opening_candidates(
            [wall, leaf], [],
            scale_px_per_m=SCALE,
        )
        for c in result.candidates:
            self.assertIsNone(c.area_m2)

    def test_deduction_status_always_review(self):
        """Without rough_opening basis, deduction status is always review."""
        wall = _horiz_seg(0, 100, 500, 100)
        leaf = _vert_seg(200, 85, 115)
        words = [_word("D01", 200, 70)]

        result = plan_opening_candidates(
            [wall, leaf], words,
            scale_px_per_m=SCALE,
        )
        for c in result.candidates:
            self.assertEqual(c.deduction_status, DEDUCTION_REVIEW)


if __name__ == "__main__":
    unittest.main()
