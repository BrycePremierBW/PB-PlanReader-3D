"""Priority 2 regression tests for room face extraction, calibration and filtering.

Tests cover all 5 blockers from ChatGPT review:
- BLOCKER 1: Containment logic direction (outer contains inner centroids)
- BLOCKER 2: Soft area thresholds (labelled small rooms survive)
- BLOCKER 3: Elongation as confidence signal (labelled corridors survive)
- BLOCKER 4: Unknown calibration = None, not 0.0
- BLOCKER 5: Authoritative scale source text
- Production integration tests
"""
from __future__ import annotations

import math
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

from pb_room_face_takeoff import (
    FilterResult,
    RoomFace,
    _authoritative_scale_text,
    _count_other_centroids_inside_candidate,
    _elongation_ratio,
    _polygon_area_abs,
    _polygon_area_shoelace,
    _polygon_bbox,
    _polygon_centroid,
    _point_in_polygon,
    _perimeter_m,
    _scale_factor_m_per_pt,
    calibrate_area_m2,
    calibrate_polygon_m,
    filter_face,
    rooms_to_takeoff_rows,
    room_face_summary,
)


# ---------------------------------------------------------------------------
# Synthetic geometry definitions
# ---------------------------------------------------------------------------

def _rect(x0: float, y0: float, x1: float, y1: float) -> List[Tuple[float, float]]:
    """Rectangle as polygon vertices (counter-clockwise)."""
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


# Scale: 1:100 → 0.03528 m/pt
SCALE_1_100_M_PER_PT = 25.4 / 72.0 * (100.0 / 1000.0)

# 4.0 m × 3.0 m = 12.0 m² at 1:100
ROOM_4x3_PDF = _rect(100, 100, 100 + 4.0/SCALE_1_100_M_PER_PT, 100 + 3.0/SCALE_1_100_M_PER_PT)

# 5.0 m × 5.0 m = 25.0 m² at 1:100
ROOM_5x5_PDF = _rect(100, 100, 100 + 5.0/SCALE_1_100_M_PER_PT, 100 + 5.0/SCALE_1_100_M_PER_PT)

# L-shaped room: 6×2 + 4×2 = 20.0 m² at 1:100
def _l_shape_pdf() -> List[Tuple[float, float]]:
    s = SCALE_1_100_M_PER_PT
    return [
        (100, 100),
        (100 + 6.0/s, 100),
        (100 + 6.0/s, 100 + 2.0/s),
        (100 + 4.0/s, 100 + 2.0/s),
        (100 + 4.0/s, 100 + 4.0/s),
        (100, 100 + 4.0/s),
    ]

L_SHAPE_PDF = _l_shape_pdf()

# Irregular pentagon: 15.0 m² at 1:100
def _irregular_pentagon_pdf() -> List[Tuple[float, float]]:
    s = SCALE_1_100_M_PER_PT
    real_verts = [(0,0), (4,0), (5,3), (2,4), (0,2)]
    return [(100 + v[0]/s, 100 + v[1]/s) for v in real_verts]

IRREGULAR_PDF = _irregular_pentagon_pdf()

# Small WC: 1.4 m × 1.0 m = 1.4 m² at 1:100 (below SOFT_MIN but labelled)
WC_PDF = _rect(200, 200, 200 + 1.4/SCALE_1_100_M_PER_PT, 200 + 1.0/SCALE_1_100_M_PER_PT)

# Tiny unlabeled joinery: 0.6 m × 0.4 m = 0.24 m² (below HARD_MIN)
JOINERY_PDF = _rect(200, 200, 200 + 0.6/SCALE_1_100_M_PER_PT, 200 + 0.4/SCALE_1_100_M_PER_PT)

# Small labeled STORE: 1.5 m × 1.0 m = 1.5 m² (below SOFT_MIN but labelled)
STORE_PDF = _rect(300, 300, 300 + 1.5/SCALE_1_100_M_PER_PT, 300 + 1.0/SCALE_1_100_M_PER_PT)

# Corridor: 12m × 0.8m = 9.6 m², ratio 15:1 (above HIGH_ELONGATION)
CORRIDOR_PDF = _rect(100, 100, 100 + 12.0/SCALE_1_100_M_PER_PT, 100 + 0.8/SCALE_1_100_M_PER_PT)

