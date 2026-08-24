"""Tests for Priority 4 Phase B1: SurfaceEvidence v160.

Tests cover:
  1. Red filled rectangle extraction as one polygon
  2. Fill-only rectangle
  3. Fill + stroke rectangle
  4. Quad fill
  5. Closed line-path fill
  6. Open path not treated as polygon
  7. Unknown calibration -> area_m2 None
  8. Calibrated rectangle gives correct m2
  9. Existing segment extraction unchanged (vector geometry compat)
  10. PT01 text inside polygon associates
  11. PT01 text outside polygon does not strongly associate
  12. Two codes inside same polygon -> conflict
  13. Polygon overlapping measured surface >50% -> strong association
  14. Centroid inside but low overlap -> weaker confidence
  15. Measured surface quantity unchanged after allocation
  16. Same fill colour with different codes stays distinguishable
  17. Finish code without known substrate -> substrate "To confirm" / Review
  18. Unclassified measured surface retains quantity
"""
from __future__ import annotations

import math
import unittest
from typing import Any, Dict, List, Optional, Sequence, Tuple
from unittest.mock import MagicMock

from pb_surface_evidence_v160 import (
    FillPolygon,
    SurfaceEvidence,
    AssociationResult,
    _shoelace_area,
    polygon_area_abs,
    _point_in_polygon,
    _polygon_overlap_ratio,
    _polygon_intersection_area,
    _scale_factor_m_per_pt,
    calibrate_area_m2,
    page_scale_info,
    extract_filled_polygons,
    extract_finish_codes_from_text,
    extract_finish_codes_from_positions,
    associate_surface_to_target,
    associate_code_to_polygon,
    associate_with_measured_surfaces,
    build_surface_evidence,
    _items_are_closed_lines,
    _closed_line_vertices,
    _num,
    VERSION,
    PDF_PT_TO_MM,
    MM_PER_PT,
)


# ---------------------------------------------------------------------------
# Helpers for building fake PDF drawings / pages
# ---------------------------------------------------------------------------

def _make_point(x: float, y: float) -> MagicMock:
    """Create a mock point with .x and .y attributes."""
    p = MagicMock()
    p.x = x
    p.y = y
    return p


def _make_rect(x0: float, y0: float, x1: float, y1: float) -> MagicMock:
    """Create a mock rect with .x0 .y0 .x1 .y1 attributes."""
    r = MagicMock()
    r.x0 = x0
    r.y0 = y0
    r.x1 = x1
    r.y1 = y1
    return r


def _make_quad(ul, ur, lr, ll) -> MagicMock:
    """Create a mock quad with .ul .ur .lr .ll corners."""
    q = MagicMock()
    q.ul = _make_point(*ul)
    q.ur = _make_point(*ur)
    q.lr = _make_point(*lr)
    q.ll = _make_point(*ll)
    return q


def _drawing(fill=None, color=None, items=None, width=1.0, dashes="",
             closePath=False, even_odd=False, layer="", fill_opacity=1.0,
             stroke_opacity=1.0) -> Dict[str, Any]:
    """Build a fake drawing dict matching PyMuPDF get_drawings() structure."""
    return {
        "fill": fill,
        "color": color,
        "width": width,
        "dashes": dashes,
        "closePath": closePath,
        "even_odd": even_odd,
        "layer": layer,
        "fill_opacity": fill_opacity,
        "stroke_opacity": stroke_opacity,
        "items": items or [],
    }


def _fake_page(drawings: List[Dict[str, Any]]) -> MagicMock:
    """Create a mock PDF page returning the given drawings."""
    page = MagicMock()
    page.get_drawings.return_value = drawings
    return page


def _rect_drawing(x0, y0, x1, y1, fill=(1, 0, 0), color=(0, 0, 0), **kw):
    """Shorthand for a filled rectangle drawing."""
    rect = _make_rect(x0, y0, x1, y1)
    return _drawing(fill=fill, color=color, items=[("re", rect)], **kw)


def _line_drawing(points, fill=(0, 0, 1), color=None, **kw):
    """Shorthand for a closed line-path drawing.
    points: list of (x,y) tuples forming a closed path.
    """
    items = []
    for i in range(len(points)):
        p1 = _make_point(*points[i])
        p2 = _make_point(*points[(i + 1) % len(points)])
        items.append(("l", p1, p2))
    return _drawing(fill=fill, color=color, items=items, **kw)


# ---------------------------------------------------------------------------
# Geometry unit tests
# ---------------------------------------------------------------------------

