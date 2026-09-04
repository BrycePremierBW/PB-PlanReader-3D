from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pb_drawing_reading_v1226 as reading
import pb_elevation_regions_v1226 as elevation
import pb_selected_evidence_floor_v1226 as selected
import pb_takeoff_review_v1226 as review
from pb_takeoff_authority_v164 import AUTHORITY_STATUS_FIELD, model_surface_authority


class _EvidenceApp:
    def __init__(self):
        self.selected_ids = {1, 3}
        self.settings = {}

    def lquery(self, sql, params=()):
        if "SELECT id,page_label FROM pages" in sql:
            return [{"id": 1, "page_label": "A201"}, {"id": 3, "page_label": "A601"}]
        if "FROM takeoff_rows" in sql:
            return []
        return []

    def workspace_setting(self, workspace_id, key, default=""):
        return self.settings.get((workspace_id, key), default)

    def set_workspace_setting(self, workspace_id, key, value):
        self.settings[(workspace_id, key)] = value


def test_deselected_pages_are_removed_from_material_state_and_issues():
    app = _EvidenceApp()
    state = {
        "dictionary": {
            "EC1": {"sources": [{"page_id": 3, "description": "Linea", "substrate": "Lineaboard Cladding", "finish": ""}]},
            "EC2": {"sources": [{"page_id": 2, "description": "Textureboard", "substrate": "Textureboard Cladding", "finish": ""}]},
        },
        "schedule_pages": [2, 3],
        "occurrences": [{"page_id": 1, "code": "EC1"}, {"page_id": 2, "code": "EC2"}],
        "issues": [{"page_id": 2, "page_label": "A202", "message": "deselected"}],
        "review_issues": [
            {"page_id": 1, "page_label": "A201", "message": "selected"},
            {"page_id": 2, "page_label": "A202", "message": "deselected"},
        ],
    }
    filtered = selected.filter_material_state(app, 9, state)
    assert set(filtered["dictionary"]) == {"EC1"}
    assert filtered["schedule_pages"] == [3]
    assert [item["code"] for item in filtered["occurrences"]] == ["EC1"]
    assert len(filtered["review_issues"]) == 1
    assert filtered["review_issues"][0]["page_id"] == 1


def test_partition_and_floor_finishes_are_preferred_floor_sources():
    assert selected.floor_source_priority({"page_type": "Floor Plan", "extracted_text": "LEVEL 05 PARTITION PLAN"}) == (500, "Partition Plan")
    assert selected.floor_source_priority({"page_type": "Finishes Schedule", "extracted_text": "LEVEL 05 FLOOR FINISHES PLAN"}) == (450, "Floor Finishes / Finishes Plan")
    assert selected.floor_source_priority({"page_type": "Floor Plan", "extracted_text": "GENERAL FLOOR PLAN"})[0] == 220


def test_spatial_unit_area_pairing_uses_nearest_m2_on_drawing():
    original = selected.auto._pdf_word_lines
    try:
        selected.auto._pdf_word_lines = lambda _app, _page: [
            {"text": "UNIT 501", "bbox": [5, 5, 25, 15], "center": [15, 10]},
            {"text": "84.6 m²", "bbox": [8, 20, 25, 30], "center": [16, 25]},
            {"text": "UNIT 502", "bbox": [75, 70, 95, 80], "center": [85, 75]},
            {"text": "91.2 m²", "bbox": [78, 84, 98, 94], "center": [88, 89]},
        ]
        found = selected.positioned_unit_area_candidates(object(), {"id": 1})
    finally:
        selected.auto._pdf_word_lines = original
    assert [(item["label"], item["area_m2"]) for item in found] == [("Unit 501", 84.6), ("Unit 502", 91.2)]
    assert all("Spatial" in item["pairing"] for item in found)


def test_dimension_line_witness_marks_are_recognised():
    baseline = (10.0, 50.0, 110.0, 50.0)
    lines = [baseline, (10.0, 42.0, 10.0, 58.0), (110.0, 42.0, 110.0, 58.0)]
    assert reading._witness_count(baseline, lines) == 2


def test_elevation_multiple_codes_form_reviewable_bands():
    groups = [
        {"code": "EC1", "substrate": "Linea", "cx": 25.0, "cy": 50.0, "occurrences": []},
        {"code": "EC2", "substrate": "Textureboard", "cx": 75.0, "cy": 52.0, "occurrences": []},
    ]
    bands = elevation._region_bands(groups, [0, 0, 100, 80])
    assert len(bands) == 2
    assert bands[0]["polygon_px"][1][0] == bands[1]["polygon_px"][0][0]
    assert all("Approximate" in item["boundary_basis"] for item in bands)


class _DBApp:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.settings = {}

    def local_connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def lquery(self, sql, params=()):
        conn = self.local_connect()
        try:
            return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]
        finally:
            conn.close()

    def lexecute(self, sql, params=()):
        conn = self.local_connect()
        try:
            cur = conn.execute(sql, tuple(params)); conn.commit(); return int(cur.lastrowid or 0)
        finally:
            conn.close()

    def workspace_setting(self, workspace_id, key, default=""):
        return self.settings.get((int(workspace_id), str(key)), default)

    def set_workspace_setting(self, workspace_id, key, value):
        self.settings[(int(workspace_id), str(key))] = value

    def now_stamp(self):
        return "2026-08-20T12:00:00"