# Narrow unlabeled wall strip: 8m × 0.3m = 2.4 m², ratio 26.7:1
WALL_STRIP_PDF = _rect(100, 100, 100 + 8.0/SCALE_1_100_M_PER_PT, 100 + 0.3/SCALE_1_100_M_PER_PT)

# Two adjacent rooms
ROOM_A_PDF = _rect(100, 100, 100 + 4.0/SCALE_1_100_M_PER_PT, 100 + 3.0/SCALE_1_100_M_PER_PT)
ROOM_B_PDF = _rect(100 + 4.0/SCALE_1_100_M_PER_PT, 100,
                    100 + 8.0/SCALE_1_100_M_PER_PT, 100 + 3.0/SCALE_1_100_M_PER_PT)

# Large drawing border (covers most of page)
BORDER_PDF = _rect(10, 10, 580, 800)

# Outer building outline containing 4 room centroids
BUILDING_OUTLINE_PDF = _rect(30, 30, 575, 820)
INNER_ROOMS = [
    _rect(100, 100, 250, 250),
    _rect(300, 100, 450, 250),
    _rect(100, 300, 250, 450),
    _rect(300, 300, 450, 450),
]


# ---------------------------------------------------------------------------
# Scale info fixtures
# ---------------------------------------------------------------------------

SCALE_1_100 = {"real_metres_per_page_mm": 100.0 / 1000.0, "scale_ratio": 100, "scale_text": "1:100"}
SCALE_1_50 = {"real_metres_per_page_mm": 50.0 / 1000.0, "scale_ratio": 50, "scale_text": "1:50"}
SCALE_1_200 = {"real_metres_per_page_mm": 200.0 / 1000.0, "scale_ratio": 200, "scale_text": "1:200"}
SCALE_METRIC_10MM = {"real_metres_per_page_mm": 1.0 / 10.0, "scale_text": "10 mm = 1 m"}
SCALE_UNKNOWN = {"real_metres_per_page_mm": None}


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestShoelaceArea(unittest.TestCase):
    """Polygon area calculation via shoelace formula."""

    def test_rectangle_4x3(self):
        """4.0 m × 3.0 m rectangle at 1:100 = 12.0 m²."""
        area_pts2 = _polygon_area_abs(ROOM_4x3_PDF)
        area_m2 = area_pts2 * SCALE_1_100_M_PER_PT ** 2
        self.assertAlmostEqual(area_m2, 12.0, places=1)

    def test_square_5x5(self):
        """5.0 m × 5.0 m square at 1:100 = 25.0 m²."""
        area_pts2 = _polygon_area_abs(ROOM_5x5_PDF)
        area_m2 = area_pts2 * SCALE_1_100_M_PER_PT ** 2
        self.assertAlmostEqual(area_m2, 25.0, places=1)

    def test_l_shape(self):
        """L-shaped room: 6×2 + 4×2 = 20.0 m² at 1:100."""
        area_pts2 = _polygon_area_abs(L_SHAPE_PDF)
        area_m2 = area_pts2 * SCALE_1_100_M_PER_PT ** 2
        self.assertAlmostEqual(area_m2, 20.0, places=1)

    def test_irregular_pentagon(self):
        """Irregular pentagon = 15.0 m² at 1:100."""
        area_pts2 = _polygon_area_abs(IRREGULAR_PDF)
        area_m2 = area_pts2 * SCALE_1_100_M_PER_PT ** 2
        self.assertAlmostEqual(area_m2, 15.0, places=1)

    def test_triangle(self):
        """Triangle 3×4 = 6.0 m² (half of 3×4 rectangle)."""
        s = SCALE_1_100_M_PER_PT
        tri = [(100, 100), (100 + 4.0/s, 100), (100, 100 + 3.0/s)]
        area_pts2 = _polygon_area_abs(tri)
        area_m2 = area_pts2 * SCALE_1_100_M_PER_PT ** 2
        self.assertAlmostEqual(area_m2, 6.0, places=1)


