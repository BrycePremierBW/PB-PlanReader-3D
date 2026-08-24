"""PlanReader v1.5.0 height evidence model, resolver and room association.

Replaces the v136 height solver with a structured HeightEvidence model and
a resolver that enforces a strict evidence precedence chain.  Does NOT create
a parallel engine — patches v136 functions so the existing production chain
(v135 registration → v136 evidence → v139 walls → v141 takeoff) flows through
the enhanced resolver.

Evidence precedence (strongest first):
  1. Explicit semantic label (CH/CLG/CEILING/FCL + value)
  2. Contextually paired RLs (FFL→FCL, Level 1→Level 2, etc.)
  3. Raw vertical dimension (1.8–12 m range)
  4. Project default (explicit Default/fallback — never Measured)

RL safety: two unrelated RL values on the same sheet do NOT become a wall/ceiling
height merely because their difference is close to 2.7–3.2 m.  RL-derived evidence
requires contextual pairing via semantic keywords (FFL, FCL, LEVEL, STOREY, etc.).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

VERSION = "1.5.0"
SETTING_KEY = "height_evidence_v150"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

_HEIGHT_TYPES = (
    "floor_to_ceiling", "floor_to_floor", "ceiling_bulkhead",
    "parapet", "soffit", "facade", "generic",
)

# Valid height ranges per type (metres)
_TYPE_RANGES: Dict[str, Tuple[float, float]] = {
    "floor_to_ceiling": (1.8, 6.0),
    "floor_to_floor": (2.4, 10.0),
    "ceiling_bulkhead": (0.1, 2.0),
    "parapet": (0.3, 5.0),
    "soffit": (0.3, 5.0),
    "facade": (1.8, 60.0),
    "generic": (0.5, 60.0),
}

_PRIORITY_MAP = {
    "semantic_label": 0,   # explicit CH/CLG/FCL — strongest
    "rl_difference": 1,    # contextual RL pair
    "dimension_parse": 2,  # raw dimension
    "section": 3,          # section-derived
    "default": 99,         # fallback
}


@dataclass
class HeightEvidence:
    """Single unit of height evidence with provenance and confidence."""
    id: str = ""
    source_page_id: int = 0
    source_page_label: str = ""
    height_type: str = "generic"       # floor_to_ceiling, floor_to_floor, etc.
    raw_text: str = ""
    height_m: float = 2.7
    extraction_method: str = "default"  # semantic_label, rl_difference, dimension_parse, default
    confidence: float = 0.50
    confidence_reason: str = ""
    status: str = "Default/fallback"    # Measured, Provisional measured, Default/fallback, Review
    evidence: List[str] = field(default_factory=list)
    # Spatial fields (mutually exclusive modes):
    #   text_span: [start, end] character offsets from plain text extraction
    #   bbox: [x0, y0, x1, y1] real PDF coordinates from positioned words
    #   anchor: (cx, cy) centre point for spatial matching
    text_span: List[int] = field(default_factory=list)
    bbox: List[float] = field(default_factory=list)
    anchor: Optional[Tuple[float, float]] = None
    # Legacy field — kept for backwards compat, prefer text_span/bbox/anchor
    position: List[int] = field(default_factory=list)
    priority: int = 99  # lower = higher precedence; derived from extraction_method

    def __post_init__(self) -> None:
        if self.priority == 99 and self.extraction_method in _PRIORITY_MAP:
            self.priority = _PRIORITY_MAP[self.extraction_method]
        # Backwards compat: if legacy position set but text_span empty, migrate
        if self.position and not self.text_span and not self.bbox:
            self.text_span = list(self.position)


@dataclass
class WordBox:
    """Positioned text word from PDF extraction (bbox in PDF coordinates)."""
    text: str
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0
    page_id: int = 0
    line_id: int = 0

    @property
    def centre(self) -> Tuple[float, float]:
        return ((self.x0 + self.x1) / 2.0, (self.y0 + self.y1) / 2.0)

    @property
    def bbox(self) -> Tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Semantic ceiling height labels (lookbehind allows start-of-string / after space)
_CEIL_RE = re.compile(
    r"(?<!\w)(?:CH|CLG|CEILING\s*HEIGHT|FCL|CEIL)\s*[:=]?\s*"
    r"(\d{1,5}(?:\.\d+)?)\s*(mm|m)?",
    re.IGNORECASE,
)

# Floor-to-floor / floor finish level (standalone datum — NOT a wall height)
_FFL_RE = re.compile(
    r"\b(?:FFL|FINISHED\s*FLOOR\s*LEVEL)\s*[:=]?\s*"
    r"(-?\d{1,3}(?:\.\d{1,4})?)\s*(mm|m)?",
    re.IGNORECASE,
)

# RL / AHD (Reduced Level / Australian Height Datum)
_RL_RE = re.compile(
    r"\b(?:RL|AHD)\s*[:=]?\s*(-?\d{1,3}(?:\.\d{1,4})?)\b",
    re.IGNORECASE,
)

# Storey / level references
_LEVEL_RE = re.compile(
    r"\b(?:LEVEL|LVL|FLOOR|STOREY|STORY)\s*(\d{1,2})\b",
    re.IGNORECASE,
)

# Generic bare dimension: 3–5 digits, optional mm/m suffix
_DIM_RE = re.compile(
    r"(?<![:\d])(\d{3,5}(?:\.\d+)?)\s*(mm|m)?(?!\s*[:\d])",
    re.IGNORECASE,
)

# Context patterns for dimension orientation classification
_VERTICAL_CONTEXT_RE = re.compile(
    r"\b(?:elevation|section|height|ht|vertical|storey|story|floor.to."
    r"(?:ceiling|floor|roof)|ceiling|soffit|parapet|facade|clg|fcl|ch)\b",
    re.IGNORECASE,
)
_HORIZONTAL_CONTEXT_RE = re.compile(
    r"\b(?:plan|width|breadth|room|horizontal|long|span|length|internal|"
    r"external|area|garage|kitchen|bed|bath|living|dining|entry)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Semantic RL pairing
# ---------------------------------------------------------------------------

# Contextual keywords that indicate a legitimate vertical reference
_RL_CONTEXT_KEYWORDS = (
    "ffl", "fcl", "ahd", "floor", "ceiling", "level", "storey",
    "story", "soffit", "parapet", "natural", "ground", "finished",
)

# Regex for detecting RL-context keywords (avoids false matches from
# "rl" substring inside words like "FCL")
_RL_KW_RE = re.compile(
    r"\b(?:ffl|fcl|ahd|floor|ceiling|level|storey|story|soffit|parapet|"
    r"natural|ground|finished)\b",
    re.IGNORECASE,
)

# Semantic keywords that indicate height type when paired with RL values
_F2C_KEYWORDS = {"fcl", "ceiling", "soffit"}
_F2F_KEYWORDS = {"ffl", "floor", "level", "storey", "story", "finished"}
_PARAPET_KEYWORDS = {"parapet"}
_RL_KEYWORDS = {"rl", "ahd", "natural", "ground"}


def _infer_rl_height_type(kw_a: str, kw_b: str) -> str:
    """Determine height type from the semantic identities of both RL endpoints.

    FFL → FCL = floor_to_ceiling
    Level 1 FFL → Level 2 FFL = floor_to_floor
    FFL → SOFFIT = soffit
    FFL → PARAPET = parapet
    """
    a, b = kw_a.lower(), kw_b.lower()
    pair = (min(a, b), max(a, b))
    # Known semantic pairs take precedence
    _PAIR_TYPES = {
        ("fcl", "ffl"): "floor_to_ceiling",
        ("ceiling", "ffl"): "floor_to_ceiling",
        ("fcl", "floor"): "floor_to_ceiling",
        ("ceiling", "floor"): "floor_to_ceiling",
        ("ffl", "soffit"): "generic",
        ("fcl", "soffit"): "floor_to_ceiling",
        ("ffl", "parapet"): "parapet",
    }
    if pair in _PAIR_TYPES:
        return _PAIR_TYPES[pair]
    # Same keyword family → that type
    if a == b:
        if a in _F2C_KEYWORDS:
            return "floor_to_ceiling"
        if a in _F2F_KEYWORDS:
            return "floor_to_floor"
    # FFL with non-FFL → type of the non-FFL endpoint
    if a == "ffl" or b == "ffl":
        other = b if a == "ffl" else a
        if other in _F2C_KEYWORDS:
            return "floor_to_ceiling"
        if other in _PARAPET_KEYWORDS:
            return "parapet"
        return "floor_to_floor"  # FFL + level/storey/other = f2f
    # Mixed types
    types_a = set()
    types_b = set()
    for kw_set, htype in [(_F2C_KEYWORDS, "floor_to_ceiling"),
                           (_F2F_KEYWORDS, "floor_to_floor"),
                           (_PARAPET_KEYWORDS, "parapet"),
                           (_RL_KEYWORDS, "generic")]:
        if a in kw_set:
            types_a.add(htype)
        if b in kw_set:
            types_b.add(htype)
    all_types = types_a | types_b
    if "floor_to_ceiling" in all_types and "floor_to_floor" not in all_types:
        return "floor_to_ceiling"
    if "floor_to_floor" in all_types and "floor_to_ceiling" not in all_types:
        return "floor_to_floor"
    if "parapet" in all_types:
        return "parapet"
    return "generic"


def _find_paired_rls(
    text: str,
    tolerance: float = 3.0,
    min_height: float = 1.8,
    max_height: float = 12.0,
) -> List[HeightEvidence]:
    """Find RL values that are contextually paired for vertical measurement.

    Only RLs near semantic height keywords (FFL, FCL, LEVEL, etc.) are
    considered.  Two unrelated RLs on the same sheet must NOT become a
    wall/ceiling height merely because abs(RL2 − RL1) ≈ 2.7–3.2 m.
    Height type is inferred from the semantic identity of BOTH endpoints.
    """
    paired_rls: List[Tuple[float, str, int]] = []
    for m in _RL_RE.finditer(text):
        rl_val = float(m.group(1))
        rl_pos = m.start()
        # Search wider context for the CLOSEST semantic keyword
        start = max(0, rl_pos - 50)
        end = min(len(text), m.end() + 50)
        context = text[start:end]
        best_kw = None
        best_dist = 999
        for kw_m in _RL_KW_RE.finditer(context):
            dist = abs(kw_m.start() - (rl_pos - start))
            if dist < best_dist:
                best_dist = dist
                best_kw = kw_m.group(0).lower()
        if best_kw:
            paired_rls.append((rl_val, best_kw, rl_pos))

    if len(paired_rls) < 2:
        return []

    results: List[HeightEvidence] = []
    seen: set[Tuple[float, float]] = set()
    for i, (a, kw_a, pos_a) in enumerate(paired_rls):
        for b, kw_b, pos_b in paired_rls[i + 1:]:
            key = (min(a, b), max(a, b))
            if key in seen:
                continue
            seen.add(key)
            diff = round(abs(b - a), 4)
            if min_height <= diff <= max_height:
                ht = _infer_rl_height_type(kw_a, kw_b)
                best_dist = abs(diff - tolerance)
                ev = HeightEvidence(
                    id="",  # assigned by caller
                    source_page_id=0,
                    source_page_label="",
                    height_type=ht,
                    raw_text=f"RL {a} -> RL {b} (context: {kw_a}/{kw_b})",
                    height_m=diff,
                    extraction_method="rl_difference",
                    confidence=0.95,
                    confidence_reason=f"Contextual RL pairing ({kw_a}/{kw_b}) -> {ht}",
                    status="Measured",
                    evidence=[
                        f"RL {a} -> RL {b} = {diff:.3f} m",
                        f"Semantic context: {kw_a}/{kw_b}",
                        f"Inferred type: {ht}",
                        f"Distance from 3.0 m anchor: {best_dist:.3f} m",
                    ],
                    position=[pos_a, pos_b],
                )
                results.append(ev)
    results.sort(key=lambda e: abs(e.height_m - tolerance))
    return results[:3]  # top 3 closest to 3.0 m


# ---------------------------------------------------------------------------
# Semantic height label extraction
# ---------------------------------------------------------------------------

def _extract_semantic_heights(text: str) -> List[HeightEvidence]:
    """Extract explicitly labelled heights: CH 2700, CLG 3000, etc.

    FFL/level values are stored as level_references, NOT as usable heights.
    A standalone FFL 10.000 is an absolute datum, not a 10 m wall height.
    Heights are derived from PAIRED level references by _derive_level_heights.
    """
    results: List[HeightEvidence] = []

    # --- Ceiling height labels → usable height evidence ---
    for m in _CEIL_RE.finditer(text):
        raw = float(m.group(1))
        unit = (m.group(2) or "").lower()
        if unit == "mm" or (not unit and raw >= 100):
            val = raw / 1000.0
        elif unit == "m":
            val = raw
        elif not unit and 1.8 <= raw <= 12.0:
            val = raw  # bare value in metres (e.g. FCL 2.7)
        else:
            continue
        rng = _TYPE_RANGES["floor_to_ceiling"]
        if rng[0] <= val <= rng[1]:
            results.append(HeightEvidence(
                id="", source_page_id=0, source_page_label="",
                height_type="floor_to_ceiling",
                raw_text=m.group(0).strip(),
                height_m=round(val, 4),
                extraction_method="semantic_label",
                confidence=0.95,
                confidence_reason="Explicit ceiling height label",
                status="Measured",
                evidence=[f"Ceiling height: {m.group(0).strip()} = {val:.3f} m"],
                position=[m.start(), m.end()],
            ))

    # FFL/LEVEL references are handled by _extract_ffl_level_refs
    return results


def _extract_ffl_level_refs(text: str) -> List[HeightEvidence]:
    """Extract FFL/LEVEL line pairs with associated RL values.

    Handles patterns like:
      FFL 10.000         → level ref 10.0 m
      LEVEL 1 FFL 10.000 → level ref 10.0 m
      LEVEL 2 RL 13.200  → level ref 13.2 m
    """
    results: List[HeightEvidence] = []
    for m in re.finditer(
        r"(?:LEVEL\s+(\d{1,2})\s+)?(?P<kw>FFL|FCL|FINISHED\s*FLOOR\s*LEVEL)"
        r"\s*[:=]?\s*(-?\d{1,3}(?:\.\d{1,4})?)\b",
        str(text or ""), re.IGNORECASE,
    ):
        level_num = m.group(1)
        raw = float(m.group(3))
        val = raw if abs(raw) < 100 else raw / 1000.0
        matched_kw = m.group("kw").split()[0].upper()
        if level_num:
            label = f"Level {level_num} {matched_kw}"
        elif matched_kw in ("FCL",):
            label = "FCL"
        else:
            label = "FFL"
        results.append(HeightEvidence(
            id="", source_page_id=0, source_page_label="",
            height_type="level_reference",
            raw_text=f"{label} {val:.3f} m",
            height_m=round(val, 4),
            extraction_method="level_reference",
            confidence=0.90,
            confidence_reason=f"{label} absolute datum",
            status="Level reference",
            evidence=[f"{label}: {val:.3f} m (absolute datum)"],
            position=[m.start(), m.end()],
        ))
    return results


def _derive_level_heights(level_refs: List[HeightEvidence]) -> List[HeightEvidence]:
    """Derive height evidence from paired level references.

    Same-level pairing:
      FFL 10.000 → FCL 12.700 = floor_to_ceiling 2.700 m
    Cross-level pairing:
      Level 1 FFL 10.000 → Level 2 FFL 13.200 = floor_to_floor 3.200 m
    Cross-level FFL→FCL is rejected (not a valid room height).
    """
    # Specific height-type keywords (exclude "level"/"storey" which are just
    # level identifiers like "Level 1", not the actual height type)
    _TYPE_KW_RE = re.compile(r"\b(ffl|fcl|ceiling|floor|soffit|parapet)\b",
                             re.IGNORECASE)
    _LEVEL_NUM_RE = re.compile(r"\bLevel\s+(\d{1,2})\b", re.IGNORECASE)

    def _extract_type_kw(raw: str) -> str:
        """Extract the height-type keyword from a level reference raw_text."""
        for m in _TYPE_KW_RE.finditer(raw):
            return m.group(1).lower()
        return ""

    def _extract_level_num(raw: str) -> Optional[int]:
        """Extract the level number from raw_text, or None if no level prefix."""
        m = _LEVEL_NUM_RE.search(raw)
        return int(m.group(1)) if m else None

    results: List[HeightEvidence] = []
    for i, a in enumerate(level_refs):
        for b in level_refs[i + 1:]:
            diff = round(abs(b.height_m - a.height_m), 4)
            if 1.8 <= diff <= 12.0:
                kw_a = _extract_type_kw(a.raw_text)
                kw_b = _extract_type_kw(b.raw_text)
                lev_a = _extract_level_num(a.raw_text)
                lev_b = _extract_level_num(b.raw_text)

                # Cross-level safety: FFL→FCL across different levels is not
                # a valid room height (it's a cross-storey datum span).
                if (lev_a is not None and lev_b is not None and lev_a != lev_b
                        and {kw_a, kw_b} == {"ffl", "fcl"}):
                    continue

                ht = _infer_rl_height_type(kw_a, kw_b)
                results.append(HeightEvidence(
                    id="",
                    source_page_id=a.source_page_id,
                    source_page_label=a.source_page_label,
                    height_type=ht,
                    raw_text=f"{a.raw_text} -> {b.raw_text}",
                    height_m=diff,
                    extraction_method="rl_difference",
                    confidence=0.90,
                    confidence_reason=f"Derived from level references ({a.raw_text} -> {b.raw_text})",
                    status="Measured",
                    evidence=[
                        f"Level ref A: {a.raw_text} = {a.height_m:.3f} m",
                        f"Level ref B: {b.raw_text} = {b.height_m:.3f} m",
                        f"Derived height: {diff:.3f} m ({ht})",
                    ],
                    position=a.position + b.position if a.position and b.position else [],
                ))
    return results


# ---------------------------------------------------------------------------
# Raw dimension extraction
# ---------------------------------------------------------------------------

def _classify_dimension_orientation(text: str, match_start: int, match_end: int) -> str:
    """Classify whether a dimension is vertical, horizontal, or unknown.

    Checks surrounding text context for orientation cues. Dimensions in
    section/elevation context are vertical; dimensions in plan/room context
    are horizontal. Unknown = Review/unresolved.
    """
    start = max(0, match_start - 60)
    end = min(len(text), match_end + 60)
    context = text[start:end]
    has_vert = bool(_VERTICAL_CONTEXT_RE.search(context))
    has_horiz = bool(_HORIZONTAL_CONTEXT_RE.search(context))
    if has_vert and not has_horiz:
        return "vertical"
    if has_horiz and not has_vert:
        return "horizontal"
    if has_vert and has_horiz:
        return "unknown"  # ambiguous
    return "unknown"


def _extract_dimension_heights(
    text: str,
    height_type: str = "generic",
    is_section_or_elevation: bool = False,
) -> List[HeightEvidence]:
    """Extract dimensions in the plausible height range (1.8–12 m).

    A raw dimension may only become height evidence when geometry/context
    proves it is vertical. Without orientation context → Review/unresolved,
    and it must NOT outrank the project fallback for production wall height.
    """
    rng = _TYPE_RANGES.get(height_type, _TYPE_RANGES["generic"])
    min_h = max(1.8, rng[0])
    max_h = min(12.0, rng[1])
    results: List[HeightEvidence] = []
    for m in _DIM_RE.finditer(text):
        raw = float(m.group(1))
        unit = (m.group(2) or "").lower()
        if unit == "mm" or (not unit and raw >= 100):
            val = raw / 1000.0
        elif unit == "m":
            val = raw
        else:
            continue
        if not (min_h <= val <= max_h):
            continue

        if is_section_or_elevation:
            orient = "vertical"
            conf = 0.70
            status = "Provisional measured"
            reason = "Dimension in section/elevation context (vertical assumed)"
        else:
            orient = _classify_dimension_orientation(text, m.start(), m.end())
            if orient == "vertical":
                conf = 0.70
                status = "Provisional measured"
                reason = "Dimension with vertical context cues"
            elif orient == "horizontal":
                continue  # horizontal dimension → NOT height evidence
            else:
                conf = 0.40
                status = "Review"
                reason = "Dimension with unknown orientation — not usable as height"

        results.append(HeightEvidence(
            id="", source_page_id=0, source_page_label="",
            height_type=height_type,
            raw_text=m.group(0).strip(),
            height_m=round(val, 4),
            extraction_method="dimension_parse",
            confidence=conf,
            confidence_reason=reason,
            status=status,
            evidence=[f"Dimension: {m.group(0).strip()} = {val:.3f} m ({orient})"],
            position=[m.start(), m.end()],
        ))
    return results


# ---------------------------------------------------------------------------
# Section height extraction
# ---------------------------------------------------------------------------

def _extract_section_heights(
    text: str,
    page_id: int = 0,
    page_label: str = "",
) -> List[HeightEvidence]:
    """Extract heights from section drawing text.

    Sections carry storey heights and vertical spans that elevations may not.
    Uses the same semantic/RL/dimension parsing but with section-specific
    confidence adjustments.
    """
    results: List[HeightEvidence] = []
    for ev in _extract_semantic_heights(text):
        ev.source_page_id = page_id
        ev.source_page_label = page_label
        ev.confidence = max(0.80, ev.confidence - 0.05)
        results.append(ev)

    for ev in _find_paired_rls(text):
        ev.source_page_id = page_id
        ev.source_page_label = page_label
        ev.confidence = max(0.80, ev.confidence - 0.05)
        results.append(ev)

    for ev in _extract_dimension_heights(text, "generic", is_section_or_elevation=True):
        ev.source_page_id = page_id
        ev.source_page_label = page_label
        ev.confidence = max(0.65, ev.confidence - 0.05)
        results.append(ev)

    return results


# ---------------------------------------------------------------------------
# All-evidence aggregator
# ---------------------------------------------------------------------------

def _words_to_text_with_map(words: Sequence[WordBox]) -> Tuple[str, List[WordBox]]:
    """Reconstruct text from positioned words and build char→word mapping."""
    lines: Dict[int, List[WordBox]] = {}
    for w in words:
        lines.setdefault(w.line_id, []).append(w)
    parts: List[str] = []
    word_map: List[WordBox] = []
    for lid in sorted(lines.keys()):
        line_words = sorted(lines[lid], key=lambda w: w.x0)
        line_words2 = [w for w in line_words if w.text.strip()]
        for w in line_words2:
            parts.append(w.text)
            word_map.append(w)
            parts.append(" ")
        if parts and parts[-1] == " ":
            parts[-1] = "\n"
    return "".join(parts), word_map


def _pos_char_to_word_bbox(
    char_pos: int,
    text: str,
    word_map: List[WordBox],
) -> Optional[List[float]]:
    """Map a character position in reconstructed text to word bbox."""
    idx = 0
    for wi, w in enumerate(word_map):
        end = idx + len(w.text)
        if idx <= char_pos < end:
            return list(w.bbox)
        idx = end + 1  # +1 for space/newline
    if word_map:
        return list(word_map[-1].bbox)
    return None


def _set_evidence_spatial(
    ev: HeightEvidence,
    char_pos: int,
    text: str,
    word_map: List[WordBox],
) -> None:
    """Set bbox and anchor from positioned word map. Keeps text_span for trace."""
    if ev.text_span and len(ev.text_span) == 2:
        bbox = _pos_char_to_word_bbox(ev.text_span[0], text, word_map)
        if bbox:
            ev.bbox = list(bbox)
            ev.anchor = ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def _extract_with_positions(
    words: Sequence[WordBox],
    page_id: int = 0,
    page_label: str = "",
    page_type: str = "",
) -> List[HeightEvidence]:
    """Extract height evidence from positioned PDF words with real bbox."""
    if not words:
        return []

    text, word_map = _words_to_text_with_map(words)
    is_sect = "section" in str(page_type).lower()
    ev_id_counter = 0

    def _next_id() -> str:
        nonlocal ev_id_counter
        ev_id_counter += 1
        return f"H{page_id:04d}_{ev_id_counter:02d}"

    results: List[HeightEvidence] = []

    for ev in _extract_semantic_heights(text):
        ev.id = _next_id()
        ev.source_page_id = page_id
        ev.source_page_label = page_label
        _set_evidence_spatial(ev, 0, text, word_map)
        results.append(ev)

    ffl_refs = _extract_ffl_level_refs(text)
    for ev in ffl_refs:
        ev.id = _next_id()
        ev.source_page_id = page_id
        ev.source_page_label = page_label
        _set_evidence_spatial(ev, 0, text, word_map)
        results.append(ev)

    for ev in _derive_level_heights(ffl_refs):
        ev.id = _next_id()
        ev.source_page_id = page_id
        ev.source_page_label = page_label
        _set_evidence_spatial(ev, 0, text, word_map)
        results.append(ev)

    for ev in _find_paired_rls(text):
        ev.id = _next_id()
        ev.source_page_id = page_id
        ev.source_page_label = page_label
        _set_evidence_spatial(ev, 0, text, word_map)
        results.append(ev)

    for ev in _extract_dimension_heights(text, is_section_or_elevation=is_sect):
        ev.id = _next_id()
        ev.source_page_id = page_id
        ev.source_page_label = page_label
        _set_evidence_spatial(ev, 0, text, word_map)
        results.append(ev)

    if is_sect:
        for ev in _extract_section_heights(text, page_id, page_label):
            if not any(e.raw_text == ev.raw_text and e.height_m == ev.height_m
                       for e in results):
                ev.id = _next_id()
                results.append(ev)

    results.sort(key=lambda e: (-e.confidence, e.priority))
    return results


def extract_all_height_evidence(
    text_or_words: Any,
    page_id: int = 0,
    page_label: str = "",
    page_type: str = "",
) -> List[HeightEvidence]:
    """Extract all height evidence from a page.

    Accepts either plain text (str) or positioned words (list of WordBox).
    When positioned words are provided, evidence gets real PDF bbox coordinates.
    """
    if isinstance(text_or_words, (list, tuple)) and text_or_words and isinstance(text_or_words[0], WordBox):
        return _extract_with_positions(
            list(text_or_words), page_id=page_id,
            page_label=page_label, page_type=page_type,
        )

    text = str(text_or_words or "")
    is_sect = "section" in str(page_type).lower()
    ev_id_counter = 0

    def _next_id() -> str:
        nonlocal ev_id_counter
        ev_id_counter += 1
        return f"H{page_id:04d}_{ev_id_counter:02d}"

    results: List[HeightEvidence] = []

    for ev in _extract_semantic_heights(text):
        ev.id = _next_id()
        ev.source_page_id = page_id
        ev.source_page_label = page_label
        results.append(ev)

    # Extract FFL/level references and derive heights from paired levels
    ffl_refs = _extract_ffl_level_refs(text)
    for ev in ffl_refs:
        ev.id = _next_id()
        ev.source_page_id = page_id
        ev.source_page_label = page_label
        results.append(ev)

    for ev in _derive_level_heights(ffl_refs):
        ev.id = _next_id()
        ev.source_page_id = page_id
        ev.source_page_label = page_label
        results.append(ev)

    for ev in _find_paired_rls(text):
        ev.id = _next_id()
        ev.source_page_id = page_id
        ev.source_page_label = page_label
        results.append(ev)

    for ev in _extract_dimension_heights(text, is_section_or_elevation=is_sect):
        ev.id = _next_id()
        ev.source_page_id = page_id
        ev.source_page_label = page_label
        results.append(ev)

    if is_sect:
        for ev in _extract_section_heights(text, page_id, page_label):
            if not any(e.raw_text == ev.raw_text and e.height_m == ev.height_m
                       for e in results):
                ev.id = _next_id()
                results.append(ev)

    results.sort(key=lambda e: (-e.confidence, e.priority))
    return results


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

def resolve_height(
    evidence_list: Sequence[HeightEvidence],
    target_type: str = "floor_to_ceiling",
    allow_floor_to_floor: bool = True,
    wall_ref: str = "",
    side: str = "",
    default_height: float = 2.7,
) -> Tuple[float, HeightEvidence]:
    """Resolve the best height from evidence, respecting type compatibility.

    Args:
        evidence_list: All available height evidence, sorted or unsorted.
        target_type: What height type is needed.
        allow_floor_to_floor: If False, reject floor-to-floor evidence.
        wall_ref: Optional wall reference for context logging.
        side: Optional building side for context logging.
        default_height: Project-configured default wall height (metres).

    Returns:
        (height_m, best_evidence) — always returns a valid height.
    """
    if not evidence_list:
        return round(default_height, 4), HeightEvidence(
            id="", source_page_id=0, source_page_label="",
            height_type="generic", raw_text="project default",
            height_m=default_height, extraction_method="default",
            confidence=0.50, confidence_reason="No height evidence available",
            status="Default/fallback",
            evidence=[f"Project default {default_height:.1f} m — no measured evidence"],
        )

    compatible: List[HeightEvidence] = []
    for ev in evidence_list:
        # Level references are absolute datums — never usable as wall heights
        if ev.height_type == "level_reference":
            continue
        if ev.height_type == "floor_to_floor" and not allow_floor_to_floor:
            continue
        if target_type in ("floor_to_ceiling", "ceiling_bulkhead"):
            if ev.height_type == "floor_to_floor":
                continue
        # Review/unknown-orientation evidence is retained for debugging but
        # must NOT drive production wall m² unless explicitly overridden.
        if ev.status == "Review":
            continue
        compatible.append(ev)

    if not compatible:
        default_ev = HeightEvidence(
            id="", source_page_id=0, source_page_label="",
            height_type="generic",
            raw_text="project default (no compatible evidence)",
            height_m=default_height, extraction_method="default",
            confidence=0.50,
            confidence_reason=f"No compatible evidence for {target_type}",
            status="Default/fallback",
            evidence=[f"Project default {default_height:.1f} m — "
                      f"no evidence compatible with {target_type}"],
        )
        return round(default_height, 4), default_ev

    compatible.sort(key=lambda e: (e.priority, -e.confidence))
    best = compatible[0]
    return round(best.height_m, 4), best


# ---------------------------------------------------------------------------
# Per-room height association
# ---------------------------------------------------------------------------

def resolve_room_heights(
    rooms: Sequence[Dict[str, Any]],
    all_evidence: Sequence[HeightEvidence],
    default_height: float = 2.7,
) -> Dict[str, Dict[str, Any]]:
    """Associate height evidence with rooms using Priority 2 room polygons.

    Each room gets the best compatible height evidence:
    - Semantic label inside the room polygon wins first
    - Paired RL evidence for the registered side
    - Raw dimension in the room area
    - Default/fallback if no evidence found
    """
    results: Dict[str, Dict[str, Any]] = {}

    for room in rooms:
        label = str(room.get("label") or "")
        room_type = _room_height_type(label)
        allow_f2f = room_type != "floor_to_ceiling"
        polygon = room.get("polygon") or []

        spatial = []
        generic = []
        for ev in all_evidence:
            # Use anchor (centre of bbox) for spatial matching when available.
            # Only evidence with real PDF coordinates (from positioned words)
            # participates in spatial room association.
            if ev.anchor is not None and polygon:
                if _point_in_polygon(ev.anchor, polygon):
                    spatial.append(ev)
                else:
                    generic.append(ev)
            elif ev.bbox and len(ev.bbox) == 4 and polygon:
                cx = (ev.bbox[0] + ev.bbox[2]) / 2.0
                cy = (ev.bbox[1] + ev.bbox[3]) / 2.0
                if _point_in_polygon((cx, cy), polygon):
                    spatial.append(ev)
                else:
                    generic.append(ev)
            else:
                generic.append(ev)

        candidates = []
        for ev in spatial:
            if ev.height_type == "level_reference":
                continue
            if ev.height_type == "floor_to_floor" and not allow_f2f:
                continue
            if room_type in ("floor_to_ceiling", "ceiling_bulkhead"):
                if ev.height_type == "floor_to_floor":
                    continue
            candidates.append(ev)

        if not candidates:
            for ev in generic:
                if ev.extraction_method == "default":
                    candidates.append(ev)
                    break
                # Non-positioned evidence without real bbox must NOT leak
                # across rooms — only spatial or default evidence is safe

        if candidates:
            candidates.sort(key=lambda e: (e.priority, -e.confidence))
            best = candidates[0]
            h = round(best.height_m, 4)
            source = best.extraction_method
            status = best.status
            conf = best.confidence
            ev_id = best.id
        else:
            h = default_height
            source = "default"
            status = "Default/fallback"
            conf = 0.50
            ev_id = ""

        room_key = label or room.get("room_ref") or f"room_{len(results)}"
        results[room_key] = {
            "height_m": h,
            "height_type": room_type,
            "height_status": status,
            "height_confidence": "Verified" if conf >= 0.90 else
                                 "High" if conf >= 0.80 else
                                 "Derived" if conf >= 0.65 else "Review",
            "height_source": source,
            "height_evidence_id": ev_id,
        }

    return results


def _room_height_type(label: str) -> str:
    """Determine the expected height type for a room based on its label."""
    lbl = str(label).upper()
    if any(kw in lbl for kw in ("BED", "LIVING", "LOUNGE", "DINING", "KITCHEN",
                                 "FAMILY", "RUMPUS", "MEDIA", "STUDY", "OFFICE")):
        return "floor_to_ceiling"
    if any(kw in lbl for kw in ("WC", "TOILET", "BATH", "ENSUITE", "SHOWER",
                                 "POWDER", "LAUNDRY")):
        return "floor_to_ceiling"
    if any(kw in lbl for kw in ("CORRIDOR", "HALL", "PASSAGE", "ENTRY",
                                 "FOYER", "VESTIBULE")):
        return "floor_to_ceiling"
    if any(kw in lbl for kw in ("GARAGE", "CARPORT")):
        return "floor_to_floor"
    return "generic"


def _point_in_polygon(point: Tuple[float, float], polygon: Sequence) -> bool:
    """Ray-casting point-in-polygon test."""
    x, y = float(point[0]), float(point[1])
    pts = [(float(p[0]), float(p[1])) for p in polygon]
    n = len(pts)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def store_height_evidence(
    app: Any,
    workspace_id: int,
    records: List[HeightEvidence],
) -> None:
    """Persist height evidence to workspace settings."""
    payload = {
        "version": VERSION,
        "records": [asdict(r) for r in records],
    }
    app.set_workspace_setting(
        int(workspace_id), SETTING_KEY,
        json.dumps(payload, separators=(",", ":")),
    )


def get_height_evidence(app: Any, workspace_id: int) -> List[HeightEvidence]:
    """Retrieve stored height evidence."""
    try:
        raw = app.workspace_setting(int(workspace_id), SETTING_KEY, "{}")
        parsed = json.loads(str(raw or "{}"))
        return [HeightEvidence(**r) for r in parsed.get("records", [])]
    except Exception:
        return []


def get_default_height(app: Any, workspace_id: int) -> float:
    """Get the project default wall height."""
    try:
        return max(0.5, float(
            app.workspace_setting(int(workspace_id), "default_wall_height_m", 2.7)
        ))
    except Exception:
        return 2.7


def _get_room_heights(app: Any, workspace_id: int) -> Dict[str, Dict[str, Any]]:
    """Retrieve stored per-room height map."""
    try:
        raw = app.workspace_setting(
            int(workspace_id), "room_heights_v150", "{}",
        )
        return json.loads(str(raw or "{}"))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Startup wiring — patches v136 to use enhanced resolver
# ---------------------------------------------------------------------------

def apply(app: Any) -> None:
    if getattr(app, "_pb_height_evidence_v150_applied", False):
        return
    app._pb_height_evidence_v150_applied = True

    # Import v136 to patch its functions
    try:
        import pb_elevation_profile_v136 as v136
    except ImportError:
        return

    _original_solve = v136.solve_height_from_text

    def _enhanced_solve(text: Any, default_height: float = 2.7) -> Dict[str, Any]:
        ev_list = extract_all_height_evidence(str(text or ""))
        h, best = resolve_height(
            ev_list, target_type="generic",
            default_height=default_height,
        )
        # v150 is authoritative — NEVER resurrect old v136 RL/dimension results.
        # Build result from v150 exclusively.
        result: Dict[str, Any] = {
            "height_m": round(h, 4),
            "height_evidence": asdict(best),
            "status": best.status,
            "confidence": (
                "Verified" if best.confidence >= 0.90 else
                "High" if best.confidence >= 0.80 else
                "Derived" if best.confidence >= 0.65 else "Review"
            ),
            "source": best.extraction_method,
            "rls": [],
            "dimensions": [],
        }
        return result

    v136.solve_height_from_text = _enhanced_solve

    _original_build = v136.build_profiles

    def _enhanced_buildprofiles(app_obj: Any, workspace_id: int) -> Dict[str, Any]:
        default = get_default_height(app_obj, workspace_id)
        pages = {
            int(r["id"]): dict(r)
            for r in app_obj.lquery(
                "SELECT id,document_id,page_no,extracted_text,"
                "page_label,page_type FROM pages "
                "WHERE workspace_id=?",
                (int(workspace_id),),
            )
        }

        # Try to get positioned words from the PDF for each page
        _page_words: Dict[int, List[WordBox]] = {}
        _pdf_cache: Dict[int, Any] = {}  # document_id → fitz.Document
        try:
            import fitz as _fitz
            for pid, page in pages.items():
                doc_id = int(page.get("document_id") or 0)
                page_no = int(page.get("page_no") or 1)
                if doc_id not in _pdf_cache:
                    docs = app_obj.lquery(
                        "SELECT path FROM documents WHERE id=?",
                        (doc_id,),
                    )
                    if docs and docs[0].get("path"):
                        try:
                            _pdf_cache[doc_id] = _fitz.open(docs[0]["path"])
                        except Exception:
                            _pdf_cache[doc_id] = None
                    else:
                        _pdf_cache[doc_id] = None
                doc = _pdf_cache.get(doc_id)
                if doc and page_no - 1 < len(doc):
                    try:
                        fitz_page = doc[page_no - 1]
                        words_raw = fitz_page.get_text("words")
                        _page_words[pid] = [
                            WordBox(
                                text=str(w[4]),
                                x0=float(w[0]), y0=float(w[1]),
                                x1=float(w[2]), y1=float(w[3]),
                                page_id=pid,
                                line_id=int(w[6]) if len(w) > 6 else 0,
                            )
                            for w in words_raw
                            if str(w[4]).strip()
                        ]
                    except Exception:
                        pass
        except ImportError:
            pass  # PyMuPDF not available — fall back to plain text

        all_evidence: List[HeightEvidence] = []
        for pid, page in pages.items():
            words = _page_words.get(pid)
            if words:
                ev_list = extract_all_height_evidence(
                    words, page_id=pid,
                    page_label=str(page.get("page_label") or ""),
                    page_type=str(page.get("page_type") or ""),
                )
            else:
                text = str(page.get("extracted_text") or "")
                ev_list = extract_all_height_evidence(
                    text, page_id=pid,
                    page_label=str(page.get("page_label") or ""),
                    page_type=str(page.get("page_type") or ""),
                )
            all_evidence.extend(ev_list)

        # Clean up PDF handles
        for doc in _pdf_cache.values():
            if doc:
                try:
                    doc.close()
                except Exception:
                    pass

        store_height_evidence(app_obj, workspace_id, all_evidence)

        reg = (app_obj.register_elevations_v135(int(workspace_id))
               if hasattr(app_obj, "register_elevations_v135")
               else {"elevations": []})

        profiles = []
        for item in reg.get("elevations") or []:
            pid = int(item.get("page_id") or 0)
            side = str(item.get("orientation") or "")
            page = pages.get(pid, {})
            words = _page_words.get(pid)
            if words:
                ev_list = extract_all_height_evidence(
                    words, page_id=pid,
                    page_label=str(page.get("page_label") or ""),
                    page_type=str(page.get("page_type") or ""),
                )
            else:
                ev_list = extract_all_height_evidence(
                    str(page.get("extracted_text") or ""),
                    page_id=pid,
                    page_label=str(page.get("page_label") or ""),
                )
            side_ev = [e for e in ev_list if e.source_page_id == pid]
            h, best = resolve_height(
                side_ev, target_type="generic",
                allow_floor_to_floor=True, side=side,
                default_height=default,
            )
            profiles.append({
                "side": side,
                "page_id": item.get("page_id"),
                "page_label": item.get("page_label"),
                "height_m": round(h, 4),
                "status": best.status,
                "confidence": "Verified" if best.confidence >= 0.90 else
                              "High" if best.confidence >= 0.80 else
                              "Derived" if best.confidence >= 0.65 else "Review",
                "rls": [],
                "dimensions": [],
                "height_evidence_id": best.id,
            })

        # --- BLOCKER 2: resolve per-room heights from Priority 2 polygons ---
        room_height_map: Dict[str, Dict[str, Any]] = {}
        try:
            import pb_room_face_takeoff as _rft
            for pid, page in pages.items():
                page_type = str(page.get("page_type") or "").lower()
                if "floor" not in page_type and "partition" not in page_type:
                    continue
                try:
                    room_faces = _rft.extract_room_faces_from_page(app_obj, page)
                    if not room_faces:
                        continue
                    # Convert RoomFace → dict format expected by resolve_room_heights
                    rooms_for_resolver = []
                    for rf in room_faces:
                        rooms_for_resolver.append({
                            "label": rf.label or rf.room_ref,
                            "room_ref": rf.room_ref,
                            "polygon": rf.polygon_pdf_pts,
                        })
                    # Get evidence for this page
                    words = _page_words.get(pid)
                    if words:
                        page_ev = extract_all_height_evidence(
                            words, page_id=pid,
                            page_label=str(page.get("page_label") or ""),
                            page_type=str(page.get("page_type") or ""),
                        )
                    else:
                        page_ev = extract_all_height_evidence(
                            str(page.get("extracted_text") or ""),
                            page_id=pid,
                            page_label=str(page.get("page_label") or ""),
                            page_type=str(page.get("page_type") or ""),
                        )
                    resolved = resolve_room_heights(
                        rooms_for_resolver, page_ev,
                        default_height=default,
                    )
                    room_height_map.update(resolved)
                except Exception:
                    continue  # don't break production if room height resolution fails
        except ImportError:
            pass  # room_face_takeoff not available

        # Store room height map for downstream consumers
        if room_height_map:
            app_obj.set_workspace_setting(
                int(workspace_id), "room_heights_v150",
                json.dumps(room_height_map, separators=(",", ":")),
            )

        payload = {
            "version": VERSION,
            "profiles": profiles,
            "room_heights": room_height_map,
        }
        app_obj.set_workspace_setting(
            int(workspace_id), "elevation_profiles_v136",
            json.dumps(payload, separators=(",", ":")),
        )
        return payload

    v136.build_profiles = _enhanced_buildprofiles

    # Wire public API onto app
    app.extract_height_evidence_v150 = extract_all_height_evidence
    app.resolve_height_v150 = resolve_height
    app.resolve_room_heights_v150 = resolve_room_heights
    app.get_height_evidence_v150 = lambda wid: get_height_evidence(app, int(wid))
    app.get_default_height_v150 = lambda wid: get_default_height(app, int(wid))
    app.store_height_evidence_v150 = lambda wid, records: store_height_evidence(
        app, int(wid), records,
    )
    app.extract_section_heights_v150 = _extract_section_heights
    app.get_room_heights_v150 = lambda wid: _get_room_heights(app, int(wid))
