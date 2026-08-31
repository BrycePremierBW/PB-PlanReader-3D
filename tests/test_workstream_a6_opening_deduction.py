"""tests/test_workstream_a6_opening_deduction.py — Workstream A6 Regression Suite.

Issue #82: Door, window, & opening schedule deduction engine.
"""
from __future__ import annotations

import sqlite3
import unittest

from pb_opening_deduction_v175 import (
    OpeningRecord,
    OpeningDeductionRegistry,
    parse_opening_dimensions,
    derive_opening_deductions,
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


class WorkstreamA6OpeningTests(unittest.TestCase):

    def setUp(self):
        self.conn = _create_mock_db()
        cur = self.conn.cursor()
        cur.execute("INSERT INTO workspaces VALUES (1, 'JOB-A6', 'Opening Deduction Test')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_01_parse_opening_dimensions(self):
        """Parses opening height and width from schedule text strings."""
        h1, w1 = parse_opening_dimensions("Door D01 2040 x 820")
        h2, w2 = parse_opening_dimensions("Window W02 1200h x 1800w")

        self.assertEqual((h1, w1), (2040.0, 820.0))
        self.assertEqual((h2, w2), (1200.0, 1800.0))

    def test_02_deduction_threshold_rule(self):
        """Enforces Australian Standard threshold rule (< 0.5m² not deducted)."""
        cur = self.conn.cursor()
        # Large door 2040 x 820 = 1.67m² (Deductible)
        cur.execute("INSERT INTO register_items VALUES (10, 1, 'Door D01 2040 x 820 Qty: 1', 'Internal')")
        # Small vent opening 600 x 600 = 0.36m² (Not deductible)
        cur.execute("INSERT INTO register_items VALUES (20, 1, 'Opening V01 600 x 600 Qty: 1', 'Internal')")
        self.conn.commit()

        registry = derive_opening_deductions(self.conn, 1)
        self.assertEqual(len(registry.openings), 2)

        op10 = registry._by_id[10]
        op20 = registry._by_id[20]

        self.assertTrue(op10.is_deductible)
        self.assertAlmostEqual(op10.net_deduction_area_m2, 1.67, places=2)

        self.assertFalse(op20.is_deductible)
        self.assertEqual(op20.net_deduction_area_m2, 0.0)

    def test_03_door_leaf_paint_area(self):
        """Calculates door leaf paint surface area (2 x gross area for both faces)."""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO register_items VALUES (10, 1, 'Door D01 2040 x 820 Qty: 2', 'Internal')")
        self.conn.commit()

        registry = derive_opening_deductions(self.conn, 1)
        op10 = registry._by_id[10]

        # 2040x820 = 1.6728m² * 2 doors = 3.3456m² gross * 2 faces = 6.69m²
        self.assertAlmostEqual(op10.door_leaf_paint_area_m2, 6.69, places=2)

    def test_04_workspace_opening_deduction_registry(self):
        """derive_opening_deductions computes workspace total net deductions and total door leaf areas."""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO register_items VALUES (10, 1, 'Door D01 2040 x 820 Qty: 1', 'Internal')")
        cur.execute("INSERT INTO register_items VALUES (20, 1, 'Window W01 1200 x 1500 Qty: 1', 'External')")
        self.conn.commit()

        registry = derive_opening_deductions(self.conn, 1)
        # Door net = 1.67m², Window net = 1.80m² -> Total net = 3.47m²
        self.assertAlmostEqual(registry.total_net_deduction_m2(), 3.47, places=2)
        self.assertAlmostEqual(registry.total_door_leaf_paint_m2(), 3.35, places=2)


if __name__ == "__main__":
    unittest.main()
