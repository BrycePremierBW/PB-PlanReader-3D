"""tests/test_workstream_a9_australian_takeoff_standards.py — Workstream A9 Regression Suite.

Issue #85: Deductions, height rules, & Australian standards takeoff authority.
"""
from __future__ import annotations

import sqlite3
import unittest

from pb_australian_takeoff_standards_v178 import (
    AustralianTakeoffRuleResult,
    AustralianTakeoffRegistry,
    calculate_height_surcharge_factor,
    apply_minimum_area_rule,
    derive_australian_takeoff_authority,
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
            unit TEXT,
            location TEXT,
            notes TEXT
        );
        """
    )
    conn.commit()
    return conn


class WorkstreamA9StandardsTests(unittest.TestCase):

    def setUp(self):
        self.conn = _create_mock_db()
        cur = self.conn.cursor()
        cur.execute("INSERT INTO workspaces VALUES (1, 'JOB-A9', 'Australian Standards Test')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_01_height_surcharge_factors(self):
        """Applies height work surcharges (<3.0m 1.0x, 3.0-4.5m 1.15x, >=4.5m 1.30x)."""
        factor1, scaf1, _ = calculate_height_surcharge_factor(2.7)
        factor2, scaf2, _ = calculate_height_surcharge_factor(3.6)
        factor3, scaf3, _ = calculate_height_surcharge_factor(5.0)

        self.assertEqual((factor1, scaf1), (1.00, False))
        self.assertEqual((factor2, scaf2), (1.15, True))
        self.assertEqual((factor3, scaf3), (1.30, True))

    def test_02_minimum_area_rule(self):
        """Enforces 0.5m² minimum area rule for small structural items/columns."""
        qty_small = apply_minimum_area_rule(0.2, "m²", is_small_item=True)
        qty_normal = apply_minimum_area_rule(0.2, "m²", is_small_item=False)

        self.assertEqual(qty_small, 0.5)
        self.assertEqual(qty_normal, 0.2)

    def test_03_workspace_australian_takeoff_registry(self):
        """derive_australian_takeoff_authority computes height surcharges and scaffold area."""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO takeoff_rows VALUES (10, 1, 'Standard Wall', 100.0, 'm²', 'G01', 'Standard')")
        cur.execute("INSERT INTO takeoff_rows VALUES (20, 1, 'High Void Wall', 100.0, 'm²', 'G02', 'High Void')")
        self.conn.commit()

        registry = derive_australian_takeoff_authority(self.conn, 1, default_height=2.7)
        self.assertEqual(len(registry.rule_results), 2)

        res10 = registry._by_id[10]
        res20 = registry._by_id[20]

        self.assertEqual(res10.height_surcharge_factor, 1.00)
        self.assertEqual(res10.adjusted_quantity, 100.0)

        self.assertEqual(res20.height_surcharge_factor, 1.30)
        self.assertEqual(res20.adjusted_quantity, 130.0)
        self.assertEqual(registry.total_scaffold_surface_area_m2(), 130.0)


if __name__ == "__main__":
    unittest.main()
