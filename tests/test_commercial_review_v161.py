"""Unit & Integration Tests for PlanReader Phase 6B Commercial Review (v1.6.1 Correction Pass)."""

import json
import math
import sqlite3
import time
import pytest

from pb_takeoff_authority_v164 import approve_model_surface_row

from pb_commercial_review_v161 import (
    MODULE_VERSION,
    REQUIRED_FAMILIES,
    CommercialReviewResult,
    CommercialReviewSignal,
    _safe_float,
    _safe_int,
    _safe_str,
    collect_commercial_review_signals,
    collect_model_review_signals,
    collect_register_review_signals,
    collect_scale_review_signals,
    collect_takeoff_review_signals,
)


class MockApp:
    def __init__(self, db_path):
        self.db_path = db_path

    def lquery(self, sql, params=()):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        result = [dict(r) for r in rows]
        conn.close()
        return result

    def scale_gate_issues(self, workspace_id):
        return []


@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "test_review.db"
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE workspaces (id INTEGER PRIMARY KEY, job_no TEXT, job_name TEXT, drawing_issue TEXT)"
    )
    cur.execute(
        """CREATE TABLE takeoff_rows (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            page_id INTEGER,
            document_id INTEGER,
            element TEXT,
            notes TEXT,
            location TEXT,
            unit TEXT,
            quantity REAL,
            quantity_status TEXT,
            confidence TEXT,
            inclusion_status TEXT,
            source_reference TEXT
        )"""
    )
    cur.execute(
        """CREATE TABLE register_items (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            register_name TEXT,
            title TEXT,
            detail TEXT,
            status TEXT,
            priority TEXT,
            source_reference TEXT
        )"""
    )
    cur.execute(
        """CREATE TABLE workspace_settings (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            key TEXT,
            value TEXT
        )"""
    )
    conn.commit()
    conn.close()
    return str(db_file)


def test_module_version():
    assert MODULE_VERSION == "1.6.1"


def test_safe_helpers():
    assert _safe_float(None) is None
    assert _safe_float(True) is None
    assert _safe_float("15.5") == 15.5
    assert _safe_float("0.0") == 0.0
    assert _safe_float("NaN") is None
    assert _safe_float("Inf") is None

    assert _safe_int(None) is None
    assert _safe_int(True) is None
    assert _safe_int("101") == 101
    assert _safe_int("abc") is None

    assert _safe_str(None) == ""
    assert _safe_str({"a": 1}) == '{"a": 1}'
    assert _safe_str([1, 2]) == "[1, 2]"


def test_invalid_workspace_fails_closed():
    res1 = collect_commercial_review_signals(None, None)
    assert res1.workspace_id == 0
    assert res1.required_coverage_complete is False
    assert res1.signal_count == 0

    res2 = collect_commercial_review_signals(None, {"id": -1})
    assert res2.workspace_id == 0
    assert res2.required_coverage_complete is False


def test_takeoff_signals_basic(test_db):
    app = MockApp(test_db)
    conn = sqlite3.connect(test_db)
    cur = conn.cursor()
    cur.execute("INSERT INTO workspaces VALUES (101, 'JOB-101', 'Test Job', 'Issue A')")
    cur.execute(
        "INSERT INTO takeoff_rows VALUES (1, 101, 10, 1, 'Wall Paint', 'Acrylic', 'L1', 'm2', NULL, 'To measure', 'low', 'included', 'A-101')"
    )
    conn.commit()
    conn.close()

    res = collect_commercial_review_signals(app, {"id": 101})
    assert res.workspace_id == 101
    assert res.required_coverage_complete is True
    assert res.signal_count == 1
    sig = res.signals[0]
    assert sig.signal_id == "review:101:takeoff:1:Measurement"
    assert sig.severity == "BLOCKER"
    assert sig.category == "Measurement"
    assert sig.takeoff_row_id == 1
    assert sig.drawing_reference == "A-101"


def test_confirmed_numeric_zero_produces_no_signal(test_db):
    app = MockApp(test_db)
    conn = sqlite3.connect(test_db)
    cur = conn.cursor()
    cur.execute("INSERT INTO workspaces VALUES (101, 'JOB-101', 'Test Job', 'Issue A')")
    cur.execute(
        "INSERT INTO takeoff_rows VALUES (1, 101, 10, 1, 'Wall Paint', 'Zero Area', 'L1', 'm2', 0.0, 'Measured', 'Verified', 'included', 'A-101')"
    )
    conn.commit()
    conn.close()

    res = collect_commercial_review_signals(app, {"id": 101})
    assert res.signal_count == 0


