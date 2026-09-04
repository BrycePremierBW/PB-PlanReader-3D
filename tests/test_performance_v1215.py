from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pb_3d_quickstart_v1213 as quick_v1213
import pb_3d_surface_editor_v1212 as surface_v1212
import pb_performance_v1215 as perf
import pb_takeoff_studio_v1211 as studio_v1211


class _FakeTab:
    def __init__(self, is_open: bool):
        self.open = is_open

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeStreamlit:
    def __init__(self, selected: int):
        self.selected = selected
        self.tab_kwargs = None
        self.messages = []

    def tabs(self, labels, **kwargs):
        self.tab_kwargs = dict(kwargs)
        return [_FakeTab(i == self.selected) for i, _ in enumerate(labels)]

    def markdown(self, message):
        self.messages.append(("markdown", str(message)))

    def caption(self, message):
        self.messages.append(("caption", str(message)))


class _LazyApp:
    def __init__(self, selected: int):
        self.st = _FakeStreamlit(selected)
        self.hero_calls = 0

    def hero(self, _workspace):
        self.hero_calls += 1


class _DBApp:
    def __init__(self, db_path: Path, query_rows=None):
        self.db_path = str(db_path)
        self.query_rows = list(query_rows or [])
        self.connect_count = 0

    def local_connect(self):
        self.connect_count += 1
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def lquery(self, _sql, _params=()):
        return [dict(row) for row in self.query_rows]

    @staticmethod
    def now_stamp():
        return "2026-08-19T14:30:00"


