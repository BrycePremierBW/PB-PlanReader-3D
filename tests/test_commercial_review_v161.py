"""Unit & Integration Tests for PlanReader Phase 6B Commercial Review (v1.6.1)."""

import json
import math
import sqlite3
import time
import pytest

from pb_commercial_review_v161 import (
    MODULE_VERSION,
    CommercialReviewResult,
    CommercialReviewSignal,
    _safe_float,
    collect_commercial_review_signals,
    collect_model_review_signals,
    collect_register_review_signals,
    collect_scale_review_signals,
    collect_takeoff_review_signals,
)


class MockApp:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def lquery(self, sql: str, params: tuple = ()):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def ldf(self, sql: str, params: tuple = ()):
        return self.lquery(sql, params)

    def scale_gate_issues(self, workspace_id: int):
        rows = self.lquery(
            "SELECT p.id, p.page_no, p.page_label FROM pages p "
            "WHERE p.workspace_id=? AND (p.px_per_m IS NULL OR p.px_per_m <= 0) "
            "AND p.id IN (SELECT DISTINCT page_id FROM mapped_zones WHERE workspace_id=?)",
            (workspace_id, workspace_id),
        )
        return [
            {
                "page_id": r["id"],
                "page_label": r.get("page_label") or str(r.get("page_no")),
                "reason": "Drawing page feeds measurement but scale calibration is not confirmed",
            }
            for r in rows
        ]


@pytest.fixture
def test_db(tmp_path):
    db_file = str(tmp_path / "test_review.db")
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    
    cur.execute("""
        CREATE TABLE workspaces (
            id INTEGER PRIMARY KEY,
            job_no TEXT,
            job_name TEXT,
            drawing_issue TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            file_name TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE pages (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            document_id INTEGER,
            page_no INTEGER,
            page_label TEXT,
            selected INTEGER,
            px_per_m REAL
        )
    """)
    cur.execute("""
        CREATE TABLE takeoff_rows (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            document_id INTEGER,
            page_id INTEGER,
            element TEXT,
            description TEXT,
            location TEXT,
            unit TEXT,
            quantity REAL,
            quantity_status TEXT,
            confidence TEXT,
            inclusion_status TEXT,
            drawing_reference TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE register_items (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            register_name TEXT,
            title TEXT,
            detail TEXT,
            status TEXT,
            priority TEXT,
            drawing_ref TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE workspace_settings (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            key TEXT,
            value TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE mapped_zones (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            page_id INTEGER,
            zone_name TEXT
        )
    """)
    conn.commit()
    conn.close()
    return db_file


def test_module_version():
    assert MODULE_VERSION == "1.6.1"


def test_safe_float_helper():
    assert _safe_float(None) is None
    assert _safe_float(True) is None
    assert _safe_float(False) is None
    assert _safe_float(10.5) == 10.5
    assert _safe_float("  25.4  ") == 25.4
    assert _safe_float(0.0) == 0.0
    assert _safe_float("0") == 0.0
    assert _safe_float("NaN") is None
    assert _safe_float(float("nan")) is None
    assert _safe_float("Inf") is None
    assert _safe_float("-Inf") is None
    assert _safe_float("abc") is None


def test_takeoff_signals_basic(test_db):
    app = MockApp(test_db)
    conn = sqlite3.connect(test_db)
    cur = conn.cursor()
    cur.execute("INSERT INTO workspaces VALUES (101, 'JOB-101', 'Test Job', 'Issue A')")
    cur.execute(
        "INSERT INTO takeoff_rows VALUES (1, 101, 1, 1, 'Wall Painting', 'Internal wall', 'L1', 'm2', NULL, 'To measure', 'high', 'included', 'A-101')"
    )
    cur.execute(
        "INSERT INTO takeoff_rows VALUES (2, 101, 1, 1, 'Skirting', 'Timber skirting', 'L1', 'm', NULL, 'Measured', 'high', 'included', 'A-101')"
    )
    cur.execute(
        "INSERT INTO takeoff_rows VALUES (3, 101, 1, 1, 'Ceiling Paint', 'Gyprock ceiling', 'L1', 'm2', 150.0, 'Provisional measured', 'high', 'included', 'A-101')"
    )
    conn.commit()
    conn.close()

    res = collect_commercial_review_signals(app, {"id": 101})
    assert res.signal_count == 3
    
    # Check row 1 (To measure -> BLOCKER)
    sig1 = [s for s in res.signals if s.source_id == "1"][0]
    assert sig1.severity == "BLOCKER"
    assert "To measure" in sig1.summary

    # Check row 2 (Measured with NULL quantity -> BLOCKER)
    sig2 = [s for s in res.signals if s.source_id == "2"][0]
    assert sig2.severity == "BLOCKER"
    assert "numeric quantity" in sig2.summary

    # Check row 3 (Provisional measured -> REVIEW)
    sig3 = [s for s in res.signals if s.source_id == "3"][0]
    assert sig3.severity == "REVIEW"
    assert "provisional" in sig3.summary.lower()