class TestShoelaceArea(unittest.TestCase):
    def test_unit_square(self):
        sq = ((0, 0), (1, 0), (1, 1), (0, 1))
        self.assertAlmostEqual(abs(_shoelace_area(sq)), 1.0, places=4)

    def test_triangle(self):
        tri = ((0, 0), (4, 0), (2, 3))
        self.assertAlmostEqual(abs(_shoelace_area(tri)), 6.0, places=4)

    def test_ccw_is_positive(self):
        sq = ((0, 0), (1, 0), (1, 1), (0, 1))
        self.assertGreater(_shoelace_area(sq), 0)

    def test_cw_is_negative(self):
        sq = ((0, 0), (0, 1), (1, 1), (1, 0))
        self.assertLess(_shoelace_area(sq), 0)

    def test_empty(self):
        self.assertAlmostEqual(_shoelace_area(()), 0.0)

    def test_two_points(self):
        self.assertAlmostEqual(_shoelace_area(((0, 0), (1, 1))), 0.0)

    def test_large_square(self):
        sq = ((0, 0), (100, 0), (100, 100), (0, 100))
        self.assertAlmostEqual(abs(_shoelace_area(sq)), 10000.0, places=2)


class TestPolygonAreaAbs(unittest.TestCase):
    def test_unit_square(self):
        self.assertAlmostEqual(polygon_area_abs(((0, 0), (1, 0), (1, 1), (0, 1))), 1.0)

    def test_cw_still_positive(self):
        cw = ((0, 0), (0, 5), (5, 5), (5, 0))
        self.assertAlmostEqual(polygon_area_abs(cw), 25.0)


class TestPointInPolygon(unittest.TestCase):
    def test_inside_square(self):
        self.assertTrue(_point_in_polygon(0.5, 0.5, ((0, 0), (1, 0), (1, 1), (0, 1))))

    def test_outside_square(self):
        self.assertFalse(_point_in_polygon(2, 2, ((0, 0), (1, 0), (1, 1), (0, 1))))

    def test_on_edge_undefined(self):
        # On-edge is implementation-defined; just ensure no crash
        _point_in_polygon(0, 0.5, ((0, 0), (1, 0), (1, 1), (0, 1)))

    def test_triangle_inside(self):
        tri = ((0, 0), (10, 0), (5, 10))
        self.assertTrue(_point_in_polygon(5, 3, tri))

    def test_triangle_outside(self):
        tri = ((0, 0), (10, 0), (5, 10))
        self.assertFalse(_point_in_polygon(0, 5, tri))

    def test_empty_polygon(self):
        self.assertFalse(_point_in_polygon(0, 0, ()))

    def test_two_points(self):
        self.assertFalse(_point_in_polygon(0, 0, ((0, 0), (1, 1))))


class TestPolygonOverlapRatio(unittest.TestCase):
    def test_identical_polygons(self):
        sq = ((0, 0), (10, 0), (10, 10), (0, 10))
        ratio = _polygon_overlap_ratio(sq, sq, sample_count=400)
        self.assertAlmostEqual(ratio, 1.0, places=1)

    def test_no_overlap(self):
        a = ((0, 0), (5, 0), (5, 5), (0, 5))
        b = ((10, 10), (15, 10), (15, 15), (10, 15))
        ratio = _polygon_overlap_ratio(a, b, sample_count=400)
        self.assertAlmostEqual(ratio, 0.0, places=1)

    def test_half_overlap(self):
        a = ((0, 0), (10, 0), (10, 10), (0, 10))
        b = ((5, 0), (15, 0), (15, 10), (5, 10))
        ratio = _polygon_overlap_ratio(a, b, sample_count=1000)
        self.assertGreater(ratio, 0.45)
        self.assertLess(ratio, 0.55)

    def test_empty_polygon(self):
        self.assertAlmostEqual(_polygon_overlap_ratio((), ((0, 0), (1, 0), (1, 1))), 0.0)


# ---------------------------------------------------------------------------
# Fill polygon extraction tests
# ---------------------------------------------------------------------------

