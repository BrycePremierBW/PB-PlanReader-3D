import unittest

import pb_floor_mapper_v128 as fm


class FloorMapperPolygonTests(unittest.TestCase):
    def test_polygon_area_uses_shoelace_with_non_rectangular_shape(self):
        area = fm.measured_floor_area_m2(
            {
                "points": [
                    {"x": 10, "y": 10},
                    {"x": 70, "y": 10},
                    {"x": 70, "y": 40},
                    {"x": 40, "y": 40},
                    {"x": 40, "y": 70},
                    {"x": 10, "y": 70},
                ]
            },
            1000,
            1000,
            100,
        )
        self.assertAlmostEqual(area, 27.0)

    def test_legacy_rectangle_still_measures(self):
        area = fm.measured_floor_area_m2(
            {"x": 10, "y": 20, "w": 50, "h": 40},
            1000,
            1000,
            100,
        )
        self.assertAlmostEqual(area, 20.0)

    def test_manual_override_still_wins(self):
        area = fm.measured_floor_area_m2(
            {
                "points": [
                    {"x": 0, "y": 0},
                    {"x": 100, "y": 0},
                    {"x": 100, "y": 100},
                    {"x": 0, "y": 100},
                ],
                "manual_m2": 88.25,
            },
            1000,
            1000,
            100,
        )
        self.assertEqual(area, 88.25)

    def test_generated_row_remains_floor_area_reference_only(self):
        rows = fm.build_floor_area_rows(
            [{"id": "unit-501", "label": "Unit 501", "points": [
                {"x": 10, "y": 10}, {"x": 70, "y": 10},
                {"x": 70, "y": 40}, {"x": 40, "y": 40},
                {"x": 40, "y": 70}, {"x": 10, "y": 70},
            ]}],
            1000,
            1000,
            100,
            "Level 5",
            5,
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["row_role"], "floor_area")
        self.assertEqual(row["unit"], "m²")
        self.assertEqual(row["coats"], 0)
        self.assertEqual(row["coverage_m2_per_litre"], 0)
        self.assertEqual(row["productivity_m2_per_hour"], 0)
        self.assertEqual(row["rate_per_unit"], 0)
        self.assertAlmostEqual(row["quantity"], 27.0)


if __name__ == "__main__":
    unittest.main()
