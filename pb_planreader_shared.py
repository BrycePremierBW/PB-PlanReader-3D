"""Shared utilities for PB PlanReader.

Consolidates duplicated logic across the codebase: scale-ratio parsing,
dimension extraction, and page classification.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Scale-ratio parsing (was duplicated in 4 files)
# ---------------------------------------------------------------------------
SCALE_RE = re.compile(r"(?<!\d)1\s*:\s*(\d{2,4})(?!\d)", re.IGNORECASE)


def extract_scale_ratio(text: str) -> Optional[int]:
    """Extract a scale ratio like 1:100 from text. Returns the denominator or None."""
    m = SCALE_RE.search(str(text or ""))
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Dimension extraction (consolidated from auto_geometry + plan_read_engine)
# ---------------------------------------------------------------------------
DIM_RE = re.compile(
    r"(?<![:\d])(?P<num>\d{2,5}(?:\.\d{1,3})?)\s*(?P<unit>mm|m)?(?!\s*[:\d])",
    re.IGNORECASE,
)


def dimension_value_m(raw: str) -> Optional[float]:
    """Parse a dimension string (e.g. '3600', '3.6m', '1200mm') into metres.

    Returns None if the value is outside a reasonable architectural range.
    """
    compact = re.sub(r"\s+", "", str(raw or "")).lower()
    try:
        if compact.endswith("mm"):
            value = float(compact[:-2]) / 1000.0
            return value if 0.05 <= value <= 200.0 else None
        if compact.endswith("m") and not compact.endswith("mm"):
            value = float(compact[:-1])
            return value if 0.05 <= value <= 200.0 else None
        # Bare numbers in millimetres (e.g. 3600, 1200)
        m = DIM_RE.fullmatch(compact)
        if m:
            value = float(m.group("num"))
            unit = str(m.group("unit") or "").lower()
            if unit == "mm" or (not unit and value >= 100):
                value /= 1000.0
            elif unit == "m":
                pass
            else:
                return None
            return value if 0.05 <= value <= 200.0 else None
    except (ValueError, TypeError):
        pass
    return None


# ---------------------------------------------------------------------------
# Page classification keywords (consolidated from 6 files)
# ---------------------------------------------------------------------------
_FLOOR_PLAN_KW = [
    "floor plan", "site plan", "ground floor", "first floor",
    "second floor", "roof plan", "reflected ceiling plan",
    "basement plan", "upper floor", "lower floor",
]
_ELEVATION_KW = [
    "elevation", "external elevation", "internal elevation",
    "north elevation", "south elevation", "east elevation", "west elevation",
    "facade", "front elevation", "rear elevation", "side elevation",
]
_SECTION_KW = [
    "section", "cross section", "longitudinal section",
    "transverse section", "building section", "wall section",
]
_SCHEDULE_KW = [
    "schedule", "door schedule", "window schedule",
    "finishing schedule", "material schedule", "brick schedule",
    "specification", "colour schedule", "paint schedule",
]
_TITLE_KW = [
    "title", "drawing register", "title / drawing register",
    "project information", "general notes", "location plan",
    "key plan", "site layout",
]
CEILING_KW = [
    "reflected ceiling plan", "rcp", "ceiling plan",
    "ceiling layout", "suspended ceiling",
]

# Drawing number pattern (consolidated from 3 files)
DRAWING_NUMBER_RE = re.compile(
    r"\b([A-Z]{1,3}\d{2,4}(?:[-/.][A-Z0-9]+)?)\b",
)


def classify_page(text: str) -> str:
    """Classify a page as Floor Plan, Elevation, Section, Schedule, or Title."""
    lower = (text or "").lower()

    scores = {
        "Floor Plan": sum(1 for kw in _FLOOR_PLAN_KW if kw in lower),
        "Elevation": sum(1 for kw in _ELEVATION_KW if kw in lower),
        "Section": sum(1 for kw in _SECTION_KW if kw in lower),
        "Schedule": sum(1 for kw in _SCHEDULE_KW if kw in lower),
        "Title / Drawing Register": sum(1 for kw in _TITLE_KW if kw in lower),
        "Reflected Ceiling Plan": sum(1 for kw in CEILING_KW if kw in lower),
    }

    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return "Other"
    return best


def detect_scale_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Find scale ratio in text and return dict with ratio and confidence."""
    m = SCALE_RE.search(str(text or ""))
    if not m:
        return None
    ratio = int(m.group(1))
    confidence = "high" if ratio in (50, 100, 200, 500, 1000) else "medium"
    return {"ratio": ratio, "denominator": ratio, "text": m.group(0), "confidence": confidence}


def detect_drawing_number(text: str) -> Optional[str]:
    """Extract a drawing number from text."""
    m = DRAWING_NUMBER_RE.search(str(text or ""))
    return m.group(1) if m else None