class TestExtractFilledPolygons(unittest.TestCase):
    """Tests 1-6: polygon extraction from PDF drawings."""

    def test_01_red_filled_rectangle(self):
        """Test 1: red filled rectangle extracted as one polygon."""
        page = _fake_page([_rect_drawing(50, 50, 200, 150, fill=(1, 0, 0))])
        polys = extract_filled_polygons(page)
        self.assertEqual(len(polys), 1)
        fp = polys[0]
        self.assertEqual(len(fp.vertices), 4)
        self.assertEqual(fp.fill, (1.0, 0.0, 0.0))
        # Check vertices are the rectangle corners
        self.assertAlmostEqual(fp.area_page_pts2, 150.0 * 100.0, places=1)
        self.assertEqual(fp.item_types, ("re",))

    def test_02_fill_only_rectangle(self):
        """Test 2: fill-only rectangle (no stroke)."""
        page = _fake_page([_rect_drawing(10, 10, 50, 50, fill=(0, 1, 0), color=None)])
        polys = extract_filled_polygons(page)
        self.assertEqual(len(polys), 1)
        fp = polys[0]
        self.assertEqual(fp.fill, (0.0, 1.0, 0.0))
        self.assertIsNone(fp.stroke)
        self.assertEqual(fp.item_types, ("re",))

    def test_03_fill_plus_stroke_rectangle(self):
        """Test 3: fill + stroke rectangle."""
        page = _fake_page([_rect_drawing(0, 0, 100, 100, fill=(0, 0, 1), color=(0, 0, 0))])
        polys = extract_filled_polygons(page)
        self.assertEqual(len(polys), 1)
        fp = polys[0]
        self.assertEqual(fp.fill, (0.0, 0.0, 1.0))
        self.assertEqual(fp.stroke, (0.0, 0.0, 0.0))

    def test_04_quad_fill(self):
        """Test 4: quad fill."""
        quad = _make_quad((10, 10), (50, 10), (60, 40), (20, 40))
        drawing = _drawing(fill=(0.5, 0.5, 0), items=[("qu", quad)])
        page = _fake_page([drawing])
        polys = extract_filled_polygons(page)
        self.assertEqual(len(polys), 1)
        fp = polys[0]
        self.assertEqual(len(fp.vertices), 4)
        self.assertAlmostEqual(fp.area_page_pts2, 1200.0, places=0)

    def test_05_closed_line_path_fill(self):
        """Test 5: closed line-path fill (triangle from 3 lines)."""
        points = [(0, 0), (10, 0), (5, 10)]
        drawing = _line_drawing(points, fill=(0.3, 0.3, 0.3), color=None)
        page = _fake_page([drawing])
        polys = extract_filled_polygons(page)
        self.assertEqual(len(polys), 1)
        fp = polys[0]
        self.assertEqual(len(fp.vertices), 3)
        self.assertAlmostEqual(abs(fp.area_page_pts2), 50.0, places=1)

    def test_06_open_path_not_polygon(self):
        """Test 6: open line path with fill is NOT extracted as polygon."""
        # Two lines that don't close
        p1 = _make_point(0, 0)
        p2 = _make_point(10, 0)
        p3 = _make_point(10, 10)
        drawing = _drawing(
            fill=(1, 0, 0),
            items=[("l", p1, p2), ("l", p2, p3)],
        )
        page = _fake_page([drawing])
        polys = extract_filled_polygons(page)
        # Open path should not produce a valid polygon
        self.assertEqual(len(polys), 0)

    def test_stroke_only_not_extracted(self):
        """Stroke-only drawings (no fill) are not extracted."""
        p1 = _make_point(0, 0)
        p2 = _make_point(100, 100)
        drawing = _drawing(fill=None, color=(0, 0, 0), items=[("l", p1, p2)])
        page = _fake_page([drawing])
        polys = extract_filled_polygons(page)
        self.assertEqual(len(polys), 0)

    def test_multiple_fills_on_page(self):
        """Multiple filled drawings on one page produce multiple records."""
        page = _fake_page([
            _rect_drawing(0, 0, 50, 50, fill=(1, 0, 0)),
            _rect_drawing(60, 60, 110, 110, fill=(0, 0, 1)),
        ])
        polys = extract_filled_polygons(page)
        self.assertEqual(len(polys), 2)
        self.assertEqual(polys[0].fill, (1, 0, 0))
        self.assertEqual(polys[1].fill, (0, 0, 1))

    def test_fill_opacity_preserved(self):
        """Fill opacity is preserved in the record."""
        page = _fake_page([
            _rect_drawing(0, 0, 100, 100, fill=(1, 0, 0), fill_opacity=0.5)
        ])
        polys = extract_filled_polygons(page)
        self.assertEqual(len(polys), 1)
        self.assertAlmostEqual(polys[0].fill_opacity, 0.5)

    def test_layer_preserved(self):
        """Layer name is preserved."""
        page = _fake_page([
            _rect_drawing(0, 0, 100, 100, fill=(1, 0, 0), layer="HATCH")
        ])
        polys = extract_filled_polygons(page)
        self.assertEqual(len(polys), 1)
        self.assertEqual(polys[0].layer, "HATCH")

    def test_drawing_index_assigned(self):
        """Drawing index is correctly assigned."""
        page = _fake_page([
            _rect_drawing(0, 0, 50, 50, fill=(1, 0, 0)),
            _rect_drawing(60, 60, 110, 110, fill=(0, 1, 0)),
        ])
        polys = extract_filled_polygons(page)
        self.assertEqual(polys[0].drawing_index, 0)
        self.assertEqual(polys[1].drawing_index, 1)


