from __future__ import annotations
import math
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock

from pb_multi_page_scale_v170 import (
    PageScaleRecord,
    MultiPageScaleRegistry,
    derive_workspace_scale_authority,
    calculate_m_per_pt_from_px_per_m,
    recompute_page_scale_geometry,
)
from pb_commercial_review_v161 import collect_scale_review_signals


class ScaleAuthorityHardeningTests(unittest.TestCase):
    """Comprehensive regressions for Priority 1 through Priority 6 scale authority hardening."""

    def _create_production_db(self) -> sqlite3.Connection:
        """Creates an in-memory SQLite DB matching exact PlanReader production schema (pages.page_no)."""
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE workspaces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_no TEXT,
                job_name TEXT,
                drawing_issue TEXT,
                jobhub_job_id INTEGER
            );
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER,
                file_name TEXT
            );
            CREATE TABLE pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                workspace_id INTEGER NOT NULL,
                page_no INTEGER,
                page_label TEXT,
                page_type TEXT DEFAULT 'drawing',
                scale_text TEXT,
                px_per_m REAL,
                selected INTEGER DEFAULT 1,
                FOREIGN KEY(document_id) REFERENCES documents(id),
                FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
            );
            CREATE TABLE measurement_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER NOT NULL,
                page_id INTEGER NOT NULL,
                line_type TEXT,
                length_m REAL,
                area_m2 REAL
            );
            CREATE TABLE takeoff_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER NOT NULL,
                source_page TEXT,
                quantity REAL,
                quantity_status TEXT
            );
            """
        )
        conn.commit()
        return conn

    def test_priority1_production_schema_page_no_query(self):
        """Priority 1: derive_workspace_scale_authority succeeds on production pages.page_no schema."""
        conn = self._create_production_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO workspaces (id, job_no, job_name) VALUES (1, 'JOB-SCALE-1', 'Scale Project')")
        cur.execute("INSERT INTO documents (id, workspace_id, file_name) VALUES (10, 1, 'Plans.pdf')")
        cur.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, page_label, selected, px_per_m) VALUES (101, 1, 10, 1, 'A-01', 1, 100.0)")
        cur.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, page_label, selected, px_per_m) VALUES (102, 1, 10, 2, 'A-02', 1, 100.0)")
        conn.commit()

        # Call canonical authority derivation
        auth = derive_workspace_scale_authority(conn, 1)
        self.assertIsInstance(auth, MultiPageScaleRegistry)
        self.assertEqual(len(auth.records), 2)
        self.assertFalse(auth.is_blocked())
        self.assertEqual(len(auth.get_issues()), 0)

        # Verify page sequence / label ordering
        rec1 = auth._by_id.get(101)
        self.assertIsNotNone(rec1)
        self.assertEqual(rec1.scale_status, "CALIBRATED")
        self.assertEqual(rec1.px_per_m, 100.0)

    def test_priority1_legacy_page_number_fallback(self):
        """Priority 1: Legacy databases containing page_number also continue to resolve cleanly."""
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER NOT NULL,
                page_number INTEGER,
                page_label TEXT,
                page_type TEXT DEFAULT 'drawing',
                scale_text TEXT,
                px_per_m REAL,
                selected INTEGER DEFAULT 1
            );
            """
        )
        cur.execute("INSERT INTO pages (id, workspace_id, page_number, page_label, px_per_m, selected) VALUES (1, 99, 1, 'Old-1', 50.0, 1)")
        conn.commit()

        auth = derive_workspace_scale_authority(conn, 99)
        self.assertEqual(len(auth.records), 1)
        self.assertEqual(auth.records[0].scale_status, "CALIBRATED")

    def test_priority2_non_finite_scale_fails_closed(self):
        """Priority 2: NaN and +/-inf px_per_m are rejected and never become CALIBRATED."""
        conn = self._create_production_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO workspaces (id, job_no, job_name) VALUES (1, 'JOB-INF', 'Inf Project')")
        cur.execute("INSERT INTO documents (id, workspace_id, file_name) VALUES (10, 1, 'Plans.pdf')")

        # Insert non-finite scale rows
        cur.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, page_label, selected, px_per_m) VALUES (201, 1, 10, 1, 'P-Inf', 1, ?)", (float("inf"),))
        cur.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, page_label, selected, px_per_m) VALUES (202, 1, 10, 2, 'P-NegInf', 1, ?)", (float("-inf"),))
        cur.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, page_label, selected, px_per_m) VALUES (203, 1, 10, 3, 'P-Zero', 1, 0.0)")
        conn.commit()

        auth = derive_workspace_scale_authority(conn, 1)
        self.assertTrue(auth.is_blocked())

        rec_inf = auth._by_id.get(201)
        self.assertNotEqual(rec_inf.scale_status, "CALIBRATED")
        self.assertEqual(rec_inf.scale_status, "UNCALIBRATED")

        rec_neginf = auth._by_id.get(202)
        self.assertNotEqual(rec_neginf.scale_status, "CALIBRATED")
        self.assertEqual(rec_neginf.scale_status, "UNCALIBRATED")

        issues = auth.get_issues()
        issue_ids = {iss["page_id"] for iss in issues}
        self.assertIn(201, issue_ids)
        self.assertIn(202, issue_ids)
        self.assertIn(203, issue_ids)

    def test_priority3_raw_scale_malformed_result_fails_closed_to_unavailable(self):
        """Priority 3: Malformed issues returned from authority fail closed to UNAVAILABLE."""
        conn = self._create_production_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO workspaces (id, job_no, job_name) VALUES (1, 'JOB-TEST', 'Test')")
        conn.commit()

        # Case 1: get_issues returns a non-list (e.g. dict, string, None)
        mock_auth = MagicMock()
        mock_auth.get_issues.return_value = {"not": "a list"}
        with unittest.mock.patch("pb_commercial_review_v161.derive_workspace_scale_authority", return_value=mock_auth):
            signals, coverage = collect_scale_review_signals(conn, 1)
            self.assertEqual(coverage, "UNAVAILABLE")
            self.assertEqual(signals, [])

        # Case 2: get_issues returns a list containing non-dict items (e.g. [None] or ["bad"])
        mock_auth.get_issues.return_value = [None, {"page_id": 1, "reason": "ok"}]
        with unittest.mock.patch("pb_commercial_review_v161.derive_workspace_scale_authority", return_value=mock_auth):
            signals, coverage = collect_scale_review_signals(conn, 1)
            self.assertEqual(coverage, "UNAVAILABLE")
            self.assertEqual(signals, [])

        # Case 3: get_issues raises exception
        mock_auth.get_issues.side_effect = RuntimeError("DB corruption")
        with unittest.mock.patch("pb_commercial_review_v161.derive_workspace_scale_authority", return_value=mock_auth):
            signals, coverage = collect_scale_review_signals(conn, 1)
            self.assertEqual(coverage, "UNAVAILABLE")
            self.assertEqual(signals, [])

    def test_priority4_preserve_canonical_description(self):
        """Priority 4: Diagnostic signal preserves canonical 'description' when 'reason' is absent."""
        conn = self._create_production_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO workspaces (id, job_no, job_name) VALUES (1, 'JOB-DESC', 'Desc Test')")
        conn.commit()

        mock_auth = MagicMock()
        mock_auth.get_issues.return_value = [
            {
                "page_id": 501,
                "page_label": "Sheet-501",
                "description": "Specific canonical explanation of uncalibrated drawing",
            }
        ]
        with unittest.mock.patch("pb_commercial_review_v161.derive_workspace_scale_authority", return_value=mock_auth):
            signals, coverage = collect_scale_review_signals(conn, 1)
            self.assertEqual(coverage, "AVAILABLE")
            self.assertEqual(len(signals), 1)
            sig = signals[0]
            self.assertEqual(sig.summary, "Specific canonical explanation of uncalibrated drawing")
            self.assertIn("Specific canonical explanation of uncalibrated drawing", sig.reasons)

    def test_priority5_real_raw_sqlite_commercial_review_states(self):
        """Priority 5: Real raw SQLite connection integration through collect_scale_review_signals."""
        conn = self._create_production_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO workspaces (id, job_no, job_name) VALUES (1, 'JOB-A', 'A')")
        cur.execute("INSERT INTO workspaces (id, job_no, job_name) VALUES (2, 'JOB-B', 'B')")
        cur.execute("INSERT INTO documents (id, workspace_id, file_name) VALUES (10, 1, 'DocA.pdf')")
        cur.execute("INSERT INTO documents (id, workspace_id, file_name) VALUES (20, 2, 'DocB.pdf')")

        # Workspace 1: Fully calibrated
        cur.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, page_label, selected, px_per_m) VALUES (11, 1, 10, 1, 'A-01', 1, 100.0)")
        # Workspace 2: Uncalibrated
        cur.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, page_label, selected, px_per_m) VALUES (21, 2, 20, 1, 'B-01', 1, NULL)")
        conn.commit()

        # Workspace 1 must be clean AVAILABLE
        sigs_1, cov_1 = collect_scale_review_signals(conn, 1)
        self.assertEqual(cov_1, "AVAILABLE")
        self.assertEqual(len(sigs_1), 0)

        # Workspace 2 must yield BLOCKER signal
        sigs_2, cov_2 = collect_scale_review_signals(conn, 2)
        self.assertEqual(cov_2, "AVAILABLE")
        self.assertEqual(len(sigs_2), 1)
        self.assertEqual(sigs_2[0].severity, "BLOCKER")
        self.assertEqual(sigs_2[0].page_id, 21)

        # Closed connection must return UNAVAILABLE
        closed_conn = sqlite3.connect(":memory:")
        closed_conn.close()
        sigs_c, cov_c = collect_scale_review_signals(closed_conn, 1)
        self.assertEqual(cov_c, "UNAVAILABLE")
        self.assertEqual(sigs_c, [])

    def test_priority12_recompute_geometry_numeric_safety(self):
        """Priority 12: Non-finite values in recompute_page_scale_geometry fail closed."""
        conn = self._create_production_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO workspaces (id, job_no, job_name) VALUES (1, 'JOB-G', 'Geom')")
        cur.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, selected, px_per_m) VALUES (1, 1, 1, 1, 1, 100.0)")
        cur.execute("INSERT INTO measurement_lines (id, workspace_id, page_id, line_type, length_m, area_m2) VALUES (10, 1, 1, 'wall', 10.0, 30.0)")
        conn.commit()

        # Passing inf or nan returns 0 updates without crashing or writing corrupted numbers
        self.assertEqual(recompute_page_scale_geometry(conn, 1, 1, float("inf"), 100.0), 0)
        self.assertEqual(recompute_page_scale_geometry(conn, 1, 1, 100.0, float("inf")), 0)
        self.assertEqual(recompute_page_scale_geometry(conn, 1, 1, float("nan"), 100.0), 0)
        self.assertEqual(recompute_page_scale_geometry(conn, 1, 1, 100.0, float("nan")), 0)

        # Valid finite recompute updates correctly
        updated = recompute_page_scale_geometry(conn, 1, 1, 100.0, 200.0)
        self.assertEqual(updated, 1)
        cur.execute("SELECT length_m, area_m2 FROM measurement_lines WHERE id=10")
        length_m, area_m2 = cur.fetchone()
        self.assertAlmostEqual(length_m, 5.0)
        self.assertAlmostEqual(area_m2, 7.5)

    def test_priority13_calculate_m_per_pt_numeric_safety(self):
        """Priority 13: calculate_m_per_pt_from_px_per_m returns 0.0 on non-finite px_per_m."""
        self.assertEqual(calculate_m_per_pt_from_px_per_m(float("inf")), 0.0)
        self.assertEqual(calculate_m_per_pt_from_px_per_m(float("-inf")), 0.0)
        self.assertEqual(calculate_m_per_pt_from_px_per_m(float("nan")), 0.0)
        self.assertEqual(calculate_m_per_pt_from_px_per_m(0.0), 0.0)
        self.assertEqual(calculate_m_per_pt_from_px_per_m(-50.0), 0.0)

        # Valid finite conversion
        m_pt = calculate_m_per_pt_from_px_per_m(100.0, render_zoom=2.0)
        self.assertAlmostEqual(m_pt, 2.0 / 100.0)

    def test_priority6_cross_boundary_scale_and_phase6d(self):
        """Priority 6: Scale UNAVAILABLE or BLOCKER must prevent Phase 6D publish eligibility."""
        from pb_commercial_review_v161 import collect_commercial_review_signals

        conn = self._create_production_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO workspaces (id, job_no, job_name) VALUES (10, 'JOB-X', 'Cross')")
        cur.execute("INSERT INTO documents (id, workspace_id, file_name) VALUES (100, 10, 'P.pdf')")
        cur.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, page_label, selected, px_per_m) VALUES (1, 10, 100, 1, 'P1', 1, 100.0)")
        conn.commit()

        # Case A: Valid scale on production schema
        class RealConnApp:
            def __init__(self, c):
                self.conn = c
            def execute(self, *a, **kw):
                return self.conn.execute(*a, **kw)
            def cursor(self):
                return self.conn.cursor()

        app_obj = RealConnApp(conn)
        ws_dict = {"id": 10, "job_no": "JOB-X", "job_name": "Cross"}

        res_clean = collect_commercial_review_signals(app_obj, ws_dict)
        self.assertEqual(res_clean.source_coverage.get("scale"), "AVAILABLE")

        # Case B: Malformed scale helper monkeypatched -> coverage UNAVAILABLE -> coverage incomplete
        mock_bad_auth = MagicMock()
        mock_bad_auth.get_issues.return_value = "invalid-type"
        with unittest.mock.patch("pb_commercial_review_v161.derive_workspace_scale_authority", return_value=mock_bad_auth):
            res_bad = collect_commercial_review_signals(app_obj, ws_dict)
            self.assertEqual(res_bad.source_coverage.get("scale"), "UNAVAILABLE")
            self.assertFalse(res_bad.required_coverage_complete)

    def test_priority7_8_sqlite_begin_immediate_contention(self):
        """Priority 7 & 8: Explicit real SQLite lock contention proof with BEGIN IMMEDIATE."""
        import threading
        import time
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            db_file = str(Path(tmpdir) / "contention.db")
            conn_init = sqlite3.connect(db_file, timeout=1.0)
            conn_init.execute("CREATE TABLE accounts (id INT PRIMARY KEY, balance REAL)")
            conn_init.execute("INSERT INTO accounts VALUES (1, 1000.0)")
            conn_init.commit()
            conn_init.close()

            lock_acquired = threading.Event()
            release_lock = threading.Event()
            worker_error = []

            def locker():
                c = sqlite3.connect(db_file, timeout=1.0)
                cur = c.cursor()
                cur.execute("BEGIN IMMEDIATE")
                lock_acquired.set()
                release_lock.wait(timeout=5.0)
                c.rollback()
                c.close()

            def contender():
                lock_acquired.wait(timeout=5.0)
                c2 = sqlite3.connect(db_file, timeout=0.1)
                cur2 = c2.cursor()
                try:
                    cur2.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    worker_error.append(exc)
                finally:
                    c2.close()

            t1 = threading.Thread(target=locker)
            t2 = threading.Thread(target=contender)

            t1.start()
            t2.start()

            t2.join(timeout=3.0)
            release_lock.set()
            t1.join(timeout=3.0)

            self.assertFalse(t1.is_alive())
            self.assertFalse(t2.is_alive())
            self.assertEqual(len(worker_error), 1)
            self.assertIn("locked", str(worker_error[0]).lower())

    def test_section13_a_provisional_title_block_scale_blocks_commercial_authority(self):
        """Section 13.A: Title-block-only provisional scale cannot silently become final commercial authority."""
        conn = self._create_production_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO workspaces (id, job_no, job_name) VALUES (1, 'JOB-TB', 'Title Block Project')")
        cur.execute("INSERT INTO documents (id, workspace_id, file_name) VALUES (10, 1, 'Plans.pdf')")
        cur.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, page_label, selected, px_per_m, scale_text) VALUES (101, 1, 10, 1, 'A-01', 1, 0.0, '1:100')")
        conn.commit()

        auth = derive_workspace_scale_authority(conn, 1)
        rec = auth._by_id[101]
        self.assertEqual(rec.scale_status, "PROVISIONAL_AUTO")
        self.assertEqual(rec.calibration_method, "TITLE_BLOCK_TEXT")
        self.assertGreater(rec.px_per_m, 0.0)

        # MUST be blocked for commercial authority
        self.assertTrue(auth.is_blocked())
        issues = auth.get_issues()
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["page_id"], 101)
        self.assertEqual(issues[0]["issue_type"], "PROVISIONAL_TITLE_BLOCK_SCALE")
        self.assertIn("provisional", issues[0]["description"].lower())

    def test_section13_b_provisional_inherited_scale_blocks_commercial_authority(self):
        """Section 13.B: Inherited provisional scale cannot silently become final commercial authority."""
        conn = self._create_production_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO workspaces (id, job_no, job_name) VALUES (1, 'JOB-INH', 'Inherited Project')")
        cur.execute("INSERT INTO documents (id, workspace_id, file_name) VALUES (10, 1, 'Plans.pdf')")
        cur.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, page_label, selected, px_per_m, scale_text) VALUES (101, 1, 10, 1, 'A-01', 1, 100.0, '1:100')")
        cur.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, page_label, selected, px_per_m, scale_text) VALUES (102, 1, 10, 2, 'A-02', 1, 0.0, '')")
        cur.execute("INSERT INTO takeoff_rows (id, workspace_id, source_page, quantity) VALUES (1, 1, 'A-02', 25.0)")
        conn.commit()

        auth = derive_workspace_scale_authority(conn, 1)
        rec2 = auth._by_id[102]
        self.assertEqual(rec2.scale_status, "PROVISIONAL_AUTO")
        self.assertEqual(rec2.calibration_method, "INHERITED")
        self.assertEqual(rec2.px_per_m, 100.0)

        # MUST be blocked for commercial authority
        self.assertTrue(auth.is_blocked())
        issues = auth.get_issues()
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["page_id"], 102)
        self.assertEqual(issues[0]["issue_type"], "PROVISIONAL_INHERITED_SCALE")
        self.assertIn("inherited", issues[0]["description"].lower())

    def test_section13_c_calibrated_scale_remains_unblocked(self):
        """Section 13.C: CALIBRATED scale still works and is not blocked."""
        conn = self._create_production_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO workspaces (id, job_no, job_name) VALUES (1, 'JOB-CAL', 'Calibrated')")
        cur.execute("INSERT INTO documents (id, workspace_id, file_name) VALUES (10, 1, 'Plans.pdf')")
        cur.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, page_label, selected, px_per_m) VALUES (101, 1, 10, 1, 'A-01', 1, 100.0)")
        cur.execute("INSERT INTO measurement_lines (id, workspace_id, page_id, line_type, length_m) VALUES (1, 1, 101, 'wall', 10.0)")
        cur.execute("INSERT INTO takeoff_rows (id, workspace_id, source_page, quantity) VALUES (1, 1, 'A-01', 50.0)")
        conn.commit()

        auth = derive_workspace_scale_authority(conn, 1)
        self.assertFalse(auth.is_blocked())
        self.assertEqual(len(auth.get_issues()), 0)

    def test_section13_d_not_required_pages_do_not_create_false_blockers(self):
        """Section 13.D: NOT_REQUIRED pages do not create false blockers."""
        conn = self._create_production_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO workspaces (id, job_no, job_name) VALUES (1, 'JOB-NR', 'Not Required')")
        cur.execute("INSERT INTO documents (id, workspace_id, file_name) VALUES (10, 1, 'Plans.pdf')")
        cur.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, page_label, page_type, selected, px_per_m) VALUES (101, 1, 10, 1, 'Cover Sheet', 'cover', 1, 0.0)")
        cur.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, page_label, page_type, selected, px_per_m) VALUES (102, 1, 10, 2, 'Drawing Register', 'index', 1, 0.0)")
        cur.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, page_label, page_type, selected, px_per_m) VALUES (103, 1, 10, 3, 'Floor Plan', 'plan', 1, 100.0)")
        conn.commit()

        auth = derive_workspace_scale_authority(conn, 1)
        self.assertEqual(auth._by_id[101].scale_status, "NOT_REQUIRED")
        self.assertEqual(auth._by_id[102].scale_status, "NOT_REQUIRED")
        self.assertEqual(auth._by_id[103].scale_status, "CALIBRATED")
        self.assertFalse(auth.is_blocked())
        self.assertEqual(len(auth.get_issues()), 0)

    def test_section13_e_missing_page_no_does_not_substitute_primary_key(self):
        """Section 13.E: Missing page_no/page_number does not gain authority through arbitrary primary key substitution."""
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER NOT NULL,
                page_label TEXT,
                page_type TEXT DEFAULT 'drawing',
                scale_text TEXT,
                px_per_m REAL,
                selected INTEGER DEFAULT 1
            );
            CREATE TABLE measurement_lines (id INTEGER PRIMARY KEY, workspace_id INT, page_id INT);
            CREATE TABLE takeoff_rows (id INTEGER PRIMARY KEY, workspace_id INT, source_page TEXT);
            """
        )
        cur.execute("INSERT INTO pages (id, workspace_id, page_label, px_per_m, selected) VALUES (999, 1, NULL, 100.0, 1)")
        conn.commit()

        auth = derive_workspace_scale_authority(conn, 1)
        rec = auth.records[0]
        # Label must not pretend primary key 999 is drawing sequence 'Page 999'
        self.assertEqual(rec.page_label, "Page [ID:999]")

    def test_section13_f_real_production_db_initialization(self):
        """Section 13.F: Real production DB initialization is exercised in tests."""
        from pathlib import Path
        from pb_planreader_3d_app import init_local_db
        import pb_planreader_3d_app

        with tempfile.TemporaryDirectory() as tmpdir:
            orig_data_dir = pb_planreader_3d_app.DATA_DIR
            orig_db_path = pb_planreader_3d_app.DB_PATH
            try:
                temp_dir = Path(tmpdir)
                pb_planreader_3d_app.DATA_DIR = temp_dir
                pb_planreader_3d_app.DB_PATH = temp_dir / "planreader.db"

                # Run real production schema initialization
                init_local_db()

                # Verify table creation and canonical pages.page_no presence
                conn = pb_planreader_3d_app.local_connect()
                cur = conn.cursor()
                cur.execute("PRAGMA table_info(pages)")
                cols = {r[1] for r in cur.fetchall()}
                self.assertIn("page_no", cols)
                self.assertNotIn("page_number", cols)

                cur.execute("PRAGMA table_info(measurement_lines)")
                m_cols = {r[1] for r in cur.fetchall()}
                self.assertIn("page_id", m_cols)

                cur.execute("PRAGMA table_info(takeoff_rows)")
                t_cols = {r[1] for r in cur.fetchall()}
                self.assertIn("source_page", t_cols)

                # Insert workspace and test derivation on real production db
                cur.execute("INSERT INTO workspaces (id, job_no, job_name) VALUES (1, 'PROD-1', 'Real DB')")
                cur.execute("INSERT INTO documents (id, workspace_id, file_name) VALUES (1, 1, 'Doc.pdf')")
                cur.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, page_label, selected, px_per_m) VALUES (10, 1, 1, 1, 'P-01', 1, 100.0)")
                conn.commit()

                auth = derive_workspace_scale_authority(conn, 1)
                self.assertEqual(len(auth.records), 1)
                self.assertEqual(auth.records[0].scale_status, "CALIBRATED")
                self.assertFalse(auth.is_blocked())
                conn.close()
            finally:
                pb_planreader_3d_app.DATA_DIR = orig_data_dir
                pb_planreader_3d_app.DB_PATH = orig_db_path

    def test_section13_g_missing_required_schema_fails_closed(self):
        """Section 13.G: Missing required schema cannot silently become safe empty evidence."""
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER NOT NULL,
                page_no INTEGER,
                page_label TEXT,
                page_type TEXT DEFAULT 'drawing',
                px_per_m REAL,
                scale_text TEXT,
                selected INTEGER DEFAULT 1
            );
            """
        )
        cur.execute("INSERT INTO pages (id, workspace_id, page_no, page_label, px_per_m, selected) VALUES (1, 1, 1, 'A-01', 100.0, 1)")
        conn.commit()

        auth = derive_workspace_scale_authority(conn, 1)
        self.assertTrue(auth.is_blocked())
        issues = auth.get_issues()
        issue_types = [iss["issue_type"] for iss in issues]
        self.assertIn("MISSING_REQUIRED_SCHEMA", issue_types)

        sigs, cov = collect_scale_review_signals(conn, 1)
        self.assertEqual(cov, "AVAILABLE")
        self.assertTrue(any(s.severity == "BLOCKER" for s in sigs))
        self.assertTrue(any("schema" in s.summary.lower() for s in sigs))

    def test_section13_i_commercial_description_and_title_survive_to_review_ui(self):
        """Section 13.I: Commercial reason/description and informative title survive to review UI."""
        conn = self._create_production_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO workspaces (id, job_no, job_name) VALUES (1, 'JOB-DESC', 'Desc')")
        cur.execute("INSERT INTO documents (id, workspace_id, file_name) VALUES (10, 1, 'Plans.pdf')")
        cur.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, page_label, selected, px_per_m, scale_text) VALUES (101, 1, 10, 1, 'A-101', 1, 0.0, '1:50')")
        conn.commit()

        sigs, cov = collect_scale_review_signals(conn, 1)
        self.assertEqual(cov, "AVAILABLE")
        self.assertEqual(len(sigs), 1)
        sig = sigs[0]
        self.assertEqual(sig.severity, "BLOCKER")
        self.assertIn("Provisional Title-Block Scale", sig.title)
        self.assertIn("1:50", sig.summary)
        self.assertIn("explicit calibration", sig.summary.lower())

    def test_section13_j_phase6b_and_phase6d_identical_scale_policy(self):
        """Section 13.J: Phase 6B and Phase 6D reflect the same scale policy."""
        from pb_commercial_export_preflight_v163 import derive_export_preflight
        from pb_commercial_review_v161 import collect_commercial_review_signals

        conn = self._create_production_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO workspaces (id, job_no, job_name, drawing_issue) VALUES (1, 'JOB-J', 'Policy Test', 'Rev 1')")
        cur.execute("INSERT INTO documents (id, workspace_id, file_name) VALUES (10, 1, 'Plans.pdf')")
        cur.execute("INSERT INTO pages (id, workspace_id, document_id, page_no, page_label, selected, px_per_m, scale_text) VALUES (101, 1, 10, 1, 'A-101', 1, 0.0, '1:100')")
        cur.execute("INSERT INTO takeoff_rows (id, workspace_id, source_page, quantity, quantity_status) VALUES (1, 1, 'A-101', 10.0, 'Measured')")
        conn.commit()

        class AppWrap:
            def __init__(self, c):
                self.conn = c
            def execute(self, *a, **kw):
                return self.conn.execute(*a, **kw)
            def cursor(self):
                return self.conn.cursor()

        app = AppWrap(conn)
        ws_dict = {"id": 1, "job_no": "JOB-J", "job_name": "Policy Test", "drawing_issue": "Rev 1"}

        rev_res = collect_commercial_review_signals(app, ws_dict)
        self.assertGreater(rev_res.blocker_count, 0)
        scale_blockers = [s for s in rev_res.signals if s.source_family == "scale" and s.severity == "BLOCKER"]
        self.assertEqual(len(scale_blockers), 1)

        preflight = derive_export_preflight(conn, 1, bridge_available=True)
        self.assertEqual(preflight.preflight_status, "BLOCKED")
        self.assertGreater(preflight.blocker_count, 0)
        self.assertTrue(any("scale" in r.lower() or "calibration" in r.lower() for r in preflight.blocking_reasons))