def test_excluded_rows_without_issues_produce_no_signal(test_db):
    app = MockApp(test_db)
    conn = sqlite3.connect(test_db)
    cur = conn.cursor()
    cur.execute("INSERT INTO workspaces VALUES (101, 'JOB-101', 'Test Job', 'Issue A')")
    cur.execute(
        "INSERT INTO takeoff_rows VALUES (1, 101, 10, 1, 'Wall Paint', 'Excluded Item', 'L1', 'm2', NULL, 'Excluded', 'Verified', 'included', 'A-101')"
    )
    conn.commit()
    conn.close()

    res = collect_commercial_review_signals(app, {"id": 101})
    assert res.signal_count == 0


def test_unapproved_model_surface_is_a_commercial_blocker():
    class SurfaceApp:
        def lquery(self, sql, params=()):
            return [{
                "id": 77,
                "workspace_id": 101,
                "element": "3D Front",
                "location": "Block A",
                "unit": "m²",
                "quantity": 10.0,
                "quantity_status": "Measured",
                "confidence": "Measured",
                "inclusion_status": "INCLUSION",
                "source_reference": "PB 3D Surface Editor v1.2.12 · mass:7:front",
                "row_role": "model_surface",
                "commercial_authority_status": "REVIEW_REQUIRED",
                "commercial_authority_source": "A-401",
                "commercial_authority_reviewed_by": "",
                "commercial_authority_reviewed_at": "",
            }]

    signals = collect_takeoff_review_signals(SurfaceApp(), 101)
    assert len(signals) == 1
    assert signals[0].severity == "BLOCKER"
    assert signals[0].category == "3D model authority"
    assert signals[0].source_type == "model_surface_row"
    assert "has not received commercial approval" in signals[0].summary


def test_model_surface_requires_complete_attributable_approval():
    unapproved = {
        "id": 78,
        "workspace_id": 101,
        "element": "3D Front",
        "unit": "m²",
        "quantity": 10.0,
        "quantity_status": "Measured",
        "confidence": "Verified",
        "inclusion_status": "included",
        "row_role": "model_surface",
    }
    base = approve_model_surface_row(
        unapproved,
        source="A-401 / estimator measurement M-22",
        reviewed_by="Senior Estimator",
        reviewed_at="2026-09-04T10:00:00+10:00",
    )

    class CompleteApprovalApp:
        def lquery(self, sql, params=()):
            return [base]

    assert collect_takeoff_review_signals(CompleteApprovalApp(), 101) == []

    for missing_field in (
        "commercial_authority_source",
        "commercial_authority_reviewed_by",
        "commercial_authority_reviewed_at",
    ):
        class IncompleteApprovalApp:
            def lquery(self, sql, params=()):
                return [{**base, missing_field: ""}]

        signals = collect_takeoff_review_signals(IncompleteApprovalApp(), 101)
        assert len(signals) == 1, missing_field
        assert signals[0].severity == "BLOCKER", missing_field

    class TamperedApprovalApp:
        def lquery(self, sql, params=()):
            return [{**base, "quantity": 999.0}]

    signals = collect_takeoff_review_signals(TamperedApprovalApp(), 101)
    assert len(signals) == 1
    assert signals[0].severity == "BLOCKER"
    assert "no longer matches" in signals[0].summary


def test_explicitly_excluded_model_surface_has_no_commercial_signal():
    class ExcludedSurfaceApp:
        def lquery(self, sql, params=()):
            return [{
                "id": 79,
                "workspace_id": 101,
                "element": "3D Underside",
                "quantity": 10.0,
                "quantity_status": "Measured",
                "confidence": "Measured",
                "inclusion_status": "EXCLUSION",
                "row_role": "model_surface",
            }]

    assert collect_takeoff_review_signals(ExcludedSurfaceApp(), 101) == []


