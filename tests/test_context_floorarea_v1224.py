from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

import pb_context_floorarea_v1224 as patch
import pb_material_schedule_v1222 as material


class _App:
    @staticmethod
    def lquery(_sql, _params=()):
        return []


class ContextFloorAreaTests(unittest.TestCase):
    def test_pt01_key_plan_wall_pieces_is_not_a_finish_code(self):
        text = "FINISH LEGEND\nPT01 KEY PLAN WALL PIECES\nEC01 Lineaboard Cladding"
        items = patch.parse_schedule_text(text, 7, "A210 KEY PLAN", base_parser=material.parse_schedule_text)
        codes = {item["code"] for item in items}
        self.assertNotIn("PT01", codes)
        self.assertIn("EC01", codes)

    def test_real_pt01_paint_finish_is_kept(self):
        text = "PAINT SCHEDULE\nPT01 Dulux Namadji low sheen\nPT02 Dulux Vivid White semi gloss"
        items = patch.parse_schedule_text(text, 8, "A601 FINISHES", base_parser=material.parse_schedule_text)
        by_code = {item["code"]: item for item in items}
        self.assertIn("PT01", by_code)
        self.assertIn("PT02", by_code)
        self.assertIn("Dulux Namadji", by_code["PT01"]["description"])

    def test_unknown_pt_tag_occurrence_is_ignored_but_confirmed_finish_survives(self):
        items = [
            {"code": "PT01", "text": "PT01 KEY PLAN WALL PIECES", "status": "Unknown"},
            {"code": "PT02", "text": "PT02", "status": "Confirmed"},
            {"code": "EC01", "text": "EC01", "status": "Unknown"},
        ]
        dictionary = {"PT02": {"status": "Confirmed", "finish": "Dulux White"}}
        result = patch.filter_page_occurrences(items, dictionary)
        codes = [item["code"] for item in result]
        self.assertNotIn("PT01", codes)
        self.assertIn("PT02", codes)
        self.assertIn("EC01", codes)

    def test_documented_internal_floor_area_without_unit_label_is_found(self):
        page = {"id": 4, "page_no": 4, "page_label": "A104 LEVEL 05 FLOOR PLAN", "page_type": "Floor Plan"}
        text = "LEVEL 05 FLOOR PLAN\nINTERNAL FLOOR AREA 86.40 m²\nBALCONY AREA 12.50 m²"
        result = patch.documented_internal_area_candidates(text, page)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["label"], "Level 5")
        self.assertAlmostEqual(result[0]["area_m2"], 86.4)
        self.assertEqual(result[0]["confidence"], "Documented")

    def test_external_and_balcony_area_are_not_used_as_internal_m2(self):
        page = {"id": 4, "page_no": 4, "page_label": "GROUND FLOOR", "page_type": "Floor Plan"}
        text = "BALCONY FLOOR AREA 22.0 m²\nEXTERNAL AREA 31.0 m²\nROOF AREA 140.0 m²"
        self.assertEqual(patch.documented_internal_area_candidates(text, page), [])

    def test_calibrated_closed_floor_boundary_produces_provisional_internal_area(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "floor.png"
            image = Image.new("L", (1200, 800), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((100, 100, 900, 600), outline="black", width=5)
            image.save(path)
            page = {
                "id": 9,
                "page_no": 9,
                "page_label": "GROUND FLOOR PLAN",
                "page_type": "Floor Plan",
                "extracted_text": "GROUND FLOOR PLAN",
                "px_per_m": 100.0,
                "image_path": str(path),
            }
            result = patch.geometry_internal_area_candidate(_App(), page)
            self.assertIsNotNone(result)
            self.assertEqual(result["label"], "Ground Floor")
            self.assertEqual(result["confidence"], "Derived")
            self.assertGreater(result["area_m2"], 35.0)
            self.assertLess(result["area_m2"], 45.0)

    def test_unit_row_builder_adds_documented_internal_area_fallback(self):
        page = {
            "id": 12,
            "page_no": 12,
            "page_label": "A212 LEVEL 03 FLOOR PLAN",
            "page_type": "Floor Plan",
            "extracted_text": "INTERNAL AREA 92.75 m²\nBALCONY 18.20 m²",
            "px_per_m": 0.0,
            "image_path": "",
        }

        def base_units(_app, _workspace_id, _pages):
            return [], []

        rows, summary = patch.extend_unit_rows(base_units, _App(), 77, [page])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(summary), 1)
        self.assertEqual(rows[0][0], 77)
        self.assertEqual(rows[0][1], "Internal")
        self.assertEqual(rows[0][2], "Floor area")
        self.assertEqual(rows[0][3], "Level 3")
        self.assertAlmostEqual(rows[0][6], 92.75)
        self.assertEqual(rows[0][8], "Measured")
        self.assertEqual(rows[0][18], "floor_area")


if __name__ == "__main__":
    unittest.main()
