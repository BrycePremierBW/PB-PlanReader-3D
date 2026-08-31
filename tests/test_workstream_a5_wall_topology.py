"""tests/test_workstream_a5_wall_topology.py — Workstream A5 Regression Suite.

Issue #81: Wall network topology & room space reconstruction.
"""
from __future__ import annotations

import json
import sqlite3
import unittest

from pb_wall_topology_v174 import (
    RoomSpaceRecord,
    WallTopologyRegistry,
    compute_polygon_area_and_perimeter,
    derive_wall_topology,
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

        CREATE TABLE pages (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            page_label TEXT,
            px_per_m REAL
        );

        CREATE TABLE measurement_lines (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            page_id INTEGER,
            line_type TEXT,
            length_m REAL,
            area_m2 REAL,
            raw_points TEXT
        );
        """
    )
    conn.commit()
    return conn


class WorkstreamA5TopologyTests(unittest.TestCase):

    def setUp(self):
        self.conn = _create_mock_db()
        cur = self.conn.cursor()
        cur.execute("INSERT INTO workspaces VALUES (1, 'JOB-A5', 'Topology Test')")
        cur.execute("INSERT INTO pages VALUES (10, 1, 'A-101 Floor Plan', 100.0)")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_01_polygon_area_and_perimeter_calculation(self):
        """Calculates area (m²) and perimeter (m) for a 10m x 5m rectangle at 100 px/m."""
        # 10m * 100 = 1000px, 5m * 100 = 500px
        pts = [(0, 0), (1000, 0), (1000, 500), (0, 500)]
        area_m2, perim_m = compute_polygon_area_and_perimeter(pts, px_per_m=100.0)

        self.assertAlmostEqual(area_m2, 50.0, places=2)
        self.assertAlmostEqual(perim_m, 30.0, places=2)

    def test_02_room_space_wall_surface_derivation(self):
        """Deduces room wall surface area = Perimeter * Wall Height."""
        pts = [(0, 0), (1000, 0), (1000, 500), (0, 500)]
        raw_json = json.dumps(pts)

        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO measurement_lines VALUES (100, 1, 10, 'polygon', 30.0, 50.0, ?)",
            (raw_json,)
        )
        self.conn.commit()

        registry = derive_wall_topology(self.conn, 1, default_wall_height=2.7)
        self.assertEqual(len(registry.rooms), 1)

        room = registry.rooms[0]
        self.assertEqual(room.area_m2, 50.0)
        self.assertEqual(room.perimeter_m, 30.0)
        self.assertAlmostEqual(room.wall_surface_area_m2, 81.0, places=2)  # 30 * 2.7 = 81m²

    def test_03_workspace_wall_topology_registry(self):
        """derive_wall_topology computes total floor area and total wall surface area."""
        cur = self.conn.cursor()
        pts1 = [(0, 0), (1000, 0), (1000, 500), (0, 500)]  # 50m², 30m perim
        pts2 = [(0, 0), (600, 0), (600, 400), (0, 400)]    # 24m², 20m perim
        cur.execute("INSERT INTO measurement_lines VALUES (101, 1, 10, 'polygon', 30.0, 50.0, ?)", (json.dumps(pts1),))
        cur.execute("INSERT INTO measurement_lines VALUES (102, 1, 10, 'polygon', 20.0, 24.0, ?)", (json.dumps(pts2),))
        self.conn.commit()

        registry = derive_wall_topology(self.conn, 1, default_wall_height=2.7)
        self.assertEqual(len(registry.rooms), 2)
        self.assertAlmostEqual(registry.total_floor_area_m2(), 74.0, places=2)
        self.assertAlmostEqual(registry.total_wall_surface_m2(), 135.0, places=2)  # (30+20)*2.7 = 135m²


if __name__ == "__main__":
    unittest.main()
