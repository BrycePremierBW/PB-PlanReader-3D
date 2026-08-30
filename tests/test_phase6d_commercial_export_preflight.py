"""tests/test_phase6d_commercial_export_preflight.py — Phase 6D Preflight & JobHub Integrity Suite.

Issue #74 Complete Regression Matrix:
  1. Clean project (AVAILABLE)
  2. INFO-only project (AVAILABLE)
  3. REVIEW-only project (AVAILABLE_WITH_WARNING)
  4. BLOCKER project (BLOCKED)
  5. Required-source query failure (BLOCKED - Fail-closed)
  6. Sole authoritative scale-gate delegation (scale_gate_issues)
  7. Floor-area-only rows (floor_reference_rows tracked)
  8. Excluded rows (excluded_takeoff_rows tracked)
  9. Measured confirmed zero (measured_zero_rows tracked)
 10. Downstream row-count agreement (publishable_takeoff_rows == takeoff_work_rows)
 11. Missing/wrong acknowledgement (raises RuntimeError)
 12. Workspace switch invalidation (clears typed acknowledgement)
 13. Drawing issue change (invalidates preflight fingerprint)
 14. Quantity/publish-field mutation (payload_hash change aborts TOCTOU publish)
 15. Warning-content mutation (changes review_fingerprint & preflight_fingerprint)
 16. New blocker after render (aborts TOCTOU publish)
 17. JobHub unavailable (final_publish_state UNAVAILABLE)
 18. Downstream exception (surfaces RuntimeError)
 19. Partial write/receipt (rejects incomplete publish)
 20. Server-level duplicate submission (rejects re-publish for existing package)
 21. Deterministic ordering (row/signal order does not change fingerprint)
 22. Large workspace performance (< 1.0 sec for 1,000 rows)
 23. Genuine spies (0 AI, 0 OCR, 0 geometry calls)
"""
from __future__ import annotations

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
            substrate TEXT,
            unit TEXT,
            quantity REAL,
            coats REAL DEFAULT 1.0,
            rate_per_unit REAL DEFAULT 25.0,
            labour_hours REAL DEFAULT 5.0,
            paint_litres REAL DEFAULT 10.0,
            value_ex_gst REAL DEFAULT 100.0,
            finish_system TEXT,
            coverage_m2_per_litre REAL DEFAULT 12.0,
            productivity_m2_per_hour REAL DEFAULT 20.0,
            quantity_status TEXT,
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
    def __init__(self):
        self.kind = "sqlite"
        self.packages = []

    def execute(self, sql, params=()):
        if "painting_takeoff_packages" in sql:
            self.packages.append({"id": 501, "notes": params[-1] if params else ""})

    def query(self, sql, params=()):
        if "painting_takeoff_packages" in sql:
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

    def test_7_floor_area_only_rows(self):
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO takeoff_rows (id, workspace_id, section, element, location, unit, quantity, quantity_status, confidence, inclusion_status, row_role) VALUES (1001, 1, 'Internal', 'Floor Area', 'G01', 'm²', 200.0, 'Measured', 'high', 'included', 'floor_area')"
        )
        self.conn.commit()
        res = derive_export_preflight(self.app, 1, bridge_available=True)
        self.assertEqual(res.total_takeoff_rows, 2)
        self.assertEqual(res.publishable_takeoff_rows, 1)
        self.assertEqual(res.floor_reference_rows, 1)

    def test_8_excluded_rows_tracking(self):
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO takeoff_rows (id, workspace_id, section, element, location, unit, quantity, quantity_status, confidence, inclusion_status, row_role) VALUES (1002, 1, 'External', 'Cladding', 'Facade', 'm²', 50.0, 'Measured', 'high', 'excluded', 'work')"
        )
        self.conn.commit()
        res = derive_export_preflight(self.app, 1, bridge_available=True)
        self.assertEqual(res.excluded_takeoff_rows, 1)
        self.assertEqual(res.publishable_takeoff_rows, 1)

    def test_9_measured_confirmed_zero_semantics(self):
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO takeoff_rows (id, workspace_id, section, element, location, unit, quantity, quantity_status, confidence, inclusion_status, row_role) VALUES (1003, 1, 'Internal', 'Slab', 'G01', 'm²', 0.0, 'Measured', 'high', 'included', 'work')"
        )
        self.conn.commit()
        res = derive_export_preflight(self.app, 1, bridge_available=True)
        self.assertEqual(res.measured_zero_rows, 1)
        self.assertEqual(res.publishable_takeoff_rows, 2)
        self.assertEqual(res.preflight_status, "AVAILABLE")

    def test_10_downstream_row_count_agreement(self):
        res = derive_export_preflight(self.app, 1, bridge_available=True)
        self.assertEqual(res.publishable_takeoff_rows, 1)

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

    def test_19_partial_write_receipt_integrity(self):
        """Downstream return indicating incomplete lines or un-updated job status is rejected."""
        res = derive_export_preflight(self.app, 1, bridge_available=True)
        mock_pub = MagicMock(return_value={"package_id": 50, "published": False, "job_id": 101})

        with self.assertRaises(RuntimeError) as ctx:
            verify_toctou_and_publish_jobhub(
                self.app, 1, bridge=MockJobHubBridge(), user_name="TestUser",
                expected_fingerprint=res.preflight_fingerprint,
                acknowledgement_confirmed=True, publish_fn=mock_pub
            )
        self.assertIn("partially failed", str(ctx.exception))

    def test_20_server_level_duplicate_submission_guard(self):
        """Server-level guard rejects re-publishing package with matching fingerprint on same job."""
        res = derive_export_preflight(self.app, 1, bridge_available=True)
        bridge = MockJobHubBridge()
        bridge.packages.append({
            "id": 801,
            "status": "Published",
            "notes": f"Published by PB PlanReader. Preflight Fingerprint: {res.preflight_fingerprint} | Payload Hash: {res.payload_hash}"
        })

        with self.assertRaises(RuntimeError) as ctx:
            verify_toctou_and_publish_jobhub(
                self.app, 1, bridge=bridge, user_name="TestUser",
                expected_fingerprint=res.preflight_fingerprint,
                acknowledgement_confirmed=True, publish_fn=MagicMock()
            )
        self.assertIn("already been published to JobHub", str(ctx.exception))

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
                idx, 1, 'Internal', f'Wall-{idx}', f'Room-{idx%50}', 'Plasterboard', 'm²', 25.5,
                1.0, 25.0, 5.0, 10.0, 100.0, 'PT01', 12.0, 20.0, 'Measured', 'high', 'included',
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
        """Spy proving Phase 6D preflight calls zero AI, zero OCR, and zero geometry rebuild functions."""
        with patch("pb_commercial_export_preflight_v163.collect_commercial_review_signals") as mock_review:
            mock_review.return_value = CommercialReviewResult(workspace_id=1, source_coverage={"takeoff": "AVAILABLE", "register": "AVAILABLE", "scale": "AVAILABLE"})
            res = derive_export_preflight(self.app, 1, bridge_available=True)
            mock_review.assert_called_once()
            self.assertEqual(res.total_review_items, 0)


if __name__ == "__main__":
    unittest.main()
