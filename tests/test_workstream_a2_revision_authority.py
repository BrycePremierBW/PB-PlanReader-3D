"""tests/test_workstream_a2_revision_authority.py — Workstream A2 Regression Suite.

Issue #78: Drawing set revision, superseding authority, and lineage tracking.
"""
from __future__ import annotations

import sqlite3
import unittest

from pb_revision_authority_v171 import (
    PageRevisionRecord,
    RevisionAuthorityRegistry,
    derive_revision_authority,
    parse_revision_code,
    extract_sheet_number,
)


def _create_mock_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE workspaces (
            id INTEGER PRIMARY KEY,
            job_no TEXT,
            job_name TEXT,
            drawing_issue TEXT
        );

        CREATE TABLE pages (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            page_label TEXT,
            page_type TEXT,
            page_number INTEGER,
            file_name TEXT,
            selected INTEGER DEFAULT 1
        );

        CREATE TABLE takeoff_rows (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            element TEXT,
            source_page TEXT
        );
        """
    )
    conn.commit()
    return conn


class WorkstreamA2RevisionTests(unittest.TestCase):

    def setUp(self):
        self.conn = _create_mock_db()
        cur = self.conn.cursor()
        cur.execute("INSERT INTO workspaces VALUES (1, 'JOB-A2', 'Revision Test', 'Rev B')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_01_revision_code_parsing(self):
        """Extracts revision codes and numerical rank accurately."""
        code1, rank1 = parse_revision_code("A-101_Rev_A.pdf")
        code2, rank2 = parse_revision_code("A-101_Rev_B.pdf")
        code3, rank3 = parse_revision_code("A-101_Rev_03.pdf")
        self.assertEqual((code1, rank1), ("A", 1))
        self.assertEqual((code2, rank2), ("B", 2))
        self.assertEqual((code3, rank3), ("03", 3))

    def test_02_superseding_detection_and_lineage(self):
        """Identifies current vs superseded revision per sheet number."""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO pages VALUES (10, 1, 'A-101 Floor Plan Rev A', 'Plan', 1, 'A-101_Rev_A.pdf', 1)")
        cur.execute("INSERT INTO pages VALUES (20, 1, 'A-101 Floor Plan Rev B', 'Plan', 2, 'A-101_Rev_B.pdf', 1)")
        self.conn.commit()

        registry = derive_revision_authority(self.conn, 1)
        self.assertEqual(len(registry.records), 2)

        rec_a = [r for r in registry.records if r.page_id == 10][0]
        rec_b = [r for r in registry.records if r.page_id == 20][0]

        self.assertTrue(rec_b.is_current)
        self.assertFalse(rec_b.is_superseded)

        self.assertTrue(rec_a.is_superseded)
        self.assertFalse(rec_a.is_current)
        self.assertEqual(rec_a.superseded_by_page_id, 20)

    def test_03_db_superseding_application(self):
        """apply_superseding_to_db sets selected=0 on superseded pages in DB."""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO pages VALUES (10, 1, 'A-101 Rev A', 'Plan', 1, 'A-101_Rev_A.pdf', 1)")
        cur.execute("INSERT INTO pages VALUES (20, 1, 'A-101 Rev B', 'Plan', 2, 'A-101_Rev_B.pdf', 1)")
        self.conn.commit()

        registry = derive_revision_authority(self.conn, 1)
        count = registry.apply_superseding_to_db(self.conn, 1)
        self.assertEqual(count, 2)

        cur.execute("SELECT id, selected FROM pages ORDER BY id")
        rows = dict(cur.fetchall())
        self.assertEqual(rows[10], 0)
        self.assertEqual(rows[20], 1)

    def test_04_superseded_drawing_reference_review_signal(self):
        """Detects takeoff rows referencing superseded drawing pages."""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO pages VALUES (10, 1, 'A-101 Rev A', 'Plan', 1, 'A-101_Rev_A.pdf', 1)")
        cur.execute("INSERT INTO pages VALUES (20, 1, 'A-101 Rev B', 'Plan', 2, 'A-101_Rev_B.pdf', 1)")
        cur.execute("INSERT INTO takeoff_rows VALUES (100, 1, 'Internal Wall', 'A-101 Rev A')")
        self.conn.commit()

        registry = derive_revision_authority(self.conn, 1)
        issues = registry.get_superseded_page_issues(self.conn, 1)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["issue_type"], "SUPERSEDED_DRAWING_REFERENCE")
        self.assertEqual(issues[0]["row_id"], 100)


if __name__ == "__main__":
    unittest.main()
