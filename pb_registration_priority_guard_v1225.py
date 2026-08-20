"""v1.2.25 priority guard for architectural sheet headings.

The title-block detector is primary.  This guard strengthens the text-only fallback
used during early indexing: a standalone sheet heading (e.g. PARTITION PLAN) beats
multiple lines that merely say REFER/SEE ELEVATION or SECTION.
"""
from __future__ import annotations

import re
from typing import Any, Tuple

import pb_page_registration_v1225 as registration

_VERSION = "1.2.25"
_REFERENCE_WORDS = ("refer ", "refer to", "see ", "referenced", "detail ", "matchline", "keynote")
_HEADING_RULES = (
    ("Reflected Ceiling Plan", ("reflected ceiling plan", "reflected ceiling", "rcp")),
    ("Finishes Schedule", ("finish schedule", "finishes schedule", "finishing schedule", "material schedule", "colour schedule", "color schedule", "paint schedule")),
    ("Door / Window Schedule", ("door schedule", "window schedule", "door and window schedule")),
    ("Floor Plan", ("partition plan", "floor plan", "unit plan", "apartment plan", "general arrangement", "ga plan", "tenancy plan")),
    ("Roof Plan", ("roof plan",)),
    ("Elevation", ("external elevations", "external elevation", "building elevations", "building elevation", "north elevation", "south elevation", "east elevation", "west elevation")),
    ("Section", ("building sections", "building section", "wall sections", "wall section", "cross section")),
    ("Render / Artist's Impression", ("artist's impression", "artists impression", "artist impression", "3d render", "perspective")),
)
_BASE_WEIGHTED_PAGE_TYPE = registration.weighted_page_type


def _norm_line(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _is_reference_line(line: str) -> bool:
    low = _norm_line(line)
    if any(low.startswith(token) for token in _REFERENCE_WORDS):
        return True
    return any(token in low[:24] for token in ("refer to ", "see drawing", "see elevation", "see section"))


def strong_heading_type(text: Any) -> Tuple[str, str]:
    """Return a sheet type only from concise, non-reference heading lines."""
    lines = [str(line).strip() for line in str(text or "").splitlines() if str(line).strip()]
    # PyMuPDF reading order can place the title block near the end, so inspect the
    # whole page but only accept concise heading-like lines.
    for line in lines:
        low = _norm_line(line)
        if len(low) > 120 or _is_reference_line(low):
            continue
        for page_type, phrases in _HEADING_RULES:
            for phrase in phrases:
                if phrase == "rcp":
                    if re.search(r"\brcp\b", low):
                        return page_type, line
                elif phrase in low:
                    return page_type, line
    return "", ""


def weighted_page_type(full_text: Any, file_name: Any = "", title_block: Any = ""):
    base_type, confidence, evidence = _BASE_WEIGHTED_PAGE_TYPE(full_text, file_name, title_block)
    # Native title-block evidence remains strongest and already receives a large
    # score in the base classifier. Only improve the fallback when a concise
    # heading can be found.
    title_type, title_line = strong_heading_type(title_block)
    if title_type:
        return title_type, max(int(confidence), 94), f"title block heading: {title_line}"
    heading_type, heading_line = strong_heading_type(full_text)
    if heading_type:
        return heading_type, max(int(confidence) if heading_type == base_type else 0, 84), f"page heading: {heading_line}"
    return base_type, confidence, evidence


def classify_page(text: str, file_name: str, page_no: int):
    page_type, _confidence, _evidence = weighted_page_type(text, file_name, "")
    code = registration._candidate_code(text)
    return page_type, code or f"Page {int(page_no)}"


def apply(app: Any) -> None:
    if getattr(app, "_pb_registration_priority_guard_v1225_applied", False):
        return
    app._pb_registration_priority_guard_v1225_applied = True
    registration._pb_v1225_base_weighted_page_type = _BASE_WEIGHTED_PAGE_TYPE
    registration.weighted_page_type = weighted_page_type
    registration.classify_page = classify_page
    app.classify_page = classify_page
