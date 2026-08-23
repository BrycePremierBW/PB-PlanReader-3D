"""Room face take-off: extract calibrated room polygons → trustworthy m² take-off rows.

Priority 2: wires v145 planar face extraction through the calibrated scale
system and false-positive filters to produce per-room floor/ceiling take-off rows.

Architecture:
  PDF vector segments → v145 extract_planar_faces → page-space polygons
    → scale calibration → real-world m² → false-positive filter → take-off rows

Scale chain (reuses Priority 1):
  PDF points × (25.4/72) = page mm × real_metres_per_page_mm = real metres
  area_m2 = area_page_pts² × (real_metres_per_page_mm × 25.4/72)²

Never produce a confidently stated m² quantity from an uncalibrated polygon.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from math import atan2, pi
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PDF_PT_TO_MM = 25.4 / 72.0  # 1 PDF point = 0.3528 mm
MM_PER_PT = PDF_PT_TO_MM

# Soft area thresholds (in REAL-WORLD m² once calibrated)
# Below these, confidence is reduced — NOT an unconditional rejection.
SOFT_MIN_ROOM_AREA_M2 = 1.0     # below this, very unlikely to be a room
HARD_MIN_ROOM_AREA_M2 = 0.3     # below this, reject even with label
MAX_ROOM_AREA_M2 = 2500.0       # larger = building outline
HIGH_ELONGATION = 12.0          # aspect ratio > 12:1 needs label evidence
MAX_HOLES = 6                   # rooms with > 6 internal voids are suspicious

# Position-based rejection: title blocks typically sit in bottom-right
TITLE_BLOCK_Y_MIN = 0.85
TITLE_BLOCK_X_MIN = 0.60

# Page coverage threshold for border/outline rejection
PAGE_COVERAGE_MAX = 0.75

# Containment threshold: if candidate contains > this fraction of other
# centroids, it's likely a building outline
CONTAINMENT_OUTLINE_THRESHOLD = 0.50

# Source prefix for take-off rows from this module
SOURCE_PREFIX = "PB RoomFace v2"

# ---------------------------------------------------------------------------
# BLOCKER 1 — Semantic room label vocabulary
# ---------------------------------------------------------------------------

# Exact room-type words (case-insensitive matching)
ROOM_LABEL_EXACT: set[str] = {
    # Living / sleeping
    "bedroom", "bed", "master", "guest", "lounge", "living", "family",
    "rumpus", "media", "theatre", "theater", "snug",
    # Eating / cooking
    "kitchen", "dining", "breakfast", "pantry", "meals", "cafe",
    # Wet areas
    "bath", "bathroom", "ensuite", "ensuites", "wc", "toilet",
    "powder", "shower", "laundry", "wash", "mud",
    # Storage
    "store", "storage", "linen", "cupboard", "robe", "wir",
    "walk", "closet",
    # Circulation
    "corridor", "hall", "hallway", "entry", "foyer", "vestibule",
    "passage", "landing", "stair", "stairs", "staircase",
    "en-suite", "ensuite",
    # Room (standalone — needed for multi-word phrases like "LIVING ROOM")
    "room",
    # Work
    "office", "study", "library", "workshop", "studio",
    # Utility
    "garage", "carport", "shed", "plant", "mech", "mechanical",
    "electrical", "server", "comms", "riser", "duct",
    # Other rooms
    "nursery", "playroom", "sunroom", "conservatory", "cellar",
    "basement", "attic", "loft", "void",
}

# Prefix patterns — a word starting with these MAY be a room label
# (e.g., "BEDROOMS" starts with "BEDROOM", "KITCHENETTE" starts with "KITCHEN")
ROOM_LABEL_PREFIXES: tuple[str, ...] = (
    "bedroom", "kitchen", "bathroom", "laundry", "garage",
    "lounge", "dining", "office", "study", "store",
    "pantry", "ensuite", "corridor", "hallway",
)

# Compiled regex: exact match or starts with a known prefix
_ROOM_EXACT_RE = re.compile(
    r"^(?:" + "|".join(re.escape(w) for w in sorted(ROOM_LABEL_EXACT)) + r")$",
    re.IGNORECASE,
)
_ROOM_PREFIX_RE = re.compile(
    r"^(?:" + "|".join(re.escape(p) for p in ROOM_LABEL_PREFIXES) + r")",
    re.IGNORECASE,
)

# Patterns that are NEVER room labels (dimensions, tags, codes, annotations)
_NOT_ROOM_RE = re.compile(
    r"^("                                              # start of string
    r"\d+[.,]?\d*\s*(mm|m|cm|ft|in|')?"               # pure numbers / dimensions
    r"|[A-Z]{1,4}\d{1,4}"                             # finish codes: PT01, WD02, etc.
    r"|[DdWwHhSs]\d{1,3}"                             # door/window tags: D01, W12, etc.
    r"|[Rr][Ll]\s*[\d.+-]+"                           # levels: RL 12.345
    r"|[+\-]?\d+[\d.]*\s*(mm|m|cm|ft)"               # dimensions with unit
    r"|\d+\s*[xX×]\s*\d+"                             # dimensions: 2400x1200
    r"|SHEET\s"                                        # sheet references
    r"|[A-Z]{2,4}-\d{2,}"                             # drawing numbers: A-01.01
    r"|ELEVATION|SECTION|DETAIL|PLAN|NOTES?"          # drawing type annotations
    r"|SCALE\s"                                       # scale text
    r"|[A-Z]\d+[A-Z]?"                               # short codes: A1, B2, etc.
    r")$",
    re.IGNORECASE,
)

# Synthetic labels generated by v145 attach_room_labels() when no semantic
# match is found.  These must NOT be treated as observed room evidence.
_SYNTHETIC_LABEL_RE = re.compile(r"^Room\s+\d+$", re.IGNORECASE)

# Known multi-word room phrases (lowercase, space-separated tokens).
# Used for contiguous phrase reconstruction from separate PDF words.
KNOWN_ROOM_PHRASES: frozenset[str] = frozenset({
    "master bedroom", "guest bedroom", "main bedroom",
    "bed room",  # sometimes split across words
    "living room", "family room", "rumpus room",
    "dining room", "breakfast room", "meals room",
    "walk in robe", "walk-in robe", "built in robe", "built-in robe",
    "walk in closet", "walk-in closet",
    "powder room", "shower room", "laundry room",
    "store room", "storage room", "linen room",
    "plant room", "mech room", "mechanical room",
    "meeting room", "conference room",
    "guest room", "nurse room",
    "sun room", "sunroom",
    "service duct", "service riser",
})

# Maximum horizontal gap (PDF pts) between words to consider them contiguous
# for phrase reconstruction.  ~30 pt ≈ 10 mm on an A4 page.
_MAX_PHRASE_GAP_PT = 30.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _polygon_area_shoelace(pts: Sequence[Tuple[float, float]]) -> float:
    """Signed area via shoelace.  Positive = counter-clockwise."""
    n = len(pts)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return area / 2.0


def _polygon_area_abs(pts: Sequence[Tuple[float, float]]) -> float:
    """Unsigned area of a polygon in whatever coordinate space."""
    return abs(_polygon_area_shoelace(pts))


def _polygon_bbox(pts: Sequence[Tuple[float, float]]) -> Tuple[float, float, float, float]:
    """Return (x_min, y_min, x_max, y_max)."""
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _elongation_ratio(pts: Sequence[Tuple[float, float]]) -> float:
    """Width / height of bounding box.  > 12.0 suggests corridor/wall."""
    x0, y0, x1, y1 = _polygon_bbox(pts)
    w = x1 - x0
    h = y1 - y0
    if h < 1e-9:
        return 999.0
    return w / h if w >= h else h / w


def _point_in_polygon(point: Tuple[float, float], polygon: Sequence[Tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test."""
    x, y = point
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _polygon_centroid(pts: Sequence[Tuple[float, float]]) -> Tuple[float, float]:
    """Approximate centroid (average of vertices)."""
    n = len(pts)
    if n == 0:
        return (0.0, 0.0)
    return (sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n)


