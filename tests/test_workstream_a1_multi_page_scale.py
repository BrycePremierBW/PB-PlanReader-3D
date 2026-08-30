"""tests/test_workstream_a1_multi_page_scale.py — Workstream A1 Regression Suite.

Issue #77: Multi-page scale authority, vector/text scale solver, dynamic re-scaling,
and strict scale gate delegation across multi-page drawing sets.
"""
from __future__ import annotations

import sqlite3
import unittest
import pandas as pd
from unittest.mock import MagicMock, patch

from pb_multi_page_scale_v170 import (
    PageScaleRecord,
    MultiPageScaleRegistry,
    derive_workspace_scale_authority,
    extract_scale_ratio_from_text,
    recompute_page_scale_geometry,
)
from pb_commercial_export_preflight_v163 import derive_export_preflight


def _create_mock_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE workspaces (
            id INTEGER PRIMARY KEY,
            job_no TEXT,
            job_name TEXT,
            drawing_issue TEXT,
            jobhub_job_id INTEGER,
            file_name TEXT,
            executive_summary TEXT,
            status TEXT
        );

        CREATE TABLE pages (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            page_label TEXT,
            page_type TEXT,
            page_number INTEGER,
            px_per_m REAL,
            scale_text TEXT,
            selected INTEGER DEFAULT 1,
            file_name TEXT
        );

        CREATE TABLE measurement_lines (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            page_id INTEGER,
            line_type TEXT,
            length_m REAL,
            area_m2 REAL,
            raw_points TEXT
        );

        CREATE TABLE takeoff_rows (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            section TEXT,
            element TEXT,
            location TEXT,
            substrate TEXT,
            unit TEXT,
            quantity REAL,
            quantity_status TEXT,
            coats REAL,
            rate_per_unit REAL,
            labour_hours REAL,
            paint_litres REAL,
            value_ex_gst REAL,
            finish_system TEXT,
            productivity_m2_per_hour REAL,
            coverage_m2_per_litre REAL,
            confidence TEXT,
            inclusion_status TEXT,
            row_role TEXT,
            source_page TEXT,
            source_reference TEXT,
            notes TEXT
        );

        CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            file_name TEXT,
            doc_type TEXT
        );

        CREATE TABLE mapped_zones (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            page_id INTEGER,
            zone_name TEXT
        );

        CREATE TABLE register_items (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            item_name TEXT,
            status TEXT
        );
        """
    )
    conn.commit()
    return conn


class WorkstreamA1ScaleTests(unittest.TestCase):

    def setUp(self):
        self.conn = _create_mock_db()
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO workspaces VALUES (1, 'JOB-A1', 'Test Building', 'Rev A', 101, 'A-01.pdf', 'Exec Summary', 'Scope & Read')"
        )
        self.conn.commit()

        class MockApp:
            def __init__(self, conn):
                self.conn = conn
            def local_connect(self):
                return self.conn

        self.app = MockApp(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_01_single_page_manual_calibration_authority(self):
        """Explicit user calibration px_per_m > 0 yields CALIBRATED status."""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO pages VALUES (10, 1, 'A-101', 'Plan', 1, 100.0, '1:100 @ A1', 1, 'A-101.pdf')")
        self.conn.commit()

        registry = derive_workspace_scale_authority(self.conn, 1)
        self.assertEqual(len(registry.records), 1)
        rec = registry.records[0]
        self.assertEqual(rec.page_id, 10)
        self.assertEqual(rec.scale_status, "CALIBRATED")
        self.assertEqual(rec.px_per_m, 100.0)
        self.assertEqual(rec.calibration_method, "MANUAL")
        self.assertFalse(registry.is_blocked())

    def test_02_title_block_text_scale_parsing(self):
        """Parses title block scale text e.g. 1:100 into scale_ratio and PROVISIONAL_AUTO."""
        ratio1 = extract_scale_ratio_from_text("SCALE 1:100 @ A1")
        ratio2 = extract_scale_ratio_from_text("DRAWING SCALE 1:50")
        self.assertEqual(ratio1, 100)
        self.assertEqual(ratio2, 50)

        cur = self.conn.cursor()
        cur.execute("INSERT INTO pages VALUES (11, 1, 'A-102', 'Plan', 2, 0.0, '1:50', 1, 'A-102.pdf')")
        self.conn.commit()

        registry = derive_workspace_scale_authority(self.conn, 1)
        rec = registry.records[0]
        self.assertEqual(rec.scale_status, "PROVISIONAL_AUTO")
        self.assertEqual(rec.scale_ratio, 50)
        self.assertEqual(rec.calibration_method, "TITLE_BLOCK_TEXT")

    def test_03_multi_page_scale_inheritance(self):
        """Uncalibrated page referenced by work items inherits primary plan scale context provisionally."""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO pages VALUES (10, 1, 'A-101', 'Plan', 1, 100.0, '1:100', 1, 'A-101.pdf')")
        cur.execute("INSERT INTO pages VALUES (12, 1, 'A-103', 'Detail', 2, 0.0, '', 1, 'A-103.pdf')")
        cur.execute(
            "INSERT INTO takeoff_rows VALUES (1, 1, 'Internal', 'Wall', 'G01', 'Plasterboard', 'm²', 50.0, 'Measured', 2, 25.0, 5.0, 10.0, 100.0, 'PT01', 12.0, 20.0, 'high', 'included', 'work', 'A-103', 'Ref-103', 'Notes')"
        )
        self.conn.commit()

        registry = derive_workspace_scale_authority(self.conn, 1)
        rec12 = [r for r in registry.records if r.page_id == 12][0]
        self.assertEqual(rec12.scale_status, "PROVISIONAL_AUTO")
        self.assertEqual(rec12.calibration_method, "INHERITED")
        self.assertEqual(rec12.px_per_m, 100.0)

    def test_04_uncalibrated_referenced_page_blocks_scale_gate(self):
        """Uncalibrated page with referenced takeoff work rows raises UNCALIBRATED_SCALE blocker."""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO pages VALUES (15, 1, 'A-105', 'Plan', 1, 0.0, '', 1, 'A-105.pdf')")
        cur.execute(
            "INSERT INTO takeoff_rows VALUES (2, 1, 'Internal', 'Wall', 'G01', 'Plasterboard', 'm²', 30.0, 'Measured', 2, 25.0, 5.0, 10.0, 100.0, 'PT01', 12.0, 20.0, 'high', 'included', 'work', 'A-105', 'Ref-105', 'Notes')"
        )
        self.conn.commit()

        registry = derive_workspace_scale_authority(self.conn, 1)
        self.assertTrue(registry.is_blocked())
        issues = registry.get_issues()
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["issue_type"], "UNCALIBRATED_SCALE")
        self.assertEqual(issues[0]["page_label"], "A-105")

    def test_05_unselected_or_cover_pages_do_not_block(self):
        """Cover sheets / legend pages marked NOT_REQUIRED do not block scale gate."""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO pages VALUES (1, 1, 'Cover Sheet', 'Cover', 1, 0.0, '', 1, 'Cover.pdf')")
        cur.execute("INSERT INTO pages VALUES (2, 1, 'A-101', 'Plan', 2, 100.0, '1:100', 1, 'A-101.pdf')")
        self.conn.commit()

        registry = derive_workspace_scale_authority(self.conn, 1)
        self.assertFalse(registry.is_blocked())
        rec_cover = [r for r in registry.records if r.page_id == 1][0]
        self.assertEqual(rec_cover.scale_status, "NOT_REQUIRED")

    def test_06_scale_recalibration_recomputes_geometry(self):
        """recompute_page_scale_geometry updates saved measurement_lines lengths and areas server-side."""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO pages VALUES (20, 1, 'A-200', 'Plan', 1, 100.0, '1:100', 1, 'A-200.pdf')")
        cur.execute("INSERT INTO measurement_lines VALUES (101, 1, 20, 'polygon', 10.0, 25.0, 'pts')")
        self.conn.commit()

        # Recalibrate page scale from 100.0 px/m to 200.0 px/m (scale_ratio = 100 / 200 = 0.5)
        count = recompute_page_scale_geometry(self.conn, 1, 20, 100.0, 200.0)
        self.assertEqual(count, 1)

        cur.execute("SELECT length_m, area_m2 FROM measurement_lines WHERE id=101")
        len_m, area_m2 = cur.fetchone()
        self.assertAlmostEqual(float(len_m), 5.0, places=2)
        self.assertAlmostEqual(float(area_m2), 6.25, places=2)

    def test_07_export_preflight_blocks_when_scale_gate_blocked(self):
        """Preflight status becomes BLOCKED when scale gate is blocked by an uncalibrated referenced page."""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO pages VALUES (30, 1, 'A-300', 'Plan', 1, 0.0, '', 1, 'A-300.pdf')")
        cur.execute("INSERT INTO register_items VALUES (1, 1, 'Item 1', 'Closed')")
        cur.execute(
            "INSERT INTO takeoff_rows VALUES (10, 1, 'Internal', 'Wall', 'G01', 'Plasterboard', 'm²', 40.0, 'Measured', 2, 25.0, 5.0, 10.0, 100.0, 'PT01', 12.0, 20.0, 'high', 'included', 'work', 'A-300', 'Ref-300', 'Notes')"
        )
        self.conn.commit()

        res = derive_export_preflight(self.conn, 1, bridge_available=True)
        self.assertEqual(res.preflight_status, "BLOCKED")
        self.assertTrue(any("Uncalibrated" in r or "scale" in r.lower() for r in res.blocking_reasons))


if __name__ == "__main__":
    unittest.main()
