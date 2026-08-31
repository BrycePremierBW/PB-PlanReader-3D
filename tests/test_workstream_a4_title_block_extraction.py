"""tests/test_workstream_a4_title_block_extraction.py — Workstream A4 Regression Suite.

Issue #80: Vector & text title block / scale bar / legend extraction.
"""
from __future__ import annotations

import sqlite3
import unittest

from pb_title_block_extractor_v173 import (
    TitleBlockMetadata,
    TitleBlockRegistry,
    extract_title_block_from_text,
    derive_title_block_metadata,
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
            scale_text TEXT,
            file_name TEXT
        );
        """
    )
    conn.commit()
    return conn


class WorkstreamA4TitleBlockTests(unittest.TestCase):

    def setUp(self):
        self.conn = _create_mock_db()
        cur = self.conn.cursor()
        cur.execute("INSERT INTO workspaces VALUES (1, 'JOB-A4', 'Title Block Test')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_01_title_block_text_extraction(self):
        """Extracts Job No, Sheet No, Revision, Date, and Scale text accurately."""
        sample_text = """
        PREMIER COMMERCIAL BUILDING
        JOB NO: PR-2026-99
        DRAWING TITLE: Ground Floor Plan
        DWG NO: A-101
        REV: B
        SCALE: 1:100 @ A1
        DATE: 15/08/2026
        P1 = Low Sheen Acrylic Wall System
        FC01 = 9mm Fibre Cement Board
        """
        meta = extract_title_block_from_text(10, sample_text)

        self.assertEqual(meta.job_no, "PR-2026-99")
        self.assertEqual(meta.sheet_no, "A-101")
        self.assertEqual(meta.revision, "B")
        self.assertEqual(meta.scale_text, "1:100 @ A1")
        self.assertEqual(meta.date_str, "15/08/2026")
        self.assertEqual(meta.drawing_title, "Ground Floor Plan")

    def test_02_legend_key_parsing(self):
        """Parses legend symbol keys correctly."""
        sample_text = """
        P1 - Low Sheen Acrylic Wall System
        P2 - Gloss Enamel Door System
        FC01 = 9mm Fibre Cement Sheet
        """
        meta = extract_title_block_from_text(20, sample_text)

        self.assertIn("P1", meta.legend_keys)
        self.assertIn("P2", meta.legend_keys)
        self.assertIn("FC01", meta.legend_keys)

        self.assertEqual(meta.legend_keys["P1"], "Low Sheen Acrylic Wall System")
        self.assertEqual(meta.legend_keys["FC01"], "9mm Fibre Cement Sheet")

    def test_03_workspace_title_block_registry(self):
        """derive_title_block_metadata extracts metadata for all pages in workspace."""
        cur = self.conn.cursor()
        cur.execute("INSERT INTO pages VALUES (10, 1, 'DWG NO: A-101 Floor Plan', '1:100', 'A-101_Rev_A.pdf')")
        cur.execute("INSERT INTO pages VALUES (20, 1, 'DWG NO: A-201 RCP', '1:50', 'A-201_Rev_B.pdf')")
        self.conn.commit()

        registry = derive_title_block_metadata(self.conn, 1)
        self.assertEqual(len(registry.metadata_list), 2)

        meta10 = registry._by_id[10]
        meta20 = registry._by_id[20]

        self.assertEqual(meta10.sheet_no, "A-101")
        self.assertEqual(meta20.sheet_no, "A-201")
        self.assertEqual(meta20.revision, "B")


if __name__ == "__main__":
    unittest.main()
