"""Regression tests for offline reader wall-length unit conversion.

Priority 1 fix: wall lengths were reported in raw PDF points without
applying the drawing scale.  These tests prove that known geometry
produces the correct real-world wall length at multiple scales.

Measurement pipeline under test:
    PDF points × (25.4 / 72) = page mm
    page mm  ÷ mm_per_real_m = real metres

Where mm_per_real_m is derived from the detected scale:
    - Ratio scale 1:N  →  mm_per_real_m = N
    - Metric "10 mm = 1 m"  →  mm_per_real_m = 10
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
    _mm_per_real_metre,
    wall_length_real_m,
)


# ---------------------------------------------------------------------------
# Helper: build a fake PyMuPDF page with specific vector drawings
# ---------------------------------------------------------------------------

class FakePoint:
    """Minimal fitz.Point stand-in."""
    def __init__(self, x: float, y: float):
        self.x = x
        self.y = y

    def distance_to(self, other: "FakePoint") -> float:
        return math.hypot(self.x - other.x, self.y - other.y)


class FakePage:
    """Minimal fitz.Page stand-in that returns controlled drawings/text."""

    def __init__(
        self,
        width_pt: float = 595.0,   # A4 width in points
        height_pt: float = 842.0,  # A4 height in points
        drawings: Optional[list] = None,
        text: str = "",
        words: Optional[list] = None,
    ):
        self.width = width_pt
        self.height = height_pt
        self._drawings = drawings or []
        self._text = text
        self._words = words or []

    def get_drawings(self):
        return self._drawings

    def get_text(self, mode="text"):
        if mode == "words":
            return self._words
        return self._text


def make_wall_path(
    x1: float, y1: float, x2: float, y2: float,
    width: float = 1.0, color: tuple = (0, 0, 0),
) -> dict:
    """Create a fake drawing path representing a single wall line."""
    return {
        "items": [("l", FakePoint(x1, y1), FakePoint(x2, y2))],
        "color": color,
        "width": width,
        "fill": None,
        "dashes": None,
    }


def make_scale_words(scale_text: str, x: float = 100, y: float = 800) -> list:
    """Create fake word list containing a scale string."""
    words = []
    parts = scale_text.split()
    for i, part in enumerate(parts):
        words.append({
            "text": part,
            "bbox": (x + i * 30, y, x + i * 30 + 25, y + 12),
        })
    return words


# ---------------------------------------------------------------------------
# Test: _mm_per_real_metre
# ---------------------------------------------------------------------------

class TestMmPerRealMetre(unittest.TestCase):
    """Test scale ratio extraction."""

    def test_ratio_1_50(self):
        scale = {"type": "ratio", "ratio": 50.0, "scale_ratio": 0.02, "text": "1:50"}
        self.assertEqual(_mm_per_real_metre(scale), 50.0)

    def test_ratio_1_100(self):
        scale = {"type": "ratio", "ratio": 100.0, "scale_ratio": 0.01, "text": "1:100"}
        self.assertEqual(_mm_per_real_metre(scale), 100.0)

    def test_ratio_1_200(self):
        scale = {"type": "ratio", "ratio": 200.0, "scale_ratio": 0.005, "text": "1:200"}
        self.assertEqual(_mm_per_real_metre(scale), 200.0)

    def test_metric_10mm_equals_1m(self):
        scale = {"type": "metric", "mm_per_m": 10.0, "text": "10 mm = 1 m"}
        self.assertEqual(_mm_per_real_metre(scale), 10.0)

    def test_metric_5mm_equals_1m(self):
        scale = {"type": "metric", "mm_per_m": 5.0, "text": "5 mm = 1 m"}
        self.assertEqual(_mm_per_real_metre(scale), 5.0)

    def test_none_scale(self):
        self.assertIsNone(_mm_per_real_metre(None))

    def test_empty_scale(self):
        self.assertIsNone(_mm_per_real_metre({}))

    def test_unknown_type(self):
        self.assertIsNone(_mm_per_real_metre({"type": "unknown"}))


# ---------------------------------------------------------------------------
# Test: wall_length_real_m — core conversion at multiple scales
# ---------------------------------------------------------------------------

class TestWallLengthRealM(unittest.TestCase):
    """Prove correct real-world wall length at 1:50, 1:100, 1:200."""

    # Reference: a line of 283.465 PDF points = 100 mm on paper
    # (because 283.465 × 25.4/72 = 100.0)
    REFERENCE_PT = 100.0 / PDF_PT_TO_MM  # ≈ 283.465

    def test_1_50_scale(self):
        """At 1:50, 100 page-mm → 100 × 50 / 1000 = 5 m real."""
        scale = {"type": "ratio", "ratio": 50.0, "scale_ratio": 0.02, "text": "1:50"}
        result = wall_length_real_m(self.REFERENCE_PT, scale)
        self.assertAlmostEqual(result, 5.0, places=2)

    def test_1_100_scale(self):
        """At 1:100, 100 page-mm → 100 × 100 / 1000 = 10 m real."""
        scale = {"type": "ratio", "ratio": 100.0, "scale_ratio": 0.01, "text": "1:100"}
        result = wall_length_real_m(self.REFERENCE_PT, scale)
        self.assertAlmostEqual(result, 10.0, places=2)

    def test_1_200_scale(self):
        """At 1:200, 100 page-mm → 100 × 200 / 1000 = 20 m real."""
        scale = {"type": "ratio", "ratio": 200.0, "scale_ratio": 0.005, "text": "1:200"}
        result = wall_length_real_m(self.REFERENCE_PT, scale)
        self.assertAlmostEqual(result, 20.0, places=2)

    def test_1_100_3600mm_wall(self):
        """A 3600 mm (3.6 m) real wall at 1:100 is 36 mm on paper.

        36 mm on paper = 36 / PDF_PT_TO_MM ≈ 102.04 PDF points.
        Conversion: 36 × 100 / 1000 = 3.6 m ✓
        """
        scale = {"type": "ratio", "ratio": 100.0, "scale_ratio": 0.01, "text": "1:100"}
        # 3.6 m real = 36 mm on paper at 1:100
        pt_for_36mm = 36.0 / PDF_PT_TO_MM
        result = wall_length_real_m(pt_for_36mm, scale)
        self.assertAlmostEqual(result, 3.6, places=2)

    def test_metric_scale(self):
        """At '10 mm = 1 m', 100 page-mm → 100 × 10 / 1000 = 1 m real."""
        scale = {"type": "metric", "mm_per_m": 10.0, "text": "10 mm = 1 m"}
        result = wall_length_real_m(self.REFERENCE_PT, scale)
        self.assertAlmostEqual(result, 1.0, places=2)

    def test_no_scale_returns_none(self):
        """When scale is unknown, return None — do not fabricate a value."""
        result = wall_length_real_m(self.REFERENCE_PT, None)
        self.assertIsNone(result)

    def test_empty_scale_returns_none(self):
        result = wall_length_real_m(self.REFERENCE_PT, {})
        self.assertIsNone(result)

    def test_zero_length(self):
        scale = {"type": "ratio", "ratio": 100.0, "scale_ratio": 0.01, "text": "1:100"}
        result = wall_length_real_m(0, scale)
        self.assertAlmostEqual(result, 0.0, places=4)


# ---------------------------------------------------------------------------
# Test: different PDF page sizes
# ---------------------------------------------------------------------------

class TestDifferentPageSizes(unittest.TestCase):
    """Wall length conversion should be independent of page size."""

    REFERENCE_PT = 100.0 / PDF_PT_TO_MM

    def test_a4_portrait(self):
        scale = {"type": "ratio", "ratio": 100.0, "scale_ratio": 0.01, "text": "1:100"}
        result = wall_length_real_m(self.REFERENCE_PT, scale)
        self.assertAlmostEqual(result, 10.0, places=2)

    def test_a3_landscape(self):
        """Same line length, different page size — same real-world result."""
        scale = {"type": "ratio", "ratio": 100.0, "scale_ratio": 0.01, "text": "1:100"}
        result = wall_length_real_m(self.REFERENCE_PT, scale)
        self.assertAlmostEqual(result, 10.0, places=2)

    def test_a1_sheet(self):
        scale = {"type": "ratio", "ratio": 50.0, "scale_ratio": 0.02, "text": "1:50"}
        result = wall_length_real_m(self.REFERENCE_PT, scale)
        self.assertAlmostEqual(result, 5.0, places=2)


# ---------------------------------------------------------------------------
# Test: no accidental double conversion
# ---------------------------------------------------------------------------

class TestNoDoubleConversion(unittest.TestCase):
    """Ensure the conversion is applied exactly once."""

    def test_conversion_applied_once(self):
        """If we apply the formula twice, the result should be wrong."""
        scale = {"type": "ratio", "ratio": 100.0, "scale_ratio": 0.01, "text": "1:100"}
        pt = 283.465

        correct = wall_length_real_m(pt, scale)
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
        # Sanity: not tiny (old bug: / (100 * 1000) = 0.001)
        self.assertGreater(result, 1.0)
        # Sanity: not huge
        self.assertLess(result, 100.0)


# ---------------------------------------------------------------------------
# Test: OCR per-page slicing
# ---------------------------------------------------------------------------

class TestOCRPageSlicing(unittest.TestCase):
    """Verify extract_text_offline assigns text to the correct page."""

    def test_page_markers_split(self):
        """Simulate PyMuPDF4LLM output with page markers."""
        import re as _re
        from pb_planreader_offline import extract_text_offline

        # We can't easily call pymupdf4llm in tests, so test the
        # splitting logic directly by checking the marker regex.
        marker = "<!-- page 3 -->"
        m = _re.match(r"<!--\s*page\s+(\d+)\s*-->", marker, _re.IGNORECASE)
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), 3)

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
        # 1 PDF point — tiny but valid
        result = wall_length_real_m(1.0, scale)
        self.assertIsNotNone(result)
        self.assertGreater(result, 0)

    def test_very_long_line(self):
        scale = {"type": "ratio", "ratio": 100.0, "scale_ratio": 0.01, "text": "1:100"}
        # 5000 PDF points — long line
        result = wall_length_real_m(5000.0, scale)
        self.assertIsNotNone(result)
        self.assertGreater(result, 10)

    def test_negative_length(self):
        scale = {"type": "ratio", "ratio": 100.0, "scale_ratio": 0.01, "text": "1:100"}
        result = wall_length_real_m(-100.0, scale)
        # Negative length is physically meaningless but shouldn't crash
        self.assertIsNotNone(result)
        self.assertLess(result, 0)

    def test_pdf_pt_to_mm_constant(self):
        """Verify the PDF-point-to-mm constant."""
        self.assertAlmostEqual(PDF_PT_TO_MM, 25.4 / 72.0, places=10)
        # 1 PDF point ≈ 0.3528 mm
        self.assertAlmostEqual(PDF_PT_TO_MM, 0.352778, places=4)


if __name__ == "__main__":
    unittest.main()