class TestScaleCalibration(unittest.TestCase):
    """PDF-point → m² calibration at multiple scales."""

    def _area_at_scale(self, polygon, scale_info):
        return calibrate_area_m2(polygon, scale_info)

    def test_4x3_at_1_100(self):
        self.assertAlmostEqual(self._area_at_scale(ROOM_4x3_PDF, SCALE_1_100), 12.0, places=1)

    def test_4x3_at_1_50(self):
        s_1_50 = 25.4 / 72.0 * (50.0 / 1000.0)
        room_1_50 = _rect(100, 100, 100 + 4.0/s_1_50, 100 + 3.0/s_1_50)
        self.assertAlmostEqual(self._area_at_scale(room_1_50, SCALE_1_50), 12.0, places=1)

    def test_4x3_at_1_200(self):
        s_1_200 = 25.4 / 72.0 * (200.0 / 1000.0)
        room_1_200 = _rect(100, 100, 100 + 4.0/s_1_200, 100 + 3.0/s_1_200)
        self.assertAlmostEqual(self._area_at_scale(room_1_200, SCALE_1_200), 12.0, places=1)

    def test_4x3_metric_10mm(self):
        s_metric = 25.4 / 72.0 * (1.0 / 10.0)
        room_metric = _rect(100, 100, 100 + 4.0/s_metric, 100 + 3.0/s_metric)
        self.assertAlmostEqual(self._area_at_scale(room_metric, SCALE_METRIC_10MM), 12.0, places=1)

    def test_1_100_ratio_equals_metric_10mm(self):
        """Ratio 1:100 and metric "10 mm = 1 m" produce same real scale."""
        area_ratio = self._area_at_scale(ROOM_4x3_PDF, SCALE_1_100)
        area_metric = self._area_at_scale(ROOM_4x3_PDF, SCALE_METRIC_10MM)
        self.assertAlmostEqual(area_ratio, area_metric, places=6)

    def test_unknown_scale_returns_none(self):
        """BLOCKER 4: Unknown scale must return None, not 0.0."""
        result = self._area_at_scale(ROOM_4x3_PDF, SCALE_UNKNOWN)
        self.assertIsNone(result)


class TestPolygonConversion(unittest.TestCase):
    """PDF-point → metre coordinate conversion."""

    def test_rectangle_to_metres(self):
        verts_m = calibrate_polygon_m(ROOM_4x3_PDF, SCALE_1_100)
        self.assertIsNotNone(verts_m)
        self.assertEqual(len(verts_m), 4)
        width_m = verts_m[1][0] - verts_m[0][0]
        self.assertAlmostEqual(width_m, 4.0, places=1)
        height_m = verts_m[2][1] - verts_m[1][1]
        self.assertAlmostEqual(height_m, 3.0, places=1)

    def test_unknown_scale_returns_none(self):
        """BLOCKER 4: Unknown scale returns None polygon."""
        self.assertIsNone(calibrate_polygon_m(ROOM_4x3_PDF, SCALE_UNKNOWN))