def _create_takeoff_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE takeoff_rows(
            workspace_id INTEGER, section TEXT, element TEXT, location TEXT,
            substrate TEXT, finish_system TEXT, quantity REAL, unit TEXT,
            quantity_status TEXT, source_page TEXT, source_reference TEXT,
            inclusion_status TEXT, coats REAL, coverage_m2_per_litre REAL,
            productivity_m2_per_hour REAL, rate_per_unit REAL, confidence TEXT,
            notes TEXT, row_role TEXT, created_at TEXT, updated_at TEXT,
            commercial_authority_status TEXT DEFAULT '',
            commercial_authority_source TEXT DEFAULT '',
            commercial_authority_reviewed_by TEXT DEFAULT '',
            commercial_authority_reviewed_at TEXT DEFAULT '',
            commercial_authority_fingerprint TEXT DEFAULT ''
        )"""
    )
    conn.commit()


def _sample_takeoff_row(source_reference: str) -> dict:
    return {
        "section": "External",
        "element": "Wall",
        "location": "North",
        "substrate": "Render",
        "finish_system": "To be confirmed",
        "quantity": 10.0,
        "unit": "m²",
        "quantity_status": "Measured",
        "source_page": "A-201",
        "source_reference": source_reference,
        "inclusion_status": "INCLUSION",
        "coats": 0,
        "coverage_m2_per_litre": 0,
        "productivity_m2_per_hour": 0,
        "rate_per_unit": 0,
        "confidence": "Measured",
        "notes": "test",
        "row_role": "studio_area",
    }


class PerformanceV1215Tests(unittest.TestCase):
    def test_lazy_3d_executes_only_selected_heavy_panel(self):
        calls = []
        app = _LazyApp(selected=2)
        workspace = {"id": 9}

        old_quick = quick_v1213.quick_build_panel
        old_surface = surface_v1212.surface_editor_panel
        old_studio = studio_v1211._studio_panel
        try:
            quick_v1213.quick_build_panel = lambda *_args, **_kwargs: calls.append("quick")
            surface_v1212.surface_editor_panel = lambda *_args, **_kwargs: calls.append("surface")
            studio_v1211._studio_panel = lambda *_args, **_kwargs: calls.append("studio")
            core_page = lambda *_args, **_kwargs: calls.append("core")

            perf.lazy_3d_model_page(app, core_page, workspace)

            self.assertEqual(calls, ["studio"])
            self.assertEqual(app.hero_calls, 1)
            self.assertEqual(app.st.tab_kwargs["on_change"], "rerun")
            self.assertEqual(app.st.tab_kwargs["key"], "pb_3d_lazy_tabs_9")
        finally:
            quick_v1213.quick_build_panel = old_quick
            surface_v1212.surface_editor_panel = old_surface
            studio_v1211._studio_panel = old_studio

    def test_sqlite_connection_tuning_is_memory_bounded(self):
        conn = sqlite3.connect(":memory:")
        try:
            perf.tune_sqlite_connection(conn)
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA cache_size").fetchone()[0], -4096)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        finally:
            conn.close()

    def test_indexes_are_added_only_for_existing_tables_and_columns(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE pages(id INTEGER PRIMARY KEY, workspace_id INTEGER, selected INTEGER)")
            conn.execute("CREATE TABLE model_masses(id INTEGER PRIMARY KEY, workspace_id INTEGER)")
            count = perf.ensure_performance_indexes(conn)
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            self.assertGreaterEqual(count, 3)
            self.assertIn("idx_pages_workspace_id", names)
            self.assertIn("idx_pages_workspace_selected", names)
            self.assertIn("idx_masses_workspace_id", names)
            self.assertNotIn("idx_openings_workspace_mass", names)
        finally:
            conn.close()

    def test_studio_takeoff_sync_uses_one_write_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "planreader.db"
            conn = sqlite3.connect(db)
            _create_takeoff_table(conn)
            conn.close()
            app = _DBApp(db)
            rows = [
                _sample_takeoff_row(f"{studio_v1211.SOURCE_PREFIX} · page:2 · area:A-{i}")
                for i in range(1, 6)
            ]

            perf.replace_studio_rows_batched(app, 4, 2, rows)

            self.assertEqual(app.connect_count, 1)
            check = sqlite3.connect(db)
            try:
                self.assertEqual(check.execute("SELECT COUNT(*) FROM takeoff_rows").fetchone()[0], 5)
            finally:
                check.close()

    def test_surface_takeoff_sync_uses_one_write_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "planreader.db"
            conn = sqlite3.connect(db)
            _create_takeoff_table(conn)
            conn.close()
            app = _DBApp(db)
            rows = [
                {
                    **_sample_takeoff_row(f"{surface_v1212.SOURCE_PREFIX} · mass:1:front"),
                    "row_role": "model_surface",
                    "commercial_authority_status": "REVIEW_REQUIRED",
                    "commercial_authority_source": "model_masses:1",
                    "commercial_authority_reviewed_by": "",
                    "commercial_authority_reviewed_at": "",
                    "commercial_authority_fingerprint": "",
                },
                {
                    **_sample_takeoff_row(f"{surface_v1212.SOURCE_PREFIX} · mass:1:rear"),
                    "row_role": "model_surface",
                    "commercial_authority_status": "REVIEW_REQUIRED",
                    "commercial_authority_source": "model_masses:1",
                    "commercial_authority_reviewed_by": "",
                    "commercial_authority_reviewed_at": "",
                    "commercial_authority_fingerprint": "",
                },
            ]

            perf.replace_surface_rows_batched(app, 7, rows)

            self.assertEqual(app.connect_count, 1)
            check = sqlite3.connect(db)
            try:
                self.assertEqual(check.execute("SELECT COUNT(*) FROM takeoff_rows").fetchone()[0], 2)
                saved = check.execute(
                    """SELECT row_role,commercial_authority_status,
                              commercial_authority_source
                         FROM takeoff_rows ORDER BY source_reference"""
                ).fetchall()
                self.assertEqual(
                    saved,
                    [
                        ("model_surface", "REVIEW_REQUIRED", "model_masses:1"),
                        ("model_surface", "REVIEW_REQUIRED", "model_masses:1"),
                    ],
                )
            finally:
                check.close()

    def test_quick_3d_mass_refresh_batches_all_inserts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "planreader.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """CREATE TABLE model_masses(
                    workspace_id INTEGER,label TEXT,level_name TEXT,x REAL,y REAL,z REAL,
                    width REAL,depth REAL,height REAL,finish TEXT,source_reference TEXT,
                    confidence TEXT,notes TEXT,created_at TEXT
                )"""
            )
            conn.commit()
            conn.close()
            zones = [
                {
                    "id": index,
                    "name": f"Zone {index}",
                    "x_px": 100 * index,
                    "y_px": 50,
                    "w_px": 800,
                    "h_px": 500,
                    "px_per_m": 100,
                    "wall_height_m": 2.7,
                    "finish_system": "Exterior acrylic",
                    "substrate": "Render",
                    "quantity_status": "Measured",
                    "source_reference": f"A-{index}",
                }
                for index in range(1, 11)
            ]
            app = _DBApp(db, zones)

            count = perf.refresh_zone_masses_batched(app, 3)

            self.assertEqual(count, 10)
            self.assertEqual(app.connect_count, 1)
            check = sqlite3.connect(db)
            try:
                self.assertEqual(check.execute("SELECT COUNT(*) FROM model_masses").fetchone()[0], 10)
            finally:
                check.close()


if __name__ == "__main__":
    unittest.main()
