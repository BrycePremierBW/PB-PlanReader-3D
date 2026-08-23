"""Composition smoke test for the full apply() chain.

Verifies that the production entry point loads all modules in the correct
order and produces the expected patched attributes. This catches broken
import chains and order-dependent monkey-patch regressions.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Ensure repo root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestCompositionChain(unittest.TestCase):
    """Verify the production apply() chain produces expected attributes."""

    def test_v133_entry_point_loads(self):
        """The production entry point should import without error."""
        import pb_planreader_v133_app as entry
        self.assertTrue(hasattr(entry, "app"))
        self.assertEqual(entry.app.APP_VERSION, "1.5.1")

    def test_core_app_has_required_attributes(self):
        """The base app should have all core attributes after composition."""
        import pb_planreader_v133_app as entry
        app = entry.app

        # Core Streamlit interface
        self.assertTrue(hasattr(app, "st"))
        self.assertTrue(hasattr(app, "main"))

        # Core functions
        self.assertTrue(hasattr(app, "login_screen"))
        self.assertTrue(hasattr(app, "sidebar_workspace_selector"))

        # Database
        self.assertTrue(hasattr(app, "local_connect"))

        # App version
        self.assertEqual(app.APP_VERSION, "1.5.1")

    def test_guard_modules_applied(self):
        """All guard modules should have been applied."""
        import pb_planreader_v133_app as entry
        app = entry.app

        # Check that guard markers are set
        guard_attrs = [
            "_pb_studio_path_guard_v1213_applied",
            "_pb_3d_wrapper_guard_v1214_applied",
            "_pb_db_init_guard_v1215_applied",
            "_pb_subscription_core_guard_v1218_applied",
            "_pb_auto_geometry_guard_v1219_applied",
            "_pb_autopilot_accuracy_guard_v1223_applied",
            "_pb_registration_priority_guard_v1225_applied",
            "_pb_mapper_hard_guard_v1228_applied",
            "_pb_sidebar_viewport_guard_v146_applied",
            "_pb_persistent_login_v1229_applied",
        ]
        for attr in guard_attrs:
            self.assertTrue(
                getattr(app, attr, False),
                f"Guard {attr} was not applied",
            )

    def test_offline_reader_importable(self):
        """The offline reader module should be importable."""
        from pb_planreader_shared import (
            classify_page,
            dimension_value_m,
            extract_scale_ratio,
        )

        # Basic smoke tests
        self.assertIsNotNone(classify_page("Floor Plan"))
        self.assertIsNotNone(dimension_value_m("3600mm"))
        self.assertIsNotNone(extract_scale_ratio("1:100"))

    def test_all_production_modules_compile(self):
        """All production Python files should compile without syntax errors."""
        import py_compile

        production_files = [
            "pb_planreader_3d_app.py",
            "pb_planreader_v11_app.py",
            "pb_planreader_v126_app.py",
            "pb_planreader_v133_app.py",
            "pb_planreader_reconstruction_v139_app.py",
            "pb_planreader_shared.py",
            "pb_persistent_login_v1229.py",
            "pb_startup_bootstrap_v151.py",
            "pb_runtime_performance_v149.py",
            "pb_processing_fastpath_v150.py",
        ]

        for fname in production_files:
            fpath = Path(__file__).resolve().parent.parent / fname
            if fpath.exists():
                result = py_compile.compile(str(fpath), doraise=True)
                self.assertIsNotNone(result, f"{fname} failed to compile")


if __name__ == "__main__":
    unittest.main()
