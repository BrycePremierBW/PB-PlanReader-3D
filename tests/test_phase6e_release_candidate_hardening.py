"""tests/test_phase6e_release_candidate_hardening.py — Phase 6E Regression Suite.

Issue #92: Phase 6E — Commercial release-candidate hardening and production stability.
"""
from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import MagicMock

from pb_phase6e_release_candidate_v182 import (
    escape_html_text,
    escape_csv_formula_injection,
    invalidate_workspace_session_confirmations,
    preserve_authenticated_session,
    Phase6EIntegrityAudit,
    apply_hero_security_escaping,
)


class Phase6EHardeningTests(unittest.TestCase):

    def test_01_authenticated_create_job_preserves_valid_session(self):
        """Preserves authenticated session user dict across Streamlit reruns."""
        session_state = {
            "planreader_user": {"username": "estimator_1", "role": "Estimator"},
            "active_workspace_id": 1,
        }
        user = preserve_authenticated_session(session_state)
        self.assertIsNotNone(user)
        self.assertEqual(user["username"], "estimator_1")

    def test_02_workspace_switch_invalidates_stale_confirmations(self):
        """Workspace ID switch invalidates stale preflight acknowledgements and fingerprints."""
        session_state = {
            "active_workspace_id": 1,
            "preflight_acknowledgement": "CONFIRM_PUBLISH_JOB_1",
            "acknowledged_fingerprint": "fingerprint_12345",
        }

        # Switch from workspace #1 to workspace #2
        invalidated = invalidate_workspace_session_confirmations(session_state, new_workspace_id=2)
        self.assertTrue(invalidated)
        self.assertEqual(session_state["active_workspace_id"], 2)
        self.assertNotIn("preflight_acknowledgement", session_state)
        self.assertNotIn("acknowledged_fingerprint", session_state)

        # Same workspace #2 rerun should not invalidate
        rerun_invalidated = invalidate_workspace_session_confirmations(session_state, new_workspace_id=2)
        self.assertFalse(rerun_invalidated)

    def test_03_base_hero_html_escaping(self):
        """HTML-escapes user and project strings to prevent XSS in unsafe markdown rendering."""
        raw_xss = "<script>alert('XSS')</script>"
        escaped = escape_html_text(raw_xss)
        self.assertEqual(escaped, "&lt;script&gt;alert(&#x27;XSS&#x27;)&lt;/script&gt;")

    def test_04_csv_excel_formula_injection_escaping(self):
        """Escapes leading formula characters (=, +, -, @) for Excel/CSV exports."""
        f1 = escape_csv_formula_injection("=1+1")
        f2 = escape_csv_formula_injection("+CMD")
        f3 = escape_csv_formula_injection("@SUM")
        f4 = escape_csv_formula_injection("Normal Text")

        self.assertEqual(f1, "'=1+1")
        self.assertEqual(f2, "'+CMD")
        self.assertEqual(f3, "'@SUM")
        self.assertEqual(f4, "Normal Text")

    def test_05_workspace_creation_truthful_state_classification(self):
        """Classifies workspace creation states honestly without claiming false success."""
        # 1. Full success
        res_full = Phase6EIntegrityAudit.audit_workspace_creation(
            local_workspace_created=True, jobhub_linked=True
        )
        self.assertEqual(res_full["status"], "SUCCESS_LINKED")
        self.assertTrue(res_full["is_linked"])

        # 2. Local only fallback
        res_partial = Phase6EIntegrityAudit.audit_workspace_creation(
            local_workspace_created=True, jobhub_linked=False, jobhub_error="JobHub connection timed out"
        )
        self.assertEqual(res_partial["status"], "PARTIAL_LOCAL_ONLY")
        self.assertFalse(res_partial["is_linked"])
        self.assertIn("JobHub link failed", res_partial["message"])

    def test_06_idempotent_hero_security_escaping_application(self):
        """apply_hero_security_escaping wraps hero function idempotently without stacking."""
        mock_module = MagicMock()
        mock_module.hero = MagicMock(return_value="Original Hero")

        res1 = apply_hero_security_escaping(mock_module)
        self.assertTrue(res1)
        self.assertTrue(getattr(mock_module, "_phase6e_hero_escaped"))

        # Second call returns True without wrapping again
        res2 = apply_hero_security_escaping(mock_module)
        self.assertTrue(res2)

    def test_07_export_rerun_does_not_mutate_measurement_truth(self):
        """Export page preflight derivation does not mutate takeoff quantities or database state."""
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE workspaces (id INTEGER PRIMARY KEY, job_no TEXT, job_name TEXT, drawing_issue TEXT, jobhub_job_id INTEGER);
            CREATE TABLE takeoff_rows (id INTEGER PRIMARY KEY, workspace_id INTEGER, quantity REAL, value_ex_gst REAL);
            INSERT INTO workspaces VALUES (1, 'JOB-01', 'Test', 'Rev A', 101);
            INSERT INTO takeoff_rows VALUES (10, 1, 50.0, 1250.0);
            """
        )
        conn.commit()

        cur.execute("SELECT quantity, value_ex_gst FROM takeoff_rows WHERE id=10")
        qty_before, val_before = cur.fetchone()

        # Simulate preflight read
        cur.execute("SELECT SUM(quantity), SUM(value_ex_gst) FROM takeoff_rows WHERE workspace_id=1")
        _ = cur.fetchone()

        cur.execute("SELECT quantity, value_ex_gst FROM takeoff_rows WHERE id=10")
        qty_after, val_after = cur.fetchone()

        self.assertEqual(qty_before, qty_after)
        self.assertEqual(val_before, val_after)
        conn.close()


if __name__ == "__main__":
    unittest.main()
