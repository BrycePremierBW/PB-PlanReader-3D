from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

import pb_auto_geometry_v1219 as auto
import pb_unit_floor_area_v1221 as unit


class _DBApp:
    def __init__(self, db: Path):
        self.db = str(db)

    def local_connect(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn

    def lquery(self, sql, params=()):
        conn = self.local_connect()
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()


class UnitFloorAreaV1221Tests(unittest.TestCase):
    def test_partition_plan_is_kept_for_internal_floor_area(self):
        self.assertTrue(unit.page_has_unit_plan_evidence("Other", "LAGO PARTITION PLAN - LEVEL 05", "A205"))
        base = lambda *_args: (False, "discard", 0)
        keep, reason, score = unit.enhanced_page_relevance(base)("Other", "Partition Plan - Level 05", "A205")
        self.assertTrue(keep)
        self.assertIn("floor-area", reason)
        self.assertEqual(score, 100)

    def test_documented_area_can_be_three_lines_after_unit_label(self):
        text = "UNIT 501\nTYPE A\n2 BEDROOM\n84.6 m²\nUNIT 502\nTYPE B\n91.2 m2"
        rows = unit.extract_unit_area_candidates(text)
        values = {row["label"]: row["area_m2"] for row in rows}
        self.assertEqual(values["Unit 501"], 84.6)
        self.assertEqual(values["Unit 502"], 91.2)

    def test_multi_unit_building_outline_does_not_cause_all_units_to_be_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "units.png"
            image = Image.new("L", (1000, 600), 255)
            draw = ImageDraw.Draw(image)
            # Whole building perimeter contains both unit labels.
            draw.rectangle((40, 40, 960, 560), outline=0, width=5)
            # Individual closed unit perimeters.
            draw.rectangle((80, 100, 460, 500), outline=0, width=5)
            draw.rectangle((540, 100, 920, 500), outline=0, width=5)
            image.save(path)

            page = {
                "id": 1,
                "page_type": "Floor Plan",
                "page_label": "Level 05 Partition Plan",
                "extracted_text": "UNIT 501 UNIT 502",
                "image_path": str(path),
                "px_per_m": 40.0,
            }

            original_words = auto._pdf_word_lines
            try:
                auto._pdf_word_lines = lambda _app, _page: [
                    {"text": "UNIT 501", "center": [270.0, 300.0], "bbox": [230, 285, 310, 315]},
                    {"text": "UNIT 502", "center": [730.0, 300.0], "bbox": [690, 285, 770, 315]},
                ]
                results = unit.bounded_unit_boundary_candidates(object(), page)
            finally:
                auto._pdf_word_lines = original_words

            self.assertEqual([item["label"] for item in results], ["Unit 501", "Unit 502"])
            self.assertTrue(all(80.0 <= item["area_m2"] <= 110.0 for item in results))

    def test_boundary_results_become_internal_floor_area_takeoff_rows(self):
        page = {
            "id": 7,
            "page_type": "Partition Plan",
            "page_label": "Level 05",
            "extracted_text": "UNIT 501",
        }
        original_boundary = auto._unit_boundary_candidates
        original_extract = auto.extract_unit_area_candidates
        try:
            auto._unit_boundary_candidates = lambda _app, _page: [
                {"label": "Unit 501", "area_m2": 84.6, "confidence": "Derived", "source": "test"}
            ]
            auto.extract_unit_area_candidates = lambda _text: []
            rows, summary = auto._build_unit_rows(object(), 4, [page])
        finally:
            auto._unit_boundary_candidates = original_boundary
            auto.extract_unit_area_candidates = original_extract

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row[1], "Internal")
        self.assertEqual(row[2], "Floor area")
        self.assertEqual(row[3], "Unit 501")
        self.assertEqual(row[6], 84.6)
        self.assertEqual(row[18], "floor_area")
        self.assertEqual(len(summary), 1)

    def test_recover_previous_auto_discarded_partition_sheet(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE pages(id INTEGER PRIMARY KEY,workspace_id INTEGER,page_type TEXT,page_label TEXT,extracted_text TEXT,selected INTEGER)"
            )
            conn.executemany(
                "INSERT INTO pages VALUES(?,?,?,?,?,?)",
                [
                    (1, 4, "Other", "A205", "PARTITION PLAN LEVEL 05 UNIT 501 UNIT 502", 0),
                    (2, 4, "Services", "M201", "mechanical services", 0),
                ],
            )
            conn.commit()
            conn.close()
            app = _DBApp(db)
            count = unit.restore_obvious_unit_plan_pages(app, 4)
            self.assertEqual(count, 1)
            flags = {row["id"]: row["selected"] for row in app.lquery("SELECT id,selected FROM pages ORDER BY id")}
            self.assertEqual(flags, {1: 1, 2: 0})


if __name__ == "__main__":
    unittest.main()
