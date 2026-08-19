from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

import pb_autopilot_v1223 as autopilot


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


class AutopilotV1223Tests(unittest.TestCase):
    def test_artist_reference_analysis_is_bounded_and_extracts_palette(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "artist.png"
            image = Image.new("RGB", (2400, 1600), (240, 245, 250))
            draw = ImageDraw.Draw(image)
            draw.rectangle((300, 450, 2050, 1450), fill=(184, 165, 140))
            draw.rectangle((650, 700, 1000, 1250), fill=(70, 75, 82))
            draw.polygon([(300, 450), (1100, 180), (2050, 450)], fill=(95, 98, 102))
            image.save(path)
            result = autopilot.analyse_artist_image(path)
            self.assertLessEqual(max(result["analysis_width"], result["analysis_height"]), 640)
            self.assertTrue(result["palette"])
            self.assertIsNotNone(result["silhouette_bbox_norm"])
            self.assertGreater(result["edge_density"], 0)

    def test_detect_levels_from_floor_and_partition_pages(self):
        pages = [
            {"id": 1, "page_type": "Floor Plan", "page_label": "GROUND FLOOR PLAN", "extracted_text": ""},
            {"id": 2, "page_type": "Partition Plan", "page_label": "LEVEL 01 PARTITION PLAN", "extracted_text": "UNIT 101 UNIT 102"},
            {"id": 3, "page_type": "Floor Plan", "page_label": "LEVEL 02 FLOOR PLAN", "extracted_text": ""},
            {"id": 4, "page_type": "Roof Plan", "page_label": "ROOF PLAN", "extracted_text": ""},
        ]
        levels = autopilot.detect_levels(pages)
        self.assertEqual([item["name"] for item in levels], ["Ground", "Level 1", "Level 2"])

    def test_plan_elevation_reconciliation_flags_large_mismatch(self):
        report = {
            "footprint": {"width_m": 20.0, "depth_m": 10.0},
            "facades": [
                {"page_id": 10, "page_label": "A301", "face": "front", "width_m": 20.2, "gross_m2": 120.0, "bbox": [1, 2, 3, 4]},
                {"page_id": 11, "page_label": "A302", "face": "right", "width_m": 12.0, "gross_m2": 72.0, "bbox": [1, 2, 3, 4]},
            ],
        }
        checks, issues = autopilot.geometry_reconciliation(report)
        status = {item["face"]: item["status"] for item in checks}
        self.assertEqual(status["front"], "Cross-verified")
        self.assertEqual(status["right"], "Mismatch")
        self.assertTrue(any(item["category"] == "Plan/elevation geometry" for item in issues))

    def test_missing_unit_is_explicit_review_issue(self):
        pages = [{
            "id": 4, "page_type": "Floor Plan", "page_label": "Level 1",
            "extracted_text": "UNIT 101\nUNIT 102\nUNIT 103",
        }]
        report = {"units": [{"label": "Unit 101"}, {"label": "Unit 103"}]}
        labelled, found, issues = autopilot.unit_coverage_issues(pages, report)
        self.assertEqual(labelled, 3)
        self.assertEqual(found, 2)
        self.assertEqual(len(issues), 1)
        self.assertIn("Unit 102", issues[0]["message"])

    def test_triage_keeps_artist_reference_and_rejects_unrelated_structural(self):
        keep, score, _ = autopilot._strong_page_evidence({
            "page_type": "Other", "page_label": "ARTIST IMPRESSION - STREET VIEW", "extracted_text": "3D render",
        })
        self.assertTrue(keep)
        self.assertGreaterEqual(score, 90)
        keep, _, _ = autopilot._strong_page_evidence({
            "page_type": "Structural", "page_label": "S201", "extracted_text": "footing reinforcement steel",
        })
        self.assertFalse(keep)

    def test_cross_page_calibration_uses_agreeing_same_scale_donors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_paths = []
            for idx in range(3):
                path = root / f"p{idx}.png"
                Image.new("RGB", (1000, 700), "white").save(path)
                image_paths.append(path)
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
                (1,4,1,1,"A101","Floor Plan","SCALE 1:100",1,100.0,"Manual 1:100",str(image_paths[0])),
                (2,4,1,2,"A102","Floor Plan","SCALE 1:100",1,102.0,"Auto dimension",str(image_paths[1])),
                (3,4,1,3,"A103","Floor Plan","SCALE 1:100",1,0.0,"",str(image_paths[2])),
            ])
            conn.commit(); conn.close()
            app = _DBApp(db)
            updates = autopilot.cross_page_calibration(app, 4)
            self.assertEqual(len(updates), 1)
            self.assertEqual(updates[0]["confidence"], "Cross-verified")
            pxpm = app.lquery("SELECT px_per_m,scale_text FROM pages WHERE id=3")[0]
            self.assertAlmostEqual(pxpm["px_per_m"], 101.0, places=3)
            self.assertIn("Auto cross-page 1:100", pxpm["scale_text"])

    def test_multilevel_model_uses_measured_envelope_height_and_artist_palette(self):
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
                CREATE TABLE model_openings(id INTEGER PRIMARY KEY AUTOINCREMENT,workspace_id INTEGER,mass_id INTEGER);
                CREATE TABLE workspace_settings(
                    workspace_id INTEGER,key TEXT,value TEXT,updated_at TEXT,UNIQUE(workspace_id,key)
                );
            """)
            conn.execute("INSERT INTO documents VALUES(1,4,'plans.pdf','abc')")
            conn.executemany("INSERT INTO pages VALUES(?,?,?,?,?,?,?,?,?,?,?)", [
                (1,4,1,1,"GROUND FLOOR PLAN","Floor Plan","GROUND FLOOR PLAN",1,100.0,"Manual", ""),
                (2,4,1,2,"LEVEL 01 FLOOR PLAN","Floor Plan","LEVEL 01 FLOOR PLAN",1,100.0,"Manual", ""),
            ])
            conn.commit(); conn.close()
            app = _DBApp(db)
            report = {
                "footprint": {"width_m": 20.0, "depth_m": 12.0, "page_id": 1},
                "facades": [
                    {"page_id": 10, "face": "front", "height_m": 6.0},
                    {"page_id": 11, "face": "rear", "height_m": 6.0},
                ],
            }
            material_state = {"occurrences": [
                {"page_id": 10, "status": "Confirmed", "substrate": "Lineaboard Cladding", "code": "EC1"}
            ]}
            refs = [{"page_id": 20, "face": "front", "palette": [{"hex": "#B8A58C"}]}]
            result = autopilot.build_autopilot_model(app, 4, report, material_state, refs)
            self.assertEqual(result["created"], 2)
            self.assertAlmostEqual(result["storey_height_m"], 3.0, places=3)
            masses = app.lquery("SELECT level_name,z,height,finish FROM model_masses ORDER BY z")
            self.assertEqual([row["level_name"] for row in masses], ["Ground", "Level 1"])
            self.assertEqual([round(row["z"], 2) for row in masses], [0.0, 3.0])
            self.assertTrue(any("#B8A58C" in row["finish"] for row in masses))
            settings = app.lquery("SELECT value FROM workspace_settings WHERE workspace_id=4 AND key='3d_surface_editor_v1212'")
            self.assertTrue(settings)


if __name__ == "__main__":
    unittest.main()