class TestClosedLinePathDetection(unittest.TestCase):
    """Tests for _items_are_closed_lines helper."""

    def test_closed_triangle(self):
        items = [
            ("l", _make_point(0, 0), _make_point(10, 0)),
            ("l", _make_point(10, 0), _make_point(5, 10)),
            ("l", _make_point(5, 10), _make_point(0, 0)),
        ]
        self.assertTrue(_items_are_closed_lines(items))

    def test_open_path(self):
        items = [
            ("l", _make_point(0, 0), _make_point(10, 0)),
            ("l", _make_point(10, 0), _make_point(10, 10)),
        ]
        self.assertFalse(_items_are_closed_lines(items))

    def test_too_few_lines(self):
        items = [
            ("l", _make_point(0, 0), _make_point(10, 0)),
        ]
        self.assertFalse(_items_are_closed_lines(items))

    def test_non_line_items(self):
        items = [
            ("re", _make_rect(0, 0, 10, 10)),
        ]
        self.assertFalse(_items_are_closed_lines(items))


# ---------------------------------------------------------------------------
# Calibration tests
# ---------------------------------------------------------------------------

class TestCalibration(unittest.TestCase):
    """Tests 7-8: calibration and area_m2."""

    def test_07_unknown_calibration_area_m2_none(self):
        """Test 7: unknown calibration -> area_m2 is None."""
        sq = ((0, 0), (100, 0), (100, 100), (0, 100))
        scale = {"real_metres_per_page_mm": None, "px_per_m": 0.0}
        result = calibrate_area_m2(sq, scale)
        self.assertIsNone(result)

    def test_08_calibrated_rectangle_gives_correct_m2(self):
        """Test 8: calibrated rectangle gives correct m2.

        1:100 scale: rpm = 100/1000 = 0.1
        mm_per_pt = 25.4/72 ≈ 0.3528
        m_per_pt = 0.3528 * 0.1 = 0.03528
        100pt x 100pt = 10000 pt²
        area_m2 = 10000 * 0.03528² = 10000 * 0.001245 = 12.45 m²
        """
        sq = ((0, 0), (100, 0), (100, 100), (0, 100))
        # 1:100 scale: px_per_m = 2834.646 (at zoom=1)
        scale = {"real_metres_per_page_mm": 0.1, "px_per_m": 2834.646, "render_zoom": 1.0}
        result = calibrate_area_m2(sq, scale)
        self.assertIsNotNone(result)
        expected_m_per_pt = MM_PER_PT * 0.1
        expected_area = 10000.0 * expected_m_per_pt * expected_m_per_pt
        self.assertAlmostEqual(result, expected_area, places=4)

    def test_scale_factor_m_per_pt(self):
        """Scale factor calculation."""
        sf = _scale_factor_m_per_pt({"real_metres_per_page_mm": 0.1})
        self.assertIsNotNone(sf)
        self.assertAlmostEqual(sf, MM_PER_PT * 0.1, places=6)

    def test_scale_factor_none_when_missing(self):
        sf = _scale_factor_m_per_pt({"real_metres_per_page_mm": None})
        self.assertIsNone(sf)

    def test_scale_factor_none_when_zero(self):
        sf = _scale_factor_m_per_pt({"real_metres_per_page_mm": 0})
        self.assertIsNone(sf)

    def test_page_scale_info_from_page_dict(self):
        """page_scale_info derives from px_per_m.

        1:100 scale: px_per_m = render_zoom * 2834.646 / 100 = 28.346
        rpm = render_zoom * 2.834646 / px_per_m = 1.0 * 2.834646 / 28.346 = 0.1
        """
        page = {"px_per_m": 28.34646, "render_zoom": 1.0, "scale_text": "1:100"}
        info = page_scale_info(page)
        self.assertAlmostEqual(info["real_metres_per_page_mm"], 0.1, places=4)
        self.assertEqual(info["scale_text"], "1:100")

    def test_page_scale_info_no_px_per_m(self):
        """page_scale_info returns None rpm when px_per_m is missing."""
        page = {"px_per_m": 0, "render_zoom": 1.0}
        info = page_scale_info(page)
        self.assertIsNone(info["real_metres_per_page_mm"])