def _scale_factor_m_per_pt(scale_info: Dict[str, Any]) -> Optional[float]:
    """Compute metres-per-PDF-point from scale_info.

    Returns None if scale is unknown/unavailable (not 0.0).
    """
    rpm = scale_info.get("real_metres_per_page_mm")
    if rpm is not None and rpm > 0:
        return MM_PER_PT * rpm
    return None


def _polygon_tuples(polygon: Sequence) -> Tuple[Tuple[float, float], ...]:
    """Normalize a polygon to a hashable tuple-of-tuples for identity comparison."""
    return tuple(tuple(p) for p in polygon)


# ---------------------------------------------------------------------------
# BLOCKER 1 — Semantic room label candidate filtering
# ---------------------------------------------------------------------------


def _is_room_label_candidate(word: str) -> bool:
    """Check if a single word is a credible room-label candidate.

    Returns True only for words that could plausibly be room names.
    Rejects pure numbers, dimensions, finish codes, door/window tags,
    drawing annotations, etc.
    """
    w = word.strip()
    if not w or len(w) < 2:
        return False
    # Reject obvious non-room patterns
    if _NOT_ROOM_RE.match(w):
        return False
    # Accept exact room label matches
    if _ROOM_EXACT_RE.match(w):
        return True
    # Accept words starting with known room prefixes (e.g., "BEDROOMS", "KITCHENETTE")
    if _ROOM_PREFIX_RE.match(w):
        return True
    # Handle hyphenated forms: "WALK-IN" → check "WALK" part
    if "-" in w:
        parts = [p.strip() for p in w.split("-") if p.strip()]
        for part in parts:
            if _ROOM_EXACT_RE.match(part) or _ROOM_PREFIX_RE.match(part):
                return True
    return False


def _match_room_phrase(words: Sequence[str]) -> Optional[str]:
    """Try to match a contiguous sequence of words against known room phrases.

    Args:
        words: Sequence of individual word strings (already filtered to
               room-label candidates).

    Returns:
        Matched phrase string (e.g. "MASTER BEDROOM") or None.
    """
    if not words:
        return None

    # Normalize: lowercase, collapse hyphens to spaces for matching
    normalized = []
    for w in words:
        w_lower = w.strip().lower()
        # Expand hyphens: "WALK-IN" → "WALK IN"
        w_expanded = w_lower.replace("-", " ")
        normalized.append(w_expanded)

    # Try longest match first (3+ words, then 2 words)
    for length in range(min(4, len(normalized)), 1, -1):
        for start in range(len(normalized) - length + 1):
            candidate = " ".join(normalized[start:start + length])
            # Collapse multiple spaces
            candidate = " ".join(candidate.split())
            if candidate in KNOWN_ROOM_PHRASES:
                # Return original casing from the input words
                return " ".join(words[start:start + length])

    return None


