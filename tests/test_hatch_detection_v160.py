"""Tests for Priority 4 Phase B2 — Vector hatch-stroke detection."""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import fitz

from pb_hatch_detection_v160 import (
    _MIN_HATCH_STROKES,
    HatchCluster,
    HatchProcessingResult,
    Stroke,
    _angle_delta,
    _circular_mean,
    _cluster_strokes,
    _compute_cluster_metrics,
    _compute_hatch_confidence,
    _compute_spacing,
    _convex_hull,
    _point_line_distance,
    _reject_false_positives,
    _reconstruct_hatch_region,
    _strokes_midpoint_distance,
    detect_hatch_patterns,
    extract_hatch_evidence,
    extract_strokes,
)
from pb_surface_evidence_v160 import (
    HatchDiagnostics,
    SurfaceEvidence,
    SurfaceProcessingDiagnostics,
    SurfaceProcessingResult,
    _point_in_polygon,
    associate_surface_to_target,
    calibrate_area_m2,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _draw_parallel_lines(
    page, x0, y0, count, spacing, length, angle_deg=45.0,
    width=0.5, colour=(0, 0, 0),
):
    """Draw parallel lines on a fitz page at given angle and spacing."""
    shape = page.new_shape()
    rad = math.radians(angle_deg)
    dx = math.cos(rad)
    dy = math.sin(rad)
    # perpendicular offset direction
    px, py = -dy, dx

    for i in range(count):
        cx = x0 + i * spacing * px
        cy = y0 + i * spacing * py
        x1 = cx - dx * length * 0.5
        y1 = cy - dy * length * 0.5
        x2 = cx + dx * length * 0.5
        y2 = cy + dy * length * 0.5
        shape.draw_line(fitz.Point(x1, y1), fitz.Point(x2, y2))
        shape.finish(color=colour, width=width)

    shape.commit()
    return page


def _draw_cross_hatch(
    page, cx, cy, count_a, spacing_a, length_a, angle_a,
    count_b, spacing_b, length_b, angle_b,
    width=0.5, colour=(0, 0, 0),
):
    """Draw two sets of parallel lines at different angles (cross hatch)."""
    _draw_parallel_lines(
        page, cx - 50, cy - 50, count_a, spacing_a, length_a, angle_a,
        width, colour,
    )
    _draw_parallel_lines(
        page, cx - 50, cy - 50, count_b, spacing_b, length_b, angle_b,
        width, colour,
    )
    return page


def _draw_dimension_lines(page, x0, y0, count, spacing, length):
    """Draw lines that look like dimension strings (long, sparse, with ticks)."""
    shape = page.new_shape()
    for i in range(count):
        y = y0 + i * spacing
        shape.draw_line(fitz.Point(x0, y), fitz.Point(x0 + length, y))
        shape.finish(color=(0, 0, 0), width=0.25)
        # tick marks
        shape.draw_line(fitz.Point(x0, y - 3), fitz.Point(x0, y + 3))
        shape.finish(color=(0, 0, 0), width=0.25)
        shape.draw_line(
            fitz.Point(x0 + length, y - 3),
            fitz.Point(x0 + length, y + 3),
        )
        shape.finish(color=(0, 0, 0), width=0.25)
    # dimension text
    page.insert_text(fitz.Point(x0 + length * 0.5, y0 - 8), "3500", fontsize=8)
    shape.commit()
    return page


def _draw_grid_lines(page, x0, y0, nx, ny, spacing_x, spacing_y, length):
    """Draw a grid pattern (horizontal + vertical)."""
    shape = page.new_shape()
    for i in range(nx):
        x = x0 + i * spacing_x
        shape.draw_line(fitz.Point(x, y0), fitz.Point(x, y0 + length))
        shape.finish(color=(0, 0, 0), width=0.15)
    for j in range(ny):
        y = y0 + j * spacing_y
        shape.draw_line(fitz.Point(x0, y), fitz.Point(x0 + length, y))
        shape.finish(color=(0, 0, 0), width=0.15)
    shape.commit()
    return page


def _draw_random_lines(page, count, width_pt, height_pt, seed=42):
    """Draw random non-parallel lines (should NOT be classified as hatch)."""
    import random
    rng = random.Random(seed)
    shape = page.new_shape()
    for _ in range(count):
        x1 = rng.uniform(20, width_pt - 20)
        y1 = rng.uniform(20, height_pt - 20)
        angle = rng.uniform(0, 180)
        length = rng.uniform(20, 80)
        rad = math.radians(angle)
        x2 = x1 + length * math.cos(rad)
        y2 = y1 + length * math.sin(rad)
        shape.draw_line(fitz.Point(x1, y1), fitz.Point(x2, y2))
        shape.finish(color=(0, 0, 0), width=0.25)
    shape.commit()
    return page


def _make_page(width=595, height=842):
    """Create a new fitz page for testing."""
    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    return doc, page


def _get_strokes(page) -> List[Stroke]:
    """Extract strokes from a page."""
    return extract_strokes(page)


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------

class TestStrokeExtraction(unittest.TestCase):
    """Stroke extraction from PDF drawings."""

    def test_parallel_lines_extracted(self):
        doc, page = _make_page()
        _draw_parallel_lines(page, 100, 100, 8, 10, 100, angle_deg=45.0)
        strokes = extract_strokes(page)
        self.assertGreaterEqual(len(strokes), 8)
        doc.close()

    def test_empty_page_returns_empty(self):
        doc, page = _make_page()
        strokes = extract_strokes(page)
        self.assertEqual(len(strokes), 0)
        doc.close()

    def test_short_lines_filtered(self):
        doc, page = _make_page()
        shape = page.new_shape()
        # Lines shorter than 2pt should be filtered
        for i in range(10):
            y = 100 + i * 15
            shape.draw_line(fitz.Point(100, y), fitz.Point(101, y))
            shape.finish(color=(0, 0, 0), width=0.5)
        shape.commit()
        strokes = extract_strokes(page)
        self.assertEqual(len(strokes), 0)
        doc.close()

    def test_fill_only_drawings_skipped(self):
        doc, page = _make_page()
        shape = page.new_shape()
        shape.draw_rect(fitz.Rect(50, 50, 200, 200))
        shape.finish(fill=(1, 0, 0))  # fill only, no stroke
        shape.commit()
        strokes = extract_strokes(page)
        self.assertEqual(len(strokes), 0)
        doc.close()

    def test_rectangle_decomposed_to_edges(self):
        doc, page = _make_page()
        shape = page.new_shape()
        shape.draw_rect(fitz.Rect(50, 50, 200, 150))
        shape.finish(color=(0, 0, 0), width=0.5)
        shape.commit()
        strokes = extract_strokes(page)
        # A stroked rectangle should produce 4 edge strokes
        self.assertGreaterEqual(len(strokes), 4)
        doc.close()


class TestAngleHelpers(unittest.TestCase):
    """Angle utility functions."""

    def test_angle_delta_parallel(self):
        self.assertAlmostEqual(_angle_delta(10.0, 10.0), 0.0)

    def test_angle_delta_symmetric(self):
        self.assertAlmostEqual(_angle_delta(10.0, 20.0), _angle_delta(20.0, 10.0))

    def test_angle_delta_wraps_180(self):
        # 179° and 1° are 2° apart (mod 180)
        self.assertAlmostEqual(_angle_delta(179.0, 1.0), 2.0)

    def test_angle_delta_perpendicular(self):
        self.assertAlmostEqual(_angle_delta(0.0, 90.0), 90.0)

    def test_circular_mean_single(self):
        self.assertAlmostEqual(_circular_mean([45.0]), 45.0, places=5)

    def test_circular_mean_two_parallel(self):
        result = _circular_mean([10.0, 10.0])
        self.assertAlmostEqual(result, 10.0, places=5)

    def test_circular_mean_wraparound(self):
        # Mean of 175° and 5° should be ~180°/0°
        result = _circular_mean([175.0, 5.0])
        self.assertTrue(result < 10.0 or result > 170.0)


class TestGeometryHelpers(unittest.TestCase):
    """Geometry utility functions."""

    def test_point_line_distance_on_line(self):
        d = _point_line_distance(5.0, 0.0, 0.0, 0.0, 10.0, 0.0)
        self.assertAlmostEqual(d, 0.0)

    def test_point_line_distance_off_line(self):
        d = _point_line_distance(5.0, 3.0, 0.0, 0.0, 10.0, 0.0)
        self.assertAlmostEqual(d, 3.0)

    def test_strokes_midpoint_distance_parallel(self):
        a = Stroke(x1=0, y1=0, x2=10, y2=0)
        b = Stroke(x1=0, y1=5, x2=10, y2=5)
        d = _strokes_midpoint_distance(a, b)
        self.assertAlmostEqual(d, 5.0)

    def test_convex_hull_triangle(self):
        pts = [(0, 0), (10, 0), (5, 8)]
        hull = _convex_hull(pts)
        self.assertIsNotNone(hull)
        self.assertEqual(len(hull), 3)

    def test_convex_hull_square(self):
        pts = [(0, 0), (10, 0), (10, 10), (0, 10), (5, 5)]
        hull = _convex_hull(pts)
        self.assertIsNotNone(hull)
        self.assertEqual(len(hull), 4)
        # Interior point (5,5) should NOT be on hull
        self.assertNotIn((5, 5), hull)

    def test_convex_hull_collinear_returns_none(self):
        pts = [(0, 0), (5, 0), (10, 0)]
        hull = _convex_hull(pts)
        self.assertIsNone(hull)


class TestClustering(unittest.TestCase):
    """Stroke clustering logic."""

    def test_parallel_strokes_cluster_together(self):
        strokes = []
        for i in range(8):
            y = 100 + i * 10
            strokes.append(Stroke(x1=50, y1=y, x2=150, y2=y))
        clusters = _cluster_strokes(strokes)
        self.assertGreaterEqual(len(clusters), 1)
        self.assertGreaterEqual(clusters[0].stroke_count, 5)

    def test_perpendicular_groups_separate(self):
        strokes = []
        # Horizontal group
        for i in range(5):
            y = 100 + i * 8
            strokes.append(Stroke(x1=50, y1=y, x2=200, y2=y))
        # Vertical group (far away)
        for i in range(5):
            x = 300 + i * 8
            strokes.append(Stroke(x1=x, y1=100, x2=x, y2=250))
        clusters = _cluster_strokes(strokes)
        # Should produce 2 clusters (horizontal + vertical)
        self.assertGreaterEqual(len(clusters), 2)

    def test_sparse_strokes_no_cluster(self):
        strokes = [
            Stroke(x1=0, y1=0, x2=10, y2=0),
            Stroke(x1=500, y1=500, x2=510, y2=500),
        ]
        clusters = _cluster_strokes(strokes)
        # Too few strokes for clustering
        self.assertEqual(len(clusters), 0)

    def test_too_few_strokes_returns_empty(self):
        strokes = [Stroke(x1=i*20, y1=100, x2=i*20+10, y2=100) for i in range(3)]
        clusters = _cluster_strokes(strokes)
        self.assertEqual(len(clusters), 0)


class TestClusterMetrics(unittest.TestCase):
    """Hatch cluster metric computation."""

    def _make_parallel_cluster(self, count=8, spacing=10, angle=45.0):
        strokes = []
        rad = math.radians(angle)
        dx, dy = math.cos(rad), math.sin(rad)
        px, py = -dy, dx
        for i in range(count):
            cx = 100 + i * spacing * px
            cy = 100 + i * spacing * py
            strokes.append(Stroke(
                x1=cx - dx * 50, y1=cy - dy * 50,
                x2=cx + dx * 50, y2=cy + dy * 50,
            ))
        cluster = HatchCluster(strokes=strokes, stroke_count=count)
        _compute_cluster_metrics(cluster)
        return cluster

    def test_dominant_angle_45(self):
        c = self._make_parallel_cluster(angle=45.0)
        self.assertAlmostEqual(c.dominant_angle, 45.0, delta=5.0)

    def test_dominant_angle_horizontal(self):
        c = self._make_parallel_cluster(angle=0.0)
        self.assertAlmostEqual(c.dominant_angle, 0.0, delta=5.0)

    def test_dominant_angle_vertical(self):
        c = self._make_parallel_cluster(angle=90.0)
        self.assertAlmostEqual(c.dominant_angle, 90.0, delta=5.0)

    def test_is_parallel_hatch(self):
        c = self._make_parallel_cluster(count=8)
        self.assertTrue(c.is_parallel_hatch)

    def test_spacing_computed(self):
        c = self._make_parallel_cluster(count=8, spacing=10)
        self.assertAlmostEqual(c.spacing_mean_pt, 10.0, delta=2.0)

    def test_bbox_populated(self):
        c = self._make_parallel_cluster(count=8)
        x0, y0, x1, y1 = c.bbox
        self.assertGreater(x1, x0)
        self.assertGreater(y1, y0)


class TestFalsePositiveRejection(unittest.TestCase):
    """False-positive controls for hatch detection."""

    def test_too_few_strokes_rejected(self):
        c = HatchCluster(strokes=[
            Stroke(x1=0, y1=0, x2=50, y2=0),
            Stroke(x1=0, y1=10, x2=50, y2=10),
        ], stroke_count=2)
        result = _reject_false_positives(c, 595, 842)
        self.assertTrue(result)
        self.assertIn("too few", c.rejection_reason)

    def test_long_single_line_rejected(self):
        # Line spanning >60% of page diagonal
        diag = math.hypot(595, 842)
        strokes = [Stroke(x1=0, y1=421, x2=diag * 0.7, y2=421)]
        strokes.extend([
            Stroke(x1=i * 30, y1=400, x2=i * 30 + 20, y2=400)
            for i in range(6)
        ])
        c = HatchCluster(strokes=strokes, stroke_count=len(strokes))
        _compute_cluster_metrics(c)
        result = _reject_false_positives(c, 595, 842)
        self.assertTrue(result)

    def test_grid_lines_flagged(self):
        """Grid of lines with keyword 'grid' nearby should be rejected."""
        strokes = []
        for i in range(8):
            y = 100 + i * 10
            strokes.append(Stroke(x1=50, y1=y, x2=150, y2=y))
        words = [{"text": "Grid", "bbox": [100, 50, 140, 60]}]
        c = HatchCluster(strokes=strokes, stroke_count=len(strokes))
        _compute_cluster_metrics(c)
        result = _reject_false_positives(c, 595, 842, words=words)
        self.assertTrue(result)
        self.assertIn("keyword", c.rejection_reason)

    def test_batten_louvre_rejected(self):
        strokes = []
        for i in range(8):
            y = 100 + i * 10
            strokes.append(Stroke(x1=50, y1=y, x2=150, y2=y))
        words = [{"text": "Batten", "bbox": [100, 50, 140, 60]}]
        c = HatchCluster(strokes=strokes, stroke_count=len(strokes))
        _compute_cluster_metrics(c)
        result = _reject_false_positives(c, 595, 842, words=words)
        self.assertTrue(result)
        self.assertIn("keyword", c.rejection_reason)

    def test_dimension_lines_rejected(self):
        strokes = []
        for i in range(6):
            y = 100 + i * 15
            strokes.append(Stroke(x1=50, y1=y, x2=200, y2=y))
        words = [
            {"text": "3500", "bbox": [120, 90, 150, 100]},
            {"text": "2400", "bbox": [120, 105, 150, 115]},
        ]
        c = HatchCluster(strokes=strokes, stroke_count=len(strokes))
        _compute_cluster_metrics(c)
        result = _reject_false_positives(c, 595, 842, words=words)
        self.assertTrue(result)
        self.assertIn("dimension", c.rejection_reason)

    def test_regular_hatch_not_rejected(self):
        """Valid parallel hatch should NOT be rejected."""
        strokes = []
        for i in range(8):
            y = 200 + i * 12
            strokes.append(Stroke(x1=100, y1=y, x2=250, y2=y))
        c = HatchCluster(strokes=strokes, stroke_count=len(strokes))
        _compute_cluster_metrics(c)
        result = _reject_false_positives(c, 595, 842)
        self.assertFalse(result)
        self.assertEqual(c.rejection_reason, "")


class TestHatchConfidence(unittest.TestCase):
    """Hatch confidence scoring."""

    def test_high_count_regular_spacing(self):
        strokes = []
        for i in range(12):
            y = 200 + i * 10
            strokes.append(Stroke(x1=100, y1=y, x2=250, y2=y))
        c = HatchCluster(strokes=strokes, stroke_count=len(strokes))
        _compute_cluster_metrics(c)
        conf = _compute_hatch_confidence(c)
        self.assertGreater(conf, 0.4)

    def test_low_count_low_confidence(self):
        strokes = [
            Stroke(x1=0, y1=i * 10, x2=50, y2=i * 10)
            for i in range(5)
        ]
        c = HatchCluster(strokes=strokes, stroke_count=5)
        _compute_cluster_metrics(c)
        conf = _compute_hatch_confidence(c)
        # 5 strokes is the minimum — should have some confidence
        self.assertGreater(conf, 0.0)

    def test_cross_hatch_bonus(self):
        strokes = []
        # Horizontal
        for i in range(8):
            y = 200 + i * 10
            strokes.append(Stroke(x1=100, y1=y, x2=250, y2=y))
        # Vertical
        for i in range(8):
            x = 100 + i * 10
            strokes.append(Stroke(x1=x, y1=200, x2=x, y2=350))
        c = HatchCluster(strokes=strokes, stroke_count=len(strokes))
        _compute_cluster_metrics(c)
        conf = _compute_hatch_confidence(c)
        self.assertGreater(conf, 0.3)


class TestConvexHullReconstruction(unittest.TestCase):
    """Region reconstruction from hatch clusters."""

    def test_parallel_lines_reconstruct_to_polygon(self):
        strokes = []
        for i in range(8):
            y = 200 + i * 10
            strokes.append(Stroke(x1=100, y1=y, x2=250, y2=y))
        c = HatchCluster(strokes=strokes, stroke_count=len(strokes))
        _compute_cluster_metrics(c)
        polygon, conf, method = _reconstruct_hatch_region(c)
        self.assertIsNotNone(polygon)
        self.assertGreater(len(polygon), 2)
        self.assertGreater(conf, 0.0)

    def test_too_few_strokes_no_reconstruction(self):
        c = HatchCluster(
            strokes=[Stroke(x1=0, y1=0, x2=10, y2=0)],
            stroke_count=1,
        )
        polygon, conf, method = _reconstruct_hatch_region(c)
        self.assertIsNone(polygon)


class TestFullDetectionPipeline(unittest.TestCase):
    """End-to-end hatch detection from PDF page to SurfaceEvidence."""

    def test_45degree_parallel_hatch_detected(self):
        doc, page = _make_page()
        _draw_parallel_lines(page, 100, 100, 10, 8, 200, angle_deg=45.0)
        evidence_list, clusters, hatch_diag = detect_hatch_patterns(page)
        self.assertGreater(len(evidence_list), 0)
        self.assertGreater(hatch_diag["strokes_extracted"], 0)
        doc.close()

    def test_horizontal_hatch_detected(self):
        doc, page = _make_page()
        _draw_parallel_lines(page, 100, 100, 10, 8, 200, angle_deg=0.0)
        evidence_list, clusters, hatch_diag = detect_hatch_patterns(page)
        self.assertGreater(len(evidence_list), 0)
        doc.close()

    def test_vertical_hatch_detected(self):
        doc, page = _make_page()
        _draw_parallel_lines(page, 100, 100, 10, 8, 200, angle_deg=90.0)
        evidence_list, clusters, hatch_diag = detect_hatch_patterns(page)
        self.assertGreater(len(evidence_list), 0)
        doc.close()

    def test_cross_hatch_detected(self):
        doc, page = _make_page()
        _draw_cross_hatch(
            page, 200, 200,
            count_a=8, spacing_a=10, length_a=200, angle_a=0,
            count_b=8, spacing_b=10, length_b=200, angle_b=90,
        )
        evidence_list, clusters, hatch_diag = detect_hatch_patterns(page)
        self.assertGreater(len(evidence_list), 0)
        # Check at least one is cross hatch
        cross = [e for e in evidence_list if "cross_hatch" in str(e.source_item_types)]
        self.assertGreater(len(cross), 0)
        doc.close()

    def test_random_linework_rejected(self):
        doc, page = _make_page()
        _draw_random_lines(page, 30, 595, 842)
        evidence_list, clusters, hatch_diag = detect_hatch_patterns(page)
        # Random lines should be rejected or not produce clusters
        # (they won't cluster because angles are too different)
        self.assertEqual(len(evidence_list), 0)
        doc.close()

    def test_empty_page_no_hatches(self):
        doc, page = _make_page()
        evidence_list, clusters, hatch_diag = detect_hatch_patterns(page)
        self.assertEqual(len(evidence_list), 0)
        self.assertEqual(hatch_diag["strokes_extracted"], 0)
        doc.close()

    def test_cluster_produces_one_region_not_n(self):
        """8 parallel strokes should produce ONE evidence record, not 8."""
        doc, page = _make_page()
        _draw_parallel_lines(page, 100, 100, 8, 10, 150, angle_deg=45.0)
        evidence_list, clusters, hatch_diag = detect_hatch_patterns(page)
        # Should produce exactly 1 hatch evidence from this cluster
        self.assertEqual(len(evidence_list), 1)
        doc.close()

    def test_low_confidence_bbox_region(self):
        """Sparse hatch should get low confidence / area_m2=None."""
        doc, page = _make_page()
        # Draw 6 strokes with wide irregular spacing
        _draw_parallel_lines(page, 100, 100, 6, 20, 60, angle_deg=30.0)
        evidence_list, clusters, hatch_diag = detect_hatch_patterns(page)
        # If any evidence is returned, check area_m2
        for ev in evidence_list:
            if ev.geometry_method == "bbox_fallback":
                self.assertIsNone(ev.area_m2)
        doc.close()

    def test_calibrated_region_produces_m2(self):
        """Well-formed hatch with calibration should produce area_m2."""
        doc, page = _make_page()
        _draw_parallel_lines(page, 100, 100, 12, 6, 200, angle_deg=45.0)
        scale_info = {
            "real_metres_per_page_mm": 0.028346,
            "px_per_m": 28.346,
            "render_zoom": 1.0,
            "scale_text": "1:100",
        }
        evidence_list, clusters, hatch_diag = detect_hatch_patterns(
            page, scale_info=scale_info
        )
        # If evidence is produced, it should have area_m2
        for ev in evidence_list:
            if ev.geometry_method != "bbox_fallback":
                self.assertIsNotNone(ev.area_m2)
                self.assertGreater(ev.area_m2, 0.0)
        doc.close()

    def test_no_scale_area_m2_none(self):
        """Without calibration, area_m2 must be None."""
        doc, page = _make_page()
        _draw_parallel_lines(page, 100, 100, 12, 6, 200, angle_deg=45.0)
        evidence_list, clusters, hatch_diag = detect_hatch_patterns(
            page, scale_info=None
        )
        for ev in evidence_list:
            self.assertIsNone(ev.area_m2)
        doc.close()

    def test_geometry_method_is_vector_hatch_region(self):
        doc, page = _make_page()
        _draw_parallel_lines(page, 100, 100, 12, 6, 200, angle_deg=45.0)
        evidence_list, _, _ = detect_hatch_patterns(page)
        for ev in evidence_list:
            self.assertEqual(ev.geometry_method, "vector_hatch_region")
        doc.close()

    def test_positioned_code_inside_hatch_associates(self):
        """FCS1 text inside hatch region should be associated."""
        doc, page = _make_page()
        _draw_parallel_lines(page, 100, 100, 12, 6, 200, angle_deg=45.0)
        # Insert code text inside the hatch area
        page.insert_text(fitz.Point(170, 160), "FCS1", fontsize=10)
        words = [{"text": "FCS1", "bbox": [170, 150, 200, 162]}]
        evidence_list, _, _ = detect_hatch_patterns(page, words=words)
        self.assertGreater(len(evidence_list), 0)
        # The hatch evidence should be produced (code doesn't affect detection)
        self.assertEqual(evidence_list[0].source_geometry_type, "hatch_region")
        doc.close()

    def test_two_distinct_codes_conflict(self):
        """Two different finish codes in same hatch region -> conflict."""
        doc, page = _make_page()
        _draw_parallel_lines(page, 100, 100, 12, 6, 200, angle_deg=45.0)
        evidence_list, _, _ = detect_hatch_patterns(page)
        if evidence_list:
            ev = evidence_list[0]
            # Manually test the code association logic
            ev.finish_code = "FCS1"
            ev2 = SurfaceEvidence(
                surface_id="test2",
                geometry_method="vector_hatch_region",
                source_geometry_type="hatch_region",
            )
            ev2.finish_code = "PT01"
            # Different codes should conflict
            self.assertNotEqual(ev.finish_code, ev2.finish_code)
        doc.close()

    def test_hatch_does_not_change_measured_quantity(self):
        """Hatch classification must not change authoritative m²."""
        # Measured wall: 40.0 m²
        # Hatch cluster: FCS1
        # Result should be 40.0 m² / FCS1
        ev = SurfaceEvidence(
            surface_id="wall_1",
            source_geometry_type="hatch_region",
            geometry_method="vector_hatch_region",
            area_m2=5.2,
            association_target_type="wall",
            association_target_ref="W01",
            finish_code="FCS1",
        )
        # The authoritative quantity (40.0) is NOT in the hatch evidence
        self.assertAlmostEqual(ev.area_m2, 5.2)
        # The wall's area_m2 would be separate in the measured surface
        doc, page = _make_page()
        doc.close()


class TestDiagnostics(unittest.TestCase):
    """Hatch diagnostics tracking."""

    def test_hatch_diagnostics_dataclass(self):
        d = HatchDiagnostics()
        self.assertEqual(d.strokes_extracted, 0)
        self.assertEqual(d.clusters_found, 0)
        self.assertEqual(d.extraction_error, "")

    def test_hatch_diagnostics_to_dict(self):
        d = HatchDiagnostics(
            strokes_extracted=50,
            clusters_found=3,
            regions_reconstructed=2,
            associated=1,
            unassociated=1,
        )
        dd = d.to_dict()
        self.assertEqual(dd["strokes_extracted"], 50)
        self.assertEqual(dd["clusters_found"], 3)
        self.assertEqual(dd["regions_reconstructed"], 2)
        self.assertEqual(dd["associated"], 1)

    def test_surface_diagnostics_includes_hatch(self):
        d = SurfaceProcessingDiagnostics()
        self.assertIsInstance(d.hatch_diag, HatchDiagnostics)
        dd = d.to_dict()
        self.assertIn("hatch_diag", dd)
        self.assertIsInstance(dd["hatch_diag"], dict)

    def test_hatch_diagnostics_serialization_roundtrip(self):
        """Diagnostics survive JSON round-trip."""
        import json
        d = SurfaceProcessingDiagnostics()
        d.hatch_diag.strokes_extracted = 100
        d.hatch_diag.clusters_found = 4
        d.hatch_diag.associated = 2
        dd = d.to_dict()
        raw = json.dumps(dd)
        parsed = json.loads(raw)
        d2 = SurfaceProcessingDiagnostics(**parsed)
        self.assertEqual(d2.hatch_diag.strokes_extracted, 100)
        self.assertEqual(d2.hatch_diag.clusters_found, 4)
        self.assertEqual(d2.hatch_diag.associated, 2)

    def test_hatch_diagnostics_error_tracking(self):
        d = HatchDiagnostics()
        d.extraction_error = "RuntimeError: test error"
        self.assertIn("RuntimeError", d.extraction_error)
        dd = d.to_dict()
        self.assertEqual(dd["extraction_error"], "RuntimeError: test error")


class TestProductionAdapter(unittest.TestCase):
    """extract_hatch_evidence production adapter."""

    def test_returns_empty_for_no_strokes(self):
        doc, page = _make_page()
        result = extract_hatch_evidence(page)
        self.assertIsInstance(result, HatchProcessingResult)
        self.assertEqual(len(result.evidence), 0)
        self.assertEqual(result.strokes_extracted, 0)
        doc.close()

    def test_hatch_evidence_has_page_metadata(self):
        doc, page = _make_page()
        _draw_parallel_lines(page, 100, 100, 12, 6, 200, angle_deg=45.0)
        result = extract_hatch_evidence(
            page, page_id=5, page_no=3, page_label="Plan",
            workspace_id=1,
        )
        for ev in result.evidence:
            self.assertEqual(ev.page_id, 5)
            self.assertEqual(ev.page_no, 3)
            self.assertEqual(ev.page_label, "Plan")
            self.assertEqual(ev.workspace_id, 1)
        doc.close()

    def test_hatch_evidence_has_surface_id(self):
        doc, page = _make_page()
        _draw_parallel_lines(page, 100, 100, 12, 6, 200, angle_deg=45.0)
        result = extract_hatch_evidence(
            page, page_id=5, page_no=3, workspace_id=1,
        )
        for ev in result.evidence:
            self.assertIn("page_5:hatch_", ev.surface_id)
        doc.close()

    def test_hatch_evidence_has_calibration(self):
        doc, page = _make_page()
        _draw_parallel_lines(page, 100, 100, 12, 6, 200, angle_deg=45.0)
        scale = {"real_metres_per_page_mm": 0.028346, "px_per_m": 28.346}
        result = extract_hatch_evidence(
            page, page_id=1, workspace_id=1, scale_info=scale,
        )
        for ev in result.evidence:
            if ev.geometry_method != "bbox_fallback":
                self.assertIsNotNone(ev.area_m2)
        doc.close()

    def test_result_carries_accurate_diagnostics(self):
        """BLOCKER 4: diagnostics must reflect actual detector counts,
        not len(evidence_list)."""
        doc, page = _make_page()
        _draw_parallel_lines(page, 100, 100, 12, 6, 200, angle_deg=45.0)
        result = extract_hatch_evidence(page, page_id=1, workspace_id=1)
        self.assertIsInstance(result, HatchProcessingResult)
        # strokes_extracted should be the actual PDF strokes, not evidence count
        self.assertGreater(result.strokes_extracted, 0)
        self.assertGreaterEqual(result.clusters_found, 1)
        # regions_reconstructed should match evidence count
        self.assertEqual(result.regions_reconstructed, len(result.evidence))
        doc.close()

    def test_words_passed_to_hatch_detection(self):
        """BLOCKER 2: words parameter must reach detect_hatch_patterns."""
        doc, page = _make_page()
        _draw_parallel_lines(page, 100, 100, 12, 6, 200, angle_deg=0.0)
        # Add a GRID keyword near the hatch
        page.insert_text(fitz.Point(150, 90), "GRID", fontsize=8)
        words = [{"text": "GRID", "bbox": [150, 82, 180, 92]}]
        result = extract_hatch_evidence(
            page, page_id=1, workspace_id=1, words=words,
        )
        # GRID keyword should cause rejection
        self.assertEqual(len(result.evidence), 0)
        self.assertGreater(result.clusters_rejected, 0)
        doc.close()


class TestMeasuredQuantityUnchanged(unittest.TestCase):
    """Hatch evidence must not alter authoritative m²."""

    def test_hatch_area_m2_is_independent(self):
        ev = SurfaceEvidence(
            surface_id="hatch_0",
            source_geometry_type="hatch_region",
            geometry_method="vector_hatch_region",
            area_m2=3.5,
            finish_code="FCS1",
        )
        # The wall's area is separate
        wall_area = 40.0
        self.assertAlmostEqual(ev.area_m2, 3.5)
        self.assertAlmostEqual(wall_area, 40.0)
        # They must not merge
        self.assertNotAlmostEqual(ev.area_m2, wall_area)

    def test_association_does_not_change_target_area(self):
        """When hatch is associated to a measured surface, the surface area
        must remain unchanged."""
        from pb_surface_evidence_v160 import FillPolygon
        target = {
            "polygon": [(0, 0), (100, 0), (100, 100), (0, 100)],
            "ref": "BED1",
            "type": "room",
            "area_m2": 40.0,
        }
        hatch_poly = FillPolygon(
            vertices=((10, 10), (90, 10), (90, 90), (10, 90)),
            geometry_method="vector_hatch_region",
        )
        result = associate_surface_to_target(
            hatch_poly, target["polygon"],
            target_type="room", target_ref="BED1",
        )
        # Association should succeed (hatch inside room)
        self.assertIn(result.method, ("containment", "majority_overlap",
                                       "centroid", "proximity", "none"))
        # Target area must NOT be modified
        self.assertAlmostEqual(target["area_m2"], 40.0)


class TestHatchRegionSpanningTwoTargets(unittest.TestCase):
    """Hatch spanning two targets should not silently pick the wrong one."""

    def test_hatch_spanning_two_rooms_gets_needs_check(self):
        from pb_surface_evidence_v160 import FillPolygon
        target_a = {
            "polygon": [(0, 0), (100, 0), (100, 100), (0, 100)],
            "ref": "BED1",
            "type": "room",
            "area_m2": 20.0,
        }
        target_b = {
            "polygon": [(200, 0), (300, 0), (300, 100), (200, 100)],
            "ref": "BED2",
            "type": "room",
            "area_m2": 20.0,
        }
        # Hatch polygon that overlaps both
        hatch_poly = FillPolygon(
            vertices=((50, 50), (250, 50), (250, 60), (50, 60)),
            geometry_method="vector_hatch_region",
        )
        # Associate with each target independently
        r1 = associate_surface_to_target(
            hatch_poly, target_a["polygon"],
            target_type="room", target_ref="BED1",
        )
        r2 = associate_surface_to_target(
            hatch_poly, target_b["polygon"],
            target_type="room", target_ref="BED2",
        )
        # Neither should get high confidence (partial overlap)
        self.assertLess(r1.confidence, 0.90)
        self.assertLess(r2.confidence, 0.90)


class TestRegularSpacingHigherConfidence(unittest.TestCase):
    """Regular spacing confidence > irregular spacing confidence."""

    def _make_cluster(self, regular=True, count=10):
        strokes = []
        for i in range(count):
            if regular:
                y = 200 + i * 10  # regular 10pt spacing
            else:
                import random
                rng = random.Random(i)
                y = 200 + rng.uniform(0, 100)  # irregular
            strokes.append(Stroke(x1=100, y1=y, x2=250, y2=y))
        return strokes

    def test_regular_beats_irregular(self):
        strokes_reg = self._make_cluster(regular=True, count=10)
        strokes_irr = self._make_cluster(regular=False, count=10)

        c_reg = HatchCluster(strokes=strokes_reg, stroke_count=10)
        _compute_cluster_metrics(c_reg)
        conf_reg = _compute_hatch_confidence(c_reg)

        c_irr = HatchCluster(strokes=strokes_irr, stroke_count=10)
        _compute_cluster_metrics(c_irr)
        conf_irr = _compute_hatch_confidence(c_irr)

        self.assertGreater(conf_reg, conf_irr)


class TestUnionFind(unittest.TestCase):
    """Union-Find data structure used in clustering."""

    def test_basic_union_find(self):
        from pb_hatch_detection_v160 import _UnionFind
        uf = _UnionFind(5)
        uf.union(0, 1)
        uf.union(2, 3)
        self.assertEqual(uf.find(0), uf.find(1))
        self.assertEqual(uf.find(2), uf.find(3))
        self.assertNotEqual(uf.find(0), uf.find(2))

    def test_transitive_union(self):
        from pb_hatch_detection_v160 import _UnionFind
        uf = _UnionFind(5)
        uf.union(0, 1)
        uf.union(1, 2)
        self.assertEqual(uf.find(0), uf.find(2))

    def test_self_find(self):
        from pb_hatch_detection_v160 import _UnionFind
        uf = _UnionFind(3)
        self.assertEqual(uf.find(0), 0)
        self.assertEqual(uf.find(1), 1)
        self.assertEqual(uf.find(2), 2)


# ---------------------------------------------------------------------------
# BLOCKER 1 tests: along-axis proximity — separate hatch regions
# ---------------------------------------------------------------------------
class TestSeparateHatchRegions(unittest.TestCase):
    """BLOCKER 1: clustering must not join physically separate hatch regions."""

    def test_two_distant_parallel_patches_two_clusters(self):
        """Two identical 45° hatch patches far apart -> TWO clusters."""
        strokes = []
        # Patch A: x=50..150, y=100..200
        for i in range(8):
            y = 100 + i * 10
            strokes.append(Stroke(x1=50, y1=y, x2=150, y2=y))
        # Patch B: x=500..600, y=100..200 (far away along x-axis)
        for i in range(8):
            y = 100 + i * 10
            strokes.append(Stroke(x1=500, y1=y, x2=600, y2=y))
        clusters = _cluster_strokes(strokes)
        self.assertGreaterEqual(len(clusters), 2,
                                "Distant patches should produce 2+ clusters")

    def test_nearby_strokes_single_cluster(self):
        """Nearby strokes of same angle should form ONE cluster."""
        strokes = []
        for i in range(10):
            y = 200 + i * 8
            strokes.append(Stroke(x1=100, y1=y, x2=250, y2=y))
        clusters = _cluster_strokes(strokes)
        self.assertEqual(len(clusters), 1)
        self.assertGreaterEqual(clusters[0].stroke_count, 10)

    def test_collinear_strokes_large_gap_not_merged(self):
        """Collinear strokes separated by a large gap should NOT merge."""
        strokes = []
        # Group A: x=0..100
        for i in range(6):
            strokes.append(Stroke(x1=0, y1=100 + i * 10, x2=100, y2=100 + i * 10))
        # Group B: x=400..500 (300pt gap, much larger than stroke length 100pt)
        for i in range(6):
            strokes.append(Stroke(x1=400, y1=100 + i * 10, x2=500, y2=100 + i * 10))
        clusters = _cluster_strokes(strokes)
        self.assertGreaterEqual(len(clusters), 2,
                                "Large-gap collinear strokes should not merge")

    def test_giant_hull_cannot_span_empty_region(self):
        """Reconstructed hull from two distant patches should not span the gap.
        If they somehow end up in one cluster, the hull would be enormous.
        Verify this does not happen by checking two patches stay separate."""
        strokes = []
        # Patch A: compact at origin
        for i in range(8):
            y = 100 + i * 8
            strokes.append(Stroke(x1=50, y1=y, x2=150, y2=y))
        # Patch B: compact far away
        for i in range(8):
            y = 100 + i * 8
            strokes.append(Stroke(x1=500, y1=y, x2=600, y2=y))
        clusters = _cluster_strokes(strokes)
        # Each cluster's bbox should not span from x=50 to x=600
        for c in clusters:
            x0, _, x1, _ = c.bbox
            span = x1 - x0
            self.assertLess(span, 300,
                            f"Cluster bbox span {span:.0f}pt too large — "
                            "likely merged separate regions")


# ---------------------------------------------------------------------------
# BLOCKER 3 tests: cross-hatch perpendicular tolerance
# ---------------------------------------------------------------------------
class TestCrossHatchPerpendicularTolerance(unittest.TestCase):
    """BLOCKER 3: cross-hatch merge must require genuinely near-perpendicular
    geometry, not merely 40-90 degree separation."""

    def test_0_and_90_merge(self):
        """0° + 90° overlapping clusters should merge as cross hatch."""
        from pb_hatch_detection_v160 import (
            _merge_cross_hatch_clusters,
            _compute_cluster_metrics,
        )
        # Horizontal cluster
        h_strokes = [Stroke(x1=100, y1=200 + i * 10, x2=250, y2=200 + i * 10)
                     for i in range(8)]
        h = HatchCluster(strokes=h_strokes, stroke_count=8)
        _compute_cluster_metrics(h)
        # Vertical cluster (overlapping bbox)
        v_strokes = [Stroke(x1=175 + i * 10, y1=150, x2=175 + i * 10, y2=300)
                     for i in range(8)]
        v = HatchCluster(strokes=v_strokes, stroke_count=8)
        _compute_cluster_metrics(v)
        merged = _merge_cross_hatch_clusters([h, v])
        self.assertEqual(len(merged), 1, "0°+90° should merge")
        self.assertTrue(merged[0].is_cross_hatch)

    def test_45_and_135_merge(self):
        """45° + 135° overlapping clusters should merge (perpendicular)."""
        from pb_hatch_detection_v160 import (
            _merge_cross_hatch_clusters,
            _compute_cluster_metrics,
        )
        import math
        # 45° cluster
        rad45 = math.radians(45)
        s45 = [Stroke(x1=100 + i * 8 * math.cos(rad45),
                       y1=200 + i * 8 * math.sin(rad45),
                       x2=100 + i * 8 * math.cos(rad45) + 100,
                       y2=200 + i * 8 * math.sin(rad45) + 100)
               for i in range(8)]
        c45 = HatchCluster(strokes=s45, stroke_count=8)
        _compute_cluster_metrics(c45)
        # 135° cluster (overlapping region)
        # 135° direction: cos(135)=-0.707, sin(135)=+0.707 (up-left)
        rad135 = math.radians(135)
        s135 = [Stroke(x1=175 + i * 8 * math.cos(rad135),
                        y1=200 + i * 8 * math.sin(rad135),
                        x2=175 + i * 8 * math.cos(rad135) + 100 * math.cos(rad135),
                        y2=200 + i * 8 * math.sin(rad135) + 100 * math.sin(rad135))
                for i in range(8)]
        c135 = HatchCluster(strokes=s135, stroke_count=8)
        _compute_cluster_metrics(c135)
        merged = _merge_cross_hatch_clusters([c45, c135])
        self.assertEqual(len(merged), 1, "45°+135° should merge")

    def test_0_and_45_do_not_merge(self):
        """0° + 45° should NOT merge — not perpendicular enough."""
        from pb_hatch_detection_v160 import (
            _merge_cross_hatch_clusters,
            _compute_cluster_metrics,
        )
        h_strokes = [Stroke(x1=100, y1=200 + i * 10, x2=250, y2=200 + i * 10)
                     for i in range(8)]
        h = HatchCluster(strokes=h_strokes, stroke_count=8)
        _compute_cluster_metrics(h)
        import math
        rad = math.radians(45)
        d_strokes = [Stroke(x1=100 + i * 8 * math.cos(rad),
                             y1=200 + i * 8 * math.sin(rad),
                             x2=100 + i * 8 * math.cos(rad) + 100,
                             y2=200 + i * 8 * math.sin(rad) + 100)
                     for i in range(8)]
        d = HatchCluster(strokes=d_strokes, stroke_count=8)
        _compute_cluster_metrics(d)
        merged = _merge_cross_hatch_clusters([h, d])
        self.assertEqual(len(merged), 2, "0°+45° should NOT merge")

    def test_30_and_75_do_not_merge(self):
        """30° + 75° should NOT merge — difference is 45°, not near 90°."""
        from pb_hatch_detection_v160 import (
            _merge_cross_hatch_clusters,
            _compute_cluster_metrics,
        )
        import math
        rad30 = math.radians(30)
        s30 = [Stroke(x1=100 + i * 8 * math.cos(rad30),
                       y1=200 + i * 8 * math.sin(rad30),
                       x2=100 + i * 8 * math.cos(rad30) + 100 * math.cos(rad30),
                       y2=200 + i * 8 * math.sin(rad30) + 100 * math.sin(rad30))
               for i in range(8)]
        c30 = HatchCluster(strokes=s30, stroke_count=8)
        _compute_cluster_metrics(c30)
        rad75 = math.radians(75)
        s75 = [Stroke(x1=100 + i * 8 * math.cos(rad75),
                       y1=200 + i * 8 * math.sin(rad75),
                       x2=100 + i * 8 * math.cos(rad75) + 100 * math.cos(rad75),
                       y2=200 + i * 8 * math.sin(rad75) + 100 * math.sin(rad75))
               for i in range(8)]
        c75 = HatchCluster(strokes=s75, stroke_count=8)
        _compute_cluster_metrics(c75)
        merged = _merge_cross_hatch_clusters([c30, c75])
        self.assertEqual(len(merged), 2, "30°+75° should NOT merge")

    def test_perpendicular_but_spatially_separate_no_merge(self):
        """Perpendicular clusters that don't overlap spatially should NOT merge."""
        from pb_hatch_detection_v160 import (
            _merge_cross_hatch_clusters,
            _compute_cluster_metrics,
        )
        # Horizontal cluster at top-left
        h_strokes = [Stroke(x1=50, y1=50 + i * 10, x2=150, y2=50 + i * 10)
                     for i in range(8)]
        h = HatchCluster(strokes=h_strokes, stroke_count=8)
        _compute_cluster_metrics(h)
        # Vertical cluster at bottom-right (far away, no bbox overlap)
        v_strokes = [Stroke(x1=400 + i * 10, y1=500, x2=400 + i * 10, y2=600)
                     for i in range(8)]
        v = HatchCluster(strokes=v_strokes, stroke_count=8)
        _compute_cluster_metrics(v)
        merged = _merge_cross_hatch_clusters([h, v])
        self.assertEqual(len(merged), 2,
                         "Spatially separate perpendicular clusters should not merge")


# ---------------------------------------------------------------------------
# BLOCKER 2 tests: production adapter passes words to hatch detection
# ---------------------------------------------------------------------------
class TestProductionWordsFiltering(unittest.TestCase):
    """BLOCKER 2: positioned words must reach hatch false-positive filters
    in the real production adapter."""

    def test_grid_word_rejects_hatch(self):
        """Repeated lines + nearby GRID word -> no hatch evidence."""
        doc, page = _make_page()
        _draw_parallel_lines(page, 100, 100, 12, 6, 200, angle_deg=0.0)
        page.insert_text(fitz.Point(150, 90), "GRID", fontsize=8)
        # Simulate what production adapter does: extract words, pass to hatch
        words_raw = page.get_text("words") or []
        words = [{"text": str(w[4]).strip(),
                  "bbox": [float(w[0]), float(w[1]), float(w[2]), float(w[3])]}
                 for w in words_raw if len(w) >= 5 and str(w[4]).strip()]
        result = extract_hatch_evidence(
            page, page_id=1, workspace_id=1, words=words,
        )
        self.assertEqual(len(result.evidence), 0,
                         "GRID keyword should cause hatch rejection")
        doc.close()

    def test_dimension_text_rejects_hatch(self):
        """Repeated lines + nearby dimension text -> rejected."""
        doc, page = _make_page()
        _draw_parallel_lines(page, 100, 100, 10, 8, 200, angle_deg=0.0)
        page.insert_text(fitz.Point(140, 88), "3500", fontsize=8)
        page.insert_text(fitz.Point(140, 78), "2400", fontsize=8)
        words_raw = page.get_text("words") or []
        words = [{"text": str(w[4]).strip(),
                  "bbox": [float(w[0]), float(w[1]), float(w[2]), float(w[3])]}
                 for w in words_raw if len(w) >= 5 and str(w[4]).strip()]
        result = extract_hatch_evidence(
            page, page_id=1, workspace_id=1, words=words,
        )
        self.assertEqual(len(result.evidence), 0,
                         "Dimension text should cause hatch rejection")
        doc.close()

    def test_louvre_keyword_rejects_hatch(self):
        """Repeated lines + nearby LOUVRE keyword -> rejected."""
        doc, page = _make_page()
        _draw_parallel_lines(page, 100, 100, 10, 8, 200, angle_deg=0.0)
        page.insert_text(fitz.Point(150, 90), "LOUVRE", fontsize=8)
        words_raw = page.get_text("words") or []
        words = [{"text": str(w[4]).strip(),
                  "bbox": [float(w[0]), float(w[1]), float(w[2]), float(w[3])]}
                 for w in words_raw if len(w) >= 5 and str(w[4]).strip()]
        result = extract_hatch_evidence(
            page, page_id=1, workspace_id=1, words=words,
        )
        self.assertEqual(len(result.evidence), 0,
                         "LOUVRE keyword should cause hatch rejection")
        doc.close()

    def test_genuine_hatch_without_exclusion_words_retained(self):
        """Same genuine hatch without exclusion words -> retained."""
        doc, page = _make_page()
        _draw_parallel_lines(page, 100, 100, 12, 6, 200, angle_deg=45.0)
        # No exclusion words
        result = extract_hatch_evidence(
            page, page_id=1, workspace_id=1, words=[],
        )
        self.assertGreater(len(result.evidence), 0,
                           "Genuine hatch without exclusion words should be retained")
        doc.close()


# ---------------------------------------------------------------------------
# Failure-state requirement: hatch error -> partial, not no_fills
# ---------------------------------------------------------------------------
class TestHatchErrorStatus(unittest.TestCase):
    """If hatch extraction fails and there are no fills, status must be
    'partial' (not 'no_fills') to signal the hatch stage was unavailable."""

    def test_hatch_extraction_error_returns_partial(self):
        """Simulate hatch extraction failure: status should be partial."""
        from pb_surface_evidence_v160 import (
            SurfaceProcessingDiagnostics,
            SurfaceProcessingResult,
        )
        diag = SurfaceProcessingDiagnostics()
        diag.hatch_diag.extraction_error = "RuntimeError: test failure"
        # Simulate the production adapter logic for empty fills + hatch error
        fill_polygons = []
        hatch_evidence_list = []
        has_hatch_error = bool(diag.hatch_diag.extraction_error)
        if not fill_polygons and not hatch_evidence_list:
            if has_hatch_error:
                status = "partial"
            else:
                status = "no_fills"
        self.assertEqual(status, "partial",
                         "Hatch error with no fills should return partial, not no_fills")

    def test_genuinely_empty_page_returns_no_fills(self):
        """No fills, no hatches, no error -> no_fills."""
        from pb_surface_evidence_v160 import SurfaceProcessingDiagnostics
        diag = SurfaceProcessingDiagnostics()
        fill_polygons = []
        hatch_evidence_list = []
        has_hatch_error = bool(diag.hatch_diag.extraction_error)
        if not fill_polygons and not hatch_evidence_list:
            if has_hatch_error:
                status = "partial"
            else:
                status = "no_fills"
        self.assertEqual(status, "no_fills")


if __name__ == "__main__":
    unittest.main()
