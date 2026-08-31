"""tests/test_workstream_a11_estimator_review_override.py — Workstream A11 Regression Suite.

Issue #87: Estimator review UI, manual override, & provenance tracing.
"""
from __future__ import annotations

import sqlite3
import unittest

from pb_estimator_review_override_v180 import (
    OverrideRecord,
    EstimatorOverrideRegistry,
    derive_estimator_overrides,
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
            element TEXT,
            quantity REAL,
            notes TEXT
        );

        CREATE TABLE takeoff_row_overrides (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER,
            row_id INTEGER,
            field_name TEXT,
            old_value TEXT,
            new_value TEXT,
            override_reason TEXT,
            estimator_name TEXT,
            timestamp TEXT
        );
        """
    )
    conn.commit()
    return conn


class WorkstreamA11OverrideTests(unittest.TestCase):

    def setUp(self):
        self.conn = _create_mock_db()
        cur = self.conn.cursor()
        cur.execute("INSERT INTO workspaces VALUES (1, 'JOB-A11', 'Estimator Override Test')")
        cur.execute("INSERT INTO takeoff_rows VALUES (100, 1, 'Internal Wall', 50.0, 'Initial Takeoff')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_01_apply_manual_override(self):
        """Applies manual override to takeoff row quantity and updates DB."""
        override = EstimatorOverrideRegistry.apply_override(
            self.conn,
            workspace_id=1,
            row_id=100,
            field_name="quantity",
            new_value=65.0,
            override_reason="Estimator site check verified additional wall height",
            estimator_name="Senior Estimator John",
        )

        self.assertEqual(override.row_id, 100)
        self.assertEqual(override.field_name, "quantity")
        self.assertEqual(override.old_value, "50.0")
        self.assertEqual(override.new_value, "65.0")
        self.assertEqual(override.estimator_name, "Senior Estimator John")

        # Verify DB value updated
        cur = self.conn.cursor()
        cur.execute("SELECT quantity, notes FROM takeoff_rows WHERE id=100")
        new_qty, notes = cur.fetchone()
        self.assertEqual(new_qty, 65.0)
        self.assertIn("MANUAL OVERRIDE", notes)

    def test_02_audit_trail_logging(self):
        """Records audit trail entry in takeoff_row_overrides table."""
        EstimatorOverrideRegistry.apply_override(
            self.conn,
            workspace_id=1,
            row_id=100,
            field_name="quantity",
            new_value=70.0,
            override_reason="Architectural addendum",
        )

        cur = self.conn.cursor()
        cur.execute("SELECT row_id, field_name, old_value, new_value FROM takeoff_row_overrides WHERE workspace_id=1")
        rows = cur.fetchall()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], (100, "quantity", "50.0", "70.0"))

    def test_03_workspace_override_registry(self):
        """derive_estimator_overrides loads all override records for a workspace."""
        EstimatorOverrideRegistry.apply_override(
            self.conn, workspace_id=1, row_id=100, field_name="quantity", new_value=60.0, override_reason="Reason 1"
        )
        EstimatorOverrideRegistry.apply_override(
            self.conn, workspace_id=1, row_id=100, field_name="quantity", new_value=65.0, override_reason="Reason 2"
        )

        registry = derive_estimator_overrides(self.conn, 1)
        self.assertEqual(len(registry.overrides), 2)
        self.assertEqual(registry.overrides[0].new_value, "60.0")
        self.assertEqual(registry.overrides[1].new_value, "65.0")


if __name__ == "__main__":
    unittest.main()
