"""PlanReader v1.5.2 material-review preview bounds guard.

Pillow rejects rectangles when the supplied coordinates are reversed or when a
review bbox lies beyond the rendered page.  Material review evidence can contain
PDF-space boxes from stale/alternate page renders, so normalise and clamp those
boxes before the v1.2.22 preview renderer draws them.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

from PIL import Image

import pb_material_schedule_v1222 as material

VERSION = "1.5.2"


def normalise_bbox(
    bbox: Any,
    bbox_mode: str,
    width: int,
    height: int,
) -> Tuple[float, float, float, float] | None:
    """Return an ordered, in-image xyxy bbox, or None when unusable."""
    if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
        return None
    if width <= 1 or height <= 1:
        return None
    try:
        x0, y0, a, b = [float(v) for v in bbox[:4]]
    except (TypeError, ValueError):
        return None
    if str(bbox_mode or "xyxy").lower() == "xywh":
        x1, y1 = x0 + a, y0 + b
    else:
        x1, y1 = a, b

    left, right = sorted((x0, x1))
    top, bottom = sorted((y0, y1))
    max_x = float(max(0, width - 1))
    max_y = float(max(0, height - 1))
    left = max(0.0, min(max_x, left))
    right = max(0.0, min(max_x, right))
    top = max(0.0, min(max_y, top))
    bottom = max(0.0, min(max_y, bottom))

    # Keep a drawable rectangle even when a stale bbox collapses at an edge.
    if right <= left:
        if left < max_x:
            right = min(max_x, left + 1.0)
        else:
            left = max(0.0, right - 1.0)
    if bottom <= top:
        if top < max_y:
            bottom = min(max_y, top + 1.0)
        else:
            top = max(0.0, bottom - 1.0)
    return left, top, right, bottom


def apply(app=None) -> None:
    """Patch material issue previews once; app is accepted for launcher symmetry."""
    if getattr(material, "_pb_material_preview_guard_v152_applied", False):
        return
    base_preview = material.issue_preview_bytes

    def guarded_preview(page: Dict[str, Any], issue: Dict[str, Any], max_long_edge: int = 1200) -> bytes:
        safe_issue = dict(issue or {})
        path = material.memory.regular_file((page or {}).get("image_path"))
        if path is not None and safe_issue.get("bbox") is not None:
            try:
                with Image.open(path) as source:
                    safe_box = normalise_bbox(
                        safe_issue.get("bbox"),
                        str(safe_issue.get("bbox_mode") or "xyxy"),
                        int(source.width),
                        int(source.height),
                    )
                if safe_box is None:
                    safe_issue["bbox"] = None
                else:
                    safe_issue["bbox"] = list(safe_box)
                    safe_issue["bbox_mode"] = "xyxy"
            except Exception:
                # Fall back to the renderer's banner path rather than crashing
                # the whole Subscription Take-off page over one bad preview.
                safe_issue["bbox"] = None
                safe_issue["bbox_mode"] = "xyxy"
        try:
            return base_preview(page, safe_issue, max_long_edge=max_long_edge)
        except ValueError as exc:
            # A preview is diagnostic UI only. If an unexpected geometry edge
            # case still reaches Pillow, retry without a bbox so the page stays
            # usable and the review issue remains visible.
            if "must be greater than or equal" not in str(exc):
                raise
            safe_issue["bbox"] = None
            safe_issue["bbox_mode"] = "xyxy"
            return base_preview(page, safe_issue, max_long_edge=max_long_edge)

    material.issue_preview_bytes = guarded_preview
    material._pb_material_preview_guard_v152_applied = True
