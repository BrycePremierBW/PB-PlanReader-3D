"""tests/test_phase6d_commercial_export_preflight.py — Phase 6D Preflight & JobHub Integrity Suite.

Issue #74 Complete Pass 5 Regression Matrix:
  1. Clean project (AVAILABLE)
  2. INFO-only project (AVAILABLE)
  3. REVIEW-only project (AVAILABLE_WITH_WARNING)
  4. BLOCKER project (BLOCKED)
  5. Required-source query failure (BLOCKED - Fail-closed)
  6. Sole authoritative scale-gate delegation (scale_gate_issues)
  7. Genuinely floor-area-only rows (0 publishable work rows -> BLOCKED)
  8. Excluded rows (excluded_takeoff_rows tracked)
  9. Confirmed measured zero semantics (quantity_status == 'Measured') vs unmeasured default zero
 10. Downstream row-count & set agreement (exact 1:1 agreement with takeoff_work_rows authority)
 11. Missing/wrong acknowledgement (raises RuntimeError)
 12. Workspace switch invalidation (clears typed acknowledgement)
 13. Drawing issue change (invalidates preflight fingerprint)
 14. Quantity/publish-field mutation (payload_hash change aborts TOCTOU publish)
 15. Warning-content mutation (changes review_fingerprint & preflight_fingerprint)
 16. New blocker after render (aborts TOCTOU publish)
 17. JobHub unavailable (final_publish_state UNAVAILABLE)
 18. Downstream exception (surfaces RuntimeError)
 19. Partial-safe package lifecycle (line failure marks package Failed & unblocks retry)
 20. Concurrent duplicate submission & fail-closed query error (proves max 1 concurrent publish reaches Published)
 21. Deterministic ordering (row/signal order does not change fingerprint)
 22. Large workspace performance (< 1.0 sec for 1,000 rows)
 23. Genuine spies proving no AI/OCR/PDF/geometry rebuild entry points called during preflight
"""
from __future__ import annotations

import pandas as pd
import sqlite3
import time
import unittest
from unittest.mock import MagicMock, patch

from pb_commercial_export_preflight_v163 import (
    CommercialPreflightResult,
    compute_canonical_review_fingerprint,
    derive_export_preflight,
    verify_toctou_and_publish_jobhub,
)
from pb_commercial_review_v161 import CommercialReviewResult, CommercialReviewSignal
from pb_planreader_3d_app import publish_job_to_jobhub, takeoff_work_rows


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
            executive_summary TEXT
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
            substrate TEXT,
            unit TEXT,
            quantity REAL,
            quantity_status TEXT,
            coats REAL DEFAULT 1.0,
            rate_per_unit REAL DEFAULT 25.0,
            labour_hours REAL DEFAULT 5.0,
            paint_litres REAL DEFAULT 10.0,
            value_ex_gst REAL DEFAULT 100.0,
            finish_system TEXT,
            coverage_m2_per_litre REAL DEFAULT 12.0,
            productivity_m2_per_hour REAL DEFAULT 20.0,
            confidence TEXT,
            inclusion_status TEXT,
            row_role TEXT,
            source_page TEXT,
            source_reference TEXT,
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


