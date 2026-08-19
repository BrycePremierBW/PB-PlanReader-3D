from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import pb_selected_pages_v1217 as selected


class _DBApp:
    def __init__(self, path: Path):
        self.path = str(path)

    def lquery(self, sql: str, params=()):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()


class _CaptureApp:
    def __init__(self):
        self.sql = []

    def ldf(self, sql, params=()):
        self.sql.append(("ldf", sql, params))
        return "frame"

    def lquery(self, sql, params=()):
        self.sql.append(("lquery", sql, params))
        return []


class SelectedPagesV1217Tests(unittest.TestCase):
    def test_direct_page_picker_query_is_scoped_to_selected_pages(self):
        sql = "SELECT id,page_label,page_type,image_path,selected FROM pages WHERE workspace_id=? ORDER BY id"
        scoped = selected.scope_takeoff_page_sql(sql)
        self.assertIn("COALESCE(selected,0)=1", scoped)
        self.assertIn("WHERE workspace_id=?", scoped)

    def test_aliased_page_query_is_scoped_to_selected_pages(self):
        sql = (
            "SELECT p.page_label,p.page_type,d.file_name FROM pages p "
            "JOIN documents d ON d.id=p.document_id WHERE p.workspace_id=? ORDER BY p.id"
        )
        scoped = selected.scope_takeoff_page_sql(sql)
        self.assertIn("COALESCE(p.selected,0)=1", scoped)

    def test_existing_selected_predicate_is_not_duplicated(self):
        sql = "SELECT id FROM pages WHERE workspace_id=? AND selected=1 ORDER BY id"
        self.assertEqual(selected.scope_takeoff_page_sql(sql), sql)

    def test_non_page_queries_are_untouched(self):
        sql = "SELECT * FROM takeoff_rows WHERE workspace_id=? ORDER BY id"
        self.assertEqual(selected.scope_takeoff_page_sql(sql), sql)

    def test_no_ai_builder_uses_only_selected_page_geometry(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "planreader.db"
            conn = sqlite3.connect(db)
            conn.executescript(
                """
                CREATE TABLE pages(
                    id INTEGER PRIMARY KEY, workspace_id INTEGER, page_label TEXT,
                    page_type TEXT, px_per_m REAL, selected INTEGER
                );
                CREATE TABLE mapped_zones(
                    id INTEGER PRIMARY KEY, workspace_id INTEGER, page_id INTEGER,
                    name TEXT, substrate TEXT, finish_system TEXT, area_m2 REAL,
                    px_per_m REAL, w_px REAL, h_px REAL, quantity_status TEXT,
                    source_reference TEXT, view_type TEXT
                );
                CREATE TABLE measurement_lines(
                    id INTEGER PRIMARY KEY, workspace_id INTEGER, page_id INTEGER,
                    label TEXT, kind TEXT, unit TEXT, area_m2 REAL, length_m REAL,
                    perimeter_m REAL, quantity_status TEXT, takeoff_row_id INTEGER
                );
                """
            )
            conn.executemany(
                "INSERT INTO pages(id,workspace_id,page_label,page_type,px_per_m,selected) VALUES(?,?,?,?,?,?)",
                [
                    (1, 7, "A101", "Floor plan", 100.0, 1),
                    (2, 7, "A102", "Floor plan", 100.0, 0),
                ],
            )
            conn.executemany(
                """INSERT INTO mapped_zones(
                    id,workspace_id,page_id,name,substrate,finish_system,area_m2,
                    px_per_m,w_px,h_px,quantity_status,source_reference,view_type
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (11, 7, 1, "Selected floor area", "Other", "", 18.0, 100.0, 0, 0, "Measured", "A101", "Floor plan"),
                    (12, 7, 2, "Unselected floor area", "Other", "", 99.0, 100.0, 0, 0, "Measured", "A102", "Floor plan"),
                ],
            )
            conn.executemany(
                """INSERT INTO measurement_lines(
                    id,workspace_id,page_id,label,kind,unit,area_m2,length_m,
                    perimeter_m,quantity_status,takeoff_row_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (21, 7, 1, "Selected line", "line", "lm", 0, 5.5, 0, "Measured", None),
                    (22, 7, 2, "Unselected line", "line", "lm", 0, 44.0, 0, "Measured", None),
                ],
            )
            conn.commit()
            conn.close()

            rows = selected.build_selected_no_ai_rows(_DBApp(db), 7)
            refs = [str(row["source_reference"]) for row in rows]
            quantities = [float(row["quantity"]) for row in rows]

            self.assertEqual(len(rows), 2)
            self.assertTrue(any("zone:11" in ref for ref in refs))
            self.assertTrue(any("measurement:21" in ref for ref in refs))
            self.assertFalse(any("zone:12" in ref for ref in refs))
            self.assertFalse(any("measurement:22" in ref for ref in refs))
            self.assertIn(18.0, quantities)
            self.assertIn(5.5, quantities)

    def test_cloned_core_page_filters_direct_page_queries_without_global_monkeypatch(self):
        app = _CaptureApp()

        def core_page(_workspace, _session_api_key="", _ai_provider="OpenAI"):
            return ldf(  # noqa: F821 - intentionally resolved from cloned globals
                "SELECT id,page_label,selected FROM pages WHERE workspace_id=? ORDER BY id",
                (4,),
            )

        clone = selected.clone_selected_subscription_page(app, core_page)
        result = clone({"id": 4})

        self.assertEqual(result, "frame")
        self.assertEqual(len(app.sql), 1)
        self.assertIn("COALESCE(selected,0)=1", app.sql[0][1])


if __name__ == "__main__":
    unittest.main()