def filter_room_label_candidates(
    words: List[Dict[str, Any]],
    line_y_tolerance: float = 8.0,
) -> List[Dict[str, Any]]:
    """Filter PDF words to credible room-label candidates, handling multi-word labels.

    Performs proper contiguous phrase reconstruction:
    - Groups words by vertical proximity (same line)
    - Sorts left-to-right within each line
    - Checks consecutive room-label words for known phrases (MASTER BEDROOM,
      WALK IN ROBE, LIVING ROOM, etc.)
    - Uses horizontal gap to prevent merging unrelated words
    - Does NOT concatenate arbitrary words from across an entire line

    Args:
        words: PDF word dicts with 'text' and 'bbox' keys.
        line_y_tolerance: Max vertical distance (PDF pts) to consider words on same line.

    Returns:
        Filtered list of label dicts: [{"label": str, "x": float, "y": float, "confidence": float}].
    """
    if not words:
        return []

    # Group words into lines by vertical proximity
    sorted_words = sorted(words, key=lambda w: (
        float(w.get("bbox", [0, 0, 0, 0])[1]),  # sort by y0
        float(w.get("bbox", [0, 0, 0, 0])[0]),  # then by x0
    ))

    lines: List[List[Dict[str, Any]]] = []
    current_line: List[Dict[str, Any]] = []
    current_y: float = -9999.0

    for word in sorted_words:
        bbox = word.get("bbox", [])
        if len(bbox) < 4:
            continue
        text = str(word.get("text") or "").strip()
        if not text:
            continue
        y0 = float(bbox[1])
        if abs(y0 - current_y) > line_y_tolerance and current_line:
            lines.append(current_line)
            current_line = []
        current_y = y0
        current_line.append(word)
    if current_line:
        lines.append(current_line)

    # For each line, find room label candidates using phrase reconstruction
    candidates: List[Dict[str, Any]] = []
    for line_words in lines:
        # Sort words left to right
        line_words.sort(key=lambda w: float(w.get("bbox", [0, 0, 0, 0])[0]))

        if not line_words:
            continue

        # Mark which words are room-label candidates
        is_room: List[bool] = []
        for word in line_words:
            text = str(word.get("text") or "").strip()
            is_room.append(bool(text and _is_room_label_candidate(text)))

        if not any(is_room):
            continue

        # Try contiguous phrase reconstruction.
        # Iterate over ALL words on the line, but only start phrases from
        # room-label anchor words.  Extend forward through adjacent words
        # (even non-room-label words like "IN" in "WALK IN ROBE") to match
        # known multi-word room phrases.
        used_in_phrase: set[int] = set()  # indices into line_words
        for i in range(len(line_words)):
            if not is_room[i] or i in used_in_phrase:
                continue
            anchor_text = str(line_words[i].get("text") or "").strip()

            # Try 3-word phrases: anchor + next 2 words on the line
            if i + 2 < len(line_words) and (i + 2) not in used_in_phrase:
                w2 = str(line_words[i + 1].get("text") or "").strip()
                w3 = str(line_words[i + 2].get("text") or "").strip()
                phrase_3 = _match_room_phrase([anchor_text, w2, w3])
                if phrase_3:
                    # Check gaps between consecutive words
                    g1 = float(line_words[i + 1].get("bbox", [0,0,0,0])[0]) - float(line_words[i].get("bbox", [0,0,0,0])[2])
                    g2 = float(line_words[i + 2].get("bbox", [0,0,0,0])[0]) - float(line_words[i + 1].get("bbox", [0,0,0,0])[2])
                    if g1 <= _MAX_PHRASE_GAP_PT and g2 <= _MAX_PHRASE_GAP_PT:
                        three = line_words[i:i + 3]
                        bboxes = [w["bbox"] for w in three if len(w.get("bbox", [])) >= 4]
                        if bboxes:
                            x0 = min(float(b[0]) for b in bboxes)
                            y0 = min(float(b[1]) for b in bboxes)
                            x1 = max(float(b[2]) for b in bboxes)
                            y1 = max(float(b[3]) for b in bboxes)
                            candidates.append({
                                "label": phrase_3,
                                "x": (x0 + x1) / 2.0,
                                "y": (y0 + y1) / 2.0,
                                "confidence": 0.95,
                            })
                            used_in_phrase.update(range(i, i + 3))
                            continue

            # Try 2-word phrases: anchor + next word on the line
            if i + 1 < len(line_words) and (i + 1) not in used_in_phrase:
                w2 = str(line_words[i + 1].get("text") or "").strip()
                phrase_2 = _match_room_phrase([anchor_text, w2])
                if phrase_2:
                    g = float(line_words[i + 1].get("bbox", [0,0,0,0])[0]) - float(line_words[i].get("bbox", [0,0,0,0])[2])
                    if g <= _MAX_PHRASE_GAP_PT:
                        two = line_words[i:i + 2]
                        bboxes = [w["bbox"] for w in two if len(w.get("bbox", [])) >= 4]
                        if bboxes:
                            x0 = min(float(b[0]) for b in bboxes)
                            y0 = min(float(b[1]) for b in bboxes)
                            x1 = max(float(b[2]) for b in bboxes)
                            y1 = max(float(b[3]) for b in bboxes)
                            candidates.append({
                                "label": phrase_2,
                                "x": (x0 + x1) / 2.0,
                                "y": (y0 + y1) / 2.0,
                                "confidence": 0.95,
                            })
                            used_in_phrase.update(range(i, i + 2))
                            continue

        # Individual room-label words not consumed by phrases
        for i, word in enumerate(line_words):
            if i in used_in_phrase or not is_room[i]:
                continue
            text = str(word.get("text") or "").strip()
            bbox = word.get("bbox", [])
            if len(bbox) < 4:
                continue
            cx = (float(bbox[0]) + float(bbox[2])) / 2.0
            cy = (float(bbox[1]) + float(bbox[3])) / 2.0
            candidates.append({
                "label": text,
                "x": cx, "y": cy,
                "confidence": 0.85,
            })

    return candidates


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


def calibrate_area_m2(
    polygon_pdf_pts: Sequence[Tuple[float, float]],
    scale_info: Dict[str, Any],
) -> Optional[float]:
    """Convert a polygon's raw PDF-point area to real-world m².

    Returns None if scale is unknown (not 0.0 — zero is ambiguous).
    """
    scale = _scale_factor_m_per_pt(scale_info)
    if scale is None:
        return None
    area_pts2 = _polygon_area_abs(polygon_pdf_pts)
    return round(area_pts2 * scale * scale, 3)


