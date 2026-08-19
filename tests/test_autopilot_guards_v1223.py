from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from PIL import Image

import pb_autopilot_v1223 as autopilot
import pb_autopilot_accuracy_guard_v1223 as accuracy
import pb_autopilot_upload_batch_v1223 as upload_batch


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

    def lexecute(self, sql, params=()):
        conn = self.local_connect()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    @staticmethod
    def now_stamp():
        return "2026-08-20T00:00:00"


class AutopilotGuardTests(unittest.TestCase):
    def test_cross_page_donor_replaces_only_provisional_printed_scale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            donor = root / "donor.png"
            target = root / "target.png"
            manual = root / "manual.png"
            for path in (donor, target, manual):
                Image.new("RGB", (1000, 700), "white").save(path)
            db = root / "db.sqlite"
            conn = sqlite3.connect(db)
            conn.executescript("""
                CREATE TABLE documents(id INTEGER PRIMARY KEY,workspace_id INTEGER,file_name TEXT,sha256 TEXT);
                CREATE TABLE pages(
                    id INTEGER PRIMARY KEY,workspace_id INTEGER,document_id INTEGER,page_no INTEGER,
                    page_label TEXT,page_type TEXT,extracted_text TEXT,selected INTEGER,px_per_m REAL,
                    scale_text TEXT,image_path TEXT
                );
            """)
            conn.execute("INSERT INTO documents VALUES(1,4,'plans.pdf','abc')")
            conn.executemany("INSERT INTO pages VALUES(?,?,?,?,?,?,?,?,?,?,?)", [
                (1,4,1,1,"A101","Floor Plan","SCALE 1:100",1,100.0,"Manual calibration",str(donor)),
                (2,4,1,2,"A102","Floor Plan","SCALE 1:100",1,95.0,"Auto provisional printed scale 1:100",str(target)),
                (3,4,1,3,"A103","Floor Plan","SCALE 1:100",1,98.0,"Manual/existing",str(manual)),
            ])
            conn.commit(); conn.close()
            app = _DBApp(db)
            updates = accuracy.cross_page_calibration(app, 4)
            self.assertEqual([item["page_id"] for item in updates], [2])
            rows = {row["id"]: row for row in app.lquery("SELECT id,px_per_m,scale_text FROM pages ORDER BY id")}
            self.assertAlmostEqual(rows[2]["px_per_m"], 100.0)
            self.assertIn("Auto cross-page 1:100", rows[2]["scale_text"])
            self.assertAlmostEqual(rows[3]["px_per_m"], 98.0)
            self.assertEqual(rows[3]["scale_text"], "Manual/existing")

    def test_measured_model_with_blank_source_is_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "db.sqlite"
            conn = sqlite3.connect(db)
            conn.executescript("""
                CREATE TABLE documents(id INTEGER PRIMARY KEY,workspace_id INTEGER,file_name TEXT,sha256 TEXT);
                CREATE TABLE pages(
                    id INTEGER PRIMARY KEY,workspace_id INTEGER,document_id INTEGER,page_no INTEGER,
                    page_label TEXT,page_type TEXT,extracted_text TEXT,selected INTEGER,px_per_m REAL,
                    scale_text TEXT,image_path TEXT
                );
                CREATE TABLE model_masses(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,workspace_id INTEGER,label TEXT,level_name TEXT,
                    x REAL,y REAL,z REAL,width REAL,depth REAL,height REAL,finish TEXT,source_reference TEXT,
                    confidence TEXT,notes TEXT,created_at TEXT
                );
            """)
            conn.execute("INSERT INTO documents VALUES(1,4,'plans.pdf','abc')")
            conn.execute("INSERT INTO pages VALUES(1,4,1,1,'GROUND FLOOR','Floor Plan','GROUND FLOOR',1,100,'Manual','')")
            conn.execute("""INSERT INTO model_masses(workspace_id,label,level_name,x,y,z,width,depth,height,finish,source_reference,confidence,notes,created_at)
                            VALUES(4,'Estimator mass','Ground',0,0,0,20,10,3,'','', 'Measured','','now')""")
            conn.commit(); conn.close()
            app = _DBApp(db)
            result = accuracy.build_autopilot_model(
                app, 4,
                {"footprint": {"width_m": 20, "depth_m": 10}, "facades": []},
                {"occurrences": []},
                [],
            )
            self.assertEqual(result["created"], 0)
            self.assertEqual(result["reason"], "Measured/verified estimator model preserved")
            self.assertEqual(result["protected_mass_ids"], [1])

    def test_upload_batch_wrapper_marks_pending_without_rendering_inside_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "db.sqlite"
            conn = sqlite3.connect(db)
            conn.executescript("""
                CREATE TABLE documents(id INTEGER PRIMARY KEY,workspace_id INTEGER);
                CREATE TABLE workspace_settings(
                    workspace_id INTEGER,key TEXT,value TEXT,updated_at TEXT,UNIQUE(workspace_id,key)
                );
            """)
            conn.execute("INSERT INTO documents VALUES(7,4)")
            conn.commit(); conn.close()
            app = _DBApp(db)
            seen = []

            def base_index(document_id, *args, **kwargs):
                seen.append((int(document_id), autopilot._processing_document.get()))
                return 12, "indexed"

            app.index_document_pages = base_index
            upload_batch.apply(app)
            result = app.index_document_pages(7)
            self.assertEqual(result, (12, "indexed"))
            self.assertEqual(seen, [(7, 7)])
            state = autopilot._state_get(app, 4)
            self.assertTrue(state["pending"])
            self.assertTrue(state["upload_batch_deferred"])


if __name__ == "__main__":
    unittest.main()
