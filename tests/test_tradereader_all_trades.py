from __future__ import annotations

import unittest

import tradereader_trade_detection as detection
import tradereader_trade_modules as modules
import tradereader_universal_specialist as universal
from tradereader_profiles import TRADE_OPTIONS


class TradeReaderAllTradeTests(unittest.TestCase):
    def test_every_trade_option_has_tools(self):
        for trade in TRADE_OPTIONS:
            with self.subTest(trade=trade):
                self.assertIsNotNone(modules.get_trade_module(trade))
                self.assertTrue(modules.prompt_addendum(trade))

    def test_named_custom_trade_gets_universal_tools(self):
        self.assertIsNotNone(modules.get_trade_module("Fire protection"))
        self.assertIn("FIRE PROTECTION", modules.prompt_addendum("Fire protection"))

    def test_detects_multiple_trades(self):
        pages = [
            {
                "file_name": "ELECTRICAL.pdf", "page_label": "E-201 Lighting Plan", "page_type": "Services",
                "extracted_text": "GPO switchboard emergency lighting cable tray",
            },
            {
                "file_name": "HYDRAULIC.pdf", "page_label": "H-101 Hydraulic Layout", "page_type": "Services",
                "extracted_text": "sanitary cold water hot water stormwater floor waste fixture schedule",
            },
            {
                "file_name": "RCP.pdf", "page_label": "A-401 Reflected Ceiling Plan", "page_type": "Reflected Ceiling Plan",
                "extracted_text": "plasterboard ceiling bulkhead wall type schedule acoustic wall cornice",
            },
        ]
        found = [row["trade"] for row in detection.detect_trades(pages)]
        self.assertIn("Electrical", found)
        self.assertIn("Plumbing", found)
        self.assertIn("Plastering / Linings", found)

    def test_one_weak_word_does_not_create_concrete_trade(self):
        pages = [{
            "file_name": "A101.pdf", "page_label": "Floor Plan", "page_type": "Floor Plan",
            "extracted_text": "Existing concrete path shown for context only.",
        }]
        found = [row["trade"] for row in detection.detect_trades(pages)]
        self.assertNotIn("Concreting", found)

    def test_universal_calculations_are_deterministic(self):
        linear = universal.calculate("Linear route / length", {"length_m": 10, "runs": 2, "waste_percent": 5})
        self.assertAlmostEqual(linear["quantity"], 21.0)
        self.assertEqual(linear["unit"], "m")
        wall = universal.calculate("Wall / vertical area", {"length_m": 5, "height_m": 3, "deductions": 2, "waste_percent": 0})
        self.assertAlmostEqual(wall["quantity"], 13.0)
        volume = universal.calculate("Volume", {"length_m": 4, "width_m": 3, "depth_m": 0.1})
        self.assertAlmostEqual(volume["quantity"], 1.2)


if __name__ == "__main__":
    unittest.main()
