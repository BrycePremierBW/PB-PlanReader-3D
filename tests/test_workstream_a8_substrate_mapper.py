"""tests/test_workstream_a8_substrate_mapper.py — Workstream A8 Regression Suite.

Issue #84: Finish schedule & specification substrate/system mapper.
"""
from __future__ import annotations

import sqlite3
import unittest

from pb_substrate_mapper_v177 import (
    SubstratePaintSystem,
    SubstrateMapperRegistry,
    map_finish_code_and_substrate,
    derive_substrate_mapping,
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
            finish_system TEXT,
            substrate TEXT
        );
        """
    )
    conn.commit()
    return conn


class WorkstreamA8SubstrateTests(unittest.TestCase):

    def setUp(self):
        self.conn = _create_mock_db()
        cur = self.conn.cursor()
        cur.execute("INSERT INTO workspaces VALUES (1, 'JOB-A8', 'Substrate Mapper Test')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_01_map_finish_code_and_substrate(self):
        """Maps canonical finish codes P1, P2, FC01, EP01 accurately."""
        sys_p1 = map_finish_code_and_substrate("P1", "Plasterboard")
        sys_p2 = map_finish_code_and_substrate("P2", "Timber")
        sys_fc = map_finish_code_and_substrate("FC01", "Fibre Cement")
        sys_ep = map_finish_code_and_substrate("EP01", "Concrete Floor")

        self.assertEqual(sys_p1.coat_count, 3)
        self.assertEqual(sys_p1.coverage_m2_per_litre, 16.0)

        self.assertEqual(sys_p2.substrate, "Timber / Doors")
        self.assertEqual(sys_fc.finish_code, "FC01")
        self.assertEqual(sys_ep.coat_count, 2)

    def test_02_litres_and_labour_calculation(self):
        """Calculates paint litres and labor hours accurately."""
        sys_p1 = map_finish_code_and_substrate("P1")

        # 160m² * 3 coats / 16.0 m²/L = 30 Litres
        litres = sys_p1.calculate_litres(160.0)
        self.assertAlmostEqual(litres, 30.0, places=2)

        # 160m² * 3 coats / 15.0 m²/h = 32 Labour Hours
        hours = sys_p1.calculate_labour_hours(160.0)
        self.assertAlmostEqual(hours, 32.0, places=2)

    def test_03_workspace_substrate_registry(self):
        """derive_substrate_mapping maps paint systems across all takeoff rows in workspace."""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO takeoff_rows VALUES (10, 1, 'P1', 'Plasterboard')")
        cur.execute("INSERT INTO takeoff_rows VALUES (20, 1, 'P2', 'Timber Frame')")
        self.conn.commit()

        registry = derive_substrate_mapping(self.conn, 1)
        self.assertEqual(len(registry.mapped_systems), 2)

        sys10 = registry.mapped_systems[10]
        sys20 = registry.mapped_systems[20]

        self.assertEqual(sys10.finish_code, "P1")
        self.assertEqual(sys20.finish_code, "P2")


if __name__ == "__main__":
    unittest.main()