def calibrate_polygon_m(
    polygon_pdf_pts: Sequence[Tuple[float, float]],
    scale_info: Dict[str, Any],
) -> Optional[List[Tuple[float, float]]]:
    """Convert polygon vertices from PDF points to real-world metres.

    Returns None if scale unknown.
    """
    scale = _scale_factor_m_per_pt(scale_info)
    if scale is None:
        return None
    return [(round(x * scale, 4), round(y * scale, 4)) for x, y in polygon_pdf_pts]


# ---------------------------------------------------------------------------
# BLOCKER 2 — Production calibration adapter
# ---------------------------------------------------------------------------


def page_scale_info(page: Dict[str, Any]) -> Dict[str, Any]:
    """Derive the authoritative page calibration from a PlanReader page dict.

    Uses the same ``px_per_m`` stored by PlanReader's registration/scale
    pipeline (auto_scale, floor mapper, vector geometry).  Does NOT create
    a parallel scale representation.

    Single conversion rule:
        real_metres_per_page_mm = render_zoom × 2.834646 / px_per_m

    Derivation:
        From auto_scale: px_per_m = render_zoom × 2834.646 / ratio
        Therefore: ratio = render_zoom × 2834.646 / px_per_m
        And: real_metres_per_page_mm = ratio / 1000
              = render_zoom × 2.834646 / px_per_m

    This yields the same value that ``real_metres_per_page_mm()`` in
    ``pb_planreader_offline.py`` produces for ratio 1:N → N/1000.

    Args:
        page: Page dict from database with px_per_m, render_zoom, scale_text.

    Returns:
        Dict with 'real_metres_per_page_mm', 'scale_text', 'px_per_m', etc.
    """
    px_per_m = float(page.get("px_per_m") or 0)
    render_zoom = float(page.get("render_zoom") or 1.0)
    if render_zoom <= 0:
        render_zoom = 1.0

    scale_text = str(page.get("scale_text") or "").strip()

    if px_per_m > 0:
        # Authoritative: derive real_metres_per_page_mm from stored px_per_m
        # From auto_scale: px_per_m = zoom × 2834.646 / ratio
        # Therefore: ratio = zoom × 2834.646 / px_per_m
        # And: real_metres_per_page_mm = ratio / 1000 = zoom × 2.834646 / px_per_m
        rpm = render_zoom * 2.834646 / px_per_m
        return {
            "real_metres_per_page_mm": rpm,
            "px_per_m": px_per_m,
            "render_zoom": render_zoom,
            "scale_text": scale_text,
            "source": "page.px_per_m",
        }

    # No px_per_m available — unknown scale
    return {
        "real_metres_per_page_mm": None,
        "px_per_m": 0.0,
        "render_zoom": render_zoom,
        "scale_text": scale_text,
        "source": "unknown",
    }


