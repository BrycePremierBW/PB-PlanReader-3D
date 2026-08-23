"""PlanReader v1.5.0 processing fast path and ETA progress display.

Optimisations layered on top of the existing memory-stable PDF pipeline:

* skip re-indexing unchanged documents whose page register is already complete;
* render larger PDF selections with two bounded worker processes in parallel;
* cache resized PNG bytes used for AI vision calls;
* cache expensive building-envelope CV detection by immutable page-image fingerprint;
* wrap Streamlit progress bars so they display estimated time remaining; and
* retain the existing proven take-off, scope, geometry and benchmark maths.
"""
from __future__ import annotations

import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Sequence, Tuple

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
        label = f"{initial} · complete" if fraction >= 0.999 else f"{initial} · time remaining: estimating…"
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


def _install_envelope_cache(app: Any) -> None:
    base_detector = app.auto_detect_building_envelope

    @lru_cache(maxsize=64)
    def _cached(path_text: str, mtime_ns: int, size: int, min_area_pct: float, max_contours: int):
        del mtime_ns, size
        result = base_detector(path_text, min_area_pct, max_contours)
        # Return immutable-ish cached content; public wrapper copies dictionaries.
        return tuple(tuple(sorted(item.items())) for item in result)

    def cached_detector(image_path: Any, min_area_pct: float = 0.4, max_contours: int = 3):
        path = Path(str(image_path or ""))
        try:
            stat = path.stat()
            packed = _cached(
                str(path), int(stat.st_mtime_ns), int(stat.st_size),
                float(min_area_pct), int(max_contours),
            )
            return [dict(items) for items in packed]
        except OSError:
            return base_detector(image_path, min_area_pct, max_contours)

    app.auto_detect_building_envelope = cached_detector
    app.clear_envelope_detection_cache = _cached.cache_clear


def _install_index_fastpath(app: Any) -> None:
    base_index = app.index_document_pages

    def fast_index_document_pages(document_id: int):
        docs = app.lquery("SELECT id,page_count FROM documents WHERE id=?", (int(document_id),))
        if docs:
            expected = int(docs[0].get("page_count") or 0)
            if expected > 0:
                rows = app.lquery(
                    "SELECT page_no,page_label,page_type,extracted_text FROM pages WHERE document_id=? ORDER BY page_no",
                    (int(document_id),),
                )
                if len(rows) == expected and all(
                    int(r.get("page_no") or 0) > 0
                    and str(r.get("page_label") or "").strip()
                    and str(r.get("page_type") or "").strip()
                    for r in rows
                ):
                    return expected, "Already indexed"
        return base_index(int(document_id))

    app.index_document_pages = fast_index_document_pages


def _render_worker_count(job_count: int) -> int:
    raw = str(os.environ.get("PLANREADER_RENDER_WORKERS", "2") or "2").strip()
    try:
        requested = int(raw)
    except ValueError:
        requested = 2
    # Render memory is deliberately bounded: never fan out beyond two workers by
    # default. Very small batches stay serial because process startup dominates.
    return 1 if job_count < 4 else max(1, min(requested, 2, job_count))


def _install_parallel_pdf_rendering(app: Any) -> None:
    base_render = app._render_pdf_pages_in_worker

    def parallel_render(
        pdf_path: str,
        jobs: Sequence[Tuple[int, float, str]],
        timeout: float = None,
        progress_cb: Callable[[int, int, int], None] | None = None,
    ):
        jobs = list(jobs)
        workers = _render_worker_count(len(jobs))
        if workers <= 1:
            if timeout is None:
                return base_render(pdf_path, jobs, progress_cb=progress_cb)
            return base_render(pdf_path, jobs, timeout, progress_cb)

        # Split contiguous pages so each MuPDF process reads nearby pages and
        # avoids excessive seeking. Background threads only enqueue progress;
        # Streamlit callbacks are executed on the calling/main thread below.
        chunks: List[List[Tuple[int, float, str]]] = [[] for _ in range(workers)]
        chunk_size = (len(jobs) + workers - 1) // workers
        for i in range(workers):
            chunks[i] = jobs[i * chunk_size:(i + 1) * chunk_size]
        chunks = [c for c in chunks if c]
        events: queue.Queue[int] = queue.Queue()

        def run_chunk(chunk):
            def local_progress(_completed, _total, page_no):
                events.put(int(page_no))
            if timeout is None:
                return base_render(pdf_path, chunk, progress_cb=local_progress)
            return base_render(pdf_path, chunk, timeout, local_progress)

        completed = 0
        gathered = []
        with ThreadPoolExecutor(max_workers=len(chunks), thread_name_prefix="pb-pdf-render") as pool:
            futures = [pool.submit(run_chunk, chunk) for chunk in chunks]
            while True:
                alive = any(not future.done() for future in futures)
                drained = False
                while True:
                    try:
                        page_no = events.get_nowait()
                    except queue.Empty:
                        break
                    drained = True
                    completed += 1
                    if progress_cb:
                        progress_cb(completed, len(jobs), page_no)
                if not alive:
                    break
                if not drained:
                    time.sleep(0.03)
            # Drain final progress events before returning.
            while True:
                try:
                    page_no = events.get_nowait()
                except queue.Empty:
                    break
                completed += 1
                if progress_cb:
                    progress_cb(completed, len(jobs), page_no)
            for future in futures:
                gathered.extend(future.result())

        order = {int(job[0]): i for i, job in enumerate(jobs)}
        gathered.sort(key=lambda row: order.get(int(row[0]), 10**9))
        return gathered

    app._render_pdf_pages_in_worker = parallel_render
    app.PDF_RENDER_WORKERS = _render_worker_count(100)


def _install_worker_eta(app: Any) -> None:
    base = app._run_with_progress

    def run_with_eta(worker: Callable[[], Any], caption: str) -> Any:
        start = time.monotonic()
        expected = _expected_duration(str(caption))
        caption_for_run = f"{caption} (typical {_format_seconds(expected)})" if expected else caption
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
    _install_envelope_cache(app)
    _install_parallel_pdf_rendering(app)
    _install_eta_progress(app)
    _install_worker_eta(app)
    app.PROCESSING_FASTPATH_VERSION = VERSION