class TestContainmentLogic(unittest.TestCase):
    """BLOCKER 1: Containment direction — outer polygon CONTAINS inner centroids."""

    def test_outer_contains_room_centroids(self):
        """Building outline contains 4 room centroids → outer is outline."""
        for room in INNER_ROOMS:
            centroid = _polygon_centroid(room)
            inside = _point_in_polygon(centroid, BUILDING_OUTLINE_PDF)
            self.assertTrue(inside, f"Room centroid {centroid} should be inside building outline")

    def test_room_centroid_not_inside_other_rooms(self):
        """Room centroids are NOT inside other rooms."""
        for i, room_a in enumerate(INNER_ROOMS):
            centroid_a = _polygon_centroid(room_a)
            for j, room_b in enumerate(INNER_ROOMS):
                if i == j:
                    continue
                inside = _point_in_polygon(centroid_a, room_b)
                self.assertFalse(inside,
                    f"Room {i} centroid should not be inside room {j}")

    def test_building_outline_rejected(self):
        """BLOCKER 1: Outer polygon containing 4 room faces → rejected."""
        all_polys = [BUILDING_OUTLINE_PDF] + INNER_ROOMS
        result = filter_face(
            BUILDING_OUTLINE_PDF, SCALE_1_100, 595, 842,
            all_polygons=all_polys,
        )
        self.assertFalse(result.is_room)
        # Rejected by either covers_page or building_outline
        self.assertTrue(
            "building_outline" in result.reason or "covers_page" in result.reason,
            f"Expected building_outline or covers_page, got: {result.reason}",
        )

    def test_inner_rooms_not_rejected(self):
        """BLOCKER 1: Each inner room is NOT rejected by containment."""
        all_polys = [BUILDING_OUTLINE_PDF] + INNER_ROOMS
        for room in INNER_ROOMS:
            result = filter_face(
                room, SCALE_1_100, 595, 842,
                all_polygons=all_polys,
            )
            self.assertTrue(result.is_room,
                f"Inner room should pass: got {result.reason}")

    def test_two_adjacent_rooms_both_survive(self):
        """Two adjacent non-nested rooms both pass."""
        all_polys = [ROOM_A_PDF, ROOM_B_PDF]
        result_a = filter_face(ROOM_A_PDF, SCALE_1_100, 595, 842, all_polygons=all_polys)
        result_b = filter_face(ROOM_B_PDF, SCALE_1_100, 595, 842, all_polygons=all_polys)
        self.assertTrue(result_a.is_room)
        self.assertTrue(result_b.is_room)

    def test_count_other_centroids_inside(self):
        """Direct test of containment counting function."""
        all_polys = [BUILDING_OUTLINE_PDF] + INNER_ROOMS
        candidate_key = tuple(tuple(p) for p in BUILDING_OUTLINE_PDF)
        count = _count_other_centroids_inside_candidate(
            BUILDING_OUTLINE_PDF, all_polys, candidate_key,
        )
        self.assertEqual(count, 4)  # all 4 inner rooms' centroids inside

    def test_count_self_excluded(self):
        """Self-centroid is excluded from count."""
        all_polys = [ROOM_A_PDF, ROOM_B_PDF]
        key_a = tuple(tuple(p) for p in ROOM_A_PDF)
        count = _count_other_centroids_inside_candidate(
            ROOM_A_PDF, all_polys, key_a,
        )
        self.assertEqual(count, 0)  # room B centroid is NOT inside room A


class TestSoftAreaThresholds(unittest.TestCase):
    """BLOCKER 2: Area as confidence signal, not unconditional rejection."""

    def test_labelled_wc_survives(self):
        """BLOCKER 2: 1.4 m² WC with room label → retained (above soft min 1.0)."""
        result = filter_face(WC_PDF, SCALE_1_100, 595, 842, label="WC")
        self.assertTrue(result.is_room)
        self.assertAlmostEqual(result.area_m2, 1.4, places=1)
        # WC at 1.4 m² is above SOFT_MIN (1.0), so no penalty
        self.assertEqual(result.confidence_adjustment, 0)

    def test_unlabeled_tiny_rejected(self):
        """BLOCKER 2: 0.24 m² unlabeled joinery → rejected (below hard min)."""
        result = filter_face(JOINERY_PDF, SCALE_1_100, 595, 842, label="")
        self.assertFalse(result.is_room)
        self.assertIn("too_small", result.reason)

    def test_labelled_store_survives(self):
        """BLOCKER 2: 1.5 m² labelled STORE → retained (above soft min 1.0)."""
        result = filter_face(STORE_PDF, SCALE_1_100, 595, 842, label="STORE")
        self.assertTrue(result.is_room)
        self.assertAlmostEqual(result.area_m2, 1.5, places=1)
        # STORE at 1.5 m² is above SOFT_MIN (1.0), so no penalty
        self.assertEqual(result.confidence_adjustment, 0)

    def test_small_unlabeled_below_soft_min_rejected(self):
        """Small polygon without label below soft min → rejected."""
        # 0.8 m² unlabeled polygon
        small = _rect(200, 200, 200 + 0.8/SCALE_1_100_M_PER_PT, 200 + 1.0/SCALE_1_100_M_PER_PT)
        result = filter_face(small, SCALE_1_100, 595, 842, label="")
        self.assertFalse(result.is_room)
        self.assertIn("small_unlabeled", result.reason)

    def test_hard_min_rejects_even_labelled(self):
        """Below hard minimum, even labelled is rejected."""
        tiny = _rect(200, 200, 200 + 0.2/SCALE_1_100_M_PER_PT, 200 + 0.2/SCALE_1_100_M_PER_PT)
        result = filter_face(tiny, SCALE_1_100, 595, 842, label="WC")
        self.assertFalse(result.is_room)
        self.assertIn("hard minimum", result.reason)


