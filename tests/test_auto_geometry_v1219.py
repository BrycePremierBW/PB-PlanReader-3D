from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pb_auto_geometry_v1219 as auto
import pb_auto_geometry_guard_v1219 as guard


class _DBApp:
    def __init__(self, path: Path):
        self.path = str(path)

    def local_connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def lquery(self, sql, params=()):
        conn = self.local_connect()
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()


class AutoGeometryV1219Tests(unittest.TestCase):
    def test_page_relevance_keeps_paint_sources_and_discards_services(self):
        self.assertTrue(auto.page_relevance("Floor Plan", "", "A101")[0])
        self.assertTrue(auto.page_relevance("Elevation", "", "A301")[0])
        self.assertFalse(auto.page_relevance("Services", "mechanical layout", "M101")[0])
        self.assertFalse(auto.page_relevance("Structural", "footing details", "S201")[0])
        self.assertTrue(auto.page_relevance("Other", "External cladding and paint finishes", "A901")[0])
        self.assertFalse(auto.page_relevance("Roof Plan", "roof drainage layout", "A150")[0])
        self.assertTrue(auto.page_relevance("Roof Plan", "soffit and eave setout", "A151")[0])

    def test_auto_select_only_changes_selected_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE pages(id INTEGER PRIMARY KEY, document_id INTEGER, page_no INTEGER, page_label TEXT, page_type TEXT, extracted_text TEXT, selected INTEGER)"
            )
            conn.executemany(
                "INSERT INTO pages VALUES(?,?,?,?,?,?,?)",
                [
                    (1, 9, 1, "A101", "Floor Plan", "UNIT 1", 1),
                    (2, 9, 2, "M101", "Services", "mechanical", 1),
                    (3, 9, 3, "A301", "Elevation", "north elevation", 1),
                ],
            )
            conn.commit()
            conn.close()
            app = _DBApp(db)
            result = auto.auto_select_document_pages(app, 9)
            self.assertEqual(result["kept"], 2)
            self.assertEqual(result["discarded"], 1)
            flags = {row["id"]: row["selected"] for row in app.lquery("SELECT id,selected FROM pages ORDER BY id")}
            self.assertEqual(flags, {1: 1, 2: 0, 3: 1})
            self.assertEqual(len(app.lquery("SELECT id FROM pages")), 3)

    def test_dimension_value_supports_architectural_mm_and_m_units(self):
        self.assertAlmostEqual(auto._dimension_value_m("3600"), 3.6)
        self.assertAlmostEqual(auto._dimension_value_m("3600mm"), 3.6)
        self.assertAlmostEqual(auto._dimension_value_m("12.5m"), 12.5)
        self.assertIsNone(auto._dimension_value_m("1:100"))
        self.assertIsNone(auto._dimension_value_m("42"))

    def test_dimension_consensus_beats_single_outlier(self):
        chosen = auto.choose_dimension_calibration(
            [
                {"px_per_m": 85.0, "score": 4.0, "dimension_text": "3600"},
                {"px_per_m": 87.0, "score": 4.0, "dimension_text": "4200"},
                {"px_per_m": 220.0, "score": 7.0, "dimension_text": "900"},
            ],
            expected_px_per_m=86.0,
        )
        self.assertIsNotNone(chosen)
        self.assertGreaterEqual(chosen["consensus"], 2)
        self.assertTrue(84.0 <= chosen["px_per_m"] <= 88.0)
        self.assertEqual(chosen["confidence"], "High")

    def test_extracts_documented_unit_floor_areas(self):
        text = """
        UNIT 101\nInternal floor area 84.6 m²\n
        APARTMENT 102 92.30 m2\n
        VILLA A3\nGFA 118 sqm\n
        """
        rows = auto.extract_unit_area_candidates(text)
        by_label = {row["label"]: row["area_m2"] for row in rows}
        self.assertEqual(by_label["Unit 101"], 84.6)
        self.assertEqual(by_label["Unit 102"], 92.3)
        self.assertEqual(by_label["Unit A3"], 118.0)
        self.assertTrue(all(row["confidence"] == "Documented" for row in rows))

    def test_extracts_only_unambiguous_substrate_area_lines(self):
        text = """
        EC1 Lineaboard Cladding 145.8 m2\n
        Rendered blockwork 88.2 m²\n
        EC1 + EC2 combined 200 m2\n
        Windows 44 m2\n
        """
        rows = auto.extract_substrate_area_candidates(text)
        areas = sorted(row["area_m2"] for row in rows)
        self.assertEqual(areas, [88.2, 145.8])
        self.assertEqual({row["substrate"]["code"] for row in rows}, {"EC1", "RBL"})

    def test_elevation_face_mapping_is_stable(self):
        self.assertEqual(auto._page_face("NORTH ELEVATION"), "rear")
        self.assertEqual(auto._page_face("South Elevation"), "front")
        self.assertEqual(auto._page_face("EAST ELEV"), "right")
        self.assertEqual(auto._page_face("west elevation"), "left")
        self.assertEqual(auto._page_face("Elevation 1"), "")

    def test_manual_scale_change_outranks_previous_auto_calibration(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE workspace_settings(workspace_id INTEGER,key TEXT,value TEXT)")
            report = {
                "calibrations": [
                    {"page_id": 5, "method": "Dimension line", "px_per_m": 100.0, "confidence": "High"}
                ]
            }
            conn.execute(
                "INSERT INTO workspace_settings VALUES(?,?,?)",
                (2, auto.SETTING_KEY, json.dumps(report)),
            )
            conn.commit()
            conn.close()
            app = _DBApp(db)
            unchanged = {"id": 5, "workspace_id": 2, "px_per_m": 100.0, "scale_text": "Auto dimension 3600 · High confidence"}
            corrected = {"id": 5, "workspace_id": 2, "px_per_m": 104.0, "scale_text": "Auto dimension 3600 · High confidence"}
            self.assertFalse(guard.is_manual_calibration_override(app, unchanged))
            self.assertTrue(guard.is_manual_calibration_override(app, corrected))

    def test_manual_takeoff_rows_are_detected_as_precedence_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE takeoff_rows(workspace_id INTEGER,section TEXT,element TEXT,location TEXT,source_page TEXT,source_reference TEXT,unit TEXT,row_role TEXT)"
            )
            conn.executemany(
                "INSERT INTO takeoff_rows VALUES(?,?,?,?,?,?,?,?)",
                [
                    (3, "Internal", "Floor area", "Unit 101 floor area", "A101", "PB floor mapper v1.2.7", "m²", "floor_area"),
                    (3, "External", "External walls / cladding", "North EC1", "A301", "PB Takeoff Studio v1.2.11", "m²", "studio_area"),
                    (3, "Internal", "Ceiling", "Unit 101", "A101", "Manual", "m²", ""),
                    (3, "Internal", "Floor area", "Unit 999", "A101", auto.SOURCE_PREFIX + " · unit:Unit 999", "m²", "floor_area"),
                ],
            )
            conn.commit()
            conn.close()
            app = _DBApp(db)
            self.assertIn("unit 101", guard._manual_floor_keys(app, 3))
            self.assertNotIn("unit 999", guard._manual_floor_keys(app, 3))
            self.assertEqual(guard._manual_external_pages(app, 3), {"A301"})


if __name__ == "__main__":
    unittest.main()
