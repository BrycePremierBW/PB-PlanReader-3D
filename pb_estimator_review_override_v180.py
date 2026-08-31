"""pb_estimator_review_override_v180.py — Estimator Review UI, Manual Override, & Provenance Tracing Engine.

Manages manual quantity overrides, substrate adjustments, estimator review sign-offs,
and immutable audit log trails for PB PlanReader.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import datetime
import sqlite3
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class OverrideRecord:
    override_id: int
    row_id: int
    field_name: str
    old_value: str
    new_value: str
    override_reason: str
    estimator_name: str
    timestamp: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "override_id": self.override_id,
            "row_id": self.row_id,
            "field_name": self.field_name,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "override_reason": self.override_reason,
            "estimator_name": self.estimator_name,
            "timestamp": self.timestamp,
        }


class EstimatorOverrideRegistry:
    """Manages takeoff row overrides and audit trail logs for a workspace."""

    def __init__(self, overrides: List[OverrideRecord]):
        self.overrides = overrides
        self._by_row = {}
        for o in overrides:
            self._by_row.setdefault(o.row_id, []).append(o)

    @classmethod
    def apply_override(
        cls,
        conn: sqlite3.Connection,
        workspace_id: int,
        row_id: int,
        field_name: str,
        new_value: Any,
        override_reason: str,
        estimator_name: str = "Estimator",
    ) -> OverrideRecord:
        """Apply a manual override to a takeoff row and record audit entry."""
        cur = conn.cursor()

        # Fetch current value
        cur.execute(f"SELECT {field_name} FROM takeoff_rows WHERE id=? AND workspace_id=?", (row_id, workspace_id))
        row = cur.fetchone()
        old_val = str(row[0]) if row and row[0] is not None else ""

        # Update takeoff row with new value and mark notes with override trace
        cur.execute(
            f"UPDATE takeoff_rows SET {field_name}=?, notes=COALESCE(notes, '') || ' [MANUAL OVERRIDE: ' || ? || ']' WHERE id=? AND workspace_id=?",
            (new_value, str(override_reason), row_id, workspace_id)
        )

        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        cur.execute(
            """
            INSERT INTO takeoff_row_overrides (workspace_id, row_id, field_name, old_value, new_value, override_reason, estimator_name, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (workspace_id, row_id, field_name, old_val, str(new_value), override_reason, estimator_name, ts)
        )
        override_id = cur.lastrowid
        conn.commit()

        return OverrideRecord(
            override_id=int(override_id),
            row_id=row_id,
            field_name=field_name,
            old_value=old_val,
            new_value=str(new_value),
            override_reason=override_reason,
            estimator_name=estimator_name,
            timestamp=ts,
        )

    @classmethod
    def derive_for_workspace(cls, conn: sqlite3.Connection, workspace_id: int) -> "EstimatorOverrideRegistry":
        cur = conn.cursor()
        # Create overrides table if not exists
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS takeoff_row_overrides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER,
                row_id INTEGER,
                field_name TEXT,
                old_value TEXT,
                new_value TEXT,
                override_reason TEXT,
                estimator_name TEXT,
                timestamp TEXT
            )
            """
        )
        conn.commit()

        cur.execute(
            "SELECT id, row_id, field_name, old_value, new_value, override_reason, estimator_name, timestamp FROM takeoff_row_overrides WHERE workspace_id=? ORDER BY id ASC",
            (workspace_id,)
        )
        rows = cur.fetchall()

        overrides = [
            OverrideRecord(
                override_id=int(r[0]),
                row_id=int(r[1]),
                field_name=str(r[2]),
                old_value=str(r[3]),
                new_value=str(r[4]),
                override_reason=str(r[5]),
                estimator_name=str(r[6]),
                timestamp=str(r[7]),
            )
            for r in rows
        ]

        return cls(overrides)


def derive_estimator_overrides(conn: sqlite3.Connection, workspace_id: int) -> EstimatorOverrideRegistry:
    return EstimatorOverrideRegistry.derive_for_workspace(conn, workspace_id)
