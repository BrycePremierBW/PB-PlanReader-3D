from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from pb_studio_path_guard_v1213 import filter_studio_pages, is_regular_image_file


class _FakeStreamlit:
    def __init__(self):
        self.warnings = []

    def warning(self, message):
        self.warnings.append(str(message))


class _FakeApp:
    def __init__(self):
        self.st = _FakeStreamlit()


class StudioPathGuardTests(unittest.TestCase):
    def test_blank_and_directory_paths_are_not_files(self):
        self.assertFalse(is_regular_image_file(None))
        self.assertFalse(is_regular_image_file(""))
        self.assertFalse(is_regular_image_file("   "))
        self.assertFalse(is_regular_image_file("."))

    def test_real_file_is_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.png"
            path.write_bytes(b"png")
            self.assertTrue(is_regular_image_file(str(path)))

    def test_filter_removes_blank_stale_and_directory_paths(self):
        app = _FakeApp()
        with tempfile.TemporaryDirectory() as tmp:
            valid = Path(tmp) / "page.png"
            valid.write_bytes(b"png")
            missing = Path(tmp) / "missing.png"
            frame = pd.DataFrame(
                [
                    {"id": 1, "image_path": str(valid)},
                    {"id": 2, "image_path": ""},
                    {"id": 3, "image_path": "."},
                    {"id": 4, "image_path": str(missing)},
                ]
            )

            filtered = filter_studio_pages(app, 10, lambda _app, _workspace: frame)

            self.assertEqual(filtered["id"].tolist(), [1])
            self.assertEqual(len(app.st.warnings), 1)
            self.assertIn("skipped 3", app.st.warnings[0].lower())

    def test_missing_image_path_column_returns_empty_frame(self):
        app = _FakeApp()
        frame = pd.DataFrame([{"id": 1}])
        filtered = filter_studio_pages(app, 10, lambda _app, _workspace: frame)
        self.assertTrue(filtered.empty)
        self.assertEqual(len(app.st.warnings), 1)


if __name__ == "__main__":
    unittest.main()
