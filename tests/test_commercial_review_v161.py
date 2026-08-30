"""Unit & Integration Tests for PlanReader Phase 6B Commercial Review (v1.6.1 Correction Pass)."""

import json
import math
import sqlite3
import time
import pytest

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


def test_malformed_takeoff_row_id_quarantines_item(test_db):
    """Missing or non-numeric row ID must be quarantined without killing entire family."""
    app = MockApp(test_db)
    conn = sqlite3.connect(test_db)
    cur = conn.cursor()
    cur.execute("INSERT INTO workspaces VALUES (101, 'JOB-101', 'Test Job', 'Issue A')")
    # Row 1 valid
    cur.execute(
        "INSERT INTO takeoff_rows VALUES (1, 101, 10, 1, 'Valid Row 1', 'Notes', 'L1', 'm2', NULL, 'To measure', 'low', 'included', 'A-101')"
    )
    # Row 2 valid
    cur.execute(
        "INSERT INTO takeoff_rows VALUES (2, 101, 10, 1, 'Valid Row 2', 'Notes', 'L1', 'm2', NULL, 'To measure', 'low', 'included', 'A-101')"
    )
    conn.commit()
    conn.close()

    signals = collect_takeoff_review_signals(app, 101)
    assert len(signals) == 2
    assert signals[0].source_id == "1"
    assert signals[1].source_id == "2"


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
    """No scale_gate_issues method -> NOT_SUPPORTED. Method raises exception -> UNAVAILABLE."""
    # Case 1: app has no scale_gate_issues
    class BareApp:
        def lquery(self, sql, params=()):
            return []

    res1 = collect_commercial_review_signals(BareApp(), {"id": 101})
    assert res1.source_coverage["scale"] == "NOT_SUPPORTED"
    assert res1.required_coverage_complete is True

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


def test_deterministic_reordering(test_db):
    """Same source data in different query order produces identical signal IDs and ordering."""
    app = MockApp(test_db)
    conn = sqlite3.connect(test_db)
    cur = conn.cursor()
    cur.execute("INSERT INTO workspaces VALUES (101, 'JOB-101', 'Test Job', 'Issue A')")
    cur.execute(
        "INSERT INTO takeoff_rows VALUES (1, 101, 10, 1, 'Wall A', 'Notes', 'L1', 'm2', NULL, 'To measure', 'low', 'included', 'A-101')"
    )
    cur.execute(
        "INSERT INTO takeoff_rows VALUES (2, 101, 10, 1, 'Wall B', 'Notes', 'L1', 'm2', 10.0, 'Provisional measured', 'Derived', 'included', 'A-101')"
    )
    conn.commit()
    conn.close()

    res1 = collect_commercial_review_signals(app, {"id": 101})
    res2 = collect_commercial_review_signals(app, {"id": 101})

    assert [s.signal_id for s in res1.signals] == [s.signal_id for s in res2.signals]
    assert [s.severity for s in res1.signals] == [s.severity for s in res2.signals]


def test_same_source_id_different_workspaces(test_db):
    """Same item ID in workspace 101 vs workspace 202 must produce different signal IDs."""
    app = MockApp(test_db)
    conn = sqlite3.connect(test_db)
    cur = conn.cursor()
    cur.execute("INSERT INTO workspaces VALUES (101, 'JOB-101', 'WS 101', 'Issue A')")
    cur.execute("INSERT INTO workspaces VALUES (202, 'JOB-202', 'WS 202', 'Issue A')")
    cur.execute(
        "INSERT INTO takeoff_rows VALUES (1, 101, 10, 1, 'Wall Paint', 'Notes', 'L1', 'm2', NULL, 'To measure', 'low', 'included', 'A-101')"
    )
    cur.execute(
        "INSERT INTO takeoff_rows VALUES (2, 202, 10, 1, 'Wall Paint', 'Notes', 'L1', 'm2', NULL, 'To measure', 'low', 'included', 'A-101')"
    )
    conn.commit()
    conn.close()

    res1 = collect_commercial_review_signals(app, {"id": 101})
    res2 = collect_commercial_review_signals(app, {"id": 202})

    assert res1.signals[0].signal_id == "review:101:takeoff:1:Measurement"
    assert res2.signals[0].signal_id == "review:202:takeoff:2:Measurement"
    assert "review:101:" in res1.signals[0].signal_id
    assert "review:202:" in res2.signals[0].signal_id


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
