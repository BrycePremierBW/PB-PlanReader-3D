"""PlanReader v1.4.9 runtime fast path.

Targets work still repeated on normal Streamlit reruns after the existing v1.2.15
SQLite and 3D performance guards:

* cache JobHub schema metadata (table/column discovery) for a short period so
  normal navigation does not repeatedly open remote Postgres connections; and
* briefly cache the JobHub job list so several UI reruns caused by one user
  interaction do not refetch the same list from the network.

The take-off, geometry, scope, benchmark maths and existing database-init guard
are deliberately untouched.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Tuple

VERSION = "1.4.9"

_META_LOCK = threading.Lock()
_META_CACHE: Dict[Tuple[str, str, str, str], Tuple[float, Any]] = {}
_JOB_LOCK = threading.Lock()
_JOB_CACHE: Dict[Tuple[str, str], Tuple[float, List[Dict[str, Any]]]] = {}
META_TTL_SECONDS = 300.0
JOB_TTL_SECONDS = 5.0


def _fresh(entry: Tuple[float, Any] | None, ttl: float) -> bool:
    return bool(entry and (time.monotonic() - entry[0]) < ttl)


def clear_runtime_caches() -> None:
    """Clear only the small in-process JobHub performance caches."""
    with _META_LOCK:
        _META_CACHE.clear()
    with _JOB_LOCK:
        _JOB_CACHE.clear()


def apply(app: Any) -> None:
    """Install low-risk rerun optimisations once."""
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
        rows = [dict(row) for row in base_fetch_jobhub_jobs(bridge)]
        with _JOB_LOCK:
            _JOB_CACHE[key] = (time.monotonic(), [dict(row) for row in rows])
        return rows

    app.fetch_jobhub_jobs = cached_fetch_jobhub_jobs
    app.clear_runtime_performance_caches = clear_runtime_caches
    app.RUNTIME_PERFORMANCE_VERSION = VERSION