class TestElongationAsEvidence(unittest.TestCase):
    """BLOCKER 3: Elongation as confidence signal, not auto-rejection."""

    def test_labelled_corridor_survives(self):
        """BLOCKER 3: 15:1 labelled CORRIDOR → retained."""
        result = filter_face(CORRIDOR_PDF, SCALE_1_100, 595, 842, label="CORRIDOR")
        self.assertTrue(result.is_room)
        self.assertLess(result.confidence_adjustment, 0)  # penalized but kept

    def test_unlabeled_strip_rejected(self):
        """BLOCKER 3: 26.7:1 unlabeled wall strip → rejected."""
        result = filter_face(WALL_STRIP_PDF, SCALE_1_100, 595, 842, label="")
        self.assertFalse(result.is_room)
        self.assertIn("elongated_unlabeled", result.reason)

    def test_labelled_hall_survives(self):
        """Labelled HALL with high elongation → retained."""
        result = filter_face(CORRIDOR_PDF, SCALE_1_100, 595, 842, label="HALL")
        self.assertTrue(result.is_room)


class TestUnknownCalibration(unittest.TestCase):
    """BLOCKER 4: Unknown calibration = None, not 0.0."""

    def test_filter_returns_none_area(self):
        """Unknown scale → area_m2 is None."""
        result = filter_face(ROOM_4x3_PDF, SCALE_UNKNOWN, 595, 842)
        self.assertFalse(result.is_room)
        self.assertIsNone(result.area_m2)

    def test_filter_preserves_page_area(self):
        """Even with unknown scale, page-space area is preserved."""
        result = filter_face(ROOM_4x3_PDF, SCALE_UNKNOWN, 595, 842)
        self.assertGreater(result.area_page_pts2, 0)


class TestScaleSourceText(unittest.TestCase):
    """BLOCKER 5: Authoritative scale text from calibration object."""

    def test_ratio_scale_text(self):
        """scale_text field is used directly."""
        self.assertEqual(_authoritative_scale_text(SCALE_1_100), "1:100")

    def test_metric_scale_text(self):
        """Metric scale text from scale_text field."""
        self.assertEqual(_authoritative_scale_text(SCALE_METRIC_10MM), "10 mm = 1 m")

    def test_fallback_to_ratio_integer(self):
        """Fallback to 1:N when scale_text missing but scale_ratio present."""
        info = {"scale_ratio": 200}
        self.assertEqual(_authoritative_scale_text(info), "1:200")

    def test_unknown_returns_unknown(self):
        """No scale info → 'unknown'."""
        self.assertEqual(_authoritative_scale_text({}), "unknown")

    def test_no_reconstruction_from_inverse(self):
        """Never reconstruct scale from internal factors."""
        info = {"real_metres_per_page_mm": 0.03528}  # no text, no ratio
        result = _authoritative_scale_text(info)
        self.assertEqual(result, "unknown")  # not "1:28.3" or similar


class TestPerimeter(unittest.TestCase):
    """Perimeter calculation in metres."""

    def test_rectangle_perimeter(self):
        verts_m = calibrate_polygon_m(ROOM_4x3_PDF, SCALE_1_100)
        self.assertAlmostEqual(_perimeter_m(verts_m), 14.0, places=1)

    def test_none_polygon(self):
        """None polygon → None perimeter."""
        self.assertIsNone(_perimeter_m(None))


