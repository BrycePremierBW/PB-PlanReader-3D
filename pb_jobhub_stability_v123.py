from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, List, Tuple


CACHE_TTL_SECONDS = 30.0
STALE_FALLBACK_SECONDS = 300.0


def apply(app) -> None:
    """Reduce JobHub connection churn and survive short PostgreSQL/network wobbles."""
    if getattr(app, "_pb_v123_jobhub_stability_patched", False):
        return

    base_bridge = app.JobHubBridge
    base_fetch_jobs = app.fetch_jobhub_jobs

    table_cache: Dict[str, Tuple[float, List[str]]] = {}
    column_cache: Dict[Tuple[str, str], Tuple[float, List[str]]] = {}
    jobs_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}

    def bridge_key(bridge) -> str:
        raw = f"{getattr(bridge, 'kind', '')}|{getattr(bridge, 'source', '')}"
        return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()

    def cached_value(cache, key, ttl: float):
        item = cache.get(key)
        if not item:
            return None
        stamp, value = item
        if time.monotonic() - stamp <= ttl:
            return value
        return None

    def stale_value(cache, key):
        item = cache.get(key)
        if not item:
            return None
        stamp, value = item
        if time.monotonic() - stamp <= STALE_FALLBACK_SECONDS:
            return value
        return None

    class StableJobHubBridge(base_bridge):
        def table_names(self) -> List[str]:
            key = bridge_key(self)
            fresh = cached_value(table_cache, key, CACHE_TTL_SECONDS)
            if fresh is not None:
                return list(fresh)
            try:
                value = list(super().table_names())
            except Exception:
                stale = stale_value(table_cache, key)
                if stale is not None:
                    return list(stale)
                raise
            table_cache[key] = (time.monotonic(), value)
            return list(value)

        def columns(self, table: str) -> List[str]:
            key = (bridge_key(self), str(table))
            fresh = cached_value(column_cache, key, CACHE_TTL_SECONDS)
            if fresh is not None:
                return list(fresh)
            try:
                value = list(super().columns(table))
            except Exception:
                stale = stale_value(column_cache, key)
                if stale is not None:
                    return list(stale)
                raise
            column_cache[key] = (time.monotonic(), value)
            return list(value)

        def query(self, sql, params=()):
            """Read-only query helper with one retry for transient connection failures."""
            try:
                return super().query(sql, params)
            except Exception:
                time.sleep(0.35)
                return super().query(sql, params)

    app.JobHubBridge = StableJobHubBridge

    def stable_fetch_jobhub_jobs(bridge) -> List[Dict[str, Any]]:
        key = bridge_key(bridge)
        fresh = cached_value(jobs_cache, key, CACHE_TTL_SECONDS)
        if fresh is not None:
            return [dict(row) for row in fresh]
        try:
            value = [dict(row) for row in base_fetch_jobs(bridge)]
        except Exception:
            stale = stale_value(jobs_cache, key)
            if stale is not None:
                return [dict(row) for row in stale]
            raise
        jobs_cache[key] = (time.monotonic(), value)
        return [dict(row) for row in value]

    app.fetch_jobhub_jobs = stable_fetch_jobhub_jobs
    app._pb_v123_jobhub_stability_patched = True