def vector_analysis_scale_info(app: Any, page_id: int) -> Optional[Dict[str, Any]]:
    """Try to get calibration from stored v130 vector analysis.

    Uses the same conversion rule as page_scale_info().

    Returns None if not available (caller should fall back to page_scale_info).
    """
    try:
        setting_key = f"vector_analysis_{page_id}"
        workspace_id = 0  # caller must provide or we skip
        raw = getattr(app, "workspace_setting", lambda *a: "")(workspace_id, setting_key, "")
        if not raw:
            return None
        import json
        analysis = json.loads(raw)
        scale = analysis.get("scale") or {}
        px_per_m = float(scale.get("px_per_m") or 0)
        if px_per_m <= 0:
            return None
        # Same conversion rule: rpm = zoom × 2.834646 / px_per_m
        # Vector analysis px_per_m already has render_zoom baked in,
        # so treat zoom=1.0 here.
        rpm = 2.834646 / px_per_m
        return {
            "real_metres_per_page_mm": rpm,
            "px_per_m": px_per_m,
            "render_zoom": 1.0,
            "scale_text": str(scale.get("label") or ""),
            "source": "vector_analysis",
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# False-positive filtering
# ---------------------------------------------------------------------------


def _bbox_in_title_block_zone(
    bbox: Tuple[float, float, float, float],
    page_width_pt: float,
    page_height_pt: float,
) -> bool:
    """True if bbox is primarily in the title-block zone (bottom-right)."""
    x0, y0, x1, y1 = bbox
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    return (
        cy > page_height_pt * TITLE_BLOCK_Y_MIN
        and cx > page_width_pt * TITLE_BLOCK_X_MIN
    )


def _count_other_centroids_inside_candidate(
    candidate: Sequence[Tuple[float, float]],
    other_polygons: Sequence[Sequence[Tuple[float, float]]],
    candidate_key: Tuple[Tuple[float, float], ...],
) -> int:
    """Count how many OTHER polygons' centroids lie inside the candidate.

    This is the correct containment direction for detecting building outlines:
    an outer outline CONTAINS many inner room centroids.
    """
    count = 0
    for other in other_polygons:
        other_key = _polygon_tuples(other)
        if other_key == candidate_key:
            continue  # skip self
        centroid = _polygon_centroid(other)
        if _point_in_polygon(centroid, candidate):
            count += 1
    return count


@dataclass
class FilterResult:
    """Result of face filtering."""
    is_room: bool
    reason: str = ""
    area_m2: Optional[float] = None   # None = uncalibrated, NOT 0.0
    area_page_pts2: float = 0.0       # raw page-space area (always available)
    polygon_m: Optional[List[Tuple[float, float]]] = None
    confidence_adjustment: float = 0.0  # negative = penalty, positive = boost
    has_voids: bool = False


def filter_face(
    polygon_pdf_pts: Sequence[Tuple[float, float]],
    scale_info: Dict[str, Any],
    page_width_pt: float,
    page_height_pt: float,
    all_polygons: Optional[Sequence[Sequence[Tuple[float, float]]]] = None,
    label: str = "",
    polygon_index: int = -1,
) -> FilterResult:
    """Determine whether a polygon is a plausible room face.

    Area below thresholds is treated as confidence evidence, not unconditional
    rejection — a small polygon with a strong room label may be retained
    provisionally.
    """
    # 1. Minimum vertex count
    if len(polygon_pdf_pts) < 3:
        return FilterResult(is_room=False, reason="degenerate (< 3 vertices)")

    # 2. Raw page-space area (always available)
    area_page_pts2 = _polygon_area_abs(polygon_pdf_pts)

    # 3. Calibrate area
    area_m2 = calibrate_area_m2(polygon_pdf_pts, scale_info)
    if area_m2 is None:
        return FilterResult(
            is_room=False,
            reason="un_calibrated (unknown scale)",
            area_page_pts2=area_page_pts2,
        )

    # 4. Hard minimum: below this, reject regardless of label
    if area_m2 < HARD_MIN_ROOM_AREA_M2:
        return FilterResult(
            is_room=False,
            reason=f"too_small ({area_m2:.2f} m² < {HARD_MIN_ROOM_AREA_M2} m² hard minimum)",
            area_m2=area_m2,
            area_page_pts2=area_page_pts2,
        )

    # 5. Maximum area: building outline / drawing border
    if area_m2 > MAX_ROOM_AREA_M2:
        return FilterResult(
            is_room=False,
            reason=f"too_large ({area_m2:.2f} m² > {MAX_ROOM_AREA_M2} m² — likely building outline)",
            area_m2=area_m2,
            area_page_pts2=area_page_pts2,
        )

    # 6. Title-block zone rejection
    bbox = _polygon_bbox(polygon_pdf_pts)
    if _bbox_in_title_block_zone(bbox, page_width_pt, page_height_pt):
        return FilterResult(
            is_room=False,
            reason="in_title_block_zone",
            area_m2=area_m2,
            area_page_pts2=area_page_pts2,
        )

    # 7. Page coverage rejection: polygon covering > PAGE_COVERAGE_MAX of page
    #    is a drawing border or building outline, not a room.
    page_area_pt2 = page_width_pt * page_height_pt
    if page_area_pt2 > 0:
        coverage = area_page_pts2 / page_area_pt2
        if coverage > PAGE_COVERAGE_MAX:
            return FilterResult(
                is_room=False,
                reason=f"covers_page ({coverage:.0%} of page — likely border/outline)",
                area_m2=area_m2,
                area_page_pts2=area_page_pts2,
            )

    # 8. Building outline rejection: candidate CONTAINS many other centroids
    confidence_adj = 0.0
    if all_polygons and len(all_polygons) > 1:
        candidate_key = _polygon_tuples(polygon_pdf_pts)
        contained = _count_other_centroids_inside_candidate(
            polygon_pdf_pts, all_polygons, candidate_key,
        )
        if contained > len(all_polygons) * CONTAINMENT_OUTLINE_THRESHOLD:
            return FilterResult(
                is_room=False,
                reason=f"building_outline (contains {contained}/{len(all_polygons)} other centroids)",
                area_m2=area_m2,
                area_page_pts2=area_page_pts2,
            )

    # 9. Soft area penalty: below SOFT_MIN, reduce confidence but don't reject
    has_label = bool(label and label.strip())
    if area_m2 < SOFT_MIN_ROOM_AREA_M2:
        if has_label:
            # Small polygon with room label → provisional
            confidence_adj -= 0.15
        else:
            # Small polygon without label → reject (likely furniture/joinery)
            return FilterResult(
                is_room=False,
                reason=f"small_unlabeled ({area_m2:.2f} m² < {SOFT_MIN_ROOM_AREA_M2} m², no room label)",
                area_m2=area_m2,
                area_page_pts2=area_page_pts2,
            )

    # 10. Elongation: high ratio needs label evidence
    elong = _elongation_ratio(polygon_pdf_pts)
    if elong > HIGH_ELONGATION:
        if has_label:
            # Long corridor with label → provisional
            confidence_adj -= 0.10
        else:
            # Elongated unlabeled strip → reject
            return FilterResult(
                is_room=False,
                reason=f"elongated_unlabeled (ratio {elong:.1f}:1 > {HIGH_ELONGATION}:1, no label)",
                area_m2=area_m2,
                area_page_pts2=area_page_pts2,
            )

    # 11. Void/hole detection: if this face contains other valid faces
    #     inside it, mark as provisional (area may include voids)
    has_voids = False
    if all_polygons and len(all_polygons) > 1:
        candidate_key = _polygon_tuples(polygon_pdf_pts)
        for other in all_polygons:
            other_key = _polygon_tuples(other)
            if other_key == candidate_key:
                continue
            if len(other) < 3:
                continue
            other_centroid = _polygon_centroid(other)
            if _point_in_polygon(other_centroid, polygon_pdf_pts):
                other_area = _polygon_area_abs(other)
                # Only count as void if it's a meaningful fraction
                if other_area > area_page_pts2 * 0.01:
                    has_voids = True
                    break

    if has_voids:
        confidence_adj -= 0.20  # significant penalty: area includes voids

    # 12. Calibrated polygon in metres
    polygon_m = calibrate_polygon_m(polygon_pdf_pts, scale_info)

    return FilterResult(
        is_room=True,
        area_m2=area_m2,
        area_page_pts2=area_page_pts2,
        polygon_m=polygon_m,
        confidence_adjustment=confidence_adj,
        has_voids=has_voids,
    )


# ---------------------------------------------------------------------------
# Room face extraction + calibration
# ---------------------------------------------------------------------------


@dataclass
class RoomFace:
    """A calibrated room face ready for take-off row production."""
    room_ref: str
    label: str
    polygon_pdf_pts: List[Tuple[float, float]]
    polygon_m: Optional[List[Tuple[float, float]]]
    floor_area_m2: Optional[float]       # None = uncalibrated
    area_page_pts2: float                # raw page-space area
    perimeter_m: Optional[float]
    geometry_confidence: float
    evidence: List[str]
    source_page: int = 0
    drawing_number: str = ""
    scale_source: str = ""               # authoritative scale text
    calibration_confidence: float = 0.0
    has_voids: bool = False
    status: str = "Measured"             # Measured / Provisional measured / Review


def _perimeter_m(polygon_m: Optional[List[Tuple[float, float]]]) -> Optional[float]:
    """Perimeter of a polygon in metres.  None if uncalibrated."""
    if not polygon_m or len(polygon_m) < 2:
        return None
    total = 0.0
    for i in range(len(polygon_m)):
        x1, y1 = polygon_m[i]
        x2, y2 = polygon_m[(i + 1) % len(polygon_m)]
        total += ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    return round(total, 3)


def _authoritative_scale_text(scale_info: Dict[str, Any]) -> str:
    """Extract the authoritative human-readable scale text.

    Uses scale_text if available, falls back to constructing from known fields.
    Never derives display labels from inverse/internal factors.
    """
    text = str(scale_info.get("scale_text") or "").strip()
    if text:
        return text

    ratio = scale_info.get("scale_ratio")
    if isinstance(ratio, (int, float)) and ratio > 0 and ratio == int(ratio):
        return f"1:{int(ratio)}"

    metric = scale_info.get("metric_scale")
    if metric:
        return str(metric)

    return "unknown"


def extract_and_calibrate_rooms(
    segments: Sequence[Dict[str, Any]],
    scale_info: Dict[str, Any],
    page_width_pt: float = 595.0,
    page_height_pt: float = 842.0,
    page_no: int = 0,
    drawing_number: str = "",
    words: Optional[List[Dict[str, Any]]] = None,
    min_area: float = 0.05,
) -> List[RoomFace]:
    """Extract room faces from vector linework, calibrate to m², filter false positives.

    Args:
        segments: Line segments as dicts with x1,y1,x2,y2 keys (PDF points).
        scale_info: Priority 1 scale calibration dict.
        page_width_pt: Page width in PDF points.
        page_height_pt: Page height in PDF points.
        page_no: PlanReader page number.
        drawing_number: Drawing reference.
        words: PDF word positions [{text, bbox}, ...].  FILTERED to credible
               room-label candidates before label association.
        min_area: Minimum face area in PDF points² for v145 extraction.

    Returns:
        List of calibrated RoomFace objects (false positives already removed).
    """
    # Import v145 face extraction
    try:
        from pb_accuracy_v13_engines_v145 import (
            extract_planar_faces,
            attach_room_labels,
        )
    except ImportError:
        return []

    # Convert dict segments to v145 tuple format: [(start, end), ...]
    v145_segments = []
    for seg in segments:
        x1 = seg.get("x1", 0.0)
        y1 = seg.get("y1", 0.0)
        x2 = seg.get("x2", 0.0)
        y2 = seg.get("y2", 0.0)
        if abs(x2 - x1) < 0.5 and abs(y2 - y1) < 0.5:
            continue
        v145_segments.append(((float(x1), float(y1)), (float(x2), float(y2))))

    if not v145_segments:
        return []

    # Extract raw faces from vector linework
    raw_faces = extract_planar_faces(v145_segments, min_area=min_area)
    if not raw_faces:
        return []

    # BLOCKER 1: Filter PDF words to credible room-label candidates
    # BEFORE passing to attach_room_labels.
    label_dicts = filter_room_label_candidates(words or [])

    # Build set of credible semantic label texts for synthetic detection
    credible_labels: set[str] = {
        str(d.get("label", "")).strip().lower()
        for d in label_dicts
        if d.get("label")
    }

    # Attach labels via point-in-polygon spatial association
    labelled = attach_room_labels(raw_faces, label_dicts)

    # BLOCKER 1: Strip synthetic "Room N" labels from v145 fallback.
    # v145 assigns "Room 1", "Room 2" etc. when no semantic label matches.
    # These must NOT be treated as observed room evidence — they are
    # generated references, not text found on the drawing.
    for labelled_room in labelled:
        raw_label = str(labelled_room.get("label", "")).strip()
        if (
            _SYNTHETIC_LABEL_RE.match(raw_label)
            or raw_label.lower() not in credible_labels
        ):
            # Not a genuine semantic label — clear it
            labelled_room["label"] = ""
            labelled_room["label_is_semantic"] = False
        else:
            labelled_room["label_is_semantic"] = True

    # Authoritative scale text
    scale_source = _authoritative_scale_text(scale_info)

    cal_scale = _scale_factor_m_per_pt(scale_info)
    calibration_conf = 0.95 if cal_scale is not None else 0.0

    # Pre-compute polygon keys for containment analysis
    all_polygon_tuples = [
        _polygon_tuples(r.get("polygon", [])) for r in labelled
    ]

    # Filter and calibrate each face
    room_faces: List[RoomFace] = []
    for idx, labelled_room in enumerate(labelled):
        polygon = [tuple(p) for p in labelled_room.get("polygon", [])]
        if len(polygon) < 3:
            continue

        label = labelled_room.get("label", "")

        result = filter_face(
            polygon,
            scale_info,
            page_width_pt,
            page_height_pt,
            all_polygons=[list(r.get("polygon", [])) for r in labelled],
            label=label,
            polygon_index=idx,
        )

        if not result.is_room:
            continue

        # Extract semantic label evidence BEFORE status determination
        is_semantic = labelled_room.get("label_is_semantic", False)

        # Compute effective confidence
        base_geom_conf = labelled_room.get("geometry_confidence", 0.9)
        effective_conf = max(0.0, min(1.0, base_geom_conf + result.confidence_adjustment))

        # Determine status based on confidence, voids, and semantic evidence
        # Unlabeled faces MUST NOT be "Measured" — geometry alone is insufficient
        if result.has_voids:
            status = "Review"
        elif is_semantic and effective_conf >= 0.85 and calibration_conf >= 0.9:
            status = "Measured"
        else:
            status = "Provisional measured"

        evidence = list(labelled_room.get("evidence") or [])
        if label and is_semantic:
            evidence.append(f"Room label '{label}' spatially associated via point-in-polygon")
        elif label:
            evidence.append(f"Label '{label}' assigned by spatial association (not verified semantic)")
        else:
            evidence.append("Closed calibrated face detected; no semantic room label found")
        if result.has_voids:
            evidence.append("Polygon contains internal voids — area may be overstated")

        room_faces.append(RoomFace(
            room_ref=labelled_room.get("room_ref", ""),
            label=label,
            polygon_pdf_pts=list(polygon),
            polygon_m=result.polygon_m,
            floor_area_m2=result.area_m2,
            area_page_pts2=result.area_page_pts2,
            perimeter_m=_perimeter_m(result.polygon_m),
            geometry_confidence=effective_conf,
            evidence=evidence,
            source_page=page_no,
            drawing_number=drawing_number,
            scale_source=scale_source,
            calibration_confidence=calibration_conf,
            has_voids=result.has_voids,
            status=status,
        ))

    return room_faces


# ---------------------------------------------------------------------------
# Take-off row production
# ---------------------------------------------------------------------------


def rooms_to_takeoff_rows(
    rooms: List[RoomFace],
    workspace_id: int,
    include_ceiling: bool = False,
) -> List[Dict[str, Any]]:
    """Convert calibrated RoomFace objects into PlanReader take-off rows.

    Args:
        rooms: Calibrated room faces from extract_and_calibrate_rooms.
        workspace_id: PlanReader workspace ID.
        include_ceiling: If True, add ceiling rows (same area as floor).

    Returns:
        List of take-off row dicts matching PlanReader schema.
    """
    rows: List[Dict[str, Any]] = []

    for room in rooms:
        # Floor area row
        floor_row = {
            "workspace_id": workspace_id,
            "section": "Internal",
            "element": "Floor area",
            "location": room.label or room.room_ref,
            "substrate": "Other",
            "unit": "m²",
            "quantity": room.floor_area_m2,  # None if uncalibrated
            "quantity_status": room.status,
            "source_page": room.source_page,
            "source_reference": (
                f"{SOURCE_PREFIX} · {room.drawing_number} · page:{room.source_page}"
                if room.drawing_number
                else f"{SOURCE_PREFIX} · page:{room.source_page}"
            ),
            "confidence": (
                "Derived" if room.calibration_confidence >= 0.9
                else "Provisional"
            ),
            "notes": (
                f"Room area from calibrated vector face extraction. "
                f"Scale: {room.scale_source}. "
                f"Geometry confidence: {room.geometry_confidence:.0%}. "
                f"Perimeter: {room.perimeter_m:.2f} m."
                if room.perimeter_m is not None
                else f"Room area from vector face extraction (uncalibrated). "
                     f"Scale: {room.scale_source}."
            ) + (
                " No semantic room label found — retained from geometry only."
                if not room.label
                else ""
            ),
            "row_role": "floor_area",
            "geometry_confidence": room.geometry_confidence,
            "calibration_confidence": room.calibration_confidence,
            "evidence": "; ".join(room.evidence) if room.evidence else "",
            "has_voids": room.has_voids,
        }
        rows.append(floor_row)

        # Ceiling row (optional — same geometry, different finish context)
        if include_ceiling:
            ceiling_row = dict(floor_row)
            ceiling_row["element"] = "Ceiling area"
            ceiling_row["row_role"] = ""
            ceiling_row["notes"] = (
                f"Ceiling area from room face geometry (same as floor). "
                f"Review: ceiling may differ due to voids, RCP changes, or scope."
            )
            rows.append(ceiling_row)

    return rows


# ---------------------------------------------------------------------------
# Production integration: extract rooms from a stored PDF page
# ---------------------------------------------------------------------------


def extract_room_faces_from_page(
    app: Any,
    page: Dict[str, Any],
) -> List[RoomFace]:
    """Extract room faces from a stored PDF page using native vector geometry.

    This is the production integration point.  It:
    1. Opens the original PDF
    2. Extracts vector segments
    3. Extracts text positions (filtered to room labels)
    4. Gets page calibration via page_scale_info()
    5. Runs v145 face extraction
    6. Calibrates and filters

    Args:
        app: PlanReader app instance (needs fitz, lquery).
        page: Page dict from database (id, page_no, page_label, page_type, px_per_m, etc.)

    Returns:
        List of calibrated RoomFace objects.
    """
    fitz = getattr(app, "fitz", None)
    if fitz is None:
        return []

    # Get document path
    doc_id = int(page.get("document_id") or 0)
    if doc_id <= 0:
        return []
    docs = app.lquery("SELECT path FROM documents WHERE id=?", (doc_id,))
    if not docs:
        return []
    pdf_path = Path(str(docs[0].get("path") or ""))
    if pdf_path.suffix.lower() != ".pdf" or not pdf_path.is_file():
        return []

    # Open PDF and extract page
    pdf = fitz.open(pdf_path)
    try:
        page_no_0 = int(page.get("page_no") or 1) - 1
        if page_no_0 < 0 or page_no_0 >= len(pdf):
            return []
        pdf_page = pdf.load_page(page_no_0)
        page_width_pt = float(pdf_page.rect.width)
        page_height_pt = float(pdf_page.rect.height)

        # Extract vector segments
        drawings = pdf_page.get_drawings() or []
        segments = []
        for draw_index, drawing in enumerate(drawings):
            for item_index, item in enumerate(drawing.get("items", []) if isinstance(drawing, dict) else []):
                if not item:
                    continue
                kind = str(item[0])
                if kind == "l" and len(item) >= 3:
                    p1, p2 = item[1], item[2]
                    try:
                        x1, y1, x2, y2 = float(p1.x), float(p1.y), float(p2.x), float(p2.y)
                    except Exception:
                        try:
                            x1, y1, x2, y2 = float(p1[0]), float(p1[1]), float(p2[0]), float(p2[1])
                        except Exception:
                            continue
                    if abs(x2 - x1) < 0.5 and abs(y2 - y1) < 0.5:
                        continue
                    segments.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2})
                elif kind == "re" and len(item) >= 2:
                    rect = item[1]
                    try:
                        rx0, ry0, rx1, ry1 = map(float, (rect.x0, rect.y0, rect.x1, rect.y1))
                    except Exception:
                        continue
                    pts = [(rx0, ry0), (rx1, ry0), (rx1, ry1), (rx0, ry1)]
                    for edge in range(4):
                        a, b = pts[edge], pts[(edge + 1) % 4]
                        segments.append({"x1": a[0], "y1": a[1], "x2": b[0], "y2": b[1]})

        # Extract ALL text positions (for label filtering — filter_room_label_candidates
        # handles the semantic filtering internally)
        words_raw = pdf_page.get_text("words") or []
        words = []
        for word in words_raw:
            if len(word) < 5:
                continue
            try:
                x0, y0, x1, y1 = map(float, word[:4])
            except Exception:
                continue
            text = str(word[4]).strip()
            if text:
                words.append({"text": text, "bbox": [x0, y0, x1, y1]})
    finally:
        pdf.close()

    if not segments:
        return []

    # BLOCKER 2: Use authoritative page calibration
    scale_info = page_scale_info(page)

    # Get drawing number
    drawing_number = str(page.get("page_label") or "")

    # Run extraction
    return extract_and_calibrate_rooms(
        segments=segments,
        scale_info=scale_info,
        page_width_pt=page_width_pt,
        page_height_pt=page_height_pt,
        page_no=int(page.get("page_no") or 0),
        drawing_number=drawing_number,
        words=words,
    )


