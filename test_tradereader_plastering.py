import unittest

from tradereader_plastering import calculate_measurement


class PlasteringMeasurementTests(unittest.TestCase):
    def test_wall_net_area_layers_sides_and_sheet_count(self):
        row = calculate_measurement(
            {
                "element_type": "Wall lining",
                "wall_length_m": 10,
                "height_m": 2.7,
                "gross_area_m2": 999,
                "openings_m2": 3,
                "layers": 2,
                "sides": 2,
                "waste_percent": 10,
                "board_width_mm": 1200,
                "board_length_mm": 2400,
            }
        )
        self.assertAlmostEqual(row["gross_area_m2"], 27.0)
        self.assertAlmostEqual(row["net_area_m2"], 24.0)
        self.assertAlmostEqual(row["installed_board_area_m2"], 96.0)
        self.assertEqual(row["sheet_count"], 37)

    def test_ceiling_uses_direct_area(self):
        row = calculate_measurement(
            {
                "element_type": "Ceiling",
                "wall_length_m": 10,
                "height_m": 2.7,
                "gross_area_m2": 42,
                "openings_m2": 2,
                "layers": 1,
                "sides": 1,
                "waste_percent": 0,
                "board_width_mm": 1200,
                "board_length_mm": 2400,
            }
        )
        self.assertAlmostEqual(row["gross_area_m2"], 42.0)
        self.assertAlmostEqual(row["net_area_m2"], 40.0)
        self.assertEqual(row["sheet_count"], 14)

    def test_opening_deduction_never_creates_negative_net(self):
        row = calculate_measurement(
            {
                "element_type": "Wall lining",
                "wall_length_m": 2,
                "height_m": 2,
                "openings_m2": 10,
                "layers": 1,
                "sides": 1,
                "board_width_mm": 1200,
                "board_length_mm": 2400,
            }
        )
        self.assertEqual(row["net_area_m2"], 0.0)
        self.assertEqual(row["installed_board_area_m2"], 0.0)
        self.assertEqual(row["sheet_count"], 0)


if __name__ == "__main__":
    unittest.main()
