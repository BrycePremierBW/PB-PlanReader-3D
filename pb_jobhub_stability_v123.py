from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, List, Tuple


CACHE_TTL_SECONDS = 120.0
STALE_FALLBACK_SECONDS = 1800.0
ERROR_COOLDOWN_SECONDS = 60.0


def apply(app) -> None:
    """Reduce JobHub connection churn and survive short PostgreSQL/network wobbles.

    Caches successful table/column/jobs results for CACHE_TTL_SECONDS and keeps a
    stale copy for STALE_FALLBACK_SECONDS. Failed connections are remembered for
    ERROR_COOLDOWN_SECONDS so a down/slow database is not re-probed on every
    Streamlit rerun (every click), which used to block the whole page.
    """
    if getattr(app, "_pb_v123_jobhub_stability_patched", False):
        return

    base_bridge = app.JobHubBridge
    base_fetch_jobs = app.fetch_jobhub_jobs

    table_cache: Dict[str, Tuple[float, List[str]]] = {}
    column_cache: Dict[Tuple[str, str], Tuple[float, List[str]]] = {}
    jobs_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
    failure_cache: Dict[str, float] = {}

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

    def cooldown_check(key, cache):
        """Return cached/stale value if available, else raise a fast error when the
        bridge recently failed so reruns do not block on connection timeouts."""
        fresh = cached_value(cache, key, CACHE_TTL_SECONDS)
        if fresh is not None:
            return list(fresh)
        last = failure_cache.get(key)
        if last is not None and time.monotonic() - last < ERROR_COOLDOWN_SECONDS:
            stale = stale_value(cache, key)
            if stale is not None:
                return list(stale)
            raise RuntimeError(
                "JobHub is unreachable (recent connection failure, will retry automatically)."
            )
        return None

    def mark_failure(key: str) -> None:
        failure_cache[key] = time.monotonic()

    def mark_success(key: str) -> None:
        failure_cache.pop(key, None)

    def _with_retry(fn):
        """Run a read once, retrying a single time after a short pause for the
        transient SSL/connection drops Render Postgres occasionally serves."""
        try:
            return fn()
        except Exception:
            time.sleep(0.4)
            return fn()

    class StableJobHubBridge(base_bridge):
        def table_names(self) -> List[str]:
            key = bridge_key(self)
            hit = cooldown_check(key, table_cache)
            if hit is not None:
                return hit

            base = super()

            def _run():
                return base.table_names()

            try:
                value = list(_with_retry(_run))
            except Exception:
                mark_failure(key)
                stale = stale_value(table_cache, key)
                if stale is not None:
                    return list(stale)
                raise
            mark_success(key)
            table_cache[key] = (time.monotonic(), value)
            return list(value)

        def columns(self, table: str) -> List[str]:
            key = (bridge_key(self), str(table))
            hit = cooldown_check(key, column_cache)
            if hit is not None:
                return hit

            base = super()

            def _run():
                return base.columns(table)

            try:
                value = list(_with_retry(_run))
            except Exception:
                mark_failure(key[0])
                stale = stale_value(column_cache, key)
                if stale is not None:
                    return list(stale)
                raise
            mark_success(key[0])
            column_cache[key] = (time.monotonic(), value)
            return list(value)

        def query(self, sql, params=()):
            """Read-only query helper with one retry for transient connection failures."""
            key = bridge_key(self)
            last = failure_cache.get(key)
            if last is not None and time.monotonic() - last < ERROR_COOLDOWN_SECONDS:
                raise RuntimeError(
                    "JobHub is unreachable (recent connection failure, will retry automatically)."
                )
            try:
                result = super().query(sql, params)
            except Exception:
                time.sleep(0.35)
                try:
                    result = super().query(sql, params)
                except Exception:
                    mark_failure(key)
                    raise
            mark_success(key)
            return result

    app.JobHubBridge = StableJobHubBridge

    def stable_fetch_jobhub_jobs(bridge) -> List[Dict[str, Any]]:
        key = bridge_key(bridge)
        fresh = cached_value(jobs_cache, key, CACHE_TTL_SECONDS)
        if fresh is not None:
            return [dict(row) for row in fresh]
        last = failure_cache.get(key)
        if last is not None and time.monotonic() - last < ERROR_COOLDOWN_SECONDS:
            stale = stale_value(jobs_cache, key)
            if stale is not None:
                return [dict(row) for row in stale]
            raise RuntimeError(
                "JobHub is unreachable (recent connection failure, will retry automatically)."
            )
        try:
            value = [dict(row) for row in base_fetch_jobs(bridge)]
        except Exception:
            mark_failure(key)
            stale = stale_value(jobs_cache, key)
            if stale is not None:
                return [dict(row) for row in stale]
            raise
        mark_success(key)
        jobs_cache[key] = (time.monotonic(), value)
        return [dict(row) for row in value]

    app.fetch_jobhub_jobs = stable_fetch_jobhub_jobs
    app._pb_v123_jobhub_stability_patched = True