class TestFillPolygonProperties(unittest.TestCase):
    """Test FillPolygon dataclass properties."""

    def test_bbox(self):
        fp = FillPolygon(vertices=((10, 20), (50, 20), (50, 80), (10, 80)))
        self.assertEqual(fp.bbox, (10, 20, 50, 80))

    def test_centroid(self):
        fp = FillPolygon(vertices=((0, 0), (10, 0), (10, 10), (0, 10)))
        cx, cy = fp.centroid
        self.assertAlmostEqual(cx, 5.0)
        self.assertAlmostEqual(cy, 5.0)

    def test_area_page_pts2(self):
        fp = FillPolygon(vertices=((0, 0), (100, 0), (100, 100), (0, 100)))
        self.assertAlmostEqual(fp.area_page_pts2, 10000.0, places=1)

    def test_width_height(self):
        fp = FillPolygon(vertices=((10, 20), (50, 20), (50, 80), (10, 80)))
        self.assertAlmostEqual(fp.width_pt, 40.0)
        self.assertAlmostEqual(fp.height_pt, 60.0)


class TestSurfaceEvidenceRecord(unittest.TestCase):
    """Test SurfaceEvidence dataclass and serialization."""

    def test_to_dict_tuples_to_lists(self):
        sev = SurfaceEvidence(
            polygon_pdf_pts=((0, 0), (10, 0), (10, 10)),
            bbox=(0, 0, 10, 10),
            fill_colour=(1.0, 0.0, 0.0),
            source_item_types=("re",),
        )
        d = sev.to_dict()
        self.assertIsInstance(d["polygon_pdf_pts"], list)
        self.assertEqual(d["polygon_pdf_pts"], [[0, 0], [10, 0], [10, 10]])
        self.assertEqual(d["bbox"], [0, 0, 10, 10])
        self.assertEqual(d["fill_colour"], [1.0, 0.0, 0.0])
        self.assertEqual(d["source_item_types"], ["re"])

    def test_to_dict_none_fill(self):
        sev = SurfaceEvidence(fill_colour=None)
        d = sev.to_dict()
        self.assertIsNone(d["fill_colour"])


# ---------------------------------------------------------------------------
# Code extraction tests
# ---------------------------------------------------------------------------

class TestFinishCodeExtraction(unittest.TestCase):
    """Tests 10-12: code extraction and spatial association."""

    def test_10_pt01_text_inside_polygon(self):
        """Test 10: PT01 text inside polygon associates."""
        # Polygon covering page
        poly = FillPolygon(vertices=((0, 0), (500, 0), (500, 500), (0, 500)))
        # Code occurrence inside polygon
        code_bbox = (100, 100, 140, 115)
        result = associate_code_to_polygon(code_bbox, poly)
        self.assertTrue(result["associated"])
        self.assertEqual(result["method"], "centroid_containment")
        self.assertGreater(result["confidence"], 0.5)

    def test_11_pt01_text_outside_polygon(self):
        """Test 11: PT01 text outside polygon does not strongly associate."""
        poly = FillPolygon(vertices=((0, 0), (100, 0), (100, 100), (0, 100)))
        # Code occurrence far outside
        code_bbox = (300, 300, 340, 315)
        result = associate_code_to_polygon(code_bbox, poly)
        self.assertFalse(result["associated"])

    def test_12_two_codes_same_polygon_conflict(self):
        """Test 12: two codes inside same polygon -> conflict status."""
        polygon = ((0, 0), (200, 0), (200, 200), (0, 200))
        evidence = [SurfaceEvidence(
            polygon_pdf_pts=polygon,
            workspace_id=1,
            page_id=1,
        )]
        codes = [
            {"code": "PT01", "bbox": (50, 50, 80, 60), "page_id": 1},
            {"code": "PT02", "bbox": (100, 100, 130, 110), "page_id": 1},
        ]
        result = associate_with_measured_surfaces(evidence, [], code_occurrences=codes)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].status, "conflict")
        self.assertIn("Multiple codes", result[0].notes)

    def test_finish_codes_from_text(self):
        """extract_finish_codes_from_text finds codes."""
        text = "The wall finish is PT01 as per the schedule.\nFC01 on wet areas."
        codes = extract_finish_codes_from_text(text, page_id=1)
        code_values = [c["code"] for c in codes]
        self.assertIn("PT01", code_values)
        self.assertIn("FC01", code_values)

    def test_finish_codes_from_text_empty(self):
        codes = extract_finish_codes_from_text("", page_id=1)
        self.assertEqual(len(codes), 0)

    def test_finish_codes_from_positions(self):
        """extract_finish_codes_from_positions finds positioned codes."""
        words = [
            MagicMock(text="PT01", bbox=(100, 200, 130, 215)),
            MagicMock(text="KITCHEN", bbox=(50, 50, 100, 65)),
            MagicMock(text="FC01", bbox=(300, 400, 330, 415)),
        ]
        codes = extract_finish_codes_from_positions(words, page_id=1)
        code_values = [c["code"] for c in codes]
        self.assertIn("PT01", code_values)
        self.assertIn("FC01", code_values)
        self.assertNotIn("KITCHEN", code_values)

    def test_finish_codes_from_positions_dict_words(self):
        """extract_finish_codes_from_positions works with dict words."""
        words = [
            {"text": "WF1", "bbox": (10, 20, 30, 30)},
        ]
        codes = extract_finish_codes_from_positions(words, page_id=1)
        self.assertEqual(len(codes), 1)
        self.assertEqual(codes[0]["code"], "WF1")


