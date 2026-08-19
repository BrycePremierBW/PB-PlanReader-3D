from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pb_no_ai_takeoff_v1216 as no_ai


class _QueryApp:
    def __init__(self, zones=None, measurements=None):
        self.zones = list(zones or [])
        self.measurements = list(measurements or [])

    def lquery(self, sql, params=()):
        if "FROM mapped_zones" in sql:
            return [dict(row) for row in self.zones]
        if "FROM measurement_lines" in sql:
            return [dict(row) for row in self.measurements if row.get("takeoff_row_id") in (None, "", 0, "0")]
        return []


class _DBApp:
    def __init__(self, db_path: Path):
        self.db_path = str(db_path)
        self.connect_count = 0

    def local_connect(self):
        self.connect_count += 1
        return sqlite3.connect(self.db_path)

    @staticmethod
    def now_stamp():
        return "2026-08-19T16:50:00"


def _create_takeoff_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE takeoff_rows(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER, section TEXT, element TEXT, location TEXT,
            substrate TEXT, finish_system TEXT, quantity REAL, unit TEXT,
            quantity_status TEXT, source_page TEXT, source_reference TEXT,
            inclusion_status TEXT, coats REAL, coverage_m2_per_litre REAL,
            productivity_m2_per_hour REAL, rate_per_unit REAL, confidence TEXT,
            notes TEXT, row_role TEXT, created_at TEXT, updated_at TEXT
        )"""
    )
    conn.commit()


class NoAITakeoffTests(unittest.TestCase):
    def test_calibrated_floor_plan_zone_becomes_floor_area_reference(self):
        row = no_ai.zone_to_takeoff_row({
            "id": 4,
            "name": "Level 1 internal footprint",
            "view_type": "Floor Plan",
            "page_type": "Floor Plan",
            "page_label": "A101",
            "area_m2": 125.678,
            "px_per_m": 100,
            "substrate": "Plasterboard",
            "quantity_status": "Measured",
        })
        self.assertIsNotNone(row)
        self.assertEqual(row["section"], "Internal")
        self.assertEqual(row["element"], "Floor area")
        self.assertEqual(row["row_role"], "floor_area")
        self.assertEqual(row["quantity"], 125.68)
        self.assertEqual(row["quantity_status"], "Measured")
        self.assertEqual(row["confidence"], "Measured")
        self.assertEqual(row["rate_per_unit"], 0)
        self.assertEqual(row["coats"], 0)

    def test_calibrated_elevation_zone_becomes_external_area_without_inventing_rate(self):
        row = no_ai.zone_to_takeoff_row({
            "id": 8,
            "name": "North elevation render wall",
            "view_type": "Elevation",
            "page_type": "Elevation",
            "page_label": "A301",
            "area_m2": 54.2,
            "px_per_m": 80,
            "substrate": "Render",
            "finish_system": "Exterior acrylic",
            "quantity_status": "Measured",
            "source_reference": "Grid 1-5",
        })
        self.assertEqual(row["section"], "External")
        self.assertEqual(row["element"], "External walls / cladding")
        self.assertEqual(row["substrate"], "Render")
        self.assertEqual(row["finish_system"], "Exterior acrylic")
        self.assertEqual(row["quantity"], 54.2)
        self.assertEqual(row["inclusion_status"], "PROVISIONAL")
        self.assertEqual(row["rate_per_unit"], 0)
        self.assertTrue(row["source_reference"].startswith(no_ai.SOURCE_PREFIX))

    def test_zone_rectangle_can_recompute_area_from_calibration(self):
        row = no_ai.zone_to_takeoff_row({
            "id": 2,
            "name": "External wall",
            "view_type": "Elevation",
            "page_type": "Elevation",
            "w_px": 1000,
            "h_px": 300,
            "px_per_m": 100,
            "area_m2": 0,
        })
        self.assertEqual(row["quantity"], 30.0)
        self.assertEqual(row["quantity_status"], "Measured")

    def test_unlinked_line_measurement_becomes_lineal_row(self):
        row = no_ai.measurement_to_takeoff_row({
            "id": 11,
            "label": "Timber balustrade",
            "kind": "line",
            "unit": "lm",
            "length_m": 18.75,
            "page_type": "Floor Plan",
            "page_label": "A102",
            "page_px_per_m": 90,
            "quantity_status": "Measured",
            "takeoff_row_id": None,
        })
        self.assertEqual(row["unit"], "lm")
        self.assertEqual(row["quantity"], 18.75)
        self.assertEqual(row["element"], "Measured lineal item")
        self.assertEqual(row["substrate"], "Other")
        self.assertEqual(row["finish_system"], "To be confirmed")
        self.assertEqual(row["quantity_status"], "Measured")

    def test_linked_measurement_is_not_imported_again(self):
        self.assertIsNone(no_ai.measurement_to_takeoff_row({
            "id": 12,
            "kind": "line",
            "unit": "lm",
            "length_m": 10,
            "takeoff_row_id": 99,
        }))

    def test_uncalibrated_geometry_stays_provisional(self):
        row = no_ai.measurement_to_takeoff_row({
            "id": 13,
            "label": "External wall area",
            "kind": "polygon",
            "unit": "m²",
            "area_m2": 20,
            "page_type": "Elevation",
            "page_px_per_m": 0,
            "quantity_status": "Measured",
        })
        self.assertEqual(row["quantity_status"], "Provisional measured")
        self.assertEqual(row["confidence"], "Derived")

    def test_build_combines_mapped_zones_and_only_unlinked_measurements(self):
        app = _QueryApp(
            zones=[{
                "id": 1, "name": "North elevation", "view_type": "Elevation",
                "page_type": "Elevation", "area_m2": 10, "px_per_m": 50,
            }],
            measurements=[
                {"id": 2, "label": "Screen", "kind": "line", "unit": "lm", "length_m": 5, "page_px_per_m": 50, "takeoff_row_id": None},
                {"id": 3, "label": "Already linked", "kind": "line", "unit": "lm", "length_m": 9, "page_px_per_m": 50, "takeoff_row_id": 7},
            ],
        )
        rows = no_ai.build_no_ai_rows(app, 4)
        self.assertEqual(len(rows), 2)
        self.assertTrue(any("zone:1" in row["source_reference"] for row in rows))
        self.assertTrue(any("measurement:2" in row["source_reference"] for row in rows))

    def test_refresh_replaces_only_no_ai_rows_and_preserves_other_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "planreader.db"
            conn = sqlite3.connect(db)
            _create_takeoff_table(conn)
            conn.execute(
                "INSERT INTO takeoff_rows(workspace_id,section,element,source_reference) VALUES(?,?,?,?)",
                (4, "Manual", "Manual wall", "Estimator manual row"),
            )
            conn.execute(
                "INSERT INTO takeoff_rows(workspace_id,section,element,source_reference) VALUES(?,?,?,?)",
                (4, "Old", "Old no-AI", f"{no_ai.SOURCE_PREFIX} · zone:999"),
            )
            conn.commit()
            conn.close()
            app = _DBApp(db)
            rows = [{
                "section": "External", "element": "External walls / cladding", "location": "North",
                "substrate": "Render", "finish_system": "To be confirmed", "quantity": 25.0,
                "unit": "m²", "quantity_status": "Measured", "source_page": "A301",
                "source_reference": f"{no_ai.SOURCE_PREFIX} · zone:1", "inclusion_status": "PROVISIONAL",
                "coats": 0, "coverage_m2_per_litre": 0, "productivity_m2_per_hour": 0,
                "rate_per_unit": 0, "confidence": "Measured", "notes": "test", "row_role": "",
            }]
            no_ai.replace_no_ai_rows(app, 4, rows)
            self.assertEqual(app.connect_count, 1)
            check = sqlite3.connect(db)
            try:
                refs = [r[0] for r in check.execute("SELECT source_reference FROM takeoff_rows WHERE workspace_id=4 ORDER BY id").fetchall()]
            finally:
                check.close()
            self.assertIn("Estimator manual row", refs)
            self.assertIn(f"{no_ai.SOURCE_PREFIX} · zone:1", refs)
            self.assertNotIn(f"{no_ai.SOURCE_PREFIX} · zone:999", refs)

    def test_schedule_save_does_not_invent_rate_or_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "planreader.db"
            conn = sqlite3.connect(db)
            _create_takeoff_table(conn)
            conn.close()
            app = _DBApp(db)
            count = no_ai.save_schedule_batched(app, 5, [{
                "section": "General", "element": "Measured area", "location": "Zone 1",
                "substrate": "Other", "finish_system": "To be confirmed", "quantity": 12.3,
                "unit": "m²", "quantity_status": "Measured", "source_page": "A101",
                "source_reference": "manual", "inclusion_status": "PROVISIONAL", "coats": 0,
                "coverage_m2_per_litre": 0, "productivity_m2_per_hour": 0,
                "rate_per_unit": 0, "confidence": "Measured", "notes": "", "row_role": "",
            }])
            self.assertEqual(count, 1)
            check = sqlite3.connect(db)
            try:
                rate, finish, coats = check.execute(
                    "SELECT rate_per_unit,finish_system,coats FROM takeoff_rows WHERE workspace_id=5"
                ).fetchone()
            finally:
                check.close()
            self.assertEqual(rate, 0)
            self.assertEqual(finish, "To be confirmed")
            self.assertEqual(coats, 0)

    def test_apply_wraps_subscription_page_once(self):
        def base_page(*_args, **_kwargs):
            return None
        app = SimpleNamespace(subscription_takeoff_page=base_page)
        no_ai.apply(app)
        installed = app.subscription_takeoff_page
        self.assertIsNot(installed, base_page)
        self.assertTrue(app._pb_no_ai_takeoff_v1216_applied)
        no_ai.apply(app)
        self.assertIs(app.subscription_takeoff_page, installed)


if __name__ == "__main__":
    unittest.main()
