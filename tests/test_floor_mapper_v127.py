import unittest

from pb_floor_mapper_v127 import (
    SOURCE_PREFIX,
    build_floor_area_rows,
    calibration_px_per_m,
    measured_box_area_m2,
)


class FloorMapperV127Tests(unittest.TestCase):
    def test_calibration_converts_percent_line_to_px_per_m(self):
        calibration = {"x1": 0, "y1": 0, "x2": 50, "y2": 0, "len_m": 10}
        self.assertAlmostEqual(calibration_px_per_m(calibration, 1000, 800), 50.0)

    def test_calibrated_floor_box_returns_real_area(self):
        calibration = {"x1": 0, "y1": 0, "x2": 100, "y2": 0, "len_m": 10}
        ppm = calibration_px_per_m(calibration, 100, 100)
        box = {"id": "unit-501", "label": "Level 5 · Unit 501", "x": 0, "y": 0, "w": 50, "h": 50}
        self.assertAlmostEqual(measured_box_area_m2(box, 100, 100, ppm), 25.0)

    def test_manual_area_override_does_not_need_scale(self):
        box = {"manual_m2": 123.45, "w": 5, "h": 5}
        self.assertEqual(measured_box_area_m2(box, 100, 100, 0), 123.45)

    def test_rows_are_floor_reference_only(self):
        rows = build_floor_area_rows(
            [{"id": "unit-501", "label": "Level 5 · Unit 501", "w": 50, "h": 50}],
            100,
            100,
            10,
            "Level 5 floor plan",
            22,
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["row_role"], "floor_area")
        self.assertEqual(row["section"], "Internal")
        self.assertEqual(row["element"], "Floor area")
        self.assertEqual(row["unit"], "m²")
        self.assertEqual(row["quantity"], 25.0)
        self.assertEqual(row["coats"], 0)
        self.assertEqual(row["rate_per_unit"], 0)
        self.assertTrue(row["source_reference"].startswith(SOURCE_PREFIX))


if __name__ == "__main__":
    unittest.main()