def test_confirmed_numeric_zero_produces_no_signal(test_db):
    """Measured + 0.0 + acceptable confidence MUST produce NO signal."""
    app = MockApp(test_db)
    conn = sqlite3.connect(test_db)
    cur = conn.cursor()
    cur.execute("INSERT INTO workspaces VALUES (101, 'JOB-101', 'Test Job', 'Issue A')")
    cur.execute(
        "INSERT INTO takeoff_rows VALUES (1, 101, 1, 1, 'Feature Wall', 'Optional wall', 'L1', 'm2', 0.0, 'Measured', 'high', 'included', 'A-101')"
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
        "INSERT INTO takeoff_rows VALUES (1, 101, 1, 1, 'External Facade', 'Not in painting scope', 'L1', 'm2', NULL, 'Excluded', 'high', 'excluded', 'A-101')"
    )
    conn.commit()
    conn.close()

    res = collect_commercial_review_signals(app, {"id": 101})
    assert res.signal_count == 0


def test_nonfinite_quantity_produces_blocker(test_db):
    app = MockApp(test_db)
    conn = sqlite3.connect(test_db)
    cur = conn.cursor()
    cur.execute("INSERT INTO workspaces VALUES (101, 'JOB-101', 'Test Job', 'Issue A')")
    cur.execute(
        "INSERT INTO takeoff_rows VALUES (1, 101, 1, 1, 'Door Frames', 'Enamel doors', 'L1', 'count', 'NaN', 'Measured', 'high', 'included', 'A-101')"
    )
    conn.commit()
    conn.close()

    res = collect_commercial_review_signals(app, {"id": 101})
    assert res.signal_count == 1
    assert res.signals[0].severity == "BLOCKER"
    assert "non-finite" in res.signals[0].summary.lower()


def test_register_signals(test_db):
    app = MockApp(test_db)
    conn = sqlite3.connect(test_db)
    cur = conn.cursor()
    cur.execute("INSERT INTO workspaces VALUES (101, 'JOB-101', 'Test Job', 'Issue A')")
    cur.execute(
        "INSERT INTO register_items VALUES (1, 101, 'RFI 01', 'Paint spec for wet area', 'Clarify waterproofing', 'Open', 'HIGH', 'A-102')"
    )
    cur.execute(
        "INSERT INTO register_items VALUES (2, 101, 'RFI 02', 'Door color schedule', 'Clarify gloss level', 'Accepted', 'LOW', 'A-103')"
    )
    conn.commit()
    conn.close()

    res = collect_commercial_review_signals(app, {"id": 101})
    assert res.signal_count == 1
    assert res.signals[0].source_family == "register"
    assert res.signals[0].severity == "BLOCKER"
    assert "RFI 01" in res.signals[0].title


def test_scale_gate_signals(test_db):
    app = MockApp(test_db)
    conn = sqlite3.connect(test_db)
    cur = conn.cursor()
    cur.execute("INSERT INTO workspaces VALUES (101, 'JOB-101', 'Test Job', 'Issue A')")
    cur.execute("INSERT INTO documents VALUES (1, 101, 'A-101.pdf')")
    cur.execute("INSERT INTO pages VALUES (10, 101, 1, 1, 'Ground Floor Plan', 1, NULL)")
    cur.execute("INSERT INTO mapped_zones VALUES (1, 101, 10, 'Zone 1')")
    conn.commit()
    conn.close()

    res = collect_commercial_review_signals(app, {"id": 101})
    assert res.signal_count == 1
    assert res.signals[0].category == "Scale & calibration"
    assert res.signals[0].severity == "BLOCKER"
    assert res.signals[0].source_id == "10"


def test_model_signals_stale_diagnostics(test_db):
    app = MockApp(test_db)
    conn = sqlite3.connect(test_db)
    cur = conn.cursor()
    cur.execute("INSERT INTO workspaces VALUES (101, 'JOB-101', 'Test Job', 'Issue A')")
    model_payload = json.dumps({"is_stale": True, "review_diagnostics": ["Unmatched wall elevation cutout"]})
    cur.execute("INSERT INTO workspace_settings VALUES (1, 101, 'canonical_3d_model_v1', ?)", (model_payload,))
    conn.commit()
    conn.close()

    res = collect_commercial_review_signals(app, {"id": 101})
    assert res.signal_count == 1
    assert res.signals[0].category == "3D model"
    assert res.signals[0].severity == "INFORMATION"
    assert "Unmatched wall elevation cutout" in res.signals[0].reasons[0]


def test_deduplication_of_reasons_on_one_row(test_db):
    app = MockApp(test_db)
    conn = sqlite3.connect(test_db)
    cur = conn.cursor()
    cur.execute("INSERT INTO workspaces VALUES (101, 'JOB-101', 'Test Job', 'Issue A')")
    cur.execute(
        "INSERT INTO takeoff_rows VALUES (1, 101, 1, 1, 'Complex Wall', 'Multi issue row', 'L1', 'm2', NULL, 'Provisional measured', 'low', 'clarification', 'A-101')"
    )
    conn.commit()
    conn.close()

    res = collect_commercial_review_signals(app, {"id": 101})
    assert res.signal_count == 1
    sig = res.signals[0]
    # Single card with multiple reasons
    assert len(sig.reasons) >= 2
    assert any("provisional" in r.lower() for r in sig.reasons)
    assert any("confidence" in r.lower() for r in sig.reasons)