# ---------------------------------------------------------------------------
# BLOCKER 3 — Production wiring
# ---------------------------------------------------------------------------


def apply(app: Any) -> None:
    """Wire room face extraction into the PlanReader production pipeline.

    Called from pb_planreader_v126_app.py startup chain, after
    apply_vector_geometry_v130 (which registers extract_native_page etc.).
    """
    if getattr(app, "_pb_room_face_takeoff_applied", False):
        return
    app._pb_room_face_takeoff_applied = True

    # Store module reference for testing
    import pb_room_face_takeoff as _mod
    app.room_face_takeoff = _mod

    # Wire into _build_unit_rows to add room-face floor area rows.
    # This follows the same monkey-patch pattern used by:
    #   pb_unit_floor_area_gate_v1221
    #   pb_selected_evidence_floor_v1226
    #   pb_context_floorarea_v1224
    try:
        import pb_auto_geometry_v1219 as auto
        original_build = auto._build_unit_rows

        def _build_unit_rows_with_room_faces(
            app_obj: Any,
            workspace_id: int,
            pages: Sequence[Dict[str, Any]],
        ):
            # Run original builder (which may itself be wrapped by other modules)
            rows, summary = original_build(app_obj, workspace_id, pages)

            # Add room face rows for floor plan pages
            for page in pages:
                page_type = str(page.get("page_type") or "").lower()
                if "floor" not in page_type and "partition" not in page_type:
                    continue
                try:
                    room_faces = extract_room_faces_from_page(app_obj, page)
                    room_rows = rooms_to_takeoff_rows(room_faces, workspace_id)
                    for row in room_rows:
                        rows.append((
                            row.get("workspace_id"),
                            row.get("section"),
                            row.get("element"),
                            row.get("location"),
                            row.get("substrate"),
                            row.get("unit"),
                            row.get("quantity"),
                            row.get("quantity_status"),
                            row.get("source_page"),
                            row.get("source_reference"),
                            row.get("confidence"),
                            row.get("notes"),
                            row.get("row_role"),
                        ))
                        summary.append({
                            "label": row.get("location"),
                            "area_m2": row.get("quantity"),
                            "confidence": row.get("confidence"),
                            "source": row.get("source_reference"),
                            "page_id": int(page.get("id") or 0),
                            "page_label": str(page.get("page_label") or ""),
                            "quantity_status": row.get("quantity_status"),
                            "room_face": True,
                        })
                except Exception:
                    continue  # don't break production if room face extraction fails

            return rows, summary

        auto._build_unit_rows = _build_unit_rows_with_room_faces
    except Exception:
        pass  # If auto_geometry not available, skip wiring


# ---------------------------------------------------------------------------
# Convenience: summary
# ---------------------------------------------------------------------------


def room_face_summary(rooms: List[RoomFace]) -> Dict[str, Any]:
    """Aggregate room face statistics."""
    calibrated = [r for r in rooms if r.floor_area_m2 is not None]
    total_floor = sum(r.floor_area_m2 for r in calibrated)
    total_perimeter = sum(r.perimeter_m for r in calibrated if r.perimeter_m is not None)
    return {
        "room_count": len(rooms),
        "calibrated_count": len(calibrated),
        "total_floor_area_m2": round(total_floor, 3) if calibrated else None,
        "total_perimeter_m": round(total_perimeter, 3) if calibrated else None,
        "scale_sources": list({r.scale_source for r in rooms}),
        "min_confidence": round(min(
            (r.calibration_confidence for r in rooms), default=0.0
        ), 3),
        "review_count": sum(
            1 for r in rooms
            if r.status == "Review"
            or r.calibration_confidence < 0.9
            or r.geometry_confidence < 0.9
        ),
        "void_count": sum(1 for r in rooms if r.has_voids),
    }
