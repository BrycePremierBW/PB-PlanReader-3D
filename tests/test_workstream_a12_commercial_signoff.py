"""tests/test_workstream_a12_commercial_signoff.py — Workstream A12 Regression Suite.

Issue #88: JobHub export integrity & commercial takeoff sign-off.
"""
from __future__ import annotations

import sqlite3
import unittest

from pb_commercial_signoff_v181 import (
    CommercialSignoffRecord,
    CommercialSignoffAuthority,
    compute_commercial_signature,
    execute_commercial_signoff,
)


def _create_mock_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE workspaces (
            id INTEGER PRIMARY KEY,
            job_no TEXT,
            job_name TEXT
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
            paint_litres REAL,
            labour_hours REAL,
            value_ex_gst REAL,
            finish_system TEXT,
            source_page TEXT,
            notes TEXT,
            inclusion_status TEXT DEFAULT 'included'
        );
        """
    )
    conn.commit()
    return conn


class WorkstreamA12SignoffTests(unittest.TestCase):

    def setUp(self):
        self.conn = _create_mock_db()
        cur = self.conn.cursor()
        cur.execute("INSERT INTO workspaces VALUES (1, 'JOB-A12', 'Commercial Sign-Off Test')")
        cur.execute("INSERT INTO takeoff_rows VALUES (10, 1, 'Internal', 'Internal Wall', 'G01', 'Plasterboard', 'm²', 100.0, 20.0, 10.0, 2500.0, 'P1', 'A-101', 'Notes', 'included')")
        cur.execute("INSERT INTO takeoff_rows VALUES (20, 1, 'Internal', 'Flat Ceiling', 'G01', 'Plasterboard', 'm²', 50.0, 10.0, 5.0, 1250.0, 'P1', 'A-101', 'Notes', 'included')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_01_commercial_signature_computation(self):
        """Computes deterministic cryptographic signature for sign-off integrity."""
        sig1 = compute_commercial_signature(1, 3750.0, 30.0, 15.0, "fp_12345")
        sig2 = compute_commercial_signature(1, 3750.0, 30.0, 15.0, "fp_12345")
        sig3 = compute_commercial_signature(1, 3750.0, 30.0, 15.0, "fp_67890")

        self.assertEqual(sig1, sig2)
        self.assertNotEqual(sig1, sig3)
        self.assertEqual(len(sig1), 64)

    def test_02_execute_commercial_signoff(self):
        """Executes commercial sign-off and stores audit entry in DB."""
        signoff = execute_commercial_signoff(
            self.conn,
            workspace_id=1,
            estimator_name="Chief Estimator Bryce",
            fingerprint="fp_abc123",
        )

        self.assertEqual(signoff.workspace_id, 1)
        self.assertEqual(signoff.signoff_status, "SIGNED_OFF")
        self.assertEqual(signoff.estimator_name, "Chief Estimator Bryce")
        self.assertEqual(signoff.total_takeoff_value_ex_gst, 3750.0)
        self.assertEqual(signoff.total_paint_litres, 30.0)
        self.assertEqual(signoff.total_labour_hours, 15.0)

        # Audit summary covers all 12 workstreams
        self.assertEqual(len(signoff.workstream_audit_summary), 12)
        self.assertEqual(signoff.workstream_audit_summary["A12_CommercialSignoff"], "PASSED")

    def test_03_get_latest_signoff(self):
        """get_latest_signoff retrieves latest commercial sign-off record."""
        execute_commercial_signoff(self.conn, 1, "Estimator A", "fp_1")
        execute_commercial_signoff(self.conn, 1, "Estimator B", "fp_2")

        latest = CommercialSignoffAuthority.get_latest_signoff(self.conn, 1)
        self.assertIsNotNone(latest)
        self.assertEqual(latest.estimator_name, "Estimator B")
        self.assertEqual(latest.preflight_fingerprint, "fp_2")

    def test_04_full_master_accuracy_program_audit(self):
        """End-to-end verification that all 12 Master Accuracy Workstreams execute successfully."""
        from pb_multi_page_scale_v170 import derive_workspace_scale_authority
        from pb_revision_authority_v171 import derive_revision_authority
        from pb_sheet_classification_v172 import derive_sheet_classifications
        from pb_title_block_extractor_v173 import derive_title_block_metadata
        from pb_wall_topology_v174 import derive_wall_topology
        from pb_opening_deduction_v175 import derive_opening_deductions
        from pb_paintable_surface_v176 import derive_surface_registry
        from pb_substrate_mapper_v177 import derive_substrate_mapping
        from pb_australian_takeoff_standards_v178 import derive_australian_takeoff_authority
        from pb_3d_spatial_provenance_v179 import derive_3d_scene_provenance
        from pb_estimator_review_override_v180 import derive_estimator_overrides
        from pb_commercial_signoff_v181 import execute_commercial_signoff

        # Add tables for complete program verification
        cur = self.conn.cursor()
        cur.executescript(
            """
            CREATE TABLE pages (id INTEGER PRIMARY KEY, workspace_id INTEGER, page_label TEXT, page_type TEXT, page_number INTEGER, px_per_m REAL, scale_text TEXT, file_name TEXT, selected INTEGER DEFAULT 1);
            CREATE TABLE measurement_lines (id INTEGER PRIMARY KEY, workspace_id INTEGER, page_id INTEGER, line_type TEXT, length_m REAL, area_m2 REAL, raw_points TEXT);
            CREATE TABLE register_items (id INTEGER PRIMARY KEY, workspace_id INTEGER, item_name TEXT, status TEXT);
            INSERT INTO pages VALUES (10, 1, 'A-101 Floor Plan Rev B', 'Plan', 1, 100.0, '1:100', 'A-101_Rev_B.pdf', 1);
            """
        )
        self.conn.commit()

        # Run all 12 Workstream engines sequentially
        reg1 = derive_workspace_scale_authority(self.conn, 1)
        reg2 = derive_revision_authority(self.conn, 1)
        reg3 = derive_sheet_classifications(self.conn, 1)
        reg4 = derive_title_block_metadata(self.conn, 1)
        reg5 = derive_wall_topology(self.conn, 1)
        reg6 = derive_opening_deductions(self.conn, 1)
        reg7 = derive_surface_registry(self.conn, 1)
        reg8 = derive_substrate_mapping(self.conn, 1)
        reg9 = derive_australian_takeoff_authority(self.conn, 1)
        reg10 = derive_3d_scene_provenance(self.conn, 1)
        reg11 = derive_estimator_overrides(self.conn, 1)
        signoff = execute_commercial_signoff(self.conn, 1, "Lead Estimator", "fp_program_pass")

        self.assertEqual(signoff.signoff_status, "SIGNED_OFF")
        self.assertEqual(len(signoff.workstream_audit_summary), 12)
        for ws, status in signoff.workstream_audit_summary.items():
            self.assertEqual(status, "PASSED", f"Workstream {ws} failed audit")


if __name__ == "__main__":
    unittest.main()
