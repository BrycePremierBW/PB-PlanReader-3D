"""PlanReader runtime fast path.

Targets fixed work repeated on normal Streamlit reruns and first-page startup:

* cache JobHub schema metadata for a short period;
* briefly cache the JobHub job list;
* on a cold cache, fetch JobHub table metadata, job columns and the job list
  through one database connection instead of three separate connections; and
* derive the next shared PB job number from the already-fetched job list so the
  collapsed "Create standalone workspace" panel does not open another database
  connection just to render.

Take-off, geometry, scope and benchmark maths are deliberately untouched.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, List, Tuple

VERSION = "1.5.1"

_META_LOCK = threading.Lock()
_META_CACHE: Dict[Tuple[str, str, str, str], Tuple[float, Any]] = {}
_JOB_LOCK = threading.Lock()
_JOB_CACHE: Dict[Tuple[str, str], Tuple[float, List[Dict[str, Any]]]] = {}
META_TTL_SECONDS = 300.0
JOB_TTL_SECONDS = 15.0


def _fresh(entry: Tuple[float, Any] | None, ttl: float) -> bool:
    return bool(entry and (time.monotonic() - entry[0]) < ttl)


def clear_runtime_caches() -> None:
    """Clear only the small in-process JobHub performance caches."""
    with _META_LOCK:
        _META_CACHE.clear()
    with _JOB_LOCK:
        _JOB_CACHE.clear()


def _column_names(cur: Any, kind: str, table: str) -> List[str]:
    safe = re.sub(r"[^A-Za-z0-9_]", "", str(table))
    if kind == "postgres":
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
            (safe,),
        )
        return [str(row[0]) for row in cur.fetchall()]
    cur.execute(f"PRAGMA table_info({safe})")
    return [str(row[1]) for row in cur.fetchall()]


def _single_connection_job_fetch(bridge: Any) -> tuple[List[Dict[str, Any]], List[str], List[str]]:
    """Fetch startup JobHub metadata + jobs in one connection.

    The original helper called ``table_names()``, ``columns('jobs')`` and
    ``query(...)`` separately. Each opens a new Postgres connection. On Render,
    connection setup latency can dominate the entire first page load.
    """
    if not hasattr(bridge, "connect"):
        raise AttributeError("bridge has no shared connection context")

    kind = str(getattr(bridge, "kind", ""))
    with bridge.connect() as conn:
        cur = conn.cursor()
        if kind == "postgres":
            cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
            tables = [str(row[0]) for row in cur.fetchall()]
        else:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [str(row[0]) for row in cur.fetchall()]

        if "jobs" not in set(tables):
            return [], tables, []

        job_cols = _column_names(cur, kind, "jobs")
        job_set = set(job_cols)
        builder_expr = "'' AS builder_client"
        join = ""
        if "builder_client_id" in job_set and "builders_clients" in set(tables):
            builder_cols = set(_column_names(cur, kind, "builders_clients"))
            if "id" in builder_cols and "name" in builder_cols:
                join = " LEFT JOIN builders_clients b ON b.id=j.builder_client_id "
                builder_expr = "COALESCE(b.name,'') AS builder_client"
        elif "builder_client" in job_set:
            builder_expr = "COALESCE(j.builder_client,'') AS builder_client"

        fields = [
            "j.id",
            "COALESCE(j.job_no,'') AS job_no" if "job_no" in job_set else "CAST(j.id AS TEXT) AS job_no",
            "COALESCE(j.job_name,'') AS job_name" if "job_name" in job_set else "'' AS job_name",
            builder_expr,
            "COALESCE(j.site_address,'') AS site_address" if "site_address" in job_set else "'' AS site_address",
            "COALESCE(j.status,'') AS status" if "status" in job_set else "'' AS status",
        ]
        cur.execute(f"SELECT {', '.join(fields)} FROM jobs j {join} ORDER BY j.id DESC")
        names = [str(col[0]) for col in cur.description]
        rows = [dict(zip(names, row)) for row in cur.fetchall()]
        return rows, tables, job_cols


def _next_pb_number_from_rows(rows: List[Dict[str, Any]]) -> str:
    candidates: List[tuple[int, str]] = []
    for row in rows:
        value = str(row.get("job_no") or "").strip()
        if not value.upper().startswith("PB"):
            continue
        digits = "".join(ch for ch in value if ch.isdigit())
        if not digits:
            continue
        prefix = "".join(ch for ch in value if not ch.isdigit())
        try:
            candidates.append((int(digits), prefix))
        except ValueError:
            continue
    if not candidates:
        return "PB25001"
    number, prefix = max(candidates, key=lambda item: item[0])
    return f"{prefix}{number + 1:05d}"


def apply(app: Any) -> None:
    """Install low-risk startup/rerun optimisations once."""
    if getattr(app, "_pb_runtime_performance_v149_applied", False):
        return
    app._pb_runtime_performance_v149_applied = True

    bridge_cls = app.JobHubBridge
    base_table_names = bridge_cls.table_names
    base_columns = bridge_cls.columns

    def cached_table_names(self):
        key = (str(self.kind), str(self.source), "tables", "")
        with _META_LOCK:
            entry = _META_CACHE.get(key)
            if _fresh(entry, META_TTL_SECONDS):
                return list(entry[1])
        value = list(base_table_names(self))
        with _META_LOCK:
            _META_CACHE[key] = (time.monotonic(), tuple(value))
        return value

    def cached_columns(self, table: str):
        safe_table = str(table)
        key = (str(self.kind), str(self.source), "columns", safe_table)
        with _META_LOCK:
            entry = _META_CACHE.get(key)
            if _fresh(entry, META_TTL_SECONDS):
                return list(entry[1])
        value = list(base_columns(self, safe_table))
        with _META_LOCK:
            _META_CACHE[key] = (time.monotonic(), tuple(value))
        return value

    bridge_cls.table_names = cached_table_names
    bridge_cls.columns = cached_columns

    base_fetch_jobhub_jobs = app.fetch_jobhub_jobs

    def cached_fetch_jobhub_jobs(bridge):
        key = (str(bridge.kind), str(bridge.source))
        with _JOB_LOCK:
            entry = _JOB_CACHE.get(key)
            if _fresh(entry, JOB_TTL_SECONDS):
                return [dict(row) for row in entry[1]]

        try:
            rows, tables, job_cols = _single_connection_job_fetch(bridge)
            stamp = time.monotonic()
            meta_prefix = (str(bridge.kind), str(bridge.source))
            with _META_LOCK:
                _META_CACHE[(meta_prefix[0], meta_prefix[1], "tables", "")] = (stamp, tuple(tables))
                if job_cols:
                    _META_CACHE[(meta_prefix[0], meta_prefix[1], "columns", "jobs")] = (stamp, tuple(job_cols))
        except Exception:
            # Compatibility fallback for test doubles and unusual bridge types.
            rows = [dict(row) for row in base_fetch_jobhub_jobs(bridge)]

        rows = [dict(row) for row in rows]
        with _JOB_LOCK:
            _JOB_CACHE[key] = (time.monotonic(), [dict(row) for row in rows])
        return rows

    app.fetch_jobhub_jobs = cached_fetch_jobhub_jobs

    if hasattr(app, "next_jobhub_job_no"):
        base_next_job_no = app.next_jobhub_job_no

        def cached_next_jobhub_job_no(bridge):
            if bridge is None:
                return base_next_job_no(bridge)
            key = (str(bridge.kind), str(bridge.source))
            with _JOB_LOCK:
                entry = _JOB_CACHE.get(key)
                if _fresh(entry, JOB_TTL_SECONDS):
                    return _next_pb_number_from_rows([dict(row) for row in entry[1]])
            return base_next_job_no(bridge)

        app.next_jobhub_job_no = cached_next_jobhub_job_no

    app.clear_runtime_performance_caches = clear_runtime_caches
    app.RUNTIME_PERFORMANCE_VERSION = VERSION
