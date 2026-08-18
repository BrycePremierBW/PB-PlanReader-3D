from __future__ import annotations

import unittest

from pb_takeoff_studio_v1211 import (
    build_studio_takeoff_rows,
    completion_summary,
    normalise_studio_area,
    polygon_area_m2,
)


class TakeoffStudioTests(unittest.TestCase):
    def test_polygon_area_uses_page_scale(self):
        points = [
            {"x": 10, "y": 10},
            {"x": 60, "y": 10},
            {"x": 60, "y": 60},
            {"x": 10, "y": 60},
        ]
        # 50% of a 1000 px page = 500 px = 5 m.
        # 50% of a 500 px page = 250 px = 2.5 m.
        self.assertEqual(polygon_area_m2(points, 1000, 500, 100), 12.5)

    def test_manual_area_override_wins_without_scale(self):
        points = [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 10, "y": 10}]
        self.assertEqual(polygon_area_m2(points, 0, 0, 0, 6.25), 6.25)

    def test_normalise_recomputes_area_and_clamps_progress(self):
        area = normalise_studio_area(
            {
                "id": "A-001",
                "label": "Soffit 1",
                "substrate": "SOF",
                "progress_pct": 130,
                "points": [
                    {"x": 0, "y": 0},
                    {"x": 20, "y": 0},
                    {"x": 20, "y": 10},
                    {"x": 0, "y": 10},
                ],
            },
            1,
            width_px=1000,
            height_px=500,
            px_per_m=100,
            view_label="Front",
        )
        self.assertEqual(area["progress_pct"], 100.0)
        self.assertEqual(area["area_m2"], 1.0)
        self.assertEqual(area["elevation"], "Front")

    def test_completion_summary_excludes_excluded_areas(self):
        result = completion_summary(
            [
                {"area_m2": 10, "progress_pct": 50, "status": "Paint Included"},
                {"area_m2": 20, "progress_pct": 100, "status": "Excluded"},
                {"area_m2": 5, "progress_pct": 100, "status": "Separate Item"},
            ]
        )
        self.assertEqual(result["total_m2"], 15.0)
        self.assertEqual(result["completed_m2"], 10.0)
        self.assertEqual(result["remaining_m2"], 5.0)
        self.assertEqual(result["completed_pct"], 66.7)

    def test_render_rows_are_provisional_and_unpriced(self):
        rows = build_studio_takeoff_rows(
            [
                {
                    "id": "A-001",
                    "label": "Front soffit",
                    "substrate": "SOF",
                    "elevation": "Front",
                    "status": "Paint Included",
                    "progress_pct": 65,
                    "area_m2": 6.25,
                    "manual_m2": 0,
                    "notes": "",
                }
            ],
            page_id=12,
            page_label="WD-3.04",
            page_type="Render / Artist's Impression",
            px_per_m=120,
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["quantity_status"], "Provisional measured")
        self.assertEqual(row["confidence"], "Derived")
        self.assertEqual(row["quantity"], 6.25)
        self.assertEqual(row["rate_per_unit"], 0)
        self.assertEqual(row["coats"], 0)
        self.assertEqual(row["productivity_m2_per_hour"], 0)
        self.assertIn("Progress 65%", row["notes"])
        self.assertIn("perspective", row["notes"].lower())

    def test_calibrated_elevation_row_is_measured(self):
        rows = build_studio_takeoff_rows(
            [
                {
                    "id": "A-002",
                    "label": "Rendered wall",
                    "substrate": "RBL",
                    "elevation": "Front elevation",
                    "status": "Separate Item",
                    "progress_pct": 0,
                    "area_m2": 18.4,
                    "manual_m2": 0,
                    "notes": "",
                }
            ],
            page_id=13,
            page_label="A-401",
            page_type="Elevation",
            px_per_m=95,
        )
        row = rows[0]
        self.assertEqual(row["quantity_status"], "Measured")
        self.assertEqual(row["confidence"], "Measured")
        self.assertEqual(row["inclusion_status"], "SEPARATE ITEM")
        self.assertEqual(row["row_role"], "studio_area")


if __name__ == "__main__":
    unittest.main()
