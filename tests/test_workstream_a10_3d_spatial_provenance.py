"""tests/test_workstream_a10_3d_spatial_provenance.py — Workstream A10 Regression Suite.

Issue #86: Canonical 3D scene & spatial inspection provenance engine.
"""
from __future__ import annotations

import sqlite3
import unittest

from pb_3d_spatial_provenance_v179 import (
    SpatialProvenanceNode,
    SceneProvenanceGraph,
    derive_3d_scene_provenance,
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
            page_label TEXT
        );

        CREATE TABLE takeoff_rows (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            element TEXT,
            quantity REAL,
            source_page TEXT
        );
        """
    )
    conn.commit()
    return conn


class WorkstreamA10ProvenanceTests(unittest.TestCase):

    def setUp(self):
        self.conn = _create_mock_db()
        cur = self.conn.cursor()
        cur.execute("INSERT INTO workspaces VALUES (1, 'JOB-A10', '3D Scene Provenance Test')")
        cur.execute("INSERT INTO pages VALUES (10, 1, 'A-101')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_01_spatial_provenance_node_creation(self):
        """Creates 3D spatial node with deterministic provenance hash."""
        node = SpatialProvenanceNode(
            node_id="node_3d_100",
            takeoff_row_id=100,
            measurement_line_id=1000,
            page_id=10,
            sheet_number="A-101",
            element_type="WALL_MESH",
            position_3d=(0.0, 0.0, 0.0),
            dimensions_3d=(10.0, 5.0, 2.7),
            surface_area_m2=50.0,
            provenance_hash="a1b2c3d4e5f6",
        )
        self.assertEqual(node.node_id, "node_3d_100")
        self.assertEqual(node.element_type, "WALL_MESH")
        self.assertEqual(node.provenance_hash, "a1b2c3d4e5f6")

    def test_02_bidirectional_lookup(self):
        """Looks up 3D scene node by 2D takeoff row ID."""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO takeoff_rows VALUES (100, 1, 'Internal Wall', 50.0, 'A-101')")
        self.conn.commit()

        graph = derive_3d_scene_provenance(self.conn, 1)
        node = graph.lookup_by_takeoff_row(100)

        self.assertIsNotNone(node)
        self.assertEqual(node.node_id, "node_3d_100")
        self.assertEqual(node.surface_area_m2, 50.0)

    def test_03_workspace_3d_scene_graph(self):
        """derive_3d_scene_provenance builds total scene graph across workspace."""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO takeoff_rows VALUES (100, 1, 'Internal Wall', 50.0, 'A-101')")
        cur.execute("INSERT INTO takeoff_rows VALUES (200, 1, 'Flat Ceiling', 40.0, 'A-101')")
        self.conn.commit()

        graph = derive_3d_scene_provenance(self.conn, 1)
        self.assertEqual(len(graph.nodes), 2)
        self.assertEqual(graph.total_scene_surface_area_m2(), 90.0)


if __name__ == "__main__":
    unittest.main()
