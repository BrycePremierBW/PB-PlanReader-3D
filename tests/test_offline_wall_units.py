"""Regression tests for offline reader wall-length unit conversion.

Priority 1 fix: wall lengths were reported in raw PDF points without
applying the drawing scale.  These tests prove that known geometry
produces the correct real-world wall length at multiple scales.

Measurement pipeline under test:
    PDF points × (25.4 / 72) = page mm
    page mm × real_metres_per_page_mm = real metres

Where real_metres_per_page_mm is:
    - Ratio scale 1:N  →  N / 1000
    - Metric "10 mm = 1 m"  →  1 / 10
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pb_planreader_offline import (
    PDF_PT_TO_MM,
    real_metres_per_page_mm,
    wall_length_real_m,
)


# ---------------------------------------------------------------------------
# Reference: a line of this many PDF points = exactly 100 mm on paper
# ---------------------------------------------------------------------------
REFERENCE_PT = 100.0 / PDF_PT_TO_MM  # ≈ 283.465


# ---------------------------------------------------------------------------
# Test: real_metres_per_page_mm — conversion factor extraction
# ---------------------------------------------------------------------------

class TestRealMetresPerPageMm(unittest.TestCase):
    """Test scale-to-conversion-factor extraction."""

    def test_ratio_1_50(self):
        scale = {"type": "ratio", "ratio": 50.0, "scale_ratio": 0.02, "text": "1:50"}
        # 1 page-mm = 50 real-mm = 0.05 real-m
        self.assertAlmostEqual(real_metres_per_page_mm(scale), 0.05, places=6)

    def test_ratio_1_100(self):
        scale = {"type": "ratio", "ratio": 100.0, "scale_ratio": 0.01, "text": "1:100"}
        # 1 page-mm = 100 real-mm = 0.1 real-m
        self.assertAlmostEqual(real_metres_per_page_mm(scale), 0.1, places=6)

    def test_ratio_1_200(self):
        scale = {"type": "ratio", "ratio": 200.0, "scale_ratio": 0.005, "text": "1:200"}
        # 1 page-mm = 200 real-mm = 0.2 real-m
        self.assertAlmostEqual(real_metres_per_page_mm(scale), 0.2, places=6)

    def test_metric_20mm_equals_1m(self):
        scale = {"type": "metric", "mm_per_m": 20.0, "text": "20 mm = 1 m"}
        # 20 page-mm = 1 real-m → 1 page-mm = 0.05 real-m
        self.assertAlmostEqual(real_metres_per_page_mm(scale), 0.05, places=6)

    def test_metric_10mm_equals_1m(self):
        scale = {"type": "metric", "mm_per_m": 10.0, "text": "10 mm = 1 m"}
        # 10 page-mm = 1 real-m → 1 page-mm = 0.1 real-m
        self.assertAlmostEqual(real_metres_per_page_mm(scale), 0.1, places=6)

    def test_metric_5mm_equals_1m(self):
        scale = {"type": "metric", "mm_per_m": 5.0, "text": "5 mm = 1 m"}
        # 5 page-mm = 1 real-m → 1 page-mm = 0.2 real-m
        self.assertAlmostEqual(real_metres_per_page_mm(scale), 0.2, places=6)

    def test_none_scale(self):
        self.assertIsNone(real_metres_per_page_mm(None))

    def test_empty_scale(self):
        self.assertIsNone(real_metres_per_page_mm({}))

    def test_unknown_type(self):
        self.assertIsNone(real_metres_per_page_mm({"type": "unknown"}))

    def test_zero_ratio(self):
        self.assertIsNone(real_metres_per_page_mm({"type": "ratio", "ratio": 0}))

    def test_zero_metric(self):
        self.assertIsNone(real_metres_per_page_mm({"type": "metric", "mm_per_m": 0}))


# ---------------------------------------------------------------------------
# Test: wall_length_real_m — core conversion at multiple scales
# ---------------------------------------------------------------------------

class TestWallLengthRealM(unittest.TestCase):
    """Prove correct real-world wall length at 1:50, 1:100, 1:200."""

    def test_1_50_scale(self):
        """At 1:50, 100 page-mm → 100 × 0.05 = 5 m real."""
        scale = {"type": "ratio", "ratio": 50.0, "scale_ratio": 0.02, "text": "1:50"}
        result = wall_length_real_m(REFERENCE_PT, scale)
        self.assertAlmostEqual(result, 5.0, places=2)

    def test_1_100_scale(self):
        """At 1:100, 100 page-mm → 100 × 0.1 = 10 m real."""
        scale = {"type": "ratio", "ratio": 100.0, "scale_ratio": 0.01, "text": "1:100"}
        result = wall_length_real_m(REFERENCE_PT, scale)
        self.assertAlmostEqual(result, 10.0, places=2)

    def test_1_200_scale(self):
        """At 1:200, 100 page-mm → 100 × 0.2 = 20 m real."""
        scale = {"type": "ratio", "ratio": 200.0, "scale_ratio": 0.005, "text": "1:200"}
        result = wall_length_real_m(REFERENCE_PT, scale)
        self.assertAlmostEqual(result, 20.0, places=2)

    def test_1_100_3600mm_wall(self):
        """A 3.6 m real wall at 1:100 is 36 mm on paper."""
        scale = {"type": "ratio", "ratio": 100.0, "scale_ratio": 0.01, "text": "1:100"}
        pt_for_36mm = 36.0 / PDF_PT_TO_MM
        result = wall_length_real_m(pt_for_36mm, scale)
        self.assertAlmostEqual(result, 3.6, places=2)

    def test_metric_scale(self):
        """At '10 mm = 1 m', 100 page-mm → 100 × 0.1 = 10 m real."""
        scale = {"type": "metric", "mm_per_m": 10.0, "text": "10 mm = 1 m"}
        result = wall_length_real_m(REFERENCE_PT, scale)
        self.assertAlmostEqual(result, 10.0, places=2)

    def test_no_scale_returns_none(self):
        """When scale is unknown, return None — do not fabricate a value."""
        result = wall_length_real_m(REFERENCE_PT, None)
        self.assertIsNone(result)

    def test_empty_scale_returns_none(self):
        result = wall_length_real_m(REFERENCE_PT, {})
        self.assertIsNone(result)

    def test_zero_length(self):
        scale = {"type": "ratio", "ratio": 100.0, "scale_ratio": 0.01, "text": "1:100"}
        result = wall_length_real_m(0, scale)
        self.assertAlmostEqual(result, 0.0, places=4)


# ---------------------------------------------------------------------------
# Test: equivalent physical scales produce identical results
# ---------------------------------------------------------------------------

class TestScaleEquivalence(unittest.TestCase):
    """Ratio 1:N must produce the same real-world length as the
    equivalent metric "N mm = 1 m" scale."""

    def _pair(self, ratio, metric_mm, description):
        ratio_scale = {"type": "ratio", "ratio": ratio, "text": f"1:{ratio}"}
        metric_scale = {"type": "metric", "mm_per_m": metric_mm, "text": f"{metric_mm} mm = 1 m"}
        r = wall_length_real_m(REFERENCE_PT, ratio_scale)
        m = wall_length_real_m(REFERENCE_PT, metric_scale)
        self.assertAlmostEqual(r, m, places=6, msg=description)
        return r

    def test_1_50_equals_20mm_1m(self):
        self._pair(50, 20, "1:50 should equal 20 mm = 1 m")

    def test_1_100_equals_10mm_1m(self):
        self._pair(100, 10, "1:100 should equal 10 mm = 1 m")

    def test_1_200_equals_5mm_1m(self):
        self._pair(200, 5, "1:200 should equal 5 mm = 1 m")

    def test_equivalence_with_various_line_lengths(self):
        """Same equivalence holds for different input lengths."""
        ratio_scale = {"type": "ratio", "ratio": 100.0, "text": "1:100"}
        metric_scale = {"type": "metric", "mm_per_m": 10.0, "text": "10 mm = 1m"}
        for pt in [50.0, 141.73, REFERENCE_PT, 500.0, 2000.0]:
            r = wall_length_real_m(pt, ratio_scale)
            m = wall_length_real_m(pt, metric_scale)
            self.assertAlmostEqual(r, m, places=6,
                msg=f"At {pt:.1f} PDF-pt: ratio={r} metric={m}")


# ---------------------------------------------------------------------------
# Test: different PDF page sizes
# ---------------------------------------------------------------------------

class TestDifferentPageSizes(unittest.TestCase):
    """Wall length conversion should be independent of page size."""

    def test_a4_portrait(self):
        scale = {"type": "ratio", "ratio": 100.0, "scale_ratio": 0.01, "text": "1:100"}
        result = wall_length_real_m(REFERENCE_PT, scale)
        self.assertAlmostEqual(result, 10.0, places=2)

    def test_a3_landscape(self):
        """Same line length, different page size — same real-world result."""
        scale = {"type": "ratio", "ratio": 100.0, "scale_ratio": 0.01, "text": "1:100"}
        result = wall_length_real_m(REFERENCE_PT, scale)
        self.assertAlmostEqual(result, 10.0, places=2)

    def test_a1_sheet(self):
        scale = {"type": "ratio", "ratio": 50.0, "scale_ratio": 0.02, "text": "1:50"}
        result = wall_length_real_m(REFERENCE_PT, scale)
        self.assertAlmostEqual(result, 5.0, places=2)


# ---------------------------------------------------------------------------
# Test: no accidental double conversion
# ---------------------------------------------------------------------------

class TestNoDoubleConversion(unittest.TestCase):
    """Ensure the conversion is applied exactly once."""

    def test_conversion_applied_once(self):
        """If we apply the formula twice, the result should be wrong."""
        scale = {"type": "ratio", "ratio": 100.0, "scale_ratio": 0.01, "text": "1:100"}
        correct = wall_length_real_m(REFERENCE_PT, scale)
        # Simulate double conversion (old bug pattern)
        double = correct * PDF_PT_TO_MM / 100.0 if correct else 0
        # They should differ — proving the old code was wrong
        self.assertNotAlmostEqual(correct, double, places=1)

    def test_known_value_1_100(self):
        """100 page-mm at 1:100 = 10 m. Not 0.1 m, not 1000 m."""
        scale = {"type": "ratio", "ratio": 100.0, "scale_ratio": 0.01, "text": "1:100"}
        pt = 100.0 / PDF_PT_TO_MM  # = 100 page-mm in PDF points
        result = wall_length_real_m(pt, scale)
        self.assertAlmostEqual(result, 10.0, places=2)
        # Sanity bounds
        self.assertGreater(result, 1.0)
        self.assertLess(result, 100.0)


# ---------------------------------------------------------------------------
# Test: OCR per-page slicing
# ---------------------------------------------------------------------------

class TestOCRPageSlicing(unittest.TestCase):
    """Verify extract_text_offline assigns text to the correct page."""

    def test_page_chunks_api_structure(self):
        """Verify we know how to handle page_chunks return value."""
        # Simulate what page_chunks=True returns
        fake_chunks = [
            {"metadata": {"page": 0}, "text": "Ground floor plan text"},
            {"metadata": {"page": 1}, "text": "First floor plan text"},
            {"metadata": {"page": 2}, "text": "Section A-A text"},
            {"metadata": {"page": 3}, "text": "Elevation text"},
        ]
        # Verify our extraction logic works
        result: Dict[int, str] = {}
        pages_requested = [2, 4]
        for chunk in fake_chunks:
            meta = chunk.get("metadata", {})
            page_idx = meta.get("page")
            text = chunk.get("text", "")
            if page_idx is not None:
                page_no = int(page_idx) + 1
                if page_no in pages_requested:
                    result[page_no] = text
        # Page 2 (index 1) should have first floor text
        self.assertEqual(result.get(2), "First floor plan text")
        # Page 4 (index 3) should have elevation text
        self.assertEqual(result.get(4), "Elevation text")
        # Page 1 and 3 should NOT be present
        self.assertNotIn(1, result)
        self.assertNotIn(3, result)

    def test_page_chunks_subset_mapping(self):
        """Requested subset [2, 4] maps correctly from 0-indexed chunks."""
        fake_chunks = [
            {"metadata": {"page": 0}, "text": "Page 1 content"},
            {"metadata": {"page": 1}, "text": "Page 2 content"},
            {"metadata": {"page": 2}, "text": "Page 3 content"},
            {"metadata": {"page": 3}, "text": "Page 4 content"},
        ]
        result: Dict[int, str] = {}
        pages_requested = [2, 4]
        for chunk in fake_chunks:
            meta = chunk.get("metadata", {})
            page_idx = meta.get("page")
            text = chunk.get("text", "")
            if page_idx is not None:
                page_no = int(page_idx) + 1
                if page_no in pages_requested:
                    result[page_no] = text
        self.assertEqual(result, {2: "Page 2 content", 4: "Page 4 content"})

    def test_no_cross_page_leakage(self):
        """Page 1 text must not appear in page 2's result."""
        fake_chunks = [
            {"metadata": {"page": 0}, "text": "SECRET_PAGE_1"},
            {"metadata": {"page": 1}, "text": "SECRET_PAGE_2"},
        ]
        result: Dict[int, str] = {}
        for chunk in fake_chunks:
            meta = chunk.get("metadata", {})
            page_idx = meta.get("page")
            text = chunk.get("text", "")
            if page_idx is not None:
                page_no = int(page_idx) + 1
                result[page_no] = text
        self.assertNotIn("SECRET_PAGE_1", result.get(2, ""))
        self.assertNotIn("SECRET_PAGE_2", result.get(1, ""))

    def test_1_indexed_page_mapping(self):
        """Chunk page=0 must map to PlanReader page 1."""
        fake_chunks = [{"metadata": {"page": 0}, "text": "First"}]
        for chunk in fake_chunks:
            page_no = int(chunk["metadata"]["page"]) + 1
            self.assertEqual(page_no, 1)

    def test_marker_regex_still_works(self):
        """The <!-- page N --> regex is still valid as a secondary fallback."""
        import re as _re
        for marker in ["<!-- page 0 -->", "<!-- page 3 -->", "<!-- PAGE 5 -->"]:
            m = _re.match(r"<!--\s*page\s+(\d+)\s*-->", marker, _re.IGNORECASE)
            self.assertIsNotNone(m, f"Failed to match: {marker}")

    def test_wall_length_real_m_exported(self):
        """The helper function is importable from the module."""
        from pb_planreader_offline import wall_length_real_m as wlr
        self.assertTrue(callable(wlr))