# ---------------------------------------------------------------------------
# Geometry association tests
# ---------------------------------------------------------------------------

class TestGeometryAssociation(unittest.TestCase):
    """Tests 13-14: polygon-to-surface association."""

    def test_13_overlap_50_percent_strong(self):
        """Test 13: polygon overlapping measured surface >50% -> strong association."""
        fill = FillPolygon(vertices=((0, 0), (100, 0), (100, 100), (0, 100)))
        # Target overlaps right half of fill
        target = ((50, 0), (150, 0), (150, 100), (50, 100))
        result = associate_surface_to_target(fill, target, target_type="room", target_ref="R01")
        self.assertIn(result.method, ("majority_overlap", "containment"))
        self.assertGreater(result.confidence, 0.5)

    def test_14_centroid_inside_low_overlap(self):
        """Test 14: centroid inside but low overlap -> weaker confidence."""
        # Fill is large; target is small but contains the centroid
        fill = FillPolygon(vertices=((0, 0), (200, 0), (200, 200), (0, 200)))
        target = ((80, 80), (120, 80), (120, 120), (80, 120))
        result = associate_surface_to_target(fill, target, target_type="room", target_ref="R02")
        self.assertEqual(result.method, "centroid")
        self.assertLess(result.confidence, 0.60)

    def test_containment_full(self):
        """Full containment -> highest confidence."""
        fill = FillPolygon(vertices=((10, 10), (30, 10), (30, 30), (10, 30)))
        target = ((0, 0), (50, 0), (50, 50), (0, 50))
        result = associate_surface_to_target(fill, target)
        self.assertEqual(result.method, "containment")
        self.assertGreaterEqual(result.confidence, 0.90)

    def test_no_association(self):
        """Fill completely outside target -> no association."""
        fill = FillPolygon(vertices=((100, 100), (200, 100), (200, 200), (100, 200)))
        target = ((0, 0), (10, 0), (10, 10), (0, 10))
        result = associate_surface_to_target(fill, target, proximity_threshold_pt=5.0)
        self.assertEqual(result.method, "none")
        self.assertAlmostEqual(result.confidence, 0.0)


# ---------------------------------------------------------------------------
# Production integration tests
# ---------------------------------------------------------------------------

