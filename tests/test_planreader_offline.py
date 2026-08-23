"""Tests for the offline plan reader (pb_planreader_offline.py).

Validates that the offline analysis pipeline works correctly
without requiring AI APIs or real PDF files.
"""

from __future__ import annotations

import math
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure repo root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pb_planreader_shared import (
    SCALE_RE,
    classify_page,
    detect_drawing_number,
    detect_scale_from_text,
    dimension_value_m,
    extract_scale_ratio,
)


class TestDimensionValueM(unittest.TestCase):
    """Test dimension parsing from text to metres."""

    def test_millimetres(self):
        self.assertAlmostEqual(dimension_value_m("3600mm"), 3.6)
        self.assertAlmostEqual(dimension_value_m("1200mm"), 1.2)
        self.assertAlmostEqual(dimension_value_m("2400mm"), 2.4)

    def test_metres(self):
        self.assertAlmostEqual(dimension_value_m("3.6m"), 3.6)
        self.assertAlmostEqual(dimension_value_m("1.2m"), 1.2)
        self.assertAlmostEqual(dimension_value_m("0.6m"), 0.6)

    def test_bare_numbers_as_mm(self):
        self.assertAlmostEqual(dimension_value_m("3600"), 3.6)
        self.assertAlmostEqual(dimension_value_m("1200"), 1.2)
        self.assertAlmostEqual(dimension_value_m("2400"), 2.4)

    def test_small_bare_numbers_rejected(self):
        self.assertIsNone(dimension_value_m("5"))
        self.assertIsNone(dimension_value_m("50"))
        self.assertIsNone(dimension_value_m("99"))

    def test_out_of_range(self):
        self.assertIsNone(dimension_value_m("250000mm"))  # 250m - too big
        self.assertIsNone(dimension_value_m("0.01m"))     # too small

    def test_whitespace_handling(self):
        self.assertAlmostEqual(dimension_value_m("3 600mm"), 3.6)
        self.assertAlmostEqual(dimension_value_m(" 1200 mm "), 1.2)

    def test_empty_or_none(self):
        self.assertIsNone(dimension_value_m(""))
        self.assertIsNone(dimension_value_m(None))
        self.assertIsNone(dimension_value_m("no dimensions here"))


class TestExtractScaleRatio(unittest.TestCase):
    """Test scale ratio extraction."""

    def test_standard_scales(self):
        self.assertEqual(extract_scale_ratio("1:100"), 100)
        self.assertEqual(extract_scale_ratio("1:50"), 50)
        self.assertEqual(extract_scale_ratio("1:200"), 200)
        self.assertEqual(extract_scale_ratio("1:500"), 500)
        self.assertEqual(extract_scale_ratio("1:1000"), 1000)

    def test_scale_in_context(self):
        self.assertEqual(extract_scale_ratio("Scale: 1:100"), 100)
        self.assertEqual(extract_scale_ratio("Drawing at 1:200 scale"), 200)

    def test_no_scale(self):
        self.assertIsNone(extract_scale_ratio("No scale here"))
        self.assertIsNone(extract_scale_ratio(""))
        self.assertIsNone(extract_scale_ratio(None))

    def test_case_insensitive(self):
        self.assertEqual(extract_scale_ratio("1:100"), 100)


class TestClassifyPage(unittest.TestCase):
    """Test page classification."""

    def test_floor_plan(self):
        self.assertEqual(classify_page("Ground Floor Plan"), "Floor Plan")
        self.assertEqual(classify_page("First Floor Plan Layout"), "Floor Plan")
        self.assertEqual(classify_page("Site Plan and Landscape"), "Floor Plan")

    def test_elevation(self):
        self.assertEqual(classify_page("North Elevation"), "Elevation")
        self.assertEqual(classify_page("External Elevation - Front"), "Elevation")
        self.assertEqual(classify_page("East Facade"), "Elevation")

    def test_section(self):
        self.assertEqual(classify_page("Section A-A"), "Section")
        self.assertEqual(classify_page("Building Cross Section"), "Section")
        self.assertEqual(classify_page("Wall Section Detail"), "Section")

    def test_schedule(self):
        self.assertEqual(classify_page("Door Schedule"), "Schedule")
        self.assertEqual(classify_page("Window Schedule and Details"), "Schedule")
        self.assertEqual(classify_page("Finishing Schedule"), "Schedule")

    def test_title_page(self):
        self.assertEqual(classify_page("Drawing Register"), "Title / Drawing Register")
        self.assertEqual(classify_page("Project Information and Title"), "Title / Drawing Register")

    def test_other(self):
        self.assertEqual(classify_page("Random notes about construction"), "Other")
        self.assertEqual(classify_page(""), "Other")


class TestDetectScaleFromText(unittest.TestCase):
    """Test scale detection with confidence."""

    def test_common_scale(self):
        result = detect_scale_from_text("Scale 1:100")
        self.assertIsNotNone(result)
        self.assertEqual(result["ratio"], 100)
        self.assertEqual(result["confidence"], "high")

    def test_uncommon_scale(self):
        result = detect_scale_from_text("Scale 1:75")
        self.assertIsNotNone(result)
        self.assertEqual(result["ratio"], 75)
        self.assertEqual(result["confidence"], "medium")

    def test_no_scale(self):
        self.assertIsNone(detect_scale_from_text("No scale here"))


class TestDetectDrawingNumber(unittest.TestCase):
    """Test drawing number extraction."""

    def test_standard_format(self):
        self.assertEqual(detect_drawing_number("Drawing A01"), "A01")
        self.assertEqual(detect_drawing_number("See DWG-A02-01"), "A02-01")
        self.assertEqual(detect_drawing_number("Ref: FP01"), "FP01")

    def test_no_drawing_number(self):
        self.assertIsNone(detect_drawing_number("No drawing number here"))
        self.assertIsNone(detect_drawing_number(""))


class TestScaleRegex(unittest.TestCase):
    """Test the shared scale regex pattern."""

    def test_matches(self):
        self.assertIsNotNone(SCALE_RE.search("1:100"))
        self.assertIsNotNone(SCALE_RE.search("1 : 200"))
        self.assertIsNotNone(SCALE_RE.search("Scale 1:50"))

    def test_no_match(self):
        self.assertIsNone(SCALE_RE.search("12345"))
        self.assertIsNone(SCALE_RE.search("no scale"))


if __name__ == "__main__":
    unittest.main()