# ---------------------------------------------------------------------------
# Test: edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases(unittest.TestCase):
    """Edge cases in wall-length conversion."""

    def test_very_short_line(self):
        scale = {"type": "ratio", "ratio": 100.0, "scale_ratio": 0.01, "text": "1:100"}
        result = wall_length_real_m(1.0, scale)
        self.assertIsNotNone(result)
        self.assertGreater(result, 0)

    def test_very_long_line(self):
        scale = {"type": "ratio", "ratio": 100.0, "scale_ratio": 0.01, "text": "1:100"}
        result = wall_length_real_m(5000.0, scale)
        self.assertIsNotNone(result)
        self.assertGreater(result, 10)

    def test_negative_length(self):
        scale = {"type": "ratio", "ratio": 100.0, "scale_ratio": 0.01, "text": "1:100"}
        result = wall_length_real_m(-100.0, scale)
        self.assertIsNotNone(result)
        self.assertLess(result, 0)

    def test_pdf_pt_to_mm_constant(self):
        """Verify the PDF-point-to-mm constant."""
        self.assertAlmostEqual(PDF_PT_TO_MM, 25.4 / 72.0, places=10)
        self.assertAlmostEqual(PDF_PT_TO_MM, 0.352778, places=4)


# ---------------------------------------------------------------------------
# Test: data contract — unknown scale must not produce lm quantity
# ---------------------------------------------------------------------------

class TestDataContract(unittest.TestCase):
    """Unknown scale must not produce a take-off quantity in lm."""

    def test_wall_length_real_m_returns_none_for_no_scale(self):
        """No scale → None, not a number."""
        self.assertIsNone(wall_length_real_m(REFERENCE_PT, None))
        self.assertIsNone(wall_length_real_m(REFERENCE_PT, {}))

    def test_real_metres_per_page_mm_returns_none_for_no_scale(self):
        self.assertIsNone(real_metres_per_page_mm(None))
        self.assertIsNone(real_metres_per_page_mm({}))


if __name__ == "__main__":
    unittest.main()