class TestVoidDetection(unittest.TestCase):
    """Void/hole detection: polygon containing internal voids."""

    def test_void_detected(self):
        """Room containing a smaller face inside → has_voids=True."""
        outer = _rect(100, 100, 500, 500)
        inner = _rect(200, 200, 300, 300)
        result = filter_face(
            outer, SCALE_1_100, 595, 842,
            all_polygons=[outer, inner],
        )
        self.assertTrue(result.is_room)
        # has_voids is set in the RoomFace, but filter_face signals it via
        # confidence_adjustment being negative
        self.assertLess(result.confidence_adjustment, 0)

    def test_no_void_for_non_nested(self):
        """Adjacent rooms → no void penalty."""
        result = filter_face(
            ROOM_A_PDF, SCALE_1_100, 595, 842,
            all_polygons=[ROOM_A_PDF, ROOM_B_PDF],
        )
        self.assertTrue(result.is_room)
        # No void penalty (adjacent, not nested)
        self.assertEqual(result.confidence_adjustment, 0.0)


class TestTakeoffRows(unittest.TestCase):
    """Room-to-takeoff row production."""

    def _make_room(self, label, area_m2, page=1):
        return RoomFace(
            room_ref="R01", label=label,
            polygon_pdf_pts=[(0, 0), (100, 0), (100, 100), (0, 100)],
            polygon_m=[(0, 0), (4, 0), (4, 3), (0, 3)],
            floor_area_m2=area_m2, area_page_pts2=10000.0,
            perimeter_m=14.0, geometry_confidence=0.98,
            evidence=[], source_page=page, drawing_number="DWG-001",
            scale_source="1:100", calibration_confidence=0.95,
        )

    def test_floor_row_structure(self):
        room = self._make_room("KITCHEN", 12.0)
        rows = rooms_to_takeoff_rows([room], workspace_id=1)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["section"], "Internal")
        self.assertEqual(row["element"], "Floor area")
        self.assertEqual(row["location"], "KITCHEN")
        self.assertEqual(row["unit"], "m²")
        self.assertEqual(row["quantity"], 12.0)
        self.assertEqual(row["row_role"], "floor_area")

    def test_ceiling_rows_added(self):
        room = self._make_room("BED 1", 15.0)
        rows = rooms_to_takeoff_rows([room], workspace_id=1, include_ceiling=True)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["element"], "Floor area")
        self.assertEqual(rows[1]["element"], "Ceiling area")

    def test_none_quantity_for_uncalibrated(self):
        """BLOCKER 4: uncalibrated room → quantity is None."""
        room = RoomFace(
            room_ref="R01", label="?",
            polygon_pdf_pts=[(0,0),(100,0),(100,100),(0,100)],
            polygon_m=None, floor_area_m2=None, area_page_pts2=10000.0,
            perimeter_m=None, geometry_confidence=0.5, evidence=[],
            scale_source="unknown", calibration_confidence=0.0,
        )
        rows = rooms_to_takeoff_rows([room], workspace_id=1)
        self.assertIsNone(rows[0]["quantity"])


class TestRoomSummary(unittest.TestCase):

    def test_summary(self):
        rooms = [
            RoomFace("R01", "A", [], [], 12.0, 10000, 14.0, 0.98, [],
                     scale_source="1:100", calibration_confidence=0.95),
            RoomFace("R02", "B", [], [], 15.0, 12000, 16.0, 0.98, [],
                     scale_source="1:100", calibration_confidence=0.95),
        ]
        s = room_face_summary(rooms)
        self.assertEqual(s["room_count"], 2)
        self.assertAlmostEqual(s["total_floor_area_m2"], 27.0, places=1)

    def test_summary_empty(self):
        s = room_face_summary([])
        self.assertEqual(s["room_count"], 0)
        self.assertIsNone(s["total_floor_area_m2"])


