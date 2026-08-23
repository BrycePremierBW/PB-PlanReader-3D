"""Priority 2 regression tests for room face extraction, calibration and filtering.

Tests cover:
- Polygon area calculation (shoelace)
- Scale calibration (PDF pt → m²)
- False-positive filtering (furniture, borders, title blocks, corridors)
- Room face extraction pipeline
- Take-off row production
- Integration with real production path
"""
from __future__ import annotations

import math
import unittest
from typing import Any, Dict, List, Tuple

from pb_room_face_takeoff import (
    FilterResult,
    RoomFace,
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


# Real-world dimensions: 4.0 m × 3.0 m = 12.0 m²
# At 1:100:  4000 PDF pts × 3000 PDF pts  (1 pt = 0.3528 mm; 4000 pt = 1411 mm = 4000×0.3528)
# At 1:100:  page mm = pt × (25.4/72); real_m = page_mm × (100/1000)
# So 1 PDF pt → 0.3528 mm page → 0.03528 m real
# 4.0 m → 4.0 / 0.03528 = 113.42 PDF pts
# 3.0 m → 3.0 / 0.03528 = 85.06 PDF pts
SCALE_1_100_M_PER_PT = 25.4 / 72.0 * (100.0 / 1000.0)  # 0.03528 m/pt
ROOM_4x3_PDF = _rect(100, 100, 100 + 4.0 / SCALE_1_100_M_PER_PT, 100 + 3.0 / SCALE_1_100_M_PER_PT)

# 5.0 m × 5.0 m = 25.0 m² (square)
ROOM_5x5_PDF = _rect(100, 100, 100 + 5.0 / SCALE_1_100_M_PER_PT, 100 + 5.0 / SCALE_1_100_M_PER_PT)

# L-shaped room: two rectangles sharing a corner
# Bottom part: 6.0 m × 2.0 m = 12.0 m²
# Top-left:    4.0 m × 2.0 m =  8.0 m²
# Total:                          20.0 m²
def _l_shape_pdf() -> List[Tuple[float, float]]:
    s = SCALE_1_100_M_PER_PT
    # Vertices: (0,0)→(6,0)→(6,2)→(4,2)→(4,4)→(0,4) in real metres
    # Shoelace: 0.5 × |(0+12+12+16+0+0) - (0+0+8+8+0+0)| = 0.5×24 = 12
    # Wait, let me recompute: CCW polygon (0,0),(6,0),(6,2),(4,2),(4,4),(0,4)
    # sum1 = 0×0 + 6×2 + 6×2 + 4×4 + 4×4 + 0×0 = 0+12+12+16+16+0 = 56
    # sum2 = 0×6 + 0×6 + 2×4 + 2×4 + 4×0 + 4×0 = 0+0+8+8+0+0 = 16
    # area = 0.5 × |56 - 16| = 20.0 m²  ✓
    return [
        (100, 100),
        (100 + 6.0/s, 100),
        (100 + 6.0/s, 100 + 2.0/s),
        (100 + 4.0/s, 100 + 2.0/s),
        (100 + 4.0/s, 100 + 4.0/s),
        (100, 100 + 4.0/s),
    ]

L_SHAPE_PDF = _l_shape_pdf()

# Irregular polygon (pentagon) — approximate area computed from vertices
# Pentagon: vertices roughly forming a 5m × 4m shape
# Let's use a known-precise pentagon: area = 22.0 m²
def _irregular_pentagon_pdf() -> List[Tuple[float, float]]:
    """Pentagon with known area ≈ 22.0 m² at 1:100."""
    s = SCALE_1_100_M_PER_PT
    # 5 vertices:  (0,0), (6,0), (7,3), (3,5), (0,4) in real metres
    # area via shoelace = 0.5 × |0×0 + 6×3 + 7×5 + 3×4 + 0×0 - (0×6 + 0×7 + 3×3 + 5×0 + 4×0)|
    #                   = 0.5 × |0 + 18 + 35 + 12 + 0 - (0 + 0 + 9 + 0 + 0)|
    #                   = 0.5 × |65 - 9| = 0.5 × 56 = 28.0 m²
    # Let me recompute:
    # (0,0), (6,0), (7,3), (3,5), (0,4)
    # sum1 = 0×0 + 6×3 + 7×5 + 3×4 + 0×0 = 0 + 18 + 35 + 12 + 0 = 65
    # sum2 = 0×6 + 0×7 + 3×3 + 5×0 + 4×0 = 0 + 0 + 9 + 0 + 0 = 9
    # area = 0.5 × |65 - 9| = 28.0 m²
    # Hmm, let me make a smaller one: (0,0), (5,0), (5,3), (2,4), (0,3)
    # sum1 = 0×0 + 5×3 + 5×4 + 2×3 + 0×0 = 0 + 15 + 20 + 6 + 0 = 41
    # sum2 = 0×5 + 0×5 + 3×2 + 4×0 + 3×0 = 0 + 0 + 6 + 0 + 0 = 6
    # area = 0.5 × |41 - 6| = 17.5 m²
    # That's close to 12+5.5 = hmm. Let me just use a simpler one.
    # Actually let me compute precisely for the test.
    # Use: (0,0), (6,0), (6,3), (3,5), (0,4) → area = 28.0 m² (computed above)
    # Or: (0,0), (4,0), (5,3), (2,4), (0,2)
    # sum1 = 0×0 + 4×3 + 5×4 + 2×2 + 0×0 = 0+12+20+4+0 = 36
    # sum2 = 0×4 + 0×5 + 3×2 + 4×0 + 2×0 = 0+0+6+0+0 = 6
    # area = 0.5 × |36-6| = 15.0 m²
    real_verts = [(0,0), (4,0), (5,3), (2,4), (0,2)]
    return [(100 + v[0]/s, 100 + v[1]/s) for v in real_verts]

IRREGULAR_PDF = _irregular_pentagon_pdf()

# Two adjacent rooms
ROOM_A_PDF = _rect(100, 100, 100 + 4.0/SCALE_1_100_M_PER_PT, 100 + 3.0/SCALE_1_100_M_PER_PT)
ROOM_B_PDF = _rect(100 + 4.0/SCALE_1_100_M_PER_PT, 100,
                    100 + 8.0/SCALE_1_100_M_PER_PT, 100 + 3.0/SCALE_1_100_M_PER_PT)

# Tiny furniture rectangle (0.6 m × 0.4 m = 0.24 m² — below room minimum)
FURNITURE_PDF = _rect(200, 200, 200 + 0.6/SCALE_1_100_M_PER_PT, 200 + 0.4/SCALE_1_100_M_PER_PT)

# Large drawing border (covers most of page — 2500+ m² at 1:100)
BORDER_PDF = _rect(10, 10, 580, 800)

# Corridor: very elongated (12m × 0.8m = 9.6 m², ratio 15:1)
CORRIDOR_PDF = _rect(100, 100, 100 + 12.0/SCALE_1_100_M_PER_PT, 100 + 0.8/SCALE_1_100_M_PER_PT)


# ---------------------------------------------------------------------------
# Scale info fixtures
# ---------------------------------------------------------------------------

SCALE_1_100 = {"real_metres_per_page_mm": 100.0 / 1000.0, "scale_ratio": 100}
SCALE_1_50 = {"real_metres_per_page_mm": 50.0 / 1000.0, "scale_ratio": 50}
SCALE_1_200 = {"real_metres_per_page_mm": 200.0 / 1000.0, "scale_ratio": 200}
SCALE_METRIC_10MM = {"real_metres_per_page_mm": 1.0 / 10.0}  # "10 mm = 1 m"
SCALE_UNKNOWN = {"real_metres_per_page_mm": None}


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestShoelaceArea(unittest.TestCase):
    """Polygon area calculation via shoelace formula."""

    def test_rectangle_4x3(self):
        """4.0 m × 3.0 m rectangle at 1:100 should give 12.0 m²."""
        area_pts2 = _polygon_area_abs(ROOM_4x3_PDF)
        area_m2 = area_pts2 * SCALE_1_100_M_PER_PT ** 2
        self.assertAlmostEqual(area_m2, 12.0, places=1)

    def test_square_5x5(self):
        """5.0 m × 5.0 m square at 1:100 should give 25.0 m²."""
        area_pts2 = _polygon_area_abs(ROOM_5x5_PDF)
        area_m2 = area_pts2 * SCALE_1_100_M_PER_PT ** 2
        self.assertAlmostEqual(area_m2, 25.0, places=1)

    def test_l_shape(self):
        """L-shaped room: 6×2 main + 4×2 extension = 20.0 m² at 1:100."""
        area_pts2 = _polygon_area_abs(L_SHAPE_PDF)
        area_m2 = area_pts2 * SCALE_1_100_M_PER_PT ** 2
        self.assertAlmostEqual(area_m2, 20.0, places=1)

    def test_irregular_pentagon(self):
        """Irregular pentagon area should be 15.0 m² at 1:100."""
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

    def test_degenerate_line(self):
        """A line (2 points) has zero area."""
        self.assertEqual(_polygon_area_abs([(0, 0), (100, 0)]), 0.0)

    def test_degenerate_single_point(self):
        """A single point has zero area."""
        self.assertEqual(_polygon_area_abs([(0, 0)]), 0.0)


class TestScaleCalibration(unittest.TestCase):
    """PDF-point → m² calibration at multiple scales."""

    def _area_at_scale(self, polygon: List[Tuple[float, float]], scale_info: Dict[str, Any]) -> float:
        return calibrate_area_m2(polygon, scale_info)

    def test_4x3_at_1_100(self):
        """4m × 3m = 12.0 m² at 1:100."""
        self.assertAlmostEqual(
            self._area_at_scale(ROOM_4x3_PDF, SCALE_1_100), 12.0, places=1,
        )

    def test_4x3_at_1_50(self):
        """Same 4m × 3m at 1:50 → 12.0 m² (different PDF-pt coordinates)."""
        s_1_50 = 25.4 / 72.0 * (50.0 / 1000.0)
        room_1_50 = _rect(100, 100, 100 + 4.0/s_1_50, 100 + 3.0/s_1_50)
        self.assertAlmostEqual(
            self._area_at_scale(room_1_50, SCALE_1_50), 12.0, places=1,
        )

    def test_4x3_at_1_200(self):
        """Same 4m × 3m at 1:200 → 12.0 m²."""
        s_1_200 = 25.4 / 72.0 * (200.0 / 1000.0)
        room_1_200 = _rect(100, 100, 100 + 4.0/s_1_200, 100 + 3.0/s_1_200)
        self.assertAlmostEqual(
            self._area_at_scale(room_1_200, SCALE_1_200), 12.0, places=1,
        )

    def test_4x3_metric_10mm(self):
        """Same 4m × 3m at metric "10 mm = 1 m" → 12.0 m²."""
        s_metric = 25.4 / 72.0 * (1.0 / 10.0)
        room_metric = _rect(100, 100, 100 + 4.0/s_metric, 100 + 3.0/s_metric)
        self.assertAlmostEqual(
            self._area_at_scale(room_metric, SCALE_METRIC_10MM), 12.0, places=1,
        )

    def test_1_100_ratio_equals_metric_10mm(self):
        """Ratio 1:100 and metric "10 mm = 1 m" produce same real scale."""
        area_ratio = self._area_at_scale(ROOM_4x3_PDF, SCALE_1_100)
        area_metric = self._area_at_scale(ROOM_4x3_PDF, SCALE_METRIC_10MM)
        self.assertAlmostEqual(area_ratio, area_metric, places=6)

    def test_unknown_scale_returns_zero(self):
        """Unknown scale must return 0.0 m² — never fake a value."""
        self.assertEqual(self._area_at_scale(ROOM_4x3_PDF, SCALE_UNKNOWN), 0.0)


class TestPolygonConversion(unittest.TestCase):
    """PDF-point → metre coordinate conversion."""

    def test_rectangle_to_metres(self):
        """4m × 3m rectangle vertices convert correctly."""
        verts_m = calibrate_polygon_m(ROOM_4x3_PDF, SCALE_1_100)
        self.assertEqual(len(verts_m), 4)
        # Width in metres
        width_m = verts_m[1][0] - verts_m[0][0]
        self.assertAlmostEqual(width_m, 4.0, places=1)
        # Height in metres
        height_m = verts_m[2][1] - verts_m[1][1]
        self.assertAlmostEqual(height_m, 3.0, places=1)

    def test_unknown_scale_returns_empty(self):
        """Unknown scale returns empty polygon."""
        self.assertEqual(calibrate_polygon_m(ROOM_4x3_PDF, SCALE_UNKNOWN), [])


class TestElongation(unittest.TestCase):
    """Aspect ratio / elongation filtering."""

    def test_square_ratio_1(self):
        """Square has ratio 1:1."""
        self.assertAlmostEqual(_elongation_ratio(ROOM_5x5_PDF), 1.0, places=2)

    def test_corridor_ratio(self):
        """12m × 0.8m corridor has ratio 15:1."""
        ratio = _elongation_ratio(CORRIDOR_PDF)
        self.assertGreater(ratio, 12.0)


class TestPointInPolygon(unittest.TestCase):
    """Ray-casting point-in-polygon test."""

    def test_inside(self):
        """Point inside rectangle is detected."""
        # ROOM_4x3_PDF starts at (100,100); use a point clearly inside
        cx, cy = _polygon_centroid(ROOM_4x3_PDF)
        self.assertTrue(_point_in_polygon((cx, cy), ROOM_4x3_PDF))

    def test_outside(self):
        """Point outside rectangle is detected."""
        self.assertFalse(_point_in_polygon((50, 50), ROOM_4x3_PDF))

    def test_on_edge(self):
        """Point on edge may be inside or outside (implementation-dependent)."""
        # Just verify it doesn't crash
        _point_in_polygon((100, 200), ROOM_4x3_PDF)


class TestFilterFace(unittest.TestCase):
    """False-positive filtering."""

    def test_valid_room_passes(self):
        """4m × 3m room at 1:100 passes all filters."""
        result = filter_face(ROOM_4x3_PDF, SCALE_1_100, 595, 842)
        self.assertTrue(result.is_room)
        self.assertAlmostEqual(result.area_m2, 12.0, places=1)

    def test_too_small_rejected(self):
        """0.6m × 0.4m furniture rejected as too small."""
        result = filter_face(FURNITURE_PDF, SCALE_1_100, 595, 842)
        self.assertFalse(result.is_room)
        self.assertIn("too_small", result.reason)

    def test_too_large_rejected(self):
        """Drawing border rejected as covering most of the page."""
        result = filter_face(BORDER_PDF, SCALE_1_100, 595, 842)
        self.assertFalse(result.is_room)
        # Rejected by page-coverage check (covers > 75% of page)
        self.assertIn("covers_page", result.reason)

    def test_elongated_rejected(self):
        """12m × 0.8m corridor rejected as too elongated."""
        result = filter_face(CORRIDOR_PDF, SCALE_1_100, 595, 842)
        self.assertFalse(result.is_room)
        self.assertIn("too_elongated", result.reason)

    def test_title_block_rejected(self):
        """Polygon in bottom-right title-block zone rejected."""
        # 3m × 2m room in title-block zone
        tb_room = _rect(450, 750, 450 + 3.0/SCALE_1_100_M_PER_PT, 750 + 2.0/SCALE_1_100_M_PER_PT)
        result = filter_face(tb_room, SCALE_1_100, 595, 842)
        self.assertFalse(result.is_room)
        self.assertIn("title_block_zone", result.reason)

    def test_unknown_scale_rejected(self):
        """Unknown scale → not a trustworthy room."""
        result = filter_face(ROOM_4x3_PDF, SCALE_UNKNOWN, 595, 842)
        self.assertFalse(result.is_room)
        self.assertIn("un_calibrated", result.reason)

    def test_degenerate_polygon_rejected(self):
        """Degenerate (< 3 vertices) polygon rejected."""
        result = filter_face([(0, 0), (100, 0)], SCALE_1_100, 595, 842)
        self.assertFalse(result.is_room)
        self.assertIn("degenerate", result.reason)

    def test_building_outline_rejected(self):
        """Polygon containing most other centroids → building outline."""
        # Two room faces inside a large outline that covers > 75% of page
        outline = _rect(30, 30, 575, 820)  # 545×790 = covers ~99% of 595×842
        room1 = _rect(100, 100, 250, 300)
        room2 = _rect(300, 100, 450, 300)
        result = filter_face(outline, SCALE_1_100, 595, 842, all_polygons=[outline, room1, room2])
        self.assertFalse(result.is_room)
        # Rejected by page-coverage check (> 75% of page)
        self.assertIn("covers_page", result.reason)

    def test_two_adjacent_rooms_both_pass(self):
        """Two adjacent rooms both pass filters."""
        result_a = filter_face(ROOM_A_PDF, SCALE_1_100, 595, 842)
        result_b = filter_face(ROOM_B_PDF, SCALE_1_100, 595, 842)
        self.assertTrue(result_a.is_room)
        self.assertTrue(result_b.is_room)
        self.assertAlmostEqual(result_a.area_m2, 12.0, places=1)
        self.assertAlmostEqual(result_b.area_m2, 12.0, places=1)


class TestPerimeter(unittest.TestCase):
    """Perimeter calculation in metres."""

    def test_rectangle_perimeter(self):
        """4m × 3m → perimeter = 14.0 m."""
        verts_m = calibrate_polygon_m(ROOM_4x3_PDF, SCALE_1_100)
        self.assertAlmostEqual(_perimeter_m(verts_m), 14.0, places=1)

    def test_square_perimeter(self):
        """5m × 5m → perimeter = 20.0 m."""
        verts_m = calibrate_polygon_m(ROOM_5x5_PDF, SCALE_1_100)
        self.assertAlmostEqual(_perimeter_m(verts_m), 20.0, places=1)


class TestTakeoffRows(unittest.TestCase):
    """Room-to-takeoff row production."""

    def _make_room(self, label: str, area_m2: float, page: int = 1) -> RoomFace:
        return RoomFace(
            room_ref="R01",
            label=label,
            polygon_pdf_pts=[(0, 0), (100, 0), (100, 100), (0, 100)],
            polygon_m=[(0, 0), (4, 0), (4, 3), (0, 3)],
            floor_area_m2=area_m2,
            perimeter_m=14.0,
            geometry_confidence=0.98,
            evidence=[],
            source_page=page,
            drawing_number="DWG-001",
            scale_source="1:100",
            calibration_confidence=0.95,
        )

    def test_floor_row_structure(self):
        """Floor row has correct fields."""
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
        self.assertEqual(row["source_page"], 1)

    def test_ceiling_rows_added(self):
        """When include_ceiling=True, ceiling rows are added."""
        room = self._make_room("BED 1", 15.0)
        rows = rooms_to_takeoff_rows([room], workspace_id=1, include_ceiling=True)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["element"], "Floor area")
        self.assertEqual(rows[1]["element"], "Ceiling area")

    def test_multiple_rooms(self):
        """Multiple rooms produce multiple rows."""
        rooms = [
            self._make_room("KITCHEN", 12.0, page=1),
            self._make_room("BED 1", 15.0, page=1),
            self._make_room("BATH", 6.0, page=2),
        ]
        rows = rooms_to_takeoff_rows(rooms, workspace_id=1)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["location"], "KITCHEN")
        self.assertEqual(rows[1]["location"], "BED 1")
        self.assertEqual(rows[2]["location"], "BATH")

    def test_quantity_status(self):
        """High confidence → 'Measured'; lower → 'Provisional measured'."""
        high = self._make_room("A", 10.0)
        high.calibration_confidence = 0.95
        low = self._make_room("B", 10.0)
        low.calibration_confidence = 0.7
        rows = rooms_to_takeoff_rows([high, low], workspace_id=1)
        self.assertEqual(rows[0]["quantity_status"], "Measured")
        self.assertEqual(rows[1]["quantity_status"], "Provisional measured")