def test_malformed_takeoff_row_id_quarantines_item():
    """Missing or non-numeric row ID must be quarantined without destroying valid items or marking family UNAVAILABLE."""
    class MalformedApp:
        def lquery(self, sql, params=()):
            return [
                {"id": 1, "workspace_id": 101, "element": "Valid Row 1", "quantity_status": "To measure"},
                {"id": None, "workspace_id": 101, "element": "Malformed Row None", "quantity_status": "To measure"},
                {"id": "abc_invalid", "workspace_id": 101, "element": "Malformed Row String", "quantity_status": "To measure"},
                {"id": 3, "workspace_id": 101, "element": "Valid Row 3", "quantity_status": "To measure"},
            ]

    signals = collect_takeoff_review_signals(MalformedApp(), 101)
    # The two malformed rows (None and "abc_invalid") are quarantined (skipped).
    assert len(signals) == 2
    assert signals[0].takeoff_row_id == 1
    assert signals[1].takeoff_row_id == 3
    assert signals[0].source_id == "1"
    assert signals[1].source_id == "3"


def test_register_priority_and_status_contract(test_db):
    """HIGH priority register items must have REVIEW severity (not BLOCKER). Explicit status contract tested."""
    app = MockApp(test_db)
    conn = sqlite3.connect(test_db)
    cur = conn.cursor()
    cur.execute("INSERT INTO workspaces VALUES (101, 'JOB-101', 'Test Job', 'Issue A')")
    cur.execute(
        "INSERT INTO register_items VALUES (1, 101, 'RFI 01', 'High Priority Query', 'Clarify spec', 'Open', 'HIGH', 'A-102')"
    )
    cur.execute(
        "INSERT INTO register_items VALUES (2, 101, 'RFI 02', 'To Review Query', 'Check color', 'To review', 'NORMAL', 'A-103')"
    )
    cur.execute(
        "INSERT INTO register_items VALUES (3, 101, 'RFI 03', 'Accepted Query', 'Done', 'Accepted', 'LOW', 'A-104')"
    )
    cur.execute(
        "INSERT INTO register_items VALUES (4, 101, 'RFI 04', 'Unknown Status Query', 'Unknown status', 'ArbitraryStatus', 'NORMAL', 'A-105')"
    )
    cur.execute(
        "INSERT INTO register_items VALUES (5, 101, 'RFI 05', 'Blank Status Query', 'Blank status', '', 'NORMAL', 'A-106')"
    )
    conn.commit()
    conn.close()

    signals = collect_register_review_signals(app, 101)
    # RFI 01, 02, 04, 05 produce REVIEW signals. RFI 03 (Accepted) produces NO signal.
    assert len(signals) == 4
    for sig in signals:
        assert sig.severity == "REVIEW"

    # Check status descriptions
    sig_map = {s.register_item_id: s for s in signals}
    assert "Open" in sig_map[1].reasons[0]
    assert "To review" in sig_map[2].reasons[0]
    assert "unrecognised status" in sig_map[4].reasons[0]
    assert "blank/unrecorded" in sig_map[5].reasons[0]


def test_scale_authority_missing_and_exception(test_db):
    """No scale_gate_issues method -> NOT_SUPPORTED (coverage incomplete). Method raises exception -> UNAVAILABLE."""
    # Case 1: app has no scale_gate_issues
    class BareApp:
        def lquery(self, sql, params=()):
            return []

    res1 = collect_commercial_review_signals(BareApp(), {"id": 101})
    assert res1.source_coverage["scale"] == "NOT_SUPPORTED"
    assert res1.required_coverage_complete is False

    # Case 2: scale_gate_issues raises Exception
    class ExceptionApp:
        def lquery(self, sql, params=()):
            return []

        def scale_gate_issues(self, wid):
            raise RuntimeError("Database locked")

    res2 = collect_commercial_review_signals(ExceptionApp(), {"id": 101})
    assert res2.source_coverage["scale"] == "UNAVAILABLE"
    assert res2.required_coverage_complete is False


def test_partial_source_outage_coverage_incomplete(test_db):
    """If required source is UNAVAILABLE, required_coverage_complete is False even with 0 signals."""
    class OutageApp:
        def lquery(self, sql, params=()):
            if "takeoff_rows" in sql:
                raise RuntimeError("Takeoff query failed")
            return []

        def scale_gate_issues(self, wid):
            return []

    res = collect_commercial_review_signals(OutageApp(), {"id": 101})
    assert res.source_coverage["takeoff"] == "UNAVAILABLE"
    assert res.required_coverage_complete is False
    assert res.signal_count == 0


