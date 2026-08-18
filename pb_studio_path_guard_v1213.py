"""PlanReader v1.2.13 guard for missing/invalid Takeoff Studio image paths.

A blank image_path becomes Path('.') in pathlib.  Because '.' exists, the
v1.2.11 Studio's old exists() guard could then call read_bytes() on the
application directory and raise IsADirectoryError.  This additive patch keeps
the proven Studio code unchanged and filters its page list to regular files.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pb_takeoff_studio_v1211 as studio


def is_regular_image_file(raw_path: Any) -> bool:
    """Return True only for a non-blank path that currently points to a file."""
    text = str(raw_path or "").strip()
    if not text:
        return False
    try:
        return Path(text).is_file()
    except (OSError, ValueError):
        return False


def filter_studio_pages(app: Any, workspace_id: int, base_pages: Callable):
    """Drop selected pages whose rendered image path is blank/stale/a directory."""
    pages = base_pages(app, workspace_id)
    if getattr(pages, "empty", True):
        return pages

    if "image_path" not in pages.columns:
        app.st.warning(
            "Takeoff Studio could not find rendered-image paths for the selected drawings. "
            "Re-process the source plans before opening the Studio."
        )
        return pages.iloc[0:0].copy()

    valid_mask = pages["image_path"].map(is_regular_image_file)
    filtered = pages.loc[valid_mask].reset_index(drop=True)
    skipped = int(len(pages) - len(filtered))
    if skipped:
        app.st.warning(
            f"Takeoff Studio skipped {skipped} selected drawing page(s) because their rendered image file is missing or invalid. "
            "Re-process those pages if you need them in the Studio."
        )
    return filtered


def apply(app: Any) -> None:
    """Install the v1.2.13 path guard once without replacing Studio logic."""
    if getattr(app, "_pb_studio_path_guard_v1213_applied", False):
        return
    app._pb_studio_path_guard_v1213_applied = True

    base_pages = studio._studio_pages

    def _guarded_pages(app_obj: Any, workspace_id: int):
        return filter_studio_pages(app_obj, workspace_id, base_pages)

    studio._studio_pages = _guarded_pages