def test_workspace_isolation(test_db):
    app = MockApp(test_db)
    conn = sqlite3.connect(test_db)
    cur = conn.cursor()
    cur.execute("INSERT INTO workspaces VALUES (101, 'JOB-101', 'WS 101', 'Issue A')")
    cur.execute("INSERT INTO workspaces VALUES (202, 'JOB-202', 'WS 202', 'Issue A')")
    cur.execute(
        "INSERT INTO takeoff_rows VALUES (1, 101, 1, 1, 'Row WS101', 'Test', 'L1', 'm2', NULL, 'To measure', 'high', 'included', 'A-101')"
    )
    cur.execute(
        "INSERT INTO takeoff_rows VALUES (2, 202, 1, 1, 'Row WS202', 'Test', 'L1', 'm2', 10.0, 'Measured', 'high', 'included', 'A-101')"
    )
    conn.commit()
    conn.close()

    res101 = collect_commercial_review_signals(app, {"id": 101})
    res202 = collect_commercial_review_signals(app, {"id": 202})
    assert res101.signal_count == 1
    assert res101.signals[0].workspace_id == 101
    assert res202.signal_count == 0


def test_source_fix_regression(test_db):
    app = MockApp(test_db)
    conn = sqlite3.connect(test_db)
    cur = conn.cursor()
    cur.execute("INSERT INTO workspaces VALUES (101, 'JOB-101', 'WS 101', 'Issue A')")
    cur.execute(
        "INSERT INTO takeoff_rows VALUES (1, 101, 1, 1, 'Row 1', 'Test', 'L1', 'm2', NULL, 'To measure', 'high', 'included', 'A-101')"
    )
    conn.commit()
    conn.close()

    res_initial = collect_commercial_review_signals(app, {"id": 101})
    assert res_initial.signal_count == 1

    # Fix at source: Estimator sets Measured + 120.0
    conn = sqlite3.connect(test_db)
    cur = conn.cursor()
    cur.execute("UPDATE takeoff_rows SET quantity=120.0, quantity_status='Measured' WHERE id=1")
    conn.commit()
    conn.close()

    res_fixed = collect_commercial_review_signals(app, {"id": 101})
    assert res_fixed.signal_count == 0


def test_source_delete_regression(test_db):
    app = MockApp(test_db)
    conn = sqlite3.connect(test_db)
    cur = conn.cursor()
    cur.execute("INSERT INTO workspaces VALUES (101, 'JOB-101', 'WS 101', 'Issue A')")
    cur.execute(
        "INSERT INTO takeoff_rows VALUES (1, 101, 1, 1, 'Row 1', 'Test', 'L1', 'm2', NULL, 'To measure', 'high', 'included', 'A-101')"
    )
    conn.commit()
    conn.close()

    res_initial = collect_commercial_review_signals(app, {"id": 101})
    assert res_initial.signal_count == 1

    conn = sqlite3.connect(test_db)
    cur = conn.cursor()
    cur.execute("DELETE FROM takeoff_rows WHERE id=1")
    conn.commit()
    conn.close()

    res_deleted = collect_commercial_review_signals(app, {"id": 101})
    assert res_deleted.signal_count == 0


def test_performance_benchmark(test_db):
    app = MockApp(test_db)
    conn = sqlite3.connect(test_db)
    cur = conn.cursor()
    cur.execute("INSERT INTO workspaces VALUES (101, 'JOB-101', 'WS 101', 'Issue A')")

    # Insert 500 take-off rows
    to_tuples = []
    for i in range(1, 501):
        status = "To measure" if i % 5 == 0 else "Measured"
        qty = None if i % 5 == 0 else float(i * 10)
        to_tuples.append((i, 101, 1, 1, f"Element {i}", f"Desc {i}", "L1", "m2", qty, status, "high", "included", "A-101"))
    cur.executemany("INSERT INTO takeoff_rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", to_tuples)

    # Insert 100 register items
    reg_tuples = []
    for i in range(1, 101):
        st_val = "Open" if i % 4 == 0 else "Closed"
        reg_tuples.append((i, 101, f"RFI {i}", f"Title {i}", f"Detail {i}", st_val, "NORMAL", "A-102"))
    cur.executemany("INSERT INTO register_items VALUES (?,?,?,?,?,?,?,?)", reg_tuples)

    conn.commit()
    conn.close()

    t0 = time.perf_counter()
    res = collect_commercial_review_signals(app, {"id": 101})
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert res.signal_count == 100 + 25  # 100 to-measure takeoff rows + 25 open register items
    assert elapsed_ms < 200.0  # Must be fast under 200ms
