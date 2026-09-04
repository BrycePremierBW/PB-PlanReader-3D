"""Regression test suite for Workstream P8: Phase 6B Required Coverage.

Audit and harden Phase 6B required evidence family coverage:
- Required evidence families must fail closed when: missing, malformed, unsupported, errored.
- Never interpret zero signals as safe when required coverage is incomplete.
"""
from __future__ import annotations

import sqlite3
import unittest
from typing import Any, Dict, List

from pb_commercial_export_preflight_v163 import (
    derive_export_preflight,
)
from pb_commercial_review_v161 import (
    REQUIRED_FAMILIES,
    CommercialReviewResult,
    collect_commercial_review_signals,
    collect_register_review_signals,
    collect_scale_review_signals,
    collect_takeoff_review_signals,
)
from tests.test_phase6d_commercial_export_preflight import _create_mock_db, MockApp


class TestRequiredEvidenceFamiliesFailClosed(unittest.TestCase):
    """Test that missing, unsupported, or errored evidence families fail closed."""

    def test_app_none_fails_closed(self):
        """When app is None, takeoff and register must fail closed to NOT_SUPPORTED/UNAVAILABLE."""
        res = collect_commercial_review_signals(None, {"id": 101})
        self.assertNotEqual(res.source_coverage.get("takeoff"), "AVAILABLE")
        self.assertNotEqual(res.source_coverage.get("register"), "AVAILABLE")
        self.assertEqual(res.source_coverage.get("scale"), "NOT_SUPPORTED")
        self.assertFalse(res.required_coverage_complete)

    def test_unsupported_app_fails_closed(self):
        """An arbitrary object lacking DB/query capabilities fails closed."""
        class UnsupportedApp:
            pass

        res = collect_commercial_review_signals(UnsupportedApp(), {"id": 101})
        self.assertNotEqual(res.source_coverage.get("takeoff"), "AVAILABLE")
        self.assertNotEqual(res.source_coverage.get("register"), "AVAILABLE")
        self.assertEqual(res.source_coverage.get("scale"), "NOT_SUPPORTED")
        self.assertFalse(res.required_coverage_complete)

    def test_missing_takeoff_family_fails_closed(self):
        """An app with scale and register but missing takeoff must mark takeoff UNAVAILABLE and fail closed."""
        class AppMissingTakeoff:
            registers = []
            def scale_gate_issues(self, wid):
                return []

        res = collect_commercial_review_signals(AppMissingTakeoff(), {"id": 101})
        self.assertEqual(res.source_coverage.get("takeoff"), "UNAVAILABLE")
        self.assertFalse(res.required_coverage_complete)
        self.assertEqual(res.signal_count, 0)

    def test_missing_register_family_fails_closed(self):
        """An app with scale and takeoff but missing register must mark register UNAVAILABLE and fail closed."""
        class AppMissingRegister:
            takeoff = []
            def scale_gate_issues(self, wid):
                return []

        res = collect_commercial_review_signals(AppMissingRegister(), {"id": 101})
        self.assertEqual(res.source_coverage.get("register"), "UNAVAILABLE")
        self.assertFalse(res.required_coverage_complete)
        self.assertEqual(res.signal_count, 0)

    def test_missing_scale_family_fails_closed(self):
        """An app with takeoff and register but lacking scale_gate_issues marks scale NOT_SUPPORTED."""
        class AppMissingScale:
            takeoff = []
            registers = []

        res = collect_commercial_review_signals(AppMissingScale(), {"id": 101})
        self.assertEqual(res.source_coverage.get("scale"), "NOT_SUPPORTED")
        self.assertFalse(res.required_coverage_complete)

    def test_sqlite_missing_tables_fail_closed(self):
        """An empty SQLite database lacking takeoff_rows and register_items tables fails closed."""
        conn = sqlite3.connect(":memory:")
        res = collect_commercial_review_signals(conn, {"id": 101})
        self.assertEqual(res.source_coverage.get("takeoff"), "UNAVAILABLE")
        self.assertEqual(res.source_coverage.get("register"), "UNAVAILABLE")
        self.assertEqual(res.source_coverage.get("scale"), "UNAVAILABLE")
        self.assertFalse(res.required_coverage_complete)
        conn.close()