def test_partial_source_outage_with_valid_signals(test_db):
    """If one source fails, valid signals from other sources remain visible and coverage is incomplete."""
    class PartialApp:
        def lquery(self, sql, params=()):
            if "register_items" in sql:
                raise RuntimeError("Register DB missing")
            if "takeoff_rows" in sql:
                return [
                    {"id": 1, "workspace_id": 101, "element": "Wall Paint", "quantity_status": "To measure"}
                ]
            return []

        def scale_gate_issues(self, wid):
            return []

    res = collect_commercial_review_signals(PartialApp(), {"id": 101})
    assert res.source_coverage["register"] == "UNAVAILABLE"
    assert res.source_coverage["takeoff"] == "AVAILABLE"
    assert res.required_coverage_complete is False
    assert res.signal_count == 1
    assert res.signals[0].source_family == "takeoff"


def test_deterministic_reordering():
    """Different input row ordering yields identical, deterministically sorted signal IDs and ordering."""
    rows_order_A = [
        {"id": 2, "workspace_id": 101, "element": "Wall B", "quantity_status": "Provisional measured", "confidence": "Derived"},
        {"id": 1, "workspace_id": 101, "element": "Wall A", "quantity_status": "To measure", "confidence": "low"},
    ]
    rows_order_B = [
        {"id": 1, "workspace_id": 101, "element": "Wall A", "quantity_status": "To measure", "confidence": "low"},
        {"id": 2, "workspace_id": 101, "element": "Wall B", "quantity_status": "Provisional measured", "confidence": "Derived"},
    ]

    class MockAppA:
        def lquery(self, sql, params=()):
            if "takeoff_rows" in sql:
                return rows_order_A
            return []
        def scale_gate_issues(self, wid):
            return []

    class MockAppB:
        def lquery(self, sql, params=()):
            if "takeoff_rows" in sql:
                return rows_order_B
            return []
        def scale_gate_issues(self, wid):
            return []

    res1 = collect_commercial_review_signals(MockAppA(), {"id": 101})
    res2 = collect_commercial_review_signals(MockAppB(), {"id": 101})

    assert len(res1.signals) == 2
    assert len(res2.signals) == 2
    assert [s.signal_id for s in res1.signals] == [s.signal_id for s in res2.signals]
    assert [s.severity for s in res1.signals] == [s.severity for s in res2.signals]
    assert res1.signals[0].severity == "BLOCKER"
    assert res1.signals[1].severity == "REVIEW"


def test_same_source_id_different_workspaces():
    """Identical source item ID (e.g. id=1) across workspaces must produce distinct signal IDs."""
    class MockAppWS:
        def __init__(self, ws_id):
            self.ws_id = ws_id
        def lquery(self, sql, params=()):
            if "takeoff_rows" in sql:
                return [{"id": 1, "workspace_id": self.ws_id, "element": "Wall Paint", "quantity_status": "To measure"}]
            return []
        def scale_gate_issues(self, wid):
            return []

    res1 = collect_commercial_review_signals(MockAppWS(101), {"id": 101})
    res2 = collect_commercial_review_signals(MockAppWS(202), {"id": 202})

    assert res1.signals[0].signal_id == "review:101:takeoff:1:Measurement"
    assert res2.signals[0].signal_id == "review:202:takeoff:1:Measurement"
    assert res1.signals[0].signal_id != res2.signals[0].signal_id


def test_performance_benchmark(test_db):
    """500 takeoff rows must derive signals in under 200ms."""
    app = MockApp(test_db)
    conn = sqlite3.connect(test_db)
    cur = conn.cursor()
    cur.execute("INSERT INTO workspaces VALUES (101, 'JOB-101', 'Test Job', 'Issue A')")
    rows_data = [
        (i, 101, 10, 1, f"Element {i}", "Notes", "L1", "m2", None, "To measure", "low", "included", "A-101")
        for i in range(1, 501)
    ]
    cur.executemany("INSERT INTO takeoff_rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows_data)
    conn.commit()
    conn.close()

    t0 = time.perf_counter()
    res = collect_commercial_review_signals(app, {"id": 101})
    t1 = time.perf_counter()

    elapsed_ms = (t1 - t0) * 1000
    assert res.signal_count == 500
    assert elapsed_ms < 200.0