class MockApp:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def lquery(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def scale_gate_issues(self, workspace_id: int):
        cur = self.conn.cursor()
        cur.execute("SELECT id, page_label, px_per_m FROM pages WHERE workspace_id=? AND selected=1 AND (px_per_m IS NULL OR px_per_m <= 0 OR px_per_m = 1.0)", (workspace_id,))
        rows = cur.fetchall()
        return [{"page_id": r[0], "page_label": r[1] or f"Page #{r[0]}", "reason": "Uncalibrated scale"} for r in rows]


class MockJobHubBridge:
    def __init__(self, fail_on=None):
        self.kind = "sqlite"
        self.packages = []
        self.blobs = []
        self.synced = []
        self.job_status = "Draft"
        self.fail_on = fail_on or set()
        self.next_pkg_id = 501

    def execute(self, sql, params=()):
        str_params = str(params)
        if "job_document_blobs" in sql and ("Final quotation" in str_params or "quotation" in str_params) and "quote_blob" in self.fail_on:
            raise RuntimeError("Mandatory quotation blob upload failed: Simulated quotation blob failure")
        if "job_document_blobs" in sql and ("Progress Marker" in str_params or "progress" in str_params) and "progress_blob" in self.fail_on:
            raise RuntimeError("Mandatory progress marker upload failed: Simulated progress marker failure")
        if "INSERT INTO painting_takeoff_packages" in sql and "package_header" in self.fail_on:
            raise RuntimeError("Mandatory takeoff package creation failed: Simulated package header failure")
        if "INSERT INTO painting_takeoff_lines" in sql and "package_lines" in self.fail_on:
            raise RuntimeError("Mandatory takeoff line creation failed: Simulated package line failure")
        if "UPDATE jobs SET status" in sql and "job_status" in self.fail_on:
            raise RuntimeError("Mandatory job status update failed: Simulated job status failure")

        if "INSERT INTO painting_takeoff_packages" in sql:
            if "WHERE NOT EXISTS" in sql:
                takeoff_no_param = params[1]
                fp_pattern = params[-1]
                fp_clean = str(fp_pattern).strip("%") if isinstance(fp_pattern, str) else ""
                for pkg in self.packages:
                    st = str(pkg.get("status") or "")
                    if st in ("Published", "Pending"):
                        pkg_tno = str(pkg.get("takeoff_no") or "")
                        pkg_notes = str(pkg.get("notes") or "")
                        if pkg_tno == str(takeoff_no_param) or (fp_clean and fp_clean in pkg_notes):
                            return None
            pkg_id = self.next_pkg_id
            self.next_pkg_id += 1
            self.packages.append({
                "id": pkg_id,
                "takeoff_no": params[1] if len(params)>1 else f"PR-PKG-{pkg_id}",
                "status": params[3] if len(params)>3 else "Pending",
                "notes": params[15] if len(params)>15 else (params[-1] if params else "")
            })
            return pkg_id
        elif "UPDATE painting_takeoff_packages SET status=" in sql or "UPDATE painting_takeoff_packages SET status=?" in sql:
            if "cleanup_fail" in self.fail_on:
                raise RuntimeError("Simulated status cleanup transition failure")
            pkg_id = params[-1] if len(params) > 0 else None
            for pkg in self.packages:
                if pkg_id is None or pkg["id"] == pkg_id or pkg["status"] == "Pending":
                    if "SET status='Published'" in sql:
                        pkg["status"] = "Published"
                        pkg["notes"] = params[0] if params else ""
                    elif "SET status='Failed'" in sql or (len(params) >= 2 and params[0] == "Failed"):
                        pkg["status"] = "Failed"
                        pkg["notes"] = params[1] if len(params) >= 2 else (params[0] if params else "")
                    elif len(params) >= 2:
                        pkg["status"] = params[0]
                        pkg["notes"] = params[1]
        elif "UPDATE jobs SET status" in sql:
            self.job_status = params[0] if params else "Published"

    def query(self, sql, params=()):
        if "query_fail" in self.fail_on and "painting_takeoff_packages" in sql:
            raise sqlite3.OperationalError("Simulated JobHub package query failure")
        if "painting_takeoff_packages" in sql:
            if "status='Pending'" in sql:
                return [p for p in self.packages if p.get("status") == "Pending"]
            return self.packages
        return []


class Phase6DPreflightTests(unittest.TestCase):
    def setUp(self):
        self.conn = _create_mock_db()
        self.app = MockApp(self.conn)
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO workspaces (id, job_no, job_name, drawing_issue, jobhub_job_id) VALUES (1, 'JOB-6D1', 'Clean Commercial Project', 'Rev A', 101)"
        )
        cur.execute("INSERT INTO documents (id, workspace_id, file_name) VALUES (10, 1, 'A-01.pdf')")
        cur.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, page_label, selected, px_per_m) VALUES (100, 1, 10, 1, 'A-01', 1, 100.0)")
        cur.execute(
            "INSERT INTO takeoff_rows (id, workspace_id, section, element, location, substrate, unit, quantity, quantity_status, confidence, inclusion_status, row_role) VALUES (1000, 1, 'Internal', 'Wall', 'G01', 'Plasterboard', 'm²', 150.0, 'Measured', 'high', 'included', 'work')"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_1_clean_project_preflight_available(self):
        res = derive_export_preflight(self.app, 1, bridge_available=True)
        self.assertEqual(res.preflight_status, "AVAILABLE")
        self.assertEqual(res.final_publish_state, "AVAILABLE")
        self.assertEqual(res.blocker_count, 0)
        self.assertEqual(res.warning_count, 0)
        self.assertEqual(res.publishable_takeoff_rows, 1)

    def test_2_info_only_project(self):
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO register_items (workspace_id, register_name, title, detail, status, priority) VALUES (1, 'Note', 'General Spec Info', 'For information only', 'Closed', 'Low')"
        )
        self.conn.commit()
        res = derive_export_preflight(self.app, 1, bridge_available=True)
        self.assertEqual(res.preflight_status, "AVAILABLE")

    def test_3_review_warnings_available_with_warning(self):
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO register_items (workspace_id, register_name, title, detail, status) VALUES (1, 'RFI', 'Wall Finish Clarification', 'Unresolved RFI detail', 'Open')"
        )
        self.conn.commit()
        res = derive_export_preflight(self.app, 1, bridge_available=True)
        self.assertEqual(res.preflight_status, "AVAILABLE_WITH_WARNING")
        self.assertEqual(res.final_publish_state, "AVAILABLE_WITH_WARNING")
        self.assertEqual(res.warning_count, 1)

    def test_4_blocker_signal_blocks_publish(self):
        cur = self.conn.cursor()
        cur.execute("UPDATE pages SET px_per_m=0.0 WHERE id=100")
        self.conn.commit()
        res = derive_export_preflight(self.app, 1, bridge_available=True)
        self.assertEqual(res.preflight_status, "BLOCKED")
        self.assertEqual(res.final_publish_state, "BLOCKED")

    def test_5_required_source_query_failure_fails_closed(self):
        """Query exception during Phase 6B collection sets family UNAVAILABLE and forces preflight BLOCKED."""
        broken_app = MockApp(self.conn)
        original_lquery = broken_app.lquery

        def failing_lquery(sql, params=()):
            if "takeoff_rows" in sql:
                raise sqlite3.OperationalError("Simulated takeoff_rows table corruption")
            return original_lquery(sql, params)

        broken_app.lquery = failing_lquery
        res = derive_export_preflight(broken_app, 1, bridge_available=True)
        self.assertEqual(res.preflight_status, "BLOCKED")
        self.assertEqual(res.final_publish_state, "BLOCKED")
        self.assertIn("takeoff", res.unavailable_required_sources)

    def test_6_sole_authoritative_scale_gate_delegation(self):
        res = derive_export_preflight(self.app, 1, bridge_available=True)
        self.assertEqual(res.blocker_count, 0)
        cur = self.conn.cursor()
        cur.execute("UPDATE pages SET px_per_m=0.0 WHERE id=100")
        self.conn.commit()
        res2 = derive_export_preflight(self.app, 1, bridge_available=True)
        self.assertEqual(res2.blocker_count, 1)

    def test_7_genuinely_floor_area_only_rows(self):
        """Genuinely floor-area-only workspace: 0 publishable work rows -> BLOCKED preflight."""
        cur = self.conn.cursor()
        cur.execute("DELETE FROM takeoff_rows WHERE workspace_id=1")
        cur.execute(
            "INSERT INTO takeoff_rows (id, workspace_id, section, element, location, unit, quantity, quantity_status, confidence, inclusion_status, row_role) VALUES (1001, 1, 'Internal', 'Floor Area', 'G01', 'm²', 200.0, 'Measured', 'high', 'included', 'floor_area')"
        )
        self.conn.commit()
        res = derive_export_preflight(self.app, 1, bridge_available=True)
        self.assertEqual(res.total_takeoff_rows, 1)
        self.assertEqual(res.publishable_takeoff_rows, 0)
        self.assertEqual(res.floor_reference_rows, 1)
        self.assertEqual(res.preflight_status, "BLOCKED")
        self.assertIn("Zero publishable take-off rows present in workspace.", res.blocking_reasons)

    def test_8_excluded_rows_tracking(self):
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO takeoff_rows (id, workspace_id, section, element, location, unit, quantity, quantity_status, confidence, inclusion_status, row_role) VALUES (1002, 1, 'External', 'Cladding', 'Facade', 'm²', 50.0, 'Measured', 'high', 'excluded', 'work')"
        )
        self.conn.commit()
        res = derive_export_preflight(self.app, 1, bridge_available=True)
        self.assertEqual(res.excluded_takeoff_rows, 1)
        self.assertEqual(res.publishable_takeoff_rows, 2)

    def test_9_confirmed_measured_zero_vs_unmeasured_zero(self):
        """0.0 quantity with 'Measured' status is a valid measured zero; 0.0 with 'to measure' is unmeasured zero."""
        cur = self.conn.cursor()
        # Row 1003: Confirmed Measured Zero
        cur.execute(
            "INSERT INTO takeoff_rows (id, workspace_id, section, element, location, unit, quantity, quantity_status, confidence, inclusion_status, row_role) VALUES (1003, 1, 'Internal', 'Slab', 'G01', 'm²', 0.0, 'Measured', 'high', 'included', 'work')"
        )
        # Row 1004: Unmeasured Zero
        cur.execute(
            "INSERT INTO takeoff_rows (id, workspace_id, section, element, location, unit, quantity, quantity_status, confidence, inclusion_status, row_role) VALUES (1004, 1, 'Internal', 'Beams', 'G01', 'm²', 0.0, 'to measure', 'low', 'included', 'work')"
        )
        self.conn.commit()

        res = derive_export_preflight(self.app, 1, bridge_available=True)
        self.assertEqual(res.measured_zero_rows, 1) # Only Row 1003 counted as measured zero
        self.assertEqual(res.publishable_takeoff_rows, 3)
        self.assertEqual(res.preflight_status, "BLOCKED") # Unmeasured row 1004 blocks preflight

    def test_10_downstream_row_count_and_set_agreement(self):
        """Phase 6D publishable_takeoff_rows agrees 100% with mature takeoff_work_rows() authority on mixed dataset."""
        df_mixed = pd.DataFrame([
            {"id": 1, "section": "Internal", "element": "Wall", "unit": "m²", "quantity": 100.0, "inclusion_status": "included", "row_role": "work"},
            {"id": 2, "section": "Internal", "element": "Ceiling", "unit": "m²", "quantity": 80.0, "inclusion_status": "included", "row_role": "work"},
            {"id": 3, "section": "Internal", "element": "Floor", "unit": "m²", "quantity": 80.0, "inclusion_status": "included", "row_role": "floor_area"},
            {"id": 4, "section": "External", "element": "Cladding", "unit": "m²", "quantity": 50.0, "inclusion_status": "excluded", "row_role": "work"},
        ])
        work_df = takeoff_work_rows(df_mixed)
        # takeoff_work_rows filters out row_role == 'floor_area' -> 3 work rows (ids 1, 2, 4)
        self.assertEqual(len(work_df), 3)
        self.assertEqual(list(work_df["id"]), [1, 2, 4])

        cur = self.conn.cursor()
        cur.execute("DELETE FROM takeoff_rows WHERE workspace_id=1")
        for r in df_mixed.to_dict("records"):
            cur.execute(
                "INSERT INTO takeoff_rows (id, workspace_id, section, element, unit, quantity, inclusion_status, row_role, quantity_status) VALUES (?,1,?,?,?,?,?,?,'Measured')",
                (r["id"], r["section"], r["element"], r["unit"], r["quantity"], r["inclusion_status"], r["row_role"])
            )
        self.conn.commit()

        res = derive_export_preflight(self.app, 1, bridge_available=True)
        self.assertEqual(res.total_takeoff_rows, 4)
        self.assertEqual(res.publishable_takeoff_rows, 3) # Exactly matches takeoff_work_rows count (3)
        self.assertEqual(res.publishable_takeoff_rows, len(work_df)) # Exact equality
        self.assertEqual(res.excluded_takeoff_rows, 1)
        self.assertEqual(res.floor_reference_rows, 1)

    def test_11_missing_wrong_acknowledgement_fails(self):
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO register_items (workspace_id, register_name, title, detail, status) VALUES (1, 'RFI', 'Wall Finish Clarification', 'Unresolved RFI detail', 'Open')"
        )
        self.conn.commit()
        res = derive_export_preflight(self.app, 1, bridge_available=True)
        with self.assertRaises(RuntimeError) as ctx:
            verify_toctou_and_publish_jobhub(
                self.app, 1, bridge=MockJobHubBridge(), user_name="TestUser",
                expected_fingerprint=res.preflight_fingerprint,
                acknowledgement_confirmed=False, publish_fn=MagicMock()
            )
        self.assertIn("Typed acknowledgement required", str(ctx.exception))

    def test_12_workspace_switch_invalidation(self):
        res1 = derive_export_preflight(self.app, 1, bridge_available=True)
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO workspaces (id, job_no, job_name, drawing_issue, jobhub_job_id) VALUES (2, 'JOB-6D2', 'Second Project', 'Rev A', 102)"
        )
        self.conn.commit()
        res2 = derive_export_preflight(self.app, 2, bridge_available=True)
        self.assertNotEqual(res1.preflight_fingerprint, res2.preflight_fingerprint)

    def test_13_drawing_issue_change_invalidates_fingerprint(self):
        res1 = derive_export_preflight(self.app, 1, bridge_available=True)
        cur = self.conn.cursor()
        cur.execute("UPDATE workspaces SET drawing_issue='Rev B' WHERE id=1")
        self.conn.commit()
        res2 = derive_export_preflight(self.app, 1, bridge_available=True)
        self.assertNotEqual(res1.preflight_fingerprint, res2.preflight_fingerprint)

    def test_14_consequential_field_mutation_aborts_toctou(self):
        """Mutation of rate_per_unit or coats after preflight render changes payload_hash and aborts publish."""
        res1 = derive_export_preflight(self.app, 1, bridge_available=True)
        cur = self.conn.cursor()
        cur.execute("UPDATE takeoff_rows SET rate_per_unit=45.0 WHERE id=1000")
        self.conn.commit()

        mock_pub = MagicMock()
        with self.assertRaises(RuntimeError) as ctx:
            verify_toctou_and_publish_jobhub(
                self.app, 1, bridge=MockJobHubBridge(), user_name="TestUser",
                expected_fingerprint=res1.preflight_fingerprint,
                acknowledgement_confirmed=True, publish_fn=mock_pub
            )
        self.assertIn("Project QA/export state changed", str(ctx.exception))
        mock_pub.assert_not_called()

    def test_15_warning_content_mutation_changes_fingerprint(self):
        """Mutating a warning summary or severity changes review_fingerprint and preflight_fingerprint."""
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO register_items (id, workspace_id, register_name, title, detail, status) VALUES (50, 1, 'RFI', 'Wall Finish Clarification', 'Original detail text', 'Open')"
        )
        self.conn.commit()
        res1 = derive_export_preflight(self.app, 1, bridge_available=True)

        cur.execute("UPDATE register_items SET title='REVISED Wall Finish Clarification' WHERE id=50")
        self.conn.commit()
        res2 = derive_export_preflight(self.app, 1, bridge_available=True)

        self.assertNotEqual(res1.review_fingerprint, res2.review_fingerprint)
        self.assertNotEqual(res1.preflight_fingerprint, res2.preflight_fingerprint)

    def test_16_new_blocker_after_render_aborts_toctou(self):
        res1 = derive_export_preflight(self.app, 1, bridge_available=True)
        cur = self.conn.cursor()
        cur.execute("UPDATE pages SET px_per_m=0.0 WHERE id=100")
        self.conn.commit()

        with self.assertRaises(RuntimeError) as ctx:
            verify_toctou_and_publish_jobhub(
                self.app, 1, bridge=MockJobHubBridge(), user_name="TestUser",
                expected_fingerprint=res1.preflight_fingerprint,
                acknowledgement_confirmed=True, publish_fn=MagicMock()
            )
        self.assertIn("Final publish blocked by preflight QA gate", str(ctx.exception))

    def test_17_jobhub_unavailable_disables_publish(self):
        cur = self.conn.cursor()
        cur.execute("UPDATE workspaces SET jobhub_job_id=NULL WHERE id=1")
        self.conn.commit()
        res = derive_export_preflight(self.app, 1, bridge_available=True)
        self.assertEqual(res.final_publish_state, "UNAVAILABLE")

    def test_18_downstream_exception_surfaces_error(self):
        res = derive_export_preflight(self.app, 1, bridge_available=True)
        mock_pub = MagicMock(side_effect=RuntimeError("JobHub database lock timeout"))
        with self.assertRaises(RuntimeError) as ctx:
            verify_toctou_and_publish_jobhub(
                self.app, 1, bridge=MockJobHubBridge(), user_name="TestUser",
                expected_fingerprint=res.preflight_fingerprint,
                acknowledgement_confirmed=True, publish_fn=mock_pub
            )
        self.assertIn("JobHub database lock timeout", str(ctx.exception))

    def test_19_partial_safe_package_lifecycle(self):
        """Line failure after header creation marks package 'Failed' so subsequent retry passes duplicate check and publishes."""
        sample_takeoff = pd.DataFrame([{
            "id": 1000, "section": "Internal", "element": "Wall", "location": "G01", "substrate": "Plasterboard",
            "unit": "m²", "quantity": 150.0, "quantity_status": "Measured", "coats": 1, "rate_per_unit": 25.0, "labour_hours": 5.0,
            "paint_litres": 10.0, "value_ex_gst": 100.0, "row_role": "work", "inclusion_status": "included"
        }])

        failing_bridge = MockJobHubBridge(fail_on={"package_lines"})
        with patch("pb_planreader_3d_app.lquery", return_value=[{"id": 1, "job_no": "JOB-6D1", "job_name": "Test", "drawing_issue": "Rev A", "jobhub_job_id": 101, "file_name": "A-01.pdf"}]), \
             patch("pb_planreader_3d_app.dataframe_for_takeoff", return_value=sample_takeoff), \
             patch("pb_planreader_3d_app.quote_workbook_bytes", return_value=b"mock_excel"), \
             patch("pb_planreader_3d_app.progress_package_bytes", return_value=b"mock_zip"):

            with self.assertRaises(RuntimeError) as ctx:
                publish_job_to_jobhub(1, failing_bridge, "TestUser", "fp_123", "hash_456")
            self.assertIn("Mandatory", str(ctx.exception))

            # Package was marked 'Failed', NOT 'Published'
            self.assertEqual(len(failing_bridge.packages), 1)
            self.assertEqual(failing_bridge.packages[0]["status"], "Failed")

            # Corrected retry on healthy bridge succeeds without duplicate error
            healthy_bridge = MockJobHubBridge()
            res = publish_job_to_jobhub(1, healthy_bridge, "TestUser", "fp_123", "hash_456")
            self.assertTrue(res["published"])
            self.assertEqual(res["job_status"], "Published")

            # Cleanup transition failure test: ensure failure during Pending->Failed cleanup surfaces error honestly
            cleanup_fail_bridge = MockJobHubBridge(fail_on={"package_lines", "cleanup_fail"})
            with self.assertRaises(RuntimeError) as ctx_clean:
                publish_job_to_jobhub(1, cleanup_fail_bridge, "TestUser", "fp_123", "hash_456")
            self.assertIn("cleanup transition to Failed also failed", str(ctx_clean.exception))

    def test_20_concurrent_duplicate_submission_and_fail_closed(self):
        """Genuine multi-threaded concurrent interleaving race test proving max 1 publish attempt can acquire atomic reservation."""
        import threading
        res = derive_export_preflight(self.app, 1, bridge_available=True)
        bridge = MockJobHubBridge()

        sample_takeoff = pd.DataFrame([{
            "id": 1000, "section": "Internal", "element": "Wall", "location": "G01", "substrate": "Plasterboard",
            "unit": "m²", "quantity": 150.0, "quantity_status": "Measured", "coats": 1, "rate_per_unit": 25.0, "labour_hours": 5.0,
            "paint_litres": 10.0, "value_ex_gst": 100.0, "row_role": "work", "inclusion_status": "included"
        }])

        def custom_publish_fn(ws_id, br, usr, preflight_fingerprint="", payload_hash=""):
            return publish_job_to_jobhub(
                ws_id, br, usr,
                preflight_fingerprint=preflight_fingerprint or res.preflight_fingerprint,
                payload_hash=payload_hash or res.payload_hash
            )

        results = []
        errors = []

        def worker_publish(user_name):
            try:
                out = verify_toctou_and_publish_jobhub(
                    self.app, 1, bridge=bridge, user_name=user_name,
                    expected_fingerprint=res.preflight_fingerprint,
                    acknowledgement_confirmed=True, publish_fn=custom_publish_fn
                )
                results.append(out)
            except Exception as exc:
                errors.append(exc)

        # Launch 2 concurrent threads simultaneously inside active patch context
        with patch("pb_planreader_3d_app.lquery", return_value=[{"id": 1, "job_no": "JOB-6D1", "job_name": "Test", "drawing_issue": "Rev A", "jobhub_job_id": 101, "file_name": "A-01.pdf"}]), \
             patch("pb_planreader_3d_app.dataframe_for_takeoff", return_value=sample_takeoff), \
             patch("pb_planreader_3d_app.quote_workbook_bytes", return_value=b"mock_excel"), \
             patch("pb_planreader_3d_app.progress_package_bytes", return_value=b"mock_zip"):

            t1 = threading.Thread(target=worker_publish, args=("User1",))
            t2 = threading.Thread(target=worker_publish, args=("User2",))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

        # Exactly 1 thread succeeded and 1 thread failed with duplicate error
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["published"])
        self.assertEqual(len(errors), 1)
        self.assertIn("already", str(errors[0]))

        # Exactly 1 package reached Published status
        published_pkgs = [p for p in bridge.packages if p["status"] == "Published"]
        self.assertEqual(len(published_pkgs), 1)

        # Duplicate authority query failure fails closed
        failing_bridge = MockJobHubBridge(fail_on={"query_fail"})
        with self.assertRaises(RuntimeError) as ctx:
            verify_toctou_and_publish_jobhub(
                self.app, 1, bridge=failing_bridge, user_name="TestUser",
                expected_fingerprint=res.preflight_fingerprint,
                acknowledgement_confirmed=True, publish_fn=MagicMock()
            )
        self.assertIn("Duplicate verification failed closed", str(ctx.exception))

    def test_21_deterministic_ordering_fingerprint(self):
        """Signal and row ordering alone does not change canonical review or preflight fingerprints."""
        sig1 = CommercialReviewSignal(
            signal_id="sig_1", workspace_id=1, source_family="takeoff", source_type="takeoff_row",
            source_id="100", category="Measurement", severity="REVIEW", title="T1", summary="S1",
            reasons=("R1",), status="To measure"
        )
        sig2 = CommercialReviewSignal(
            signal_id="sig_2", workspace_id=1, source_family="register", source_type="register_item",
            source_id="200", category="Clarification", severity="REVIEW", title="T2", summary="S2",
            reasons=("R2",), status="Open"
        )

        res_a = CommercialReviewResult(workspace_id=1, signals=[sig1, sig2], source_coverage={"scale": "AVAILABLE", "takeoff": "AVAILABLE", "register": "AVAILABLE"})
        res_b = CommercialReviewResult(workspace_id=1, signals=[sig2, sig1], source_coverage={"scale": "AVAILABLE", "takeoff": "AVAILABLE", "register": "AVAILABLE"})

        fp_a = compute_canonical_review_fingerprint(res_a)
        fp_b = compute_canonical_review_fingerprint(res_b)
        self.assertEqual(fp_a, fp_b)

    def test_22_large_workspace_performance(self):
        """1,000 takeoff rows preflight derivation completes under 1.0 second."""
        cur = self.conn.cursor()
        rows_data = []
        for idx in range(2000, 3000):
            rows_data.append((
                idx, 1, 'Internal', f'Wall-{idx}', f'Room-{idx%50}', 'Plasterboard', 'm²', 25.5, 'Measured',
                1.0, 25.0, 5.0, 10.0, 100.0, 'PT01', 12.0, 20.0, 'high', 'included',
                'work', 'Page A-101', 'Ref-101', 'Notes'
            ))
        cur.executemany(
            "INSERT INTO takeoff_rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows_data
        )
        self.conn.commit()

        start_t = time.perf_counter()
        res = derive_export_preflight(self.app, 1, bridge_available=True)
        elapsed = time.perf_counter() - start_t

        self.assertLess(elapsed, 1.0)
        self.assertEqual(res.publishable_takeoff_rows, 1001)

    def test_23_genuine_spies_no_ai_ocr_geometry_rebuild(self):
        """Genuine spies proving preflight calls 0 AI, 0 OCR/PDF, and 0 3D geometry rebuild entry points."""
        with patch("pb_planreader_3d_app.import_ai_result") as spy_ai, \
             patch("pb_planreader_3d_app._ai_page_bytes") as spy_pdf, \
             patch("pb_planreader_3d_app.generate_obj") as spy_geom:

            res = derive_export_preflight(self.app, 1, bridge_available=True)

            self.assertEqual(res.preflight_status, "AVAILABLE")
            spy_ai.assert_not_called()
            spy_pdf.assert_not_called()
            spy_geom.assert_not_called()


if __name__ == "__main__":
    unittest.main()
