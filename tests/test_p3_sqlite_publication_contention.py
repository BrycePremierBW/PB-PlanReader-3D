"""tests/test_p3_sqlite_publication_contention.py — Workstream P3 Real SQLite Publication Contention Suite.

Specification:
- Use real file-backed SQLite and production publication code.
- Prove actual: BEGIN IMMEDIATE is reached.
- Tests:
  1. Lock acquisition failure
  2. Duplicate publish
  3. Pending/Published/Failed package states
  4. Multiple contenders
  5. Fresh-connection final DB state
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from pb_planreader_3d_app import (
    JobHubBridge,
    ensure_jobhub_takeoff_tables,
    ensure_shared_jobhub_schema,
    publish_job_to_jobhub,
)


class TestP3SQLitePublicationContention(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.tmpdir.name) / "jobhub_p3.db"
        self.bridge = JobHubBridge(kind="sqlite", source=str(self.db_path), timeout=0.2)
        ensure_shared_jobhub_schema(self.bridge)
        ensure_jobhub_takeoff_tables(self.bridge)

        # Seed initial job in Draft state
        self.bridge.execute(
            "INSERT INTO jobs (id, job_no, job_name, status) VALUES (?, ?, ?, ?)",
            (301, "JOB-P3-001", "P3 Real SQLite Contention Project", "Draft")
        )

        self.sample_takeoff = pd.DataFrame([
            {
                "id": 501, "section": "Internal", "element": "Wall", "location": "Room 101",
                "substrate": "Plasterboard", "unit": "m²", "quantity": 120.0,
                "quantity_status": "Measured", "coats": 2, "rate_per_unit": 22.5,
                "labour_hours": 6.0, "paint_litres": 15.0, "value_ex_gst": 2700.0,
                "row_role": "work", "inclusion_status": "included", "finish_system": "Wash&Wear Low Sheen",
                "productivity_m2_per_hour": 20.0, "confidence": "high", "notes": "Internal walls",
                "source_reference": "A-01"
            },
            {
                "id": 502, "section": "External", "element": "Facade", "location": "North",
                "substrate": "Render", "unit": "m²", "quantity": 80.0,
                "quantity_status": "Measured", "coats": 2, "rate_per_unit": 35.0,
                "labour_hours": 8.0, "paint_litres": 20.0, "value_ex_gst": 2800.0,
                "row_role": "work", "inclusion_status": "included", "finish_system": "Weathershield Gloss",
                "productivity_m2_per_hour": 10.0, "confidence": "high", "notes": "Exterior facade",
                "source_reference": "A-02"
            }
        ])

        self.workspace_mock = [{
            "id": 1, "job_no": "JOB-P3-001", "job_name": "P3 Real SQLite Contention Project",
            "drawing_issue": "Rev 1", "jobhub_job_id": 301, "file_name": "Plan.pdf",
            "executive_summary": "P3 Contention verification job"
        }]

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_p3_01_begin_immediate_reached_and_lock_acquisition_failure(self):
        """P3 Item 1: Prove actual BEGIN IMMEDIATE is reached and lock acquisition failure fails closed."""
        # Hold an exclusive BEGIN IMMEDIATE transaction on the file-backed SQLite database from an external connection
        lock_conn = sqlite3.connect(str(self.db_path), timeout=0.1)
        lock_conn.execute("BEGIN IMMEDIATE")

        with patch("pb_planreader_3d_app.lquery", return_value=self.workspace_mock), \
             patch("pb_planreader_3d_app.dataframe_for_takeoff", return_value=self.sample_takeoff), \
             patch("pb_planreader_3d_app.quote_workbook_bytes", return_value=b"mock_quote"), \
             patch("pb_planreader_3d_app.progress_package_bytes", return_value=b"mock_progress"):

            with self.assertRaises(RuntimeError) as ctx:
                publish_job_to_jobhub(
                    1, self.bridge, "TesterLock",
                    preflight_fingerprint="fp_p3_lock_test",
                    payload_hash="hash_p3_lock_test"
                )

            # Assert error identifies lock acquisition failure on SQLite
            self.assertIn("lock acquisition failed", str(ctx.exception).lower())
            self.assertIn("locked", str(ctx.exception).lower())

        lock_conn.rollback()
        lock_conn.close()

        # Fresh connection verify: database remains uncorrupted with 0 packages
        verify_conn = sqlite3.connect(str(self.db_path))
        cur = verify_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM painting_takeoff_packages")
        self.assertEqual(cur.fetchone()[0], 0)
        cur.execute("SELECT status FROM jobs WHERE id=301")
        self.assertEqual(cur.fetchone()[0], "Draft")
        verify_conn.close()

    def test_p3_02_duplicate_publish_prevention(self):
        """P3 Item 2: Duplicate publish fails closed and prevents dual package insertion."""
        with patch("pb_planreader_3d_app.lquery", return_value=self.workspace_mock), \
             patch("pb_planreader_3d_app.dataframe_for_takeoff", return_value=self.sample_takeoff), \
             patch("pb_planreader_3d_app.quote_workbook_bytes", return_value=b"mock_quote"), \
             patch("pb_planreader_3d_app.progress_package_bytes", return_value=b"mock_progress"):

            # First publish succeeds
            res1 = publish_job_to_jobhub(
                1, self.bridge, "Tester1",
                preflight_fingerprint="fp_p3_dup_test",
                payload_hash="hash_p3_dup_test"
            )
            self.assertTrue(res1["published"])
            self.assertEqual(res1["job_status"], "Published")

            # Second publish with identical fingerprint fails closed
            with self.assertRaises(RuntimeError) as ctx:
                publish_job_to_jobhub(
                    1, self.bridge, "Tester2",
                    preflight_fingerprint="fp_p3_dup_test",
                    payload_hash="hash_p3_dup_test"
                )
            self.assertIn("already published", str(ctx.exception).lower())

        # Fresh connection verify: exactly 1 package in DB
        verify_conn = sqlite3.connect(str(self.db_path))
        cur = verify_conn.cursor()
        cur.execute("SELECT COUNT(*), status FROM painting_takeoff_packages GROUP BY status")
        rows = cur.fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 1)
        self.assertEqual(rows[0][1], "Published")
        verify_conn.close()

    def test_p3_03_package_lifecycle_pending_published_failed(self):
        """P3 Item 3: Package transitions through Pending -> Failed on side-effect error, then Published on retry."""
        with patch("pb_planreader_3d_app.lquery", return_value=self.workspace_mock), \
             patch("pb_planreader_3d_app.dataframe_for_takeoff", return_value=self.sample_takeoff), \
             patch("pb_planreader_3d_app.quote_workbook_bytes", return_value=b"mock_quote"):

            # Simulate failure during progress package generation
            with patch("pb_planreader_3d_app.progress_package_bytes", side_effect=IOError("Corrupt 3D model render")):
                with self.assertRaises(RuntimeError) as ctx:
                    publish_job_to_jobhub(
                        1, self.bridge, "TesterFail",
                        preflight_fingerprint="fp_p3_lifecycle",
                        payload_hash="hash_p3_lifecycle"
                    )
                self.assertIn("Mandatory publish side effect failed", str(ctx.exception))

            # Verify package transitioned to 'Failed' in real SQLite DB
            verify_conn = sqlite3.connect(str(self.db_path))
            cur = verify_conn.cursor()
            cur.execute("SELECT id, status, notes FROM painting_takeoff_packages WHERE job_id=301")
            pkg = cur.fetchone()
            self.assertIsNotNone(pkg)
            self.assertEqual(pkg[1], "Failed")
            self.assertIn("Corrupt 3D model render", pkg[2])
            verify_conn.close()

            # Now retry with repaired downstream: must succeed without duplicate error
            with patch("pb_planreader_3d_app.progress_package_bytes", return_value=b"fixed_progress"):
                res_retry = publish_job_to_jobhub(
                    1, self.bridge, "TesterRetry",
                    preflight_fingerprint="fp_p3_lifecycle",
                    payload_hash="hash_p3_lifecycle"
                )
                self.assertTrue(res_retry["published"])
                self.assertEqual(res_retry["job_status"], "Published")

            # Verify retry published package and left failed attempt preserved for audit
            verify_conn = sqlite3.connect(str(self.db_path))
            cur = verify_conn.cursor()
            cur.execute("SELECT status FROM painting_takeoff_packages WHERE id=?", (pkg[0],))
            self.assertEqual(cur.fetchone()[0], "Failed")
            cur.execute("SELECT status FROM painting_takeoff_packages WHERE id=?", (res_retry["package_id"],))
            self.assertEqual(cur.fetchone()[0], "Published")
            cur.execute("SELECT COUNT(*) FROM painting_takeoff_packages WHERE job_id=301")
            self.assertEqual(cur.fetchone()[0], 2)
            verify_conn.close()

    def test_p3_04_multiple_contenders_concurrency_race(self):
        """P3 Item 4: Multiple contenders racing against real on-disk SQLite with BEGIN IMMEDIATE."""
        results = []
        errors = []
        lock = threading.Lock()
        num_contenders = 4
        barrier = threading.Barrier(num_contenders)

        def worker_contender(contender_idx):
            worker_bridge = JobHubBridge(kind="sqlite", source=str(self.db_path), timeout=1.0)
            try:
                barrier.wait(timeout=5.0)
                res = publish_job_to_jobhub(
                    1, worker_bridge, f"Contender_{contender_idx}",
                    preflight_fingerprint="fp_p3_race",
                    payload_hash="hash_p3_race"
                )
                with lock:
                    results.append((contender_idx, res))
            except Exception as exc:
                with lock:
                    errors.append((contender_idx, exc))

        with patch("pb_planreader_3d_app.lquery", return_value=self.workspace_mock), \
             patch("pb_planreader_3d_app.dataframe_for_takeoff", return_value=self.sample_takeoff), \
             patch("pb_planreader_3d_app.quote_workbook_bytes", return_value=b"mock_quote"), \
             patch("pb_planreader_3d_app.progress_package_bytes", return_value=b"mock_progress"):

            threads = [threading.Thread(target=worker_contender, args=(i,)) for i in range(num_contenders)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)

        # Invariant: exactly 1 winner, exactly (num_contenders - 1) failures
        self.assertEqual(len(results), 1, f"Expected exactly 1 winner, got {len(results)}. Errors: {errors}")
        self.assertEqual(len(errors), num_contenders - 1)
        self.assertTrue(results[0][1]["published"])

        # All errors must be legitimate lock contention or duplicate rejections
        for _, err in errors:
            err_msg = str(err).lower()
            self.assertTrue(
                any(k in err_msg for k in ("locked", "already", "duplicate", "lock acquisition failed")),
                f"Unexpected error: {err}"
            )

        # Verify exactly 1 package in DB
        verify_conn = sqlite3.connect(str(self.db_path))
        cur = verify_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM painting_takeoff_packages WHERE status='Published'")
        self.assertEqual(cur.fetchone()[0], 1)
        verify_conn.close()

    def test_p3_05_fresh_connection_final_db_state_integrity(self):
        """P3 Item 5: Fresh independent connection proves full SQLite DB state integrity."""
        with patch("pb_planreader_3d_app.lquery", return_value=self.workspace_mock), \
             patch("pb_planreader_3d_app.dataframe_for_takeoff", return_value=self.sample_takeoff), \
             patch("pb_planreader_3d_app.quote_workbook_bytes", return_value=b"excel_bytes_data"), \
             patch("pb_planreader_3d_app.progress_package_bytes", return_value=b"zip_bytes_data"):

            res = publish_job_to_jobhub(
                1, self.bridge, "IntegrityUser",
                preflight_fingerprint="fp_p3_integrity_test",
                payload_hash="hash_p3_integrity_test"
            )
            self.assertTrue(res["published"])

        # Completely fresh independent connection
        fresh_conn = sqlite3.connect(str(self.db_path))
        fresh_conn.row_factory = sqlite3.Row
        cur = fresh_conn.cursor()

        # 1. Verify painting_takeoff_packages
        cur.execute("SELECT * FROM painting_takeoff_packages WHERE job_id=301")
        packages = cur.fetchall()
        self.assertEqual(len(packages), 1)
        pkg = packages[0]
        self.assertEqual(pkg["status"], "Published")
        self.assertEqual(pkg["interior_total_m2"], 120.0)
        self.assertEqual(pkg["exterior_total_m2"], 80.0)
        self.assertEqual(pkg["total_labour_hours"], 14.0)
        self.assertEqual(pkg["total_paint_litres"], 35.0)
        self.assertIn("fp_p3_integrity_test", pkg["notes"])
        self.assertIn("hash_p3_integrity_test", pkg["notes"])

        # 2. Verify jobs table updated to Published
        cur.execute("SELECT status, notes FROM jobs WHERE id=301")
        job = cur.fetchone()
        self.assertEqual(job["status"], "Published")
        self.assertIn("Published by PB PlanReader", job["notes"])

        # 3. Verify painting_takeoff_lines
        cur.execute("SELECT * FROM painting_takeoff_lines WHERE package_id=? ORDER BY id", (pkg["id"],))
        lines = cur.fetchall()
        self.assertEqual(len(lines), 2)
        # Verify UTF-8 m² encoding
        self.assertEqual(lines[0]["unit"], "m²")
        self.assertEqual(lines[0]["m2"], 120.0)
        self.assertEqual(lines[1]["unit"], "m²")
        self.assertEqual(lines[1]["m2"], 80.0)

        # 4. Verify job_document_blobs (quotation & progress marker)
        cur.execute("SELECT file_name, doc_type, blob_data FROM job_document_blobs WHERE job_id=301 ORDER BY id")
        blobs = cur.fetchall()
        self.assertEqual(len(blobs), 2)
        doc_types = {b["doc_type"] for b in blobs}
        self.assertIn("Final quotation", doc_types)
        self.assertIn("Progress Marker", doc_types)

        fresh_conn.close()


if __name__ == "__main__":
    unittest.main()
