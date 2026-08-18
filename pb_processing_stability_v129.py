"""PlanReader v1.2.9 processing stability hardening.

Keeps PDF rasterisation memory bounded so processing large plan sets is less
likely to restart the Streamlit service (which would also clear login session
state). This patch is additive and leaves the existing processing workflow in
place.
"""
from __future__ import annotations

import os
from typing import Any


def _release_mupdf_cache(app: Any) -> None:
    """Release MuPDF cache retained by the parent process after PDF inspection."""
    fitz = getattr(app, "fitz", None)
    try:
        tools = getattr(fitz, "TOOLS", None)
        shrink = getattr(tools, "store_shrink", None)
        if callable(shrink):
            shrink(100)
    except Exception:
        pass
    try:
        app.gc.collect()
    except Exception:
        pass


def _render_long_edge_px() -> int:
    raw = str(os.environ.get("PLANREADER_RENDER_LONG_EDGE_PX", "1800") or "1800").strip()
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        value = 1800
    return max(1200, min(value, 2400))


def apply(app: Any) -> None:
    """Install low-memory processing guards on the production PlanReader module."""
    if getattr(app, "_pb_processing_stability_v129_applied", False):
        return
    app._pb_processing_stability_v129_applied = True

    # Rendering is for review/measurement display only; real-world quantities are
    # calibrated from the page's saved pixel scale, so lowering the raster ceiling
    # does not alter the drawing's physical measurement basis.
    app._PDF_RENDER_LONG_EDGE_PX = _render_long_edge_px()

    base_process_document = app.process_document
    base_index_document_pages = app.index_document_pages

    def _stable_process_document(*args, **kwargs):
        _release_mupdf_cache(app)
        try:
            return base_process_document(*args, **kwargs)
        finally:
            _release_mupdf_cache(app)

    def _stable_index_document_pages(*args, **kwargs):
        _release_mupdf_cache(app)
        try:
            return base_index_document_pages(*args, **kwargs)
        finally:
            _release_mupdf_cache(app)

    app.process_document = _stable_process_document
    app.index_document_pages = _stable_index_document_pages
    app.release_mupdf_cache = lambda: _release_mupdf_cache(app)