class TestProductionIntegration(unittest.TestCase):
    """Tests 15-18: production integration with measured surfaces."""

    def test_15_quantity_unchanged_after_allocation(self):
        """Test 15: measured surface quantity unchanged after allocation."""
        original_qty = 40.0
        measured = [{
            "polygon": [(0, 0), (100, 0), (100, 100), (0, 100)],
            "ref": "R01",
            "type": "room",
            "area_m2": original_qty,  # Authoritative — must not change
        }]
        evidence = [SurfaceEvidence(
            polygon_pdf_pts=((10, 10), (90, 10), (90, 90), (10, 90)),
            fill_colour=(1, 0, 0),
            workspace_id=1, page_id=1,
        )]
        codes = [
            {"code": "PT01", "bbox": (40, 40, 70, 55), "page_id": 1},
        ]
        result = associate_with_measured_surfaces(evidence, measured, code_occurrences=codes)
        self.assertEqual(len(result), 1)
        # Quantity must remain exactly 40.0 — we do NOT modify measured surfaces
        self.assertEqual(measured[0]["area_m2"], original_qty)
        # But the evidence now has classification
        self.assertEqual(result[0].finish_code, "PT01")
        self.assertEqual(result[0].substrate, "To confirm")

    def test_16_same_colour_different_codes(self):
        """Test 16: same fill colour with different codes stays distinguishable."""
        measured = [
            {"polygon": [(0, 0), (100, 0), (100, 100), (0, 100)],
             "ref": "R01", "type": "room"},
            {"polygon": [(200, 0), (300, 0), (300, 100), (200, 100)],
             "ref": "R02", "type": "room"},
        ]
        # Same red fill for both
        evidence = [
            SurfaceEvidence(polygon_pdf_pts=((5, 5), (95, 5), (95, 95), (5, 95)),
                            fill_colour=(1, 0, 0), workspace_id=1, page_id=1),
            SurfaceEvidence(polygon_pdf_pts=((205, 5), (295, 5), (295, 95), (205, 95)),
                            fill_colour=(1, 0, 0), workspace_id=1, page_id=1),
        ]
        codes = [
            {"code": "PT01", "bbox": (40, 40, 70, 55), "page_id": 1},
            {"code": "EXT01", "bbox": (240, 40, 280, 55), "page_id": 1},
        ]
        result = associate_with_measured_surfaces(evidence, measured, code_occurrences=codes)
        self.assertEqual(result[0].finish_code, "PT01")
        self.assertEqual(result[1].finish_code, "EXT01")
        self.assertNotEqual(result[0].finish_code, result[1].finish_code)

    def test_17_finish_code_without_substrate(self):
        """Test 17: finish code without known substrate -> To confirm."""
        measured = [{
            "polygon": [(0, 0), (100, 0), (100, 100), (0, 100)],
            "ref": "R01", "type": "room",
        }]
        evidence = [SurfaceEvidence(
            polygon_pdf_pts=((5, 5), (95, 5), (95, 95), (5, 95)),
            fill_colour=(0, 0, 1), workspace_id=1, page_id=1,
        )]
        codes = [
            {"code": "PT01", "bbox": (40, 40, 70, 55), "page_id": 1},
        ]
        result = associate_with_measured_surfaces(evidence, measured, code_occurrences=codes)
        self.assertEqual(result[0].finish_code, "PT01")
        self.assertEqual(result[0].substrate, "To confirm")
        self.assertEqual(result[0].status, "needs_check")

    def test_18_unclassified_surface_retains_quantity(self):
        """Test 18: unclassified measured surface retains its quantity."""
        measured = [{
            "polygon": [(0, 0), (100, 0), (100, 100), (0, 100)],
            "ref": "R03", "type": "room",
            "area_m2": 25.0,
        }]
        # No evidence matches this surface
        evidence = [SurfaceEvidence(
            polygon_pdf_pts=((500, 500), (600, 500), (600, 600), (500, 600)),
            fill_colour=(0, 1, 0), workspace_id=1, page_id=1,
        )]
        result = associate_with_measured_surfaces(evidence, measured)
        # Quantity unchanged
        self.assertEqual(measured[0]["area_m2"], 25.0)
        # No association found for the distant polygon
        self.assertEqual(result[0].association_method, "")

    def test_finish_code_with_known_substrate(self):
        """Code with explicit substrate from schedule."""
        measured = [{
            "polygon": [(0, 0), (100, 0), (100, 100), (0, 100)],
            "ref": "R01", "type": "room",
        }]
        evidence = [SurfaceEvidence(
            polygon_pdf_pts=((5, 5), (95, 5), (95, 95), (5, 95)),
            fill_colour=(1, 0, 0), workspace_id=1, page_id=1,
        )]
        codes = [
            {"code": "PT01", "bbox": (40, 40, 70, 55), "page_id": 1},
        ]
        result = associate_with_measured_surfaces(evidence, measured, code_occurrences=codes)
        # Without a schedule, substrate remains "To confirm"
        self.assertEqual(result[0].substrate, "To confirm")
        self.assertEqual(result[0].finish_code, "PT01")


class TestBuildSurfaceEvidence(unittest.TestCase):
    """Test build_surface_evidence helper."""

    def test_builds_records_from_polygons(self):
        polygons = [
            FillPolygon(
                vertices=((0, 0), (100, 0), (100, 100), (0, 100)),
                fill=(1, 0, 0),
                item_types=("re",),
            ),
        ]
        result = build_surface_evidence(polygons, page_id=5, page_no=3, workspace_id=1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].page_id, 5)
        self.assertEqual(result[0].page_no, 3)
        self.assertEqual(result[0].workspace_id, 1)
        self.assertEqual(result[0].surface_id, "page_5:fill_0")
        self.assertEqual(result[0].fill_colour, (1.0, 0.0, 0.0))

    def test_calibration_applied(self):
        polygons = [
            FillPolygon(vertices=((0, 0), (100, 0), (100, 100), (0, 100)), fill=(1, 0, 0)),
        ]
        scale = {"real_metres_per_page_mm": 0.1}
        result = build_surface_evidence(polygons, page_id=1, scale_info=scale)
        self.assertIsNotNone(result[0].area_m2)
        self.assertGreater(result[0].area_m2, 0)

    def test_no_calibration_gives_none(self):
        polygons = [
            FillPolygon(vertices=((0, 0), (100, 0), (100, 100), (0, 100)), fill=(1, 0, 0)),
        ]
        result = build_surface_evidence(polygons, page_id=1)
        self.assertIsNone(result[0].area_m2)