def _make_db(path: Path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE takeoff_rows(
            id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER, section TEXT, element TEXT, location TEXT,
            substrate TEXT, finish_system TEXT, quantity REAL, unit TEXT, quantity_status TEXT, source_page TEXT,
            source_reference TEXT, inclusion_status TEXT, coats REAL, coverage_m2_per_litre REAL,
            productivity_m2_per_hour REAL, rate_per_unit REAL, confidence TEXT, notes TEXT, row_role TEXT,
            created_at TEXT, updated_at TEXT, commercial_authority_status TEXT DEFAULT '',
            commercial_authority_source TEXT DEFAULT '', commercial_authority_reviewed_by TEXT DEFAULT '',
            commercial_authority_reviewed_at TEXT DEFAULT '', commercial_authority_fingerprint TEXT DEFAULT ''
        );
        CREATE TABLE measurement_lines(id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER, takeoff_row_id INTEGER);
        CREATE TABLE pages(id INTEGER PRIMARY KEY, workspace_id INTEGER, page_label TEXT, page_type TEXT, image_path TEXT,
            width_px REAL, height_px REAL, px_per_m REAL, selected INTEGER);
        """
    )
    conn.execute("INSERT INTO pages VALUES(1,1,'A201','Floor Plan','',1000,1000,100,1)")
    for idx, qty in enumerate((20.0, 30.0), 1):
        conn.execute(
            """INSERT INTO takeoff_rows(workspace_id,section,element,location,substrate,finish_system,quantity,unit,quantity_status,
               source_page,source_reference,inclusion_status,coats,coverage_m2_per_litre,productivity_m2_per_hour,rate_per_unit,
               confidence,notes,row_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (1,'External','Walls',f'Area {idx}','Render','Exterior',qty,'m²','Measured','A201',f'ref-{idx}','INCLUSION',3,12,8,35,'Measured','', '', 'x','x'),
        )
    conn.execute("INSERT INTO measurement_lines(workspace_id,takeoff_row_id) VALUES(1,1)")
    conn.execute("INSERT INTO measurement_lines(workspace_id,takeoff_row_id) VALUES(1,2)")
    conn.commit(); conn.close()


def test_merge_rows_sums_quantity_and_relinks_measurements():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.sqlite"
        _make_db(path); app = _DBApp(path)
        new_id = review.merge_rows(app, 1, [1, 2], {"section":"External","element":"Walls","location":"Merged","substrate":"Render","finish_system":"Exterior"})
        rows = app.lquery("SELECT * FROM takeoff_rows WHERE workspace_id=1")
        assert len(rows) == 1
        assert rows[0]["id"] == new_id
        assert rows[0]["quantity"] == 50.0
        links = app.lquery("SELECT takeoff_row_id FROM measurement_lines ORDER BY id")
        assert {row["takeoff_row_id"] for row in links} == {new_id}
        provenance = json.loads(app.workspace_setting(1, selected.PROVENANCE_SETTING_KEY, "{}"))
        assert provenance[rows[0]["source_reference"]]["merged_refs"] == ["ref-1", "ref-2"]


def test_merge_with_model_surface_cannot_launder_3d_authority():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.sqlite"
        _make_db(path)
        app = _DBApp(path)
        app.lexecute(
            """UPDATE takeoff_rows
                  SET row_role='model_surface',
                      source_reference='PB 3D Surface Editor v1.2.12 · mass:1:front'
                WHERE id=1"""
        )

        new_id = review.merge_rows(
            app,
            1,
            [1, 2],
            {
                "section": "External",
                "element": "Walls",
                "location": "Merged",
                "substrate": "Render",
                "finish_system": "Exterior",
                "row_role": "",
            },
        )
        merged = app.lquery("SELECT * FROM takeoff_rows WHERE id=?", (new_id,))[0]

        assert merged["row_role"] == "model_surface"
        assert merged[AUTHORITY_STATUS_FIELD] == "REVIEW_REQUIRED"
        assert model_surface_authority(merged)[0] is False


def test_manual_polygon_recalculates_m2_and_becomes_authoritative():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.sqlite"
        _make_db(path); app = _DBApp(path)
        row = app.lquery("SELECT * FROM takeoff_rows WHERE id=1")[0]
        page = app.lquery("SELECT * FROM pages WHERE id=1")[0]
        areas = [{
            "id":"A-1","label":"Area 1","substrate":"OTHER","elevation":"A201","status":"Paint Included",
            "progress_pct":0,"notes":"","manual_m2":0,
            "points":[{"x":0,"y":0},{"x":10,"y":0},{"x":10,"y":10},{"x":0,"y":10}],
        }]
        qty = review.save_polygon_override(app, 1, row, page, areas, {"sources":[]})
        assert qty == 1.0
        updated = app.lquery("SELECT * FROM takeoff_rows WHERE id=1")[0]
        assert updated["quantity"] == 1.0
        assert updated["source_reference"].startswith(review.MANUAL_PREFIX)
        assert updated["confidence"] == "Measured"


if __name__ == "__main__":
    tests = [name for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for name in tests:
        globals()[name]()
    print(f"{len(tests)} v1.2.26 drawing-intelligence tests passed")
