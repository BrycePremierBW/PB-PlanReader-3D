"""tests/test_workstream_a7_paintable_surface.py — Workstream A7 Regression Suite.

Issue #83: Floor, ceiling, & roof paintable surface engine.
"""
from __future__ import annotations

import sqlite3
import unittest

from pb_paintable_surface_v176 import (
    SurfaceRecord,
    SurfaceEngineRegistry,
    calculate_pitched_surface_area,
    derive_surface_registry,
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
            finish_system TEXT
        );
        """
    )
    conn.commit()
    return conn


class WorkstreamA7SurfaceTests(unittest.TestCase):

    def setUp(self):
        self.conn = _create_mock_db()
        cur = self.conn.cursor()
        cur.execute("INSERT INTO workspaces VALUES (1, 'JOB-A7', 'Surface Engine Test')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_01_pitched_surface_area_trigonometry(self):
        """Calculates actual pitched surface area = flat_area / cos(pitch_rad)."""
        # 100m² flat area at 30 deg pitch = 100 / cos(30 deg) = 115.47m²
        pitched_area = calculate_pitched_surface_area(100.0, 30.0)
        self.assertAlmostEqual(pitched_area, 115.47, places=2)

        # 0 deg pitch returns exact flat area
        flat_area = calculate_pitched_surface_area(100.0, 0.0)
        self.assertEqual(flat_area, 100.0)

    def test_02_surface_type_classification(self):
        """Classifies surfaces into walls, flat ceilings, pitched ceilings, epoxy floor, linear skirting."""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO takeoff_rows VALUES (10, 1, 'Internal', 'Internal Wall', 'G01', 'Plasterboard', 'm²', 50.0, 'PT01')")
        cur.execute("INSERT INTO takeoff_rows VALUES (20, 1, 'Internal', 'Flat Ceiling', 'G01', 'Plasterboard', 'm²', 40.0, 'PT02')")
        cur.execute("INSERT INTO takeoff_rows VALUES (30, 1, 'Internal', 'Pitched Ceiling', 'G02', 'Plasterboard', 'm²', 40.0, 'PT02')")
        cur.execute("INSERT INTO takeoff_rows VALUES (40, 1, 'Internal', 'Skirting Timber', 'G01', 'Timber', 'm', 25.0, 'PT03')")
        self.conn.commit()

        registry = derive_surface_registry(self.conn, 1)
        self.assertEqual(len(registry.surfaces), 4)

        s10 = registry._by_id[10]
        s20 = registry._by_id[20]
        s30 = registry._by_id[30]
        s40 = registry._by_id[40]

        self.assertEqual(s10.surface_type, "INTERNAL_WALL")
        self.assertEqual(s20.surface_type, "CEILING_FLAT")
        self.assertEqual(s30.surface_type, "CEILING_PITCHED")
        self.assertTrue(s30.net_paintable_area_m2 > 40.0)  # Pitched area > flat projected area
        self.assertEqual(s40.surface_type, "SKIRTING_LINEAR")
        self.assertEqual(s40.linear_metres, 25.0)

    def test_03_workspace_surface_registry(self):
        """derive_surface_registry sums total paintable m² and linear skirting metres."""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO takeoff_rows VALUES (10, 1, 'Internal', 'Internal Wall', 'G01', 'Plasterboard', 'm²', 50.0, 'PT01')")
        cur.execute("INSERT INTO takeoff_rows VALUES (20, 1, 'Internal', 'Skirting Timber', 'G01', 'Timber', 'm', 30.0, 'PT03')")
        self.conn.commit()

        registry = derive_surface_registry(self.conn, 1)
        self.assertEqual(registry.total_paintable_area_m2(), 50.0)
        self.assertEqual(registry.total_skirting_linear_m(), 30.0)


if __name__ == "__main__":
    unittest.main()