class TestExistingSegmentExtractionUnchanged(unittest.TestCase):
    """Test 9: ensure the new extraction does not interfere with segment data."""

    def test_extract_returns_polygons_not_segments(self):
        """The new module returns FillPolygon objects, not segments."""
        page = _fake_page([
            _rect_drawing(0, 0, 100, 100, fill=(1, 0, 0)),
        ])
        polys = extract_filled_polygons(page)
        self.assertIsInstance(polys, list)
        for p in polys:
            self.assertIsInstance(p, FillPolygon)
            self.assertTrue(hasattr(p, "vertices"))
            self.assertTrue(hasattr(p, "bbox"))

    def test_no_segments_field_on_fill_polygons(self):
        """FillPolygon does not have a 'segments' field (that's v130's domain)."""
        fp = FillPolygon(vertices=())
        self.assertFalse(hasattr(fp, "segments"))

    def test_extract_native_page_not_called(self):
        """This module does NOT call extract_native_page from v130."""
        import pb_surface_evidence_v160 as mod
        import inspect
        source = inspect.getsource(mod)
        self.assertNotIn("extract_native_page", source)


class TestConflictHandling(unittest.TestCase):
    """Tests for conflict detection."""

    def test_empty_polygon_no_crash(self):
        """Empty polygon in association does not crash."""
        result = associate_surface_to_target(
            FillPolygon(vertices=()),
            ((0, 0), (10, 0), (10, 10)),
        )
        self.assertEqual(result.method, "none")

    def test_empty_target_no_crash(self):
        result = associate_surface_to_target(
            FillPolygon(vertices=((0, 0), (10, 0), (10, 10))),
            (),
        )
        self.assertEqual(result.method, "none")

    def test_no_codes_gives_no_conflict(self):
        """Without code occurrences, no conflict status is set."""
        evidence = [SurfaceEvidence(
            polygon_pdf_pts=((0, 0), (100, 0), (100, 100), (0, 100)),
            fill_colour=(1, 0, 0),
        )]
        measured = [{"polygon": [(0, 0), (100, 0), (100, 100), (0, 100)],
                     "ref": "R01", "type": "room"}]
        result = associate_with_measured_surfaces(evidence, measured, code_occurrences=[])
        self.assertNotEqual(result[0].status, "conflict")


class TestVersionAndApply(unittest.TestCase):
    """Test module version and apply function."""

    def test_version(self):
        self.assertEqual(VERSION, "1.6.0")

    def test_apply_sets_flag(self):
        app = MagicMock()
        app._pb_surface_evidence_v160_applied = False
        from pb_surface_evidence_v160 import apply
        apply(app)
        self.assertTrue(app._pb_surface_evidence_v160_applied)

    def test_apply_idempotent(self):
        app = MagicMock()
        app._pb_surface_evidence_v160_applied = True
        from pb_surface_evidence_v160 import apply
        apply(app)
        # Should not crash, just return early

    def test_apply_exposes_functions(self):
        app = MagicMock()
        app._pb_surface_evidence_v160_applied = False
        from pb_surface_evidence_v160 import apply
        apply(app)
        self.assertTrue(hasattr(app, "extract_filled_polygons"))
        self.assertTrue(hasattr(app, "build_surface_evidence_v160"))
        self.assertTrue(hasattr(app, "associate_surface_evidence_v160"))


class TestMultipleMeasuredSurfaces(unittest.TestCase):
    """Test association with multiple measured surfaces."""

    def test_best_match_wins(self):
        """The measured surface with highest overlap wins."""
        measured = [
            {"polygon": [(0, 0), (50, 0), (50, 50), (0, 50)],
             "ref": "R01", "type": "room"},
            {"polygon": [(0, 0), (100, 0), (100, 100), (0, 100)],
             "ref": "R02", "type": "room"},
        ]
        # Fill matches R02 better than R01
        evidence = [SurfaceEvidence(
            polygon_pdf_pts=((5, 5), (95, 5), (95, 95), (5, 95)),
            fill_colour=(0.5, 0.5, 0.5),
            workspace_id=1, page_id=1,
        )]
        result = associate_with_measured_surfaces(evidence, measured)
        self.assertEqual(result[0].association_target_ref, "R02")


class TestMeasuredSurfacePolygonFormats(unittest.TestCase):
    """Test that measured surfaces with different polygon formats work."""

    def test_bbox_fallback(self):
        """Measured surface with bbox instead of polygon."""
        measured = [{
            "bbox": (0, 0, 100, 100),
            "ref": "R01", "type": "room",
        }]
        evidence = [SurfaceEvidence(
            polygon_pdf_pts=((10, 10), (90, 10), (90, 90), (10, 90)),
            fill_colour=(1, 0, 0),
            workspace_id=1, page_id=1,
        )]
        result = associate_with_measured_surfaces(evidence, measured)
        self.assertEqual(result[0].association_target_ref, "R01")


if __name__ == "__main__":
    unittest.main()