class TestMalformedEvidenceFamiliesFailClosed(unittest.TestCase):
    """Test that malformed collections or rows fail closed rather than being swallowed as safe empty."""

    def test_malformed_takeoff_collection_type(self):
        """Takeoff attribute that is a string, dict, or int must fail closed."""
        for bad_val in ("corrupted_string", 12345, {"a": 1}):
            class AppBadTakeoff:
                takeoff = bad_val
                registers = []
                def scale_gate_issues(self, wid):
                    return []

            res = collect_commercial_review_signals(AppBadTakeoff(), {"id": 101})
            self.assertEqual(res.source_coverage.get("takeoff"), "UNAVAILABLE", f"Failed for bad_val={bad_val}")
            self.assertFalse(res.required_coverage_complete)

    def test_malformed_takeoff_items(self):
        """Takeoff collection containing non-dict items must fail closed."""
        class AppBadItems:
            takeoff = [1, 2, "not_a_row"]
            registers = []
            def scale_gate_issues(self, wid):
                return []

        res = collect_commercial_review_signals(AppBadItems(), {"id": 101})
        self.assertEqual(res.source_coverage.get("takeoff"), "UNAVAILABLE")
        self.assertFalse(res.required_coverage_complete)

    def test_malformed_register_collection_type(self):
        """Register attribute that is a string, dict, or int must fail closed."""
        for bad_val in ("corrupted_register_string", 999, {"reg": "data"}):
            class AppBadRegister:
                takeoff = []
                registers = bad_val
                def scale_gate_issues(self, wid):
                    return []

            res = collect_commercial_review_signals(AppBadRegister(), {"id": 101})
            self.assertEqual(res.source_coverage.get("register"), "UNAVAILABLE", f"Failed for bad_val={bad_val}")
            self.assertFalse(res.required_coverage_complete)

    def test_malformed_register_items(self):
        """Register collection containing non-dict items must fail closed."""
        class AppBadRegItems:
            takeoff = []
            registers = ["invalid_item", 42]
            def scale_gate_issues(self, wid):
                return []

        res = collect_commercial_review_signals(AppBadRegItems(), {"id": 101})
        self.assertEqual(res.source_coverage.get("register"), "UNAVAILABLE")
        self.assertFalse(res.required_coverage_complete)

    def test_malformed_scale_issues_structure(self):
        """Scale gate returning non-list or containing non-dict issues must fail closed."""
        class AppBadScaleNonList:
            takeoff = []
            registers = []
            def scale_gate_issues(self, wid):
                return "not_a_list"

        res1 = collect_commercial_review_signals(AppBadScaleNonList(), {"id": 101})
        self.assertEqual(res1.source_coverage.get("scale"), "UNAVAILABLE")
        self.assertFalse(res1.required_coverage_complete)

        class AppBadScaleNonDict:
            takeoff = []
            registers = []
            def scale_gate_issues(self, wid):
                return ["malformed_issue_string"]

        res2 = collect_commercial_review_signals(AppBadScaleNonDict(), {"id": 101})
        self.assertEqual(res2.source_coverage.get("scale"), "UNAVAILABLE")
        self.assertFalse(res2.required_coverage_complete)


class TestCommercialReviewResultIntegrity(unittest.TestCase):
    """Test strict invariant enforcement on CommercialReviewResult."""

    def test_workspace_id_boolean_fails_closed(self):
        """Boolean True or False cannot satisfy required_coverage_complete."""
        res_true = CommercialReviewResult(
            workspace_id=True,
            source_coverage={"takeoff": "AVAILABLE", "register": "AVAILABLE", "scale": "AVAILABLE"},
        )
        self.assertFalse(res_true.required_coverage_complete)

        res_false = CommercialReviewResult(
            workspace_id=False,
            source_coverage={"takeoff": "AVAILABLE", "register": "AVAILABLE", "scale": "AVAILABLE"},
        )
        self.assertFalse(res_false.required_coverage_complete)

    def test_workspace_id_nonpositive_fails_closed(self):
        """Zero and negative workspace IDs fail closed."""
        for bad_id in (0, -1, -99):
            res = CommercialReviewResult(
                workspace_id=bad_id,
                source_coverage={"takeoff": "AVAILABLE", "register": "AVAILABLE", "scale": "AVAILABLE"},
            )
            self.assertFalse(res.required_coverage_complete)

    def test_errors_fail_closed(self):
        """Any collection error prevents required_coverage_complete from being True."""
        res = CommercialReviewResult(
            workspace_id=101,
            source_coverage={"takeoff": "AVAILABLE", "register": "AVAILABLE", "scale": "AVAILABLE"},
            errors=["Takeoff collection error: table missing"],
        )
        self.assertFalse(res.required_coverage_complete)

    def test_missing_required_family_in_source_coverage_fails_closed(self):
        """Missing any required family key from source_coverage fails closed."""
        res = CommercialReviewResult(
            workspace_id=101,
            source_coverage={"takeoff": "AVAILABLE", "scale": "AVAILABLE"},  # missing register
        )
        self.assertFalse(res.required_coverage_complete)

    def test_non_dict_source_coverage_fails_closed(self):
        """None or non-dict source_coverage fails closed."""
        res = CommercialReviewResult(workspace_id=101, source_coverage=None)
        self.assertFalse(res.required_coverage_complete)

    def test_clean_complete_coverage_succeeds(self):
        """Valid positive workspace ID, no errors, all required families AVAILABLE succeeds."""
        res = CommercialReviewResult(
            workspace_id=101,
            source_coverage={"takeoff": "AVAILABLE", "register": "AVAILABLE", "scale": "AVAILABLE"},
            errors=[],
        )
        self.assertTrue(res.required_coverage_complete)


