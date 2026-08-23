"""Shared test fixtures for PB PlanReader tests.

Provides reusable database shims, fake bridges, and helper functions
that are currently duplicated across 18+ test files.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock


def make_temp_db() -> sqlite3.Connection:
    """Create an in-memory SQLite database with the PlanReader schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workspaces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            jobhub_id INTEGER,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL,
            file_name TEXT,
            path TEXT,
            sha256 TEXT,
            page_count INTEGER DEFAULT 0,
            extracted_text TEXT,
            uploaded_at TEXT,
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL,
            workspace_id INTEGER NOT NULL,
            page_no INTEGER,
            page_label TEXT,
            page_type TEXT,
            text_content TEXT,
            FOREIGN KEY(document_id) REFERENCES documents(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            workspace_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT,
            updated_at TEXT,
            UNIQUE(workspace_id, key)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS takeoff_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL,
            page_no INTEGER,
            drawing TEXT,
            section TEXT,
            element TEXT,
            unit TEXT,
            quantity REAL DEFAULT 0,
            scale TEXT,
            notes TEXT,
            status TEXT DEFAULT 'To measure',
            FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
        )
    """)
    conn.execute("INSERT INTO workspaces (id, name) VALUES (1, 'test')")
    conn.commit()
    return conn


def lquery(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """Execute a query and return list of dicts (mimics app.lquery)."""
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def lexecute(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> None:
    """Execute a statement (mimics app.lexecute)."""
    conn.execute(sql, params)
    conn.commit()


class FakeBridge:
    """Fake JobHub bridge for testing without Postgres."""

    def __init__(self, tables: Optional[Dict[str, List[Dict]]] = None):
        self._tables = tables or {}
        self._calls: List[tuple] = []

    def table_names(self) -> List[str]:
        return list(self._tables.keys())

    def columns(self, table: str) -> List[str]:
        if table in self._tables and self._tables[table]:
            return list(self._tables[table][0].keys())
        return []

    def query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        self._calls.append(("query", sql, params))
        # Simple in-memory fake: return empty for most queries
        return []

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._calls.append(("execute", sql, params))


class FakeApp:
    """Minimal fake app object for testing module apply() functions."""

    def __init__(self):
        self.st = MagicMock()
        self.local_connect = make_temp_db
        self.now_stamp = lambda: "2026-01-01 00:00:00"
        self._applied: List[str] = []

    def __getattr__(self, name: str):
        # Allow dynamic attribute access for monkey-patched modules
        if name.startswith("_"):
            raise AttributeError(name)
        return MagicMock()
