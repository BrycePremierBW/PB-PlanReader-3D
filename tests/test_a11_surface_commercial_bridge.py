from __future__ import annotations

import sqlite3

from pb_3d_surface_editor_v1212 import (
    build_surface_takeoff_rows,
    derive_mass_surfaces,
    surface_records,
)
from pb_commercial_export_preflight_v163 import derive_export_preflight
from pb_takeoff_authority_v164 import approve_model_surface_row


class _App:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def lquery(self, sql, params=()):
        cursor = self.conn.execute(sql, params)
        columns = [item[0] for item in cursor.description] if cursor.description else []
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def scale_gate_issues(self, workspace_id):
        return []


def _database():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE workspaces (
            id INTEGER PRIMARY KEY, job_no TEXT, job_name TEXT,
            drawing_issue TEXT, jobhub_job_id INTEGER
        );
        CREATE TABLE takeoff_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER,
            section TEXT, element TEXT, location TEXT, substrate TEXT,
            finish_system TEXT, quantity REAL, unit TEXT, quantity_status TEXT,
            source_page TEXT, source_reference TEXT, inclusion_status TEXT,
            coats REAL, coverage_m2_per_litre REAL,
            productivity_m2_per_hour REAL, rate_per_unit REAL, confidence TEXT,
            notes TEXT, row_role TEXT, commercial_authority_status TEXT,
            commercial_authority_source TEXT, commercial_authority_reviewed_by TEXT,
            commercial_authority_reviewed_at TEXT,
            commercial_authority_fingerprint TEXT
        );
        CREATE TABLE register_items (
            id INTEGER PRIMARY KEY, workspace_id INTEGER, register_name TEXT,
            title TEXT, detail TEXT, status TEXT, priority TEXT,
            source_reference TEXT
        );
        INSERT INTO workspaces VALUES (1, 'A11-1', 'A11 bridge', 'Rev A', 501);
        """
    )
    return conn


def _insert_generated_row(conn: sqlite3.Connection):
    mass = {
        "id": 7,
        "label": "Synthetic-equivalent Block A",
        "level_name": "Ground",
        "x": 0,
        "y": 0,
        "z": 0,
        "width": 4,
        "depth": 3,
        "height": 2.5,
        "finish": "Rendered block",
        "confidence": "Verified",
        "source_reference": "",
    }
    surface = derive_mass_surfaces(mass)[0]
    record = surface_records(
        [surface], {surface["surface_id"]: {"status": "Paint Included"}}
    )[0]
    row = build_surface_takeoff_rows([record])[0]
    columns = ["workspace_id", *row.keys()]
    conn.execute(
        f"INSERT INTO takeoff_rows ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        (1, *row.values()),
    )
    conn.commit()


def test_surface_editor_to_phase6b_to_phase6d_is_fail_closed_until_reviewed():
    conn = _database()
    _insert_generated_row(conn)
    app = _App(conn)

    generated = dict(zip(
        [item[0] for item in conn.execute("SELECT * FROM takeoff_rows").description],
        conn.execute("SELECT * FROM takeoff_rows").fetchone(),
    ))
    assert generated["quantity_status"] == "Provisional measured"
    assert generated["confidence"] == "Derived"
    assert generated["commercial_authority_status"] == "REVIEW_REQUIRED"
    assert generated["commercial_authority_source"] == ""

    blocked = derive_export_preflight(app, 1, bridge_available=True)
    assert blocked.publishable_takeoff_rows == 0
    assert blocked.preflight_status == "BLOCKED"
    assert blocked.final_publish_state == "BLOCKED"
    assert any("3D model surface" in reason for reason in blocked.blocking_reasons)

    approved = approve_model_surface_row(
        generated,
        source="A-401 / calibrated estimator measurement M-22",
        reviewed_by="Senior Estimator",
        reviewed_at="2026-09-04T10:00:00+10:00",
    )
    conn.execute(
        """UPDATE takeoff_rows SET
            commercial_authority_status=?,commercial_authority_source=?,
            commercial_authority_reviewed_by=?,commercial_authority_reviewed_at=?,
            commercial_authority_fingerprint=? WHERE id=?""",
        (
            approved["commercial_authority_status"],
            approved["commercial_authority_source"],
            approved["commercial_authority_reviewed_by"],
            approved["commercial_authority_reviewed_at"],
            approved["commercial_authority_fingerprint"],
            generated["id"],
        ),
    )
    conn.commit()

    reviewed = derive_export_preflight(app, 1, bridge_available=True)
    assert reviewed.publishable_takeoff_rows == 1
    assert reviewed.preflight_status == "AVAILABLE_WITH_WARNING"
    assert reviewed.final_publish_state == "AVAILABLE_WITH_WARNING"

    conn.execute("UPDATE takeoff_rows SET quantity=999.0 WHERE id=?", (generated["id"],))
    conn.commit()
    tampered = derive_export_preflight(app, 1, bridge_available=True)
    assert tampered.publishable_takeoff_rows == 0
    assert tampered.final_publish_state == "BLOCKED"
    conn.close()