class TestZeroSignalsNeverTreatedAsSafeWhenCoverageIncomplete(unittest.TestCase):
    """Test that zero review signals from an unavailable/errored source are NEVER treated as safe."""

    def setUp(self):
        self.conn = _create_mock_db()
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO workspaces (id, job_no, job_name, drawing_issue, jobhub_job_id) VALUES (1, 'J101', 'Test Job', 'A', 99)"
        )
        cur.execute(
            """INSERT INTO takeoff_rows (
                id, workspace_id, section, element, location, unit, quantity, quantity_status, coats,
                rate_per_unit, labour_hours, paint_litres, value_ex_gst, finish_system,
                coverage_m2_per_litre, productivity_m2_per_hour, confidence, inclusion_status, row_role
            ) VALUES (
                1, 1, 'Wall', 'Paint', 'L1', 'm2', 50.0, 'Measured', 1.0,
                20.0, 2.0, 5.0, 1000.0, 'Acrylic',
                10.0, 10.0, 'Verified', 'included', 'work'
            )"""
        )
        self.conn.commit()
        self.app = MockApp(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_scale_outage_zero_signals_never_safe(self):
        """When scale is UNAVAILABLE, zero review signals must NOT allow draft_handoff or clean download."""
        def broken_scale(wid):
            raise RuntimeError("Scale subsystem offline")

        self.app.scale_gate_issues = broken_scale
        res = derive_export_preflight(self.app, 1, bridge_available=True)

        self.assertFalse(res.required_coverage_complete)
        self.assertEqual(res.blocker_count, 0)
        self.assertEqual(res.preflight_status, "BLOCKED")
        self.assertEqual(res.final_publish_state, "BLOCKED")
        self.assertEqual(res.draft_handoff_state, "UNAVAILABLE")
        self.assertEqual(res.internal_download_state, "AVAILABLE_WITH_WARNING")

    def test_register_outage_zero_signals_never_safe(self):
        """When register query fails, zero signals must NOT allow draft_handoff or clean download."""
        orig_lquery = self.app.lquery
        def failing_lquery(sql, params=()):
            if "register_items" in sql:
                raise sqlite3.OperationalError("register_items corrupted")
            return orig_lquery(sql, params)

        self.app.lquery = failing_lquery
        res = derive_export_preflight(self.app, 1, bridge_available=True)

        self.assertFalse(res.required_coverage_complete)
        self.assertEqual(res.blocker_count, 0)
        self.assertEqual(res.preflight_status, "BLOCKED")
        self.assertEqual(res.final_publish_state, "BLOCKED")
        self.assertEqual(res.draft_handoff_state, "UNAVAILABLE")
        self.assertEqual(res.internal_download_state, "AVAILABLE_WITH_WARNING")

    def test_clean_workspace_with_zero_signals_is_fully_available(self):
        """When required coverage IS complete and zero signals exist, export and handoff are clean AVAILABLE."""
        res = derive_export_preflight(self.app, 1, bridge_available=True)
        self.assertTrue(res.required_coverage_complete)
        self.assertEqual(res.blocker_count, 0)
        self.assertEqual(res.warning_count, 0)
        self.assertEqual(res.preflight_status, "AVAILABLE")
        self.assertEqual(res.final_publish_state, "AVAILABLE")
        self.assertEqual(res.draft_handoff_state, "AVAILABLE")
        self.assertEqual(res.internal_download_state, "AVAILABLE")


if __name__ == "__main__":
    unittest.main()
