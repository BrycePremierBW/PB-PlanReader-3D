from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

import pb_memory_stability_v1220 as memory


class MemoryStabilityV1220Tests(unittest.TestCase):
    def test_blank_and_directory_paths_are_not_media_files(self):
        self.assertIsNone(memory.regular_file(""))
        self.assertIsNone(memory.regular_file("   "))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(memory.regular_file(tmp))

    def test_thumbnail_is_bounded_and_blank_path_returns_no_bytes(self):
        self.assertEqual(memory.thumbnail_bytes(""), b"")
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "large.png"
            Image.new("RGB", (2200, 1400), "white").save(source)
            payload = memory.thumbnail_bytes(source, max_long_edge=1000)
            self.assertTrue(payload)
            with Image.open(io.BytesIO(payload)) as preview:
                self.assertLessEqual(max(preview.size), 1000)
                self.assertEqual(preview.mode, "RGB")

    def test_cv_working_copy_is_bounded_but_preserves_original_scale_factors(self):
        if getattr(memory.auto_v1219, "cv2", None) is None:
            self.skipTest("OpenCV unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "plan.png"
            Image.new("L", (2400, 1600), 255).save(source)
            loaded = memory._bounded_gray(source, max_long_edge=1100)
            self.assertIsNotNone(loaded)
            image, sx, sy, original_w, original_h = loaded
            self.assertLessEqual(max(image.shape[:2]), 1100)
            self.assertEqual((original_w, original_h), (2400, 1600))
            self.assertGreater(sx, 1.0)
            self.assertGreater(sy, 1.0)

    def test_drawing_component_coordinates_are_returned_in_original_pixels(self):
        if getattr(memory.auto_v1219, "cv2", None) is None:
            self.skipTest("OpenCV unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "drawing.png"
            image = Image.new("L", (2400, 1600), 255)
            draw = ImageDraw.Draw(image)
            draw.rectangle((300, 180, 1900, 1180), outline=0, width=18)
            image.save(source)
            result = memory.bounded_drawing_component(source, elevation=False)
            self.assertIsNotNone(result)
            self.assertEqual(result["image_width"], 2400)
            self.assertEqual(result["image_height"], 1600)
            x, y, width, height = result["bbox"]
            self.assertTrue(200 <= x <= 450)
            self.assertTrue(1300 <= width <= 1800)
            self.assertTrue(750 <= height <= 1200)


if __name__ == "__main__":
    unittest.main()
