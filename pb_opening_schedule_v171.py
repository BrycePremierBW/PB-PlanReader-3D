"""PlanReader v1.7.1 — Door/window schedule table parsing.

Parses tab-separated rows (from _word_rows() or equivalent) to extract
schedule entries: type mark, width, height, description, count.

Safety rules:
  - Schedule rows describe opening TYPES, not physical wall instances.
  - A schedule entry MUST NOT create a new OpeningEvidence record by
    itself — it can only enrich an existing geometric instance.
  - Enrichment requires a matching non-empty type_mark on both the
    schedule entry and the existing evidence.
  - Width + height + basis + dimension_confidence + dimension_source
    are one atomic bundle (inherited from B0 safety contract).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pb_opening_evidence_v170 import (
    DIMENSION_BASIS_UNKNOWN,
    DIMENSION_BASIS_ROUGH_OPENING,
    DEDUCTION_REVIEW,
    NON_INSTANCE_SOURCES,
    OpeningEvidence,
    enriches_by_type,
    merge_opening_evidence,
)

VERSION = "1.7.1"

# ---------------------------------------------------------------------------
# Schedule entry — one row from a door/window schedule table
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ScheduleEntry:
    """One row from a door/window schedule table.

    Represents an opening TYPE (D01, W01), not a physical instance.
    The same type mark can appear many times on the plan.
    """
    type_mark: str              # "D01", "W01"
    width_mm: Optional[int] = None
    height_mm: Optional[int] = None
    description: str = ""
    count: int = 1
    page_no: int = 0
    bbox: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    parse_source: str = ""      # "header_dims", "header_separate", "heuristic"
    dimension_basis: str = ""    # "rough_opening", "frame", "leaf", "clear_opening", ""
    basis_source: str = ""       # provenance for dimension_basis (heading text)


# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------
_HEADER_MARK_KEYWORDS = frozenset({
    "mark", "marks", "type", "code", "no", "num", "number",
    "door", "window", "tag", "ref", "label", "item",
})

_HEADER_WIDTH_KEYWORDS = frozenset({
    "width", "wide", "wdth",
})

_HEADER_HEIGHT_KEYWORDS = frozenset({
    "height", "ht", "hgt", "high",
})

_HEADER_DIMS_KEYWORDS = frozenset({
    "size", "dims", "dim", "dimensions", "w×h", "w*x", "w x h",
    "width×height", "width*height", "width x height",
    "w/h", "w×h", "wxh",
})

_HEADER_COUNT_KEYWORDS = frozenset({
    "qty", "quantity", "count", "nos", "no.", "number",
})

_HEADER_DESC_KEYWORDS = frozenset({
    "description", "desc", "notes", "note", "detail", "remarks",
    "type_desc", "material", "finish", "spec",
})

# ---------------------------------------------------------------------------
# Dimension basis inference from schedule heading text
# ---------------------------------------------------------------------------
# Only explicit heading labels can establish basis.  Generic "Width/Height"
# must remain unknown — knowing dimensions is not the same as knowing
# they represent the wall void.

_RO_HEADING_KEYWORDS = re.compile(
    r"\b(rough\s*opening|ro\s*size|ro\s*wdth|ro\s*ht|structural\s*opening"
    r"|rough\s*opening\s*(width|height|wdth|ht|hgt)"
    r"|r\.?o\.?\s*(width|height|wdth|ht|hgt))\b",
    re.IGNORECASE,
)
_FRAME_HEADING_KEYWORDS = re.compile(
    r"\b(frame\s*(size|width|height|wdth|ht|hgt)|fr\s*size)\b",
    re.IGNORECASE,
)
_LEAF_HEADING_KEYWORDS = re.compile(
    r"\b(leaf\s*(size|width|height|wdth|ht|hgt))\b",
    re.IGNORECASE,
)
_CLEAR_HEADING_KEYWORDS = re.compile(
    r"\b(clear\s*(opening|size|width|height|wdth|ht|hgt))\b",
    re.IGNORECASE,
)


def _infer_column_basis(heading_text: str) -> Tuple[str, str]:
    """Infer dimension basis from a single column heading.

    Returns (dimension_basis, basis_source) where:
      - dimension_basis is "rough_opening", "frame", "leaf",
        "clear_opening", or "" (unknown).
      - basis_source is the matched heading text for provenance.

    Only explicit heading labels establish basis.  Generic headings like
    "Width", "Height", "Size" return ("", "") because the parser cannot
    determine what physical measurement those dimensions represent.
    """
    text = heading_text.lower().strip()
    if _RO_HEADING_KEYWORDS.search(text):
        m = _RO_HEADING_KEYWORDS.search(text)
        return "rough_opening", m.group(0) if m else text
    if _FRAME_HEADING_KEYWORDS.search(text):
        m = _FRAME_HEADING_KEYWORDS.search(text)
        return "frame", m.group(0) if m else text
    if _LEAF_HEADING_KEYWORDS.search(text):
        m = _LEAF_HEADING_KEYWORDS.search(text)
        return "leaf", m.group(0) if m else text
    if _CLEAR_HEADING_KEYWORDS.search(text):
        m = _CLEAR_HEADING_KEYWORDS.search(text)
        return "clear_opening", m.group(0) if m else text
    return "", ""


def _resolve_dimension_basis(
    width_basis: str, width_source: str,
    height_basis: str, height_source: str,
    dims_basis: str, dims_source: str,
) -> Tuple[str, str]:
    """Resolve the final dimension basis from individual column bases.

    Rules:
      - If a combined Size/dims column was used, its basis applies.
      - If both width and height were used and agree on basis → use it.
      - If they disagree → unknown (mixed measurement bundle).
      - If only one was used → use that one's basis.
      - If none have basis → unknown.
    """
    # Combined Size/dims column takes precedence
    if dims_basis:
        return dims_basis, dims_source
    # Both width and height present
    if width_basis and height_basis:
        if width_basis == height_basis:
            return width_basis, width_source
        # Disagreeing bases → unsafe, treat as unknown
        return "", ""
    # Only one present
    if width_basis:
        return width_basis, width_source
    if height_basis:
        return height_basis, height_source
    return "", ""

# Patterns for mark extraction
_MARK_PATTERNS = [
    re.compile(r"\b([DW](?:D|W)?\d{1,4})\b"),         # D01, W01, WD01, DW01
    re.compile(r"\b(I{1,3}|IV|V|VI{0,3}|IX|X{1,3}|XL|L|XC|C{1,3})\b"),  # Roman
]

# Patterns for compound dimension extraction (from full row text)
# Split: x/× is strong (explicit dimension separator), / is weak (could be rating)
_DIMENSION_PATTERNS_X = [
    re.compile(r"(\d{1,5})\s*[×xX]\s*(\d{1,5})"),          # 820x2040, 820 × 2040
    re.compile(r"(\d{1,5})\s*[-–—]\s*(\d{1,5})"),           # 820-2040, 820–2040
    re.compile(r"(\d{1,5})\s*[bhBH]\s*(\d{1,5})"),          # 820b2040, 820H2040
]
_DIMENSION_PATTERNS_SLASH = [
    re.compile(r"(\d{1,5})\s*/\s*(\d{1,5})"),               # 820/2040 (weak — may be rating)
]

# Single-number pattern for fallback dimension extraction
_SINGLE_NUM_RE = re.compile(r"\b(\d{2,5})\b")

# Scale patterns for mm→m conversion
_MM_RE = re.compile(r"(\d{2,5})\s*mm", re.IGNORECASE)
_M_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3})?)\s*m\b", re.IGNORECASE)

# Patterns that indicate rating/fire/acoustic data (NOT dimensions)
_RATING_KEYWORDS = re.compile(
    r"\b(frl|fire|rating|acoustic|stc|rw|db|hr|sound|insul)\b",
    re.IGNORECASE,
)

# Minimum plausible opening dimension in mm
_MIN_PLAUSIBLE_MM = 200
# Maximum plausible opening dimension in mm
_MAX_PLAUSIBLE_MM = 6000


# ---------------------------------------------------------------------------
# Dimension parsing
# ---------------------------------------------------------------------------
def _is_plausible_dimension(w: Optional[int], h: Optional[int]) -> bool:
    """True if both dimensions are within plausible opening size ranges."""
    if w is None and h is None:
        return False
    if w is not None and (w < _MIN_PLAUSIBLE_MM or w > _MAX_PLAUSIBLE_MM):
        return False
    if h is not None and (h < _MIN_PLAUSIBLE_MM or h > _MAX_PLAUSIBLE_MM):
        return False
    return True


def _has_rating_keywords(text: str) -> bool:
    """True if text contains fire-rating, acoustic, or similar non-dimension numbers."""
    return bool(_RATING_KEYWORDS.search(text))


def parse_single_dimension(text: str) -> Optional[int]:
    """Parse a single dimension value from a cell, handling units.

    Handles: "820", "820mm", "2040 mm", "0.82m", "1m", "2m"
    Returns value in mm, or None if not parseable or implausible.
    """
    if not text or not text.strip():
        return None
    t = text.strip()
    val: Optional[int] = None

    # Try mm suffix: "820mm", "2040 mm"
    m = _MM_RE.search(t)
    if m:
        val = int(m.group(1))
    else:
        # Try m suffix: "0.82m", "1m", "2m"
        m = _M_RE.search(t)
        if m:
            val = round(float(m.group(1)) * 1000)
        else:
            # Bare number: assume mm
            t_clean = t.replace(",", "").strip()
            try:
                val = int(float(t_clean))
            except (ValueError, TypeError):
                return None

    # Plausibility check: reject implausible opening sizes
    if val is not None and not _is_plausible_dimension(val, None):
        return None
    return val


def parse_dimension(text: str, allow_slash: bool = True) -> Tuple[Optional[int], Optional[int]]:
    """Parse width × height from a dimension cell or row fragment.

    Handles: "820x2040", "820 × 2040", "820/2040", "820-2040",
             "820mm x 2040mm", "0.82m x 2.04m", standalone "2040"

    When allow_slash=False, slash-separated values (820/2040) are not
    treated as dimensions — useful when rating context (FRL, STC) may
    produce slash-separated non-dimension numbers.

    Returns (width_mm, height_mm).  Either may be None if not parseable.
    """
    if not text or not text.strip():
        return None, None

    t = text.strip()

    # Try strong compound patterns FIRST: x, ×, -, –, b, H
    for pat in _DIMENSION_PATTERNS_X:
        for m in pat.finditer(t):
            a, b = int(m.group(1)), int(m.group(2))
            if _is_plausible_dimension(a, b):
                return a, b

    # Try slash patterns only when allowed (no rating context or in a
    # dedicated dimensions column)
    if allow_slash:
        for pat in _DIMENSION_PATTERNS_SLASH:
            for m in pat.finditer(t):
                a, b = int(m.group(1)), int(m.group(2))
                if _is_plausible_dimension(a, b):
                    return a, b

    # Try mm suffix: "820mm x 2040mm"
    mm_match = _MM_RE.findall(t)
    if len(mm_match) >= 2:
        w, h = int(mm_match[0]), int(mm_match[1])
        if _is_plausible_dimension(w, h):
            return w, h
    if len(mm_match) == 1:
        val = int(mm_match[0])
        if _MIN_PLAUSIBLE_MM <= val <= _MAX_PLAUSIBLE_MM:
            return val, None  # single value → width

    # Try m suffix: "0.82m x 2.04m"
    m_match = _M_RE.findall(t)
    if len(m_match) >= 2:
        w, h = round(float(m_match[0]) * 1000), round(float(m_match[1]) * 1000)
        if _is_plausible_dimension(w, h):
            return w, h
    if len(m_match) == 1:
        val_mm = round(float(m_match[0]) * 1000)
        if _MIN_PLAUSIBLE_MM <= val_mm <= _MAX_PLAUSIBLE_MM:
            return val_mm, None  # single value → width

    # Fallback: extract standalone numbers
    nums = _SINGLE_NUM_RE.findall(t)
    if len(nums) >= 2:
        # When slash notation is disallowed, two standalone numbers likely
        # come from a slash pair (e.g. "FRL 240/240") — skip the pair.
        if allow_slash or "/" not in t:
            a, b = int(nums[0]), int(nums[1])
            if _is_plausible_dimension(a, b):
                return a, b
    if len(nums) == 1:
        val = int(nums[0])
        # Single number: treat as width if plausible width, else height if plausible height
        if _MIN_PLAUSIBLE_MM <= val <= _MAX_PLAUSIBLE_MM:
            return val, None  # assume width
        return None, None

    return None, None


# ---------------------------------------------------------------------------
# Mark extraction
# ---------------------------------------------------------------------------
def extract_mark(text: str) -> str:
    """Extract a door/window type mark from text.

    Returns the first mark found (D01, W01, WD01, etc.) or empty string.
    """
    if not text:
        return ""
    t_upper = text.upper()
    for pat in _MARK_PATTERNS:
        m = pat.search(t_upper)
        if m:
            mark = m.group(1).upper()
            # Accept D, W, WD, DW prefixes
            if mark and (mark[0] in ("D", "W")):
                return mark
    return ""


# ---------------------------------------------------------------------------
# Header detection and column mapping
# ---------------------------------------------------------------------------
def detect_header(words: Sequence[str]) -> Dict[str, Any]:
    """Detect which columns hold mark, width, height, dims, count, description.

    Also infers dimension basis from the actual selected column headings.
    Compound headings like "Rough Opening Width" are parsed into both
    the role (width) and the basis (rough_opening).

    Args:
        words: list of column header texts (one per column, lowercased)

    Returns:
        dict with:
          - role → column index for each detected role
          - "dimension_basis": inferred basis ("" = unknown)
          - "basis_source": heading text that established the basis
    """
    mapping: Dict[str, Any] = {}
    # Track per-column basis for the selected dimension columns
    width_basis = ""
    width_basis_source = ""
    height_basis = ""
    height_basis_source = ""
    dims_basis = ""
    dims_basis_source = ""

    for i, w in enumerate(words):
        w_clean = w.strip().lower()
        if not w_clean:
            continue
        if w_clean in _HEADER_MARK_KEYWORDS and "mark" not in mapping:
            mapping["mark"] = i
            continue

        # Infer basis for this column
        col_basis, col_source = _infer_column_basis(w)

        # Check role: exact match first, then substring for compound headings
        if "width" not in mapping:
            is_width = (
                w_clean in _HEADER_WIDTH_KEYWORDS
                or any(kw in w_clean for kw in _HEADER_WIDTH_KEYWORDS)
            )
            if is_width:
                mapping["width"] = i
                width_basis = col_basis
                width_basis_source = col_source
                continue

        if "height" not in mapping:
            is_height = (
                w_clean in _HEADER_HEIGHT_KEYWORDS
                or any(kw in w_clean for kw in _HEADER_HEIGHT_KEYWORDS)
            )
            if is_height:
                mapping["height"] = i
                height_basis = col_basis
                height_basis_source = col_source
                continue

        if "dims" not in mapping:
            is_dims = (
                w_clean in _HEADER_DIMS_KEYWORDS
                or any(kw in w_clean for kw in _HEADER_DIMS_KEYWORDS)
            )
            if is_dims:
                mapping["dims"] = i
                dims_basis = col_basis
                dims_basis_source = col_source
                continue

        if "count" not in mapping and w_clean in _HEADER_COUNT_KEYWORDS:
            mapping["count"] = i
            continue

        if "desc" not in mapping and w_clean in _HEADER_DESC_KEYWORDS:
            mapping["desc"] = i
            continue

    # Resolve the final basis from the selected dimension columns
    basis, basis_source = _resolve_dimension_basis(
        width_basis, width_basis_source,
        height_basis, height_basis_source,
        dims_basis, dims_basis_source,
    )
    mapping["dimension_basis"] = basis
    mapping["basis_source"] = basis_source
    return mapping


def _is_header_row(words: Sequence[str]) -> bool:
    """Heuristic: does this row look like a table header?"""
    mapping = detect_header(words)
    # Need at least mark OR dims column to be a header
    return "mark" in mapping or "dims" in mapping


# ---------------------------------------------------------------------------
# Main parser: row list → ScheduleEntry list
# ---------------------------------------------------------------------------
def parse_schedule_rows(
    rows: Sequence[Dict[str, Any]],
    page_no: int = 0,
) -> List[ScheduleEntry]:
    """Parse schedule rows (from _word_rows() or similar) into ScheduleEntry.

    Each row dict must have:
      - "text": tab-separated row text
      - "bbox": [x0, y0, x1, y1]

    Strategy:
      1. Find header row (first row with mark/dims keywords).
      2. Map columns to roles.
      3. Parse each data row: extract mark, dimensions, description, count.
    """
    if not rows:
        return []

    entries: List[ScheduleEntry] = []
    col_map: Dict[str, int] = {}
    header_idx = -1

    # Find header row
    for idx, row in enumerate(rows):
        text = row.get("text", "")
        cells = [c.strip() for c in text.split("\t")]
        if _is_header_row(cells):
            col_map = detect_header(cells)
            header_idx = idx
            break

    if header_idx < 0:
        # No header found — try heuristic extraction from all rows
        return _parse_rows_without_header(rows, page_no)

    # Parse data rows (after header)
    for row in rows[header_idx + 1:]:
        text = row.get("text", "")
        bbox = row.get("bbox", (0, 0, 0, 0))
        cells = [c.strip() for c in text.split("\t")]

        # Skip empty rows
        if not any(cells):
            continue

        # Extract type mark
        mark = ""
        if "mark" in col_map and col_map["mark"] < len(cells):
            mark = extract_mark(cells[col_map["mark"]])
        if not mark:
            # Fallback: search full row text
            mark = extract_mark(text)

        if not mark:
            continue

        # Skip header-like rows that leaked through
        if mark in ("", "MARK", "TYPE", "CODE"):
            continue

        # Skip non-door/window marks
        if not (mark.startswith("D") or mark.startswith("W")):
            continue

        # Extract dimensions
        width_mm: Optional[int] = None
        height_mm: Optional[int] = None
        parse_source = "heuristic"

        if "dims" in col_map and col_map["dims"] < len(cells):
            width_mm, height_mm = parse_dimension(cells[col_map["dims"]])
            parse_source = "header_dims"
        elif "width" in col_map and "height" in col_map:
            w_idx = col_map["width"]
            h_idx = col_map["height"]
            if w_idx < len(cells):
                width_mm = parse_single_dimension(cells[w_idx])
            if h_idx < len(cells):
                height_mm = parse_single_dimension(cells[h_idx])
            parse_source = "header_separate"
        else:
            # Fallback: try full-row extraction.
            # When rating keywords are present, disallow slash-separated values
            # (FRL 240/240 is NOT a dimension), but strong x/× compounds are OK.
            has_rating = _has_rating_keywords(text)
            width_mm, height_mm = parse_dimension(text, allow_slash=not has_rating)
            if _is_plausible_dimension(width_mm, height_mm):
                parse_source = "heuristic"
            else:
                width_mm, height_mm = None, None
                parse_source = ""

        # Final plausibility gate: reject implausible bundles regardless of source
        if width_mm is not None or height_mm is not None:
            if not _is_plausible_dimension(width_mm, height_mm):
                width_mm, height_mm = None, None
                parse_source = ""

        # Extract count
        count = 1
        if "count" in col_map and col_map["count"] < len(cells):
            try:
                count = max(1, int(cells[col_map["count"]].replace("x", "").strip()))
            except (ValueError, TypeError):
                count = 1

        # Extract description
        desc = ""
        if "desc" in col_map and col_map["desc"] < len(cells):
            desc = cells[col_map["desc"]].strip()

        entries.append(ScheduleEntry(
            type_mark=mark,
            width_mm=width_mm,
            height_mm=height_mm,
            description=desc,
            count=count,
            page_no=page_no,
            bbox=tuple(bbox) if len(bbox) >= 4 else (0, 0, 0, 0),
            parse_source=parse_source,
            dimension_basis=col_map.get("dimension_basis", ""),
            basis_source=col_map.get("basis_source", ""),
        ))

    return entries


def _parse_rows_without_header(
    rows: Sequence[Dict[str, Any]],
    page_no: int,
) -> List[ScheduleEntry]:
    """Fallback parser when no header row is detected.

    Scans all rows for mark + dimension patterns.
    Rating/fire keywords prevent standalone rating values (60/60) from
    being treated as dimensions, but a strong explicit compound like
    820x2040 is still extracted.
    """
    entries: List[ScheduleEntry] = []
    for row in rows:
        text = row.get("text", "")
        bbox = row.get("bbox", (0, 0, 0, 0))
        mark = extract_mark(text)
        if not mark:
            continue
        if not (mark.startswith("D") or mark.startswith("W")):
            continue
        # When rating keywords present, disallow slash-separated values
        has_rating = _has_rating_keywords(text)
        width_mm, height_mm = parse_dimension(text, allow_slash=not has_rating)
        if not _is_plausible_dimension(width_mm, height_mm):
            continue
        entries.append(ScheduleEntry(
            type_mark=mark,
            width_mm=width_mm,
            height_mm=height_mm,
            page_no=page_no,
            bbox=tuple(bbox) if len(bbox) >= 4 else (0, 0, 0, 0),
            parse_source="heuristic",
        ))
    return entries


# ---------------------------------------------------------------------------
# Convenience: parse from a PDF page (uses _word_rows if available)
# ---------------------------------------------------------------------------
def parse_schedule_page(
    pdf_page: Any,
    page_no: int = 0,
) -> List[ScheduleEntry]:
    """Parse a door/window schedule page from a PyMuPDF page object.

    Uses _word_rows() from pb_plan_read_engine_v1228 for row extraction.
    """
    try:
        from pb_plan_read_engine_v1228 import _word_rows
        rows = _word_rows(pdf_page)
    except ImportError:
        # Fallback: basic word extraction
        try:
            words = list(pdf_page.get_text("words") or [])
        except Exception:
            return []
        rows = _basic_word_rows(words)
    return parse_schedule_rows(rows, page_no=page_no)


def _basic_word_rows(words: list) -> List[Dict[str, Any]]:
    """Minimal row grouping fallback when _word_rows is unavailable."""
    if not words:
        return []
    usable = []
    for word in words:
        if len(word) < 5:
            continue
        try:
            x0, y0, x1, y1 = map(float, word[:4])
        except Exception:
            continue
        text = str(word[4]).strip()
        if text:
            usable.append((x0, y0, x1, y1, text))
    if not usable:
        return []

    usable.sort(key=lambda item: ((item[1] + item[3]) / 2.0, item[0]))
    rows: List[List[tuple]] = []
    row_cy: List[float] = []
    tolerance = 5.0
    for item in usable:
        cy = (item[1] + item[3]) / 2.0
        best = -1
        best_d = 1e9
        for idx in range(len(rows)):
            d = abs(cy - row_cy[idx])
            if d <= tolerance and d < best_d:
                best, best_d = idx, d
        if best < 0:
            rows.append([item])
            row_cy.append(cy)
        else:
            rows[best].append(item)
            row_cy[best] = sum((p[1] + p[3]) / 2.0 for p in rows[best]) / len(rows[best])

    output = []
    for row in rows:
        row.sort(key=lambda item: item[0])
        pieces = []
        prev_x1 = None
        for x0, y0, x1, y1, text in row:
            if prev_x1 is not None:
                gap = x0 - prev_x1
                pieces.append("\t" if gap > 18.0 else " ")
            pieces.append(text)
            prev_x1 = x1
        output.append({
            "text": "".join(pieces).strip(),
            "bbox": [min(r[0] for r in row), min(r[1] for r in row),
                     max(r[2] for r in row), max(r[3] for r in row)],
            "words": row,
        })
    output.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))
    return output


# ---------------------------------------------------------------------------
# Enrichment: apply schedule data to existing OpeningEvidence
# ---------------------------------------------------------------------------
def enrich_opening_evidence(
    instances: List[OpeningEvidence],
    schedule_entries: List[ScheduleEntry],
) -> List[OpeningEvidence]:
    """Enrich geometric instances with schedule dimensions.

    Schedule data provides width/height for openings that already have
    a matching type_mark.  The schedule does NOT create new instances.

    Rules:
      - Both must share the same non-empty type_mark (B0 safety).
      - Schedule dimensions override only if they upgrade the atomic
        dimension bundle (basis priority + confidence).
      - dimension_source is set to "schedule_parse".
      - No deduction changes — schedule enrichment is evidence-only.

    Returns:
        New list with enriched records (originals not mutated).
    """
    if not instances or not schedule_entries:
        return list(instances)

    # Build lookup: type_mark → list of ScheduleEntry (all rows for that mark)
    schedule_by_mark: Dict[str, List[ScheduleEntry]] = {}
    for entry in schedule_entries:
        if entry.type_mark:
            schedule_by_mark.setdefault(entry.type_mark, []).append(entry)

    # For each mark, determine the authoritative schedule data:
    # - Identical dimensions across duplicates → safe to use
    # - Conflicting dimensions → skip enrichment (ambiguous)
    _PROVENANCE_RANK = {"header_separate": 3, "header_dims": 2, "heuristic": 1, "": 0}
    mark_authority: Dict[str, Optional[ScheduleEntry]] = {}
    mark_conflicts: Dict[str, List[ScheduleEntry]] = {}  # conflicting entries
    for mark, entries_list in schedule_by_mark.items():
        if len(entries_list) == 1:
            mark_authority[mark] = entries_list[0]
        else:
            # Multiple rows for same mark — check dimension AND basis consistency.
            # Equal numbers with contradictory measurement meaning (frame vs
            # rough_opening) are still contradictory evidence.
            dims_set = set()
            for e in entries_list:
                dims_set.add((e.width_mm, e.height_mm, e.dimension_basis))
            if len(dims_set) == 1:
                # All identical → safe; pick strongest provenance
                best = max(entries_list,
                           key=lambda e: _PROVENANCE_RANK.get(e.parse_source, 0))
                mark_authority[mark] = best
            else:
                # Conflicting → do NOT enrich; retain alternatives for B4
                mark_authority[mark] = None
                mark_conflicts[mark] = entries_list

    enriched = []
    for inst in instances:
        mark = inst.type_mark
        if mark and mark in mark_authority:
            sched = mark_authority[mark]
            if sched is None:
                # Conflicting duplicate schedule rows — record alternatives
                # so B4 can see the exact schedule ambiguity.
                alternatives = mark_conflicts.get(mark, [])
                conflicting_obs = {
                    "source": "schedule_parse",
                    "width_m": None,
                    "height_m": None,
                    "dimension_basis": DIMENSION_BASIS_UNKNOWN,
                    "dimension_confidence": 0.0,
                    "type_mark": mark,
                    "page_no": None,
                    "accepted": False,
                    "status": "ambiguous",
                    "alternatives": [
                        {
                            "width_mm": e.width_mm,
                            "height_mm": e.height_mm,
                            "page_no": e.page_no,
                            "parse_source": e.parse_source,
                            "dimension_basis": e.dimension_basis,
                            "basis_source": e.basis_source,
                        }
                        for e in alternatives
                    ],
                }
                inst.source_observations = list(inst.source_observations) + [conflicting_obs]
                enriched.append(inst)
                continue
            # Confidence based on parse provenance
            if sched.parse_source == "header_separate":
                dim_conf = 0.8
            elif sched.parse_source == "header_dims":
                dim_conf = 0.75
            elif sched.parse_source == "heuristic":
                dim_conf = 0.5
            else:
                dim_conf = 0.5
            # Use the ScheduleEntry's dimension_basis — inferred from
            # heading text.  Only explicit headings like "Rough Opening"
            # produce rough_opening; generic "Width/Height" stays unknown.
            sched_basis = sched.dimension_basis or DIMENSION_BASIS_UNKNOWN
            # Create a schedule-sourced evidence record for merging
            sched_ev = OpeningEvidence(
                type_mark=mark,
                width_m=(sched.width_mm / 1000.0) if sched.width_mm else None,
                height_m=(sched.height_mm / 1000.0) if sched.height_mm else None,
                dimension_basis=sched_basis,
                dimension_source="schedule_parse",
                dimension_confidence=dim_conf,
                extraction_method="schedule_parse",
                page_no=sched.page_no,
            )
            # Only append schedule observation — B1 already recorded the plan obs.
            # B2 NEVER reconstructs another source's observation.
            sched_obs = {
                "source": "schedule_parse",
                "width_m": sched_ev.width_m,
                "height_m": sched_ev.height_m,
                "dimension_basis": sched_ev.dimension_basis,
                "dimension_confidence": sched_ev.dimension_confidence,
                "type_mark": mark,
                "page_no": sched_ev.page_no,
                "accepted": False,  # updated after merge
            }
            # Use B0's merge logic (basis priority + confidence)
            merged = merge_opening_evidence(inst, sched_ev)
            # Determine if schedule won the atomic bundle
            if merged.dimension_source == "schedule_parse":
                sched_obs["accepted"] = True
            # Append ONLY the schedule observation (plan obs already exists)
            merged.source_observations = list(inst.source_observations) + [sched_obs]
            enriched.append(merged)
        else:
            enriched.append(inst)

    return enriched
