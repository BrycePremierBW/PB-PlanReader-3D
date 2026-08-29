"""Composition smoke test for the full apply() chain.

Verifies that the production entry point loads all modules in the correct
order and produces the expected patched attributes. This catches broken
import chains and order-dependent monkey-patch regressions.

Isolation note
--------------
The production entry point (``pb_planreader_v133_app``) applies dozens of
``apply()`` composition patches at *import* time, many of which replace
module-level globals on shared modules (for example
``pb_material_schedule_v1222.build_material_dictionary``,
``pb_auto_geometry_v1219._build_unit_rows``,
``pb_accuracy_v13_engines_v145.detect_openings``, etc.). Importing that entry
in-process would leave all of those monkey-patches in place for every later
test module, which break unit tests that rely on pristine module globals
(order-dependent failures). To keep the regression suite deterministic, the
composition checks that require importing the entry point are executed in a
fresh subprocess so their module-level mutations cannot leak into the parent
test process.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import textwrap
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _run_isolated(code: str, desc: str = "composition check") -> None:
    """Run a code snippet in a fresh subprocess.

    The full production composition only runs on import of
    ``pb_planreader_v133_app``. Executing those checks in a subprocess keeps
    the composition's module-global monkey-patches out of the parent test
    process, which other unit-test modules rely on to stay pristine.
    """
    snippet = textwrap.dedent(code)
    wrapper = (
        "import sys\n"
        f"sys.path.insert(0, {str(_REPO_ROOT)!r})\n"
        + snippet
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "composition_check.py"
        path.write_text(wrapper, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(_REPO_ROOT),
        )
    if result.returncode != 0:
        raise AssertionError(
            f"{desc} failed in subprocess (exit {result.returncode}):\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )


class TestCompositionChain(unittest.TestCase):
    """Verify the production apply() chain produces expected attributes."""

    def test_v133_entry_point_loads(self):
        """The production entry point should import without error."""
        _run_isolated(
            """
            import pb_planreader_v133_app as entry
            assert hasattr(entry, "app"), "entry has no app"
            assert entry.app.APP_VERSION == "1.5.1", entry.app.APP_VERSION
            """,
            "v133 entry point load",
        )

    def test_core_app_has_required_attributes(self):
        """The base app should have all core attributes after composition."""
        _run_isolated(
            """
            import pb_planreader_v133_app as entry
            app = entry.app

            # Core Streamlit interface
            assert hasattr(app, "st"), "missing st"
            assert hasattr(app, "main"), "missing main"

            # Core functions
            assert hasattr(app, "login_screen"), "missing login_screen"
            assert hasattr(app, "sidebar_workspace_selector"), "missing sidebar_workspace_selector"

            # Database
            assert hasattr(app, "local_connect"), "missing local_connect"

            # App version
            assert app.APP_VERSION == "1.5.1", app.APP_VERSION
            """,
            "core app attributes",
        )

    def test_guard_modules_applied(self):
        """All guard modules should have been applied."""
        _run_isolated(
            """
            import pb_planreader_v133_app as entry
            app = entry.app

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
                assert getattr(app, attr, False), f"Guard {attr} was not applied"
            """,
            "guard modules applied",
        )

    def test_offline_reader_importable(self):
        """The offline reader module should be importable."""
        from pb_planreader_shared import (
            classify_page,
            dimension_value_m,
            extract_scale_ratio,
        )

        # Basic smoke tests (pb_planreader_shared performs no global
        # composition patches, so it is safe to import in-process).
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
            fpath = _REPO_ROOT / fname
            if fpath.exists():
                result = py_compile.compile(str(fpath), doraise=True)
                self.assertIsNotNone(result, f"{fname} failed to compile")


if __name__ == "__main__":
    unittest.main()