class TestRoomSummary(unittest.TestCase):
    """Aggregate room statistics."""

    def test_summary(self):
        """Summary reports correct totals."""
        rooms = [
            RoomFace("R01", "A", [], [], 12.0, 14.0, 0.98, [], scale_source="1:100",
                     calibration_confidence=0.95),
            RoomFace("R02", "B", [], [], 15.0, 16.0, 0.98, [], scale_source="1:100",
                     calibration_confidence=0.95),
        ]
        s = room_face_summary(rooms)
        self.assertEqual(s["room_count"], 2)
        self.assertAlmostEqual(s["total_floor_area_m2"], 27.0, places=1)
        self.assertEqual(s["review_count"], 0)

    def test_summary_empty(self):
        """Empty list produces zero totals."""
        s = room_face_summary([])
        self.assertEqual(s["room_count"], 0)
        self.assertEqual(s["total_floor_area_m2"], 0.0)


class TestIntegrationWithProduction(unittest.TestCase):
    """Integration test: exercise the full pipeline from segments to take-off rows.

    This test proves the entire chain works end-to-end:
    segments → v145 extract → filter → calibrate → take-off rows.
    """

    def test_segments_to_takeoff_rows(self):
        """Create synthetic wall segments forming a 4m×3m room at 1:100.

        The room must produce a floor_area take-off row with:
        - quantity = 12.0 m²
        - unit = m²
        - row_role = floor_area
        - location = room label
        - calibration_confidence > 0
        """
        # Create wall segments for a simple rectangle room at 1:100
        s = SCALE_1_100_M_PER_PT
        x0, y0 = 200.0, 200.0
        w = 4.0 / s  # width in PDF pts
        h = 3.0 / s  # height in PDF pts
        segments = [
            ((x0, y0), (x0 + w, y0)),         # bottom
            ((x0 + w, y0), (x0 + w, y0 + h)), # right
            ((x0 + w, y0 + h), (x0, y0 + h)), # top
            ((x0, y0 + h), (x0, y0)),          # left
        ]

        from pb_room_face_takeoff import extract_and_calibrate_rooms
        rooms = extract_and_calibrate_rooms(
            segments=segments,
            scale_info=SCALE_1_100,
            page_width_pt=595,
            page_height_pt=842,
            page_no=1,
            drawing_number="TEST-001",
        )

        # Should produce exactly 1 room
        self.assertEqual(len(rooms), 1)
        room = rooms[0]
        self.assertAlmostEqual(room.floor_area_m2, 12.0, places=1)
        self.assertGreater(room.calibration_confidence, 0)
        self.assertEqual(room.source_page, 1)

        # Convert to take-off rows
        rows = rooms_to_takeoff_rows([room], workspace_id=1)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["unit"], "m²")
        self.assertEqual(row["row_role"], "floor_area")
        self.assertAlmostEqual(row["quantity"], 12.0, places=1)

    def test_l_shape_segments_to_takeoff(self):
        """L-shaped room segments produce correct area (20.0 m² at 1:100)."""
        s = SCALE_1_100_M_PER_PT
        x0, y0 = 200.0, 200.0
        # L-shape: bottom 6m × 2m + top-left 4m × 2m = 20.0 m²
        segments = [
            ((x0, y0), (x0 + 6.0/s, y0)),                 # bottom
            ((x0 + 6.0/s, y0), (x0 + 6.0/s, y0 + 2.0/s)), # right bottom
            ((x0 + 6.0/s, y0 + 2.0/s), (x0 + 4.0/s, y0 + 2.0/s)), # notch horizontal
            ((x0 + 4.0/s, y0 + 2.0/s), (x0 + 4.0/s, y0 + 4.0/s)), # notch vertical
            ((x0 + 4.0/s, y0 + 4.0/s), (x0, y0 + 4.0/s)),        # top
            ((x0, y0 + 4.0/s), (x0, y0)),                          # left
        ]

        from pb_room_face_takeoff import extract_and_calibrate_rooms
        rooms = extract_and_calibrate_rooms(
            segments=segments,
            scale_info=SCALE_1_100,
            page_width_pt=595,
            page_height_pt=842,
            page_no=1,
        )

        self.assertEqual(len(rooms), 1)
        self.assertAlmostEqual(rooms[0].floor_area_m2, 20.0, places=1)

    def test_two_adjacent_rooms_segments(self):
        """Two adjacent room segments produce two separate take-off rows."""
        s = SCALE_1_100_M_PER_PT
        x0, y0 = 200.0, 200.0
        w = 4.0 / s
        h = 3.0 / s
        segments = [
            # Room A (left)
            ((x0, y0), (x0 + w, y0)),
            ((x0, y0), (x0, y0 + h)),
            # Room B (right)
            ((x0 + w, y0), (x0 + 2*w, y0)),
            ((x0 + 2*w, y0), (x0 + 2*w, y0 + h)),
            # Shared wall + outer walls
            ((x0 + w, y0), (x0 + w, y0 + h)),
            ((x0, y0 + h), (x0 + 2*w, y0 + h)),
        ]

        from pb_room_face_takeoff import extract_and_calibrate_rooms
        rooms = extract_and_calibrate_rooms(
            segments=segments,
            scale_info=SCALE_1_100,
            page_width_pt=595,
            page_height_pt=842,
            page_no=1,
        )

        # Should produce 2 rooms
        self.assertEqual(len(rooms), 2)
        for room in rooms:
            self.assertAlmostEqual(room.floor_area_m2, 12.0, places=1)

        rows = rooms_to_takeoff_rows(rooms, workspace_id=1)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["unit"], "m²")
            self.assertEqual(row["row_role"], "floor_area")

    def test_unknown_scale_no_takeoff_rows(self):
        """Unknown scale must NOT produce confidently stated m² quantities."""
        s = SCALE_1_100_M_PER_PT
        x0, y0 = 200.0, 200.0
        w = 4.0 / s
        h = 3.0 / s
        segments = [
            ((x0, y0), (x0 + w, y0)),
            ((x0 + w, y0), (x0 + w, y0 + h)),
            ((x0 + w, y0 + h), (x0, y0 + h)),
            ((x0, y0 + h), (x0, y0)),
        ]

        from pb_room_face_takeoff import extract_and_calibrate_rooms
        rooms = extract_and_calibrate_rooms(
            segments=segments,
            scale_info=SCALE_UNKNOWN,  # unknown scale
            page_width_pt=595,
            page_height_pt=842,
            page_no=1,
        )

        # Unknown scale → no rooms should pass filtering
        self.assertEqual(len(rooms), 0)


if __name__ == "__main__":
    unittest.main()