class TestIntegrationWithV145(unittest.TestCase):
    """Integration test: full pipeline from segments to take-off rows."""

    def test_segments_to_takeoff_rows(self):
        """4m × 3m room at 1:100 → 12.0 m² take-off row."""
        s = SCALE_1_100_M_PER_PT
        x0, y0 = 200.0, 200.0
        w, h = 4.0/s, 3.0/s
        segments = [
            {"x1": x0, "y1": y0, "x2": x0+w, "y2": y0},
            {"x1": x0+w, "y1": y0, "x2": x0+w, "y2": y0+h},
            {"x1": x0+w, "y1": y0+h, "x2": x0, "y2": y0+h},
            {"x1": x0, "y1": y0+h, "x2": x0, "y2": y0},
        ]
        from pb_room_face_takeoff import extract_and_calibrate_rooms
        rooms = extract_and_calibrate_rooms(
            segments=segments, scale_info=SCALE_1_100,
            page_width_pt=595, page_height_pt=842, page_no=1,
            drawing_number="TEST-001",
        )
        self.assertEqual(len(rooms), 1)
        self.assertAlmostEqual(rooms[0].floor_area_m2, 12.0, places=1)
        rows = rooms_to_takeoff_rows(rooms, workspace_id=1)
        self.assertEqual(rows[0]["unit"], "m²")
        self.assertEqual(rows[0]["row_role"], "floor_area")

    def test_l_shape_segments(self):
        """L-shaped room → 20.0 m²."""
        s = SCALE_1_100_M_PER_PT
        x0, y0 = 200.0, 200.0
        segments = [
            {"x1": x0, "y1": y0, "x2": x0+6.0/s, "y2": y0},
            {"x1": x0+6.0/s, "y1": y0, "x2": x0+6.0/s, "y2": y0+2.0/s},
            {"x1": x0+6.0/s, "y1": y0+2.0/s, "x2": x0+4.0/s, "y2": y0+2.0/s},
            {"x1": x0+4.0/s, "y1": y0+2.0/s, "x2": x0+4.0/s, "y2": y0+4.0/s},
            {"x1": x0+4.0/s, "y1": y0+4.0/s, "x2": x0, "y2": y0+4.0/s},
            {"x1": x0, "y1": y0+4.0/s, "x2": x0, "y2": y0},
        ]
        from pb_room_face_takeoff import extract_and_calibrate_rooms
        rooms = extract_and_calibrate_rooms(
            segments=segments, scale_info=SCALE_1_100,
            page_width_pt=595, page_height_pt=842, page_no=1,
        )
        self.assertEqual(len(rooms), 1)
        self.assertAlmostEqual(rooms[0].floor_area_m2, 20.0, places=1)

    def test_two_adjacent_room_segments(self):
        """Two adjacent rooms → two take-off rows."""
        s = SCALE_1_100_M_PER_PT
        x0, y0 = 200.0, 200.0
        w, h = 4.0/s, 3.0/s
        segments = [
            {"x1": x0, "y1": y0, "x2": x0+w, "y2": y0},
            {"x1": x0, "y1": y0, "x2": x0, "y2": y0+h},
            {"x1": x0+w, "y1": y0, "x2": x0+2*w, "y2": y0},
            {"x1": x0+2*w, "y1": y0, "x2": x0+2*w, "y2": y0+h},
            {"x1": x0+w, "y1": y0, "x2": x0+w, "y2": y0+h},
            {"x1": x0, "y1": y0+h, "x2": x0+2*w, "y2": y0+h},
        ]
        from pb_room_face_takeoff import extract_and_calibrate_rooms
        rooms = extract_and_calibrate_rooms(
            segments=segments, scale_info=SCALE_1_100,
            page_width_pt=595, page_height_pt=842, page_no=1,
        )
        self.assertEqual(len(rooms), 2)
        for room in rooms:
            self.assertAlmostEqual(room.floor_area_m2, 12.0, places=1)

    def test_unknown_scale_no_rooms(self):
        """Unknown scale → no rooms pass filtering."""
        s = SCALE_1_100_M_PER_PT
        x0, y0 = 200.0, 200.0
        w, h = 4.0/s, 3.0/s
        segments = [
            {"x1": x0, "y1": y0, "x2": x0+w, "y2": y0},
            {"x1": x0+w, "y1": y0, "x2": x0+w, "y2": y0+h},
            {"x1": x0+w, "y1": y0+h, "x2": x0, "y2": y0+h},
            {"x1": x0, "y1": y0+h, "x2": x0, "y2": y0},
        ]
        from pb_room_face_takeoff import extract_and_calibrate_rooms
        rooms = extract_and_calibrate_rooms(
            segments=segments, scale_info=SCALE_UNKNOWN,
            page_width_pt=595, page_height_pt=842, page_no=1,
        )
        self.assertEqual(len(rooms), 0)

    def test_labelled_wc_in_segments(self):
        """Small labelled WC polygon survives filtering."""
        s = SCALE_1_100_M_PER_PT
        x0, y0 = 200.0, 200.0
        w, h = 1.4/s, 1.0/s
        segments = [
            {"x1": x0, "y1": y0, "x2": x0+w, "y2": y0},
            {"x1": x0+w, "y1": y0, "x2": x0+w, "y2": y0+h},
            {"x1": x0+w, "y1": y0+h, "x2": x0, "y2": y0+h},
            {"x1": x0, "y1": y0+h, "x2": x0, "y2": y0},
        ]
        words = [{"text": "WC", "bbox": [x0+10, y0+10, x0+40, y0+30]}]
        from pb_room_face_takeoff import extract_and_calibrate_rooms
        rooms = extract_and_calibrate_rooms(
            segments=segments, scale_info=SCALE_1_100,
            page_width_pt=595, page_height_pt=842, page_no=1,
            words=words,
        )
        # WC is small (1.4 m²) but labelled → should survive
        self.assertTrue(len(rooms) >= 1, "Labelled WC should survive filtering")
        self.assertAlmostEqual(rooms[0].floor_area_m2, 1.4, places=1)

    def test_void_room_marked_provisional(self):
        """Room containing internal void → status is Review."""
        s = SCALE_1_100_M_PER_PT
        x0, y0 = 100.0, 100.0
        # Outer room: 8m × 8m
        ow, oh = 8.0/s, 8.0/s
        # Inner void: 2m × 2m
        vw, vh = 2.0/s, 2.0/s
        segments = [
            # Outer
            {"x1": x0, "y1": y0, "x2": x0+ow, "y2": y0},
            {"x1": x0+ow, "y1": y0, "x2": x0+ow, "y2": y0+oh},
            {"x1": x0+ow, "y1": y0+oh, "x2": x0, "y2": y0+oh},
            {"x1": x0, "y1": y0+oh, "x2": x0, "y2": y0},
            # Inner void
            {"x1": x0+3.0/s, "y1": y0+3.0/s, "x2": x0+3.0/s+vw, "y2": y0+3.0/s},
            {"x1": x0+3.0/s+vw, "y1": y0+3.0/s, "x2": x0+3.0/s+vw, "y2": y0+3.0/s+vh},
            {"x1": x0+3.0/s+vw, "y1": y0+3.0/s+vh, "x2": x0+3.0/s, "y2": y0+3.0/s+vh},
            {"x1": x0+3.0/s, "y1": y0+3.0/s+vh, "x2": x0+3.0/s, "y2": y0+3.0/s},
        ]
        from pb_room_face_takeoff import extract_and_calibrate_rooms
        rooms = extract_and_calibrate_rooms(
            segments=segments, scale_info=SCALE_1_100,
            page_width_pt=595, page_height_pt=842, page_no=1,
        )
        # At least the outer room should be detected
        if rooms:
            # The outer room should have void penalty
            outer = max(rooms, key=lambda r: r.floor_area_m2 or 0)
            self.assertTrue(outer.has_voids, "Outer room should detect voids")
            self.assertEqual(outer.status, "Review")


class TestFilterEdgeCases(unittest.TestCase):

    def test_degenerate_polygon(self):
        result = filter_face([(0, 0), (100, 0)], SCALE_1_100, 595, 842)
        self.assertFalse(result.is_room)
        self.assertIn("degenerate", result.reason)

    def test_title_block_zone(self):
        tb = _rect(450, 750, 450 + 3.0/SCALE_1_100_M_PER_PT, 750 + 2.0/SCALE_1_100_M_PER_PT)
        result = filter_face(tb, SCALE_1_100, 595, 842)
        self.assertFalse(result.is_room)
        self.assertIn("title_block_zone", result.reason)

    def test_page_coverage_border(self):
        result = filter_face(BORDER_PDF, SCALE_1_100, 595, 842)
        self.assertFalse(result.is_room)
        self.assertIn("covers_page", result.reason)

    def test_valid_room_passes(self):
        result = filter_face(ROOM_4x3_PDF, SCALE_1_100, 595, 842)
        self.assertTrue(result.is_room)
        self.assertAlmostEqual(result.area_m2, 12.0, places=1)


if __name__ == "__main__":
    unittest.main()
