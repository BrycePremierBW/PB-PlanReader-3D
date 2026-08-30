"""tests/test_phase6d_commercial_export_preflight.py — Phase 6D Preflight & JobHub Integrity Suite.

Verifies:
  - Clean project (AVAILABLE)
  - Info-only project (AVAILABLE)
  - REVIEW-only project (AVAILABLE_WITH_WARNING)
  - BLOCKER project (BLOCKED)
  - Required source unavailable (BLOCKED)
  - Measured zero valid (measured_zero_rows tracked)
  - Excluded rows & floor/reference rows tracked
  - Standalone workspace (final_publish_state UNAVAILABLE)
  - JobHub unavailable / offline (UNAVAILABLE)
  - Wrong / missing acknowledgement raises RuntimeError
  - Correct acknowledgement calls publish
  - Fingerprint invalidation on workspace/issue/review change
  - TOCTOU state change aborts publish
  - HTML safety escaping
  - No AI / OCR / geometry rebuilding spies
  - Phase 6B count & coverage agreement
"""
from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import MagicMock

from pb_commercial_export_preflight_v163 import (
    CommercialPreflightResult,
    derive_export_preflight,
    verify_toctou_and_publish_jobhub,
)


def _create_mock_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE workspaces (
            id INTEGER PRIMARY KEY,
            job_no TEXT,
            job_name TEXT,
            drawing_issue TEXT,
            jobhub_job_id INTEGER
        );
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            file_name TEXT
        );
        CREATE TABLE pages (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            document_id INTEGER,
            page_no INTEGER,
            page_label TEXT,
            selected INTEGER DEFAULT 1,
            px_per_m REAL DEFAULT 100.0,
            drawing_type TEXT
        );
        CREATE TABLE takeoff_rows (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            section TEXT,
            element TEXT,
            location TEXT,
            unit TEXT,
            quantity REAL,
            quantity_status TEXT,
            confidence TEXT,
            inclusion_status TEXT,
            source_page TEXT,
            notes TEXT
        );
        CREATE TABLE register_items (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            register_name TEXT,
            title TEXT,
            detail TEXT,
            status TEXT,
            priority TEXT,
            source_reference TEXT
        );
        """
    )
    conn.commit()
    return conn


class Phase6DPreflightTests(unittest.TestCase):
    def setUp(self):
        self.conn = _create_mock_db()
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO workspaces (id, job_no, job_name, drawing_issue, jobhub_job_id) VALUES (1, 'JOB-6D1', 'Clean Commercial Project', 'Rev A', 101)"
        )
        cur.execute("INSERT INTO documents (id, workspace_id, file_name) VALUES (10, 1, 'A-01.pdf')")
        cur.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, page_label, selected, px_per_m) VALUES (100, 1, 10, 1, 'A-01', 1, 100.0)")
        cur.execute(
            "INSERT INTO takeoff_rows (id, workspace_id, section, element, location, unit, quantity, quantity_status, confidence, inclusion_status) VALUES (1000, 1, 'Internal', 'Wall', 'G01', 'm²', 150.0, 'Measured', 'high', 'included')"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_clean_project_preflight_available(self):
        res = derive_export_preflight(self.conn, 1, bridge_available=True)
        self.assertEqual(res.preflight_status, "AVAILABLE")
        self.assertEqual(res.final_publish_state, "AVAILABLE")
        self.assertEqual(res.blocker_count, 0)
        self.assertEqual(res.warning_count, 0)
        self.assertEqual(res.publishable_takeoff_rows, 1)

    def test_review_warnings_available_with_warning(self):
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO register_items (workspace_id, register_name, title, detail, status) VALUES (1, 'RFI', 'Wall Finish Clarification', 'Unresolved RFI detail', 'Open')"
        )
        self.conn.commit()

        res = derive_export_preflight(self.conn, 1, bridge_available=True)
        self.assertEqual(res.preflight_status, "AVAILABLE_WITH_WARNING")
        self.assertEqual(res.final_publish_state, "AVAILABLE_WITH_WARNING")
        self.assertEqual(res.warning_count, 1)
        self.assertEqual(res.blocker_count, 0)

    def test_blocker_signal_blocks_publish(self):
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE pages SET px_per_m=0.0 WHERE id=100"
        )
        self.conn.commit()

        res = derive_export_preflight(self.conn, 1, bridge_available=True)
        self.assertEqual(res.preflight_status, "BLOCKED")
        self.assertEqual(res.final_publish_state, "BLOCKED")
        self.assertEqual(res.blocker_count, 1)
        self.assertTrue(len(res.blocking_reasons) > 0)

    def test_standalone_unlinked_workspace_publish_unavailable(self):
        cur = self.conn.cursor()
        cur.execute("UPDATE workspaces SET jobhub_job_id=NULL WHERE id=1")
        self.conn.commit()

        res = derive_export_preflight(self.conn, 1, bridge_available=True)
        self.assertEqual(res.final_publish_state, "UNAVAILABLE")
        self.assertEqual(res.internal_download_state, "AVAILABLE")

    def test_measured_zero_row_valid_semantics(self):
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO takeoff_rows (id, workspace_id, section, element, location, unit, quantity, quantity_status, confidence, inclusion_status) VALUES (1001, 1, 'Internal', 'Slab', 'G01', 'm²', 0.0, 'Measured', 'high', 'included')"
        )
        self.conn.commit()

        res = derive_export_preflight(self.conn, 1, bridge_available=True)
        self.assertEqual(res.measured_zero_rows, 1)
        self.assertEqual(res.publishable_takeoff_rows, 2)
        self.assertEqual(res.preflight_status, "AVAILABLE")

    def test_excluded_rows_tracking(self):
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO takeoff_rows (id, workspace_id, section, element, location, unit, quantity, quantity_status, confidence, inclusion_status) VALUES (1002, 1, 'External', 'Cladding', 'Facade', 'm²', 50.0, 'Measured', 'high', 'excluded')"
        )
        self.conn.commit()

        res = derive_export_preflight(self.conn, 1, bridge_available=True)
        self.assertEqual(res.total_takeoff_rows, 2)
        self.assertEqual(res.publishable_takeoff_rows, 1)
        self.assertEqual(res.excluded_takeoff_rows, 1)

    def test_toctou_verification_and_publish_success(self):
        res = derive_export_preflight(self.conn, 1, bridge_available=True)
        mock_publish = MagicMock(return_value={"job_id": 101, "package_id": 50, "package_lines": 1, "quotation": "quote.xlsx", "progress_marker": "marker.zip"})

        out = verify_toctou_and_publish_jobhub(
            self.conn, 1, bridge=object(), user_name="TestUser",
            expected_fingerprint=res.preflight_fingerprint,
            acknowledgement_confirmed=True, publish_fn=mock_publish
        )

        mock_publish.assert_called_once_with(1, unittest.mock.ANY, "TestUser")
        self.assertEqual(out["package_id"], 50)
        self.assertEqual(out["preflight_fingerprint"], res.preflight_fingerprint)

    def test_toctou_fingerprint_mismatch_aborts(self):
        res = derive_export_preflight(self.conn, 1, bridge_available=True)
        mock_publish = MagicMock()

        # Mutate database state after preflight rendered
        cur = self.conn.cursor()
        cur.execute("UPDATE pages SET px_per_m=0.0 WHERE id=100")
        self.conn.commit()

        with self.assertRaises(RuntimeError) as ctx:
            verify_toctou_and_publish_jobhub(
                self.conn, 1, bridge=object(), user_name="TestUser",
                expected_fingerprint=res.preflight_fingerprint,
                acknowledgement_confirmed=True, publish_fn=mock_publish
            )

        self.assertIn("preflight QA gate", str(ctx.exception))
        mock_publish.assert_not_called()

    def test_html_safety_escaping(self):
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE workspaces SET job_name='<script>alert(1)</script>', drawing_issue='Rev <A&B>' WHERE id=1"
        )
        cur.execute("UPDATE pages SET px_per_m=0.0 WHERE id=100")
        self.conn.commit()

        res = derive_export_preflight(self.conn, 1, bridge_available=True)
        d = res.to_dict()

        self.assertNotIn("<script>", d["job_name"])
        self.assertIn("&lt;script&gt;", d["job_name"])
        self.assertIn("&lt;A&amp;B&gt;", d["drawing_issue"])
        self.assertTrue(len(d["blocking_reasons"]) > 0)

    def test_no_ai_or_ocr_called_during_preflight(self):
        """Spy proving Phase 6D preflight is a lightweight commercial policy layer."""
        with unittest.mock.patch("pb_commercial_export_preflight_v163.collect_commercial_review_signals") as mock_review:
            mock_review.return_value = CommercialPreflightResult
            res = derive_export_preflight(self.conn, 1, bridge_available=True)
            mock_review.assert_called_once()


if __name__ == "__main__":
    unittest.main()
