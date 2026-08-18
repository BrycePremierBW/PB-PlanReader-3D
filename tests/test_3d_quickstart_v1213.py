from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pb_3d_quickstart_v1213 import QUICK_SOURCE_PREFIX, _render_pages, refresh_zone_masses


class _FakeApp:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.executed = []
        self._counter = 0

    def lquery(self, sql, params=()):
        if "FROM pages" in sql:
            return list(self.rows)
        if "FROM mapped_zones" in sql:
            return list(self.rows)
        return []

    def lexecute(self, sql, params=()):
        self.executed.append((sql, tuple(params)))
        self._counter += 1
        return self._counter

    @staticmethod
    def now_stamp():
        return "2026-08-19T02:00:00"


class Quick3DTests(unittest.TestCase):
    def test_render_pages_match_artist_impression_case_insensitively(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "render.png"
            image.write_bytes(b"img")
            app = _FakeApp([
                {"id": 1, "page_label": "R1", "page_type": "Render / Artist's Impression", "image_path": str(image)},
                {"id": 2, "page_label": "R2", "page_type": "render / artist's impression", "image_path": str(image)},
                {"id": 3, "page_label": "E1", "page_type": "Elevation", "image_path": str(image)},
                {"id": 4, "page_label": "R3", "page_type": "Render / Artist's Impression", "image_path": ""},
            ])
            rows = _render_pages(app, 7)
            self.assertEqual([row["id"] for row in rows], [1, 2])

    def test_refresh_zone_masses_uses_calibrated_dimensions(self):
        app = _FakeApp([
            {
                "id": 12,
                "name": "Ground footprint",
                "x_px": 100,
                "y_px": 50,
                "w_px": 800,
                "h_px": 500,
                "px_per_m": 100,
                "wall_height_m": 2.7,
                "finish_system": "Exterior acrylic",
                "substrate": "Render",
                "quantity_status": "Measured",
                "source_reference": "A-101",
            }
        ])
        count = refresh_zone_masses(app, 3)
        self.assertEqual(count, 1)
        delete = app.executed[0]
        self.assertIn("DELETE FROM model_masses", delete[0])
        self.assertEqual(delete[1][1], QUICK_SOURCE_PREFIX + "%")
        insert = app.executed[1]
        values = insert[1]
        self.assertEqual(values[0], 3)
        self.assertEqual(values[1], "Ground footprint")
        self.assertEqual(values[3], 1.0)
        self.assertEqual(values[4], 0.5)
        self.assertEqual(values[6], 8.0)
        self.assertEqual(values[7], 5.0)
        self.assertEqual(values[8], 2.7)
        self.assertEqual(values[10], QUICK_SOURCE_PREFIX + "12")
        self.assertEqual(values[11], "Measured")

    def test_uncalibrated_zone_is_ignored(self):
        app = _FakeApp([
            {"id": 1, "name": "Bad", "w_px": 500, "h_px": 500, "px_per_m": 0}
        ])
        count = refresh_zone_masses(app, 3)
        self.assertEqual(count, 0)
        self.assertEqual(len(app.executed), 1)


if __name__ == "__main__":
    unittest.main()
