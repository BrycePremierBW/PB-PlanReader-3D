"""PlanReader v1.5.0 processing fast path and ETA progress display.

Low-risk optimisations layered on top of the existing memory-stable PDF pipeline:

* skip re-indexing an unchanged document when its complete page register already
  exists (avoids reopening every PDF page and repeating text extraction/classification);
* cache resized PNG bytes used for AI vision calls so retries/reviews of the same
  rendered page do not repeatedly reopen, resize and recompress the image; and
* wrap Streamlit progress bars so they display an estimated time remaining.

No take-off, scope, geometry or benchmark maths is changed.
"""
from __future__ import annotations

import io
import math
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

VERSION = "1.5.0"
_DURATION_LOCK = threading.Lock()
_DURATION_HISTORY: Dict[str, Tuple[float, int]] = {}


def _normalise_fraction(value: Any) -> float:
    try:
        v = float(value)
    except Exception:
        return 0.0
    if v > 1.0:
        v /= 100.0
    return max(0.0, min(v, 1.0))


def _format_seconds(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _remember_duration(key: str, duration: float) -> None:
    if duration <= 0:
        return
    with _DURATION_LOCK:
        previous = _DURATION_HISTORY.get(key)
        if previous is None:
            _DURATION_HISTORY[key] = (duration, 1)
            return
        avg, count = previous
        count = min(count + 1, 8)
        # Recent runs should matter more than ancient ones without making ETA jumpy.
        weight = 1.0 / count
        _DURATION_HISTORY[key] = (avg * (1.0 - weight) + duration * weight, count)


def _expected_duration(key: str) -> float | None:
    with _DURATION_LOCK:
        item = _DURATION_HISTORY.get(key)
    return item[0] if item else None


class _ETAProgress:
    """Proxy around a Streamlit progress element that adds ETA text."""

    def __init__(self, target: Any, initial_value: Any, initial_text: str | None = None):
        self._target = target
        self._start = time.monotonic()
        self._last_fraction = _normalise_fraction(initial_value)
        self._initial_text = initial_text or ""

    def _eta_text(self, fraction: float, supplied: str | None = None) -> str:
        base = supplied or self._initial_text or "Loading"
        elapsed = max(0.0, time.monotonic() - self._start)
        if fraction >= 0.999:
            return f"{base} · complete"
        # Do not claim a numeric ETA until there is enough real progress to infer it.
        if elapsed < 0.35 or fraction < 0.02:
            return f"{base} · time remaining: estimating…"
        eta = elapsed * (1.0 - fraction) / max(fraction, 0.001)
        eta = min(eta, 6 * 60 * 60)
        return f"{base} · ~{_format_seconds(eta)} remaining"

    def progress(self, value: Any, text: str | None = None):
        fraction = _normalise_fraction(value)
        self._last_fraction = fraction
        label = self._eta_text(fraction, text)
        try:
            self._target.progress(value, text=label)
        except TypeError:
            self._target.progress(value)
        return self

    def empty(self):
        return self._target.empty()

    def __getattr__(self, name: str):
        return getattr(self._target, name)


def _install_eta_progress(app: Any) -> None:
    if getattr(app, "_pb_eta_progress_v150_applied", False):
        return
    app._pb_eta_progress_v150_applied = True
    base_progress = app.st.progress

    def progress_with_eta(value: Any, text: str | None = None, *args, **kwargs):
        fraction = _normalise_fraction(value)
        initial = text or "Loading"
        if fraction >= 0.999:
            label = f"{initial} · complete"
        else:
            label = f"{initial} · time remaining: estimating…"
        try:
            target = base_progress(value, text=label, *args, **kwargs)
        except TypeError:
            target = base_progress(value, *args, **kwargs)
        return _ETAProgress(target, value, initial)

    app.st.progress = progress_with_eta


def _install_ai_image_cache(app: Any) -> None:
    base_ai_page_bytes = app._ai_page_bytes

    @lru_cache(maxsize=96)
    def _cached(path_text: str, mtime_ns: int, size: int, max_long_edge: int) -> bytes:
        # The mtime/size values are intentionally part of the cache key so a
        # replaced page image cannot reuse stale bytes.
        del mtime_ns, size
        return base_ai_page_bytes(Path(path_text), max_long_edge)

    def cached_ai_page_bytes(path: Path, max_long_edge: int = None) -> bytes:
        path = Path(path)
        if max_long_edge is None:
            max_long_edge = int(getattr(app, "_AI_IMAGE_LONG_EDGE_PX", 1600))
        try:
            stat = path.stat()
            return _cached(str(path), int(stat.st_mtime_ns), int(stat.st_size), int(max_long_edge))
        except OSError:
            return base_ai_page_bytes(path, int(max_long_edge))

    app._ai_page_bytes = cached_ai_page_bytes
    app.clear_ai_image_cache = _cached.cache_clear


def _install_index_fastpath(app: Any) -> None:
    base_index = app.index_document_pages

    def fast_index_document_pages(document_id: int):
        docs = app.lquery(
            "SELECT id,page_count FROM documents WHERE id=?",
            (int(document_id),),
        )
        if docs:
            expected = int(docs[0].get("page_count") or 0)
            if expected > 0:
                rows = app.lquery(
                    "SELECT page_no,page_label,page_type,extracted_text FROM pages WHERE document_id=? ORDER BY page_no",
                    (int(document_id),),
                )
                # A full register with text/classification is deterministic for an
                # immutable uploaded document, so rebuilding it only wastes time.
                if len(rows) == expected and all(
                    int(r.get("page_no") or 0) > 0
                    and str(r.get("page_label") or "").strip()
                    and str(r.get("page_type") or "").strip()
                    for r in rows
                ):
                    return expected, "Already indexed"
        return base_index(int(document_id))

    app.index_document_pages = fast_index_document_pages


def _install_worker_eta(app: Any) -> None:
    """Give indeterminate threaded operations a history-based ETA as well."""
    base = app._run_with_progress

    def run_with_eta(worker: Callable[[], Any], caption: str) -> Any:
        # Keep the existing worker implementation so exception and threading
        # behaviour remains proven. The global progress proxy handles the bar.
        start = time.monotonic()
        expected = _expected_duration(str(caption))
        if expected:
            # Seed the progress proxy's initial text through the normal caption;
            # numeric ETA will continue updating as the existing bar advances.
            caption_for_run = f"{caption} (typical {_format_seconds(expected)})"
        else:
            caption_for_run = caption
        try:
            return base(worker, caption_for_run)
        finally:
            _remember_duration(str(caption), time.monotonic() - start)

    app._run_with_progress = run_with_eta


def apply(app: Any) -> None:
    if getattr(app, "_pb_processing_fastpath_v150_applied", False):
        return
    app._pb_processing_fastpath_v150_applied = True
    _install_index_fastpath(app)
    _install_ai_image_cache(app)
    _install_eta_progress(app)
    _install_worker_eta(app)
    app.PROCESSING_FASTPATH_VERSION = VERSION
