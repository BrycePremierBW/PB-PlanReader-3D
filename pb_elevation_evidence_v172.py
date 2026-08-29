"""PlanReader v1.7.2 elevation evidence correlation — Phase B3.

Detects door/window rectangular regions from elevation drawings and
correlates them with B1 plan-vector instances.  The primary contribution
is providing HEIGHT (which plan view cannot measure).

Safety contract:
  - Elevation evidence NEVER creates new instances.  It enriches only
    existing B1/B2 OpeningEvidence records via merge_opening_evidence().
  - Elevation evidence NEVER sets deduct=True.
  - Elevation rectangles remain dimension_basis=unknown unless explicit
    wall-void evidence is registered (generic elevation_rect does NOT
    manufacture rough_opening authority).
  - Matching requires at least one strong instance-specific signal
    (compatible width + matching mark, or compatible width + validated
    position).  A generic side match alone is NOT sufficient.
  - Conflicting D/W marks (D01 vs W01) hard-reject the match.
  - Unmatched elevation evidence is discarded (not carried forward).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pb_opening_evidence_v170 import (
    OpeningEvidence,
    merge_opening_evidence,
    record_decision_reason,
    DIMENSION_BASIS_UNKNOWN,
    DEDUCTION_REVIEW,
    NON_INSTANCE_SOURCES,
    OPENING_TYPE_DOOR,
    OPENING_TYPE_WINDOW,
    TOLERANCE_WIDTH_M,
    TOLERANCE_POSITION_M,
)

VERSION = "1.7.2"

# ---------------------------------------------------------------------------
# Tolerances for elevation ↔ plan matching
# ---------------------------------------------------------------------------
# Elevation rectangles measure the visible opening (frame or leaf),
# which is typically slightly smaller than the plan rough opening.
# Allow a reasonable tolerance for cross-view width agreement.
ELEVATION_WIDTH_TOLERANCE_M = 0.15   # 150mm cross-view width tolerance
ELEVATION_POSITION_TOLERANCE_M = 0.25  # 250mm along-wall (less precise than B0)
ELEVATION_HEIGHT_CONFIDENCE = 0.70   # confidence for elevation-derived height
ELEVATION_DIM_BASIS_CONFIDENCE = 0.65  # confidence when basis set from elevation

# Size limits for elevation opening candidates (in metres)
_MIN_OPENING_WIDTH_M = 0.3
_MAX_OPENING_WIDTH_M = 6.0
_MIN_OPENING_HEIGHT_M = 0.3
_MAX_OPENING_HEIGHT_M = 5.0

# Physical label search radius (metres) — labels sit ~0.5m from opening
_LABEL_SEARCH_RADIUS_M = 0.5

# Minimum strong signal score for a match to qualify
_MIN_STRONG_SIGNAL = 0.3

# Mark label patterns — align with approved B1 families:
#   doors:   D, ED, ID  followed by 1-3 digits
#   windows: W, EW, IW  followed by 1-3 digits
# Full-token matching (word boundaries) rejects junk suffixes such as
# ``ED01XYZ`` or Roman-numeral-like ``I``/``II``; prefixes are matched
# longest-first so ``ED01`` is not misread as ``D01``.
_MARK_RE = re.compile(
    r"\b((?:ED|ID|EW|IW|D|W)\d{1,3})\b", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# ElevationOpening — one detected rectangular opening from an elevation
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ElevationOpening:
    """One detected rectangular opening from an elevation drawing.

    Represents a geometric candidate from an elevation page, NOT a
    confirmed physical instance.  Must be correlated with a B1 plan
    instance before contributing to the opening register.
    """
    elevation_page_no: int
    elevation_side: str       # "North", "South", "East", "West"
    bbox_px: Tuple[float, float, float, float]  # (x0, y0, x1, y1) in page coords
    width_m: float
    height_m: float
    sill_m: Optional[float] = None   # None until elevation datum registered
    head_m: Optional[float] = None   # None until elevation datum registered
    label: str = ""           # nearby mark text (D01, W01) if detected
    confidence: float = 0.5
    extraction_method: str = "elevation_rect"
    # --- Phase 2A provenance / coordinate-space (ADDITIVE, defaulted) ---
    coord_space: str = "render_pixel"   # explicit: "pdf_point" | "render_pixel"
    render_dpi: Optional[float] = None  # when coord_space == "render_pixel"
    source_page_id: Optional[int] = None  # workspace page id if known
    source_page_no: Optional[int] = None  # 1-based source page number
    drawing_ref: str = ""                 # e.g. "CD3001"
    drawing_title: str = ""               # e.g. "E1 EAST ELEVATION"
    level: Optional[str] = None           # level band when objectively derived
    wall_ref: str = ""                    # candidate wall association
    calibration: Dict[str, Any] = field(default_factory=dict)  # calibration provenance
    # --- B3 correlation diagnostics (ADDITIVE, defaulted) ---
    # Recorded by correlate_elevation_to_plan() so the WHY of a match /
    # rejection / ambiguity / non-creation is inspectable.  Diagnostic-only:
    # never influences matching or enrichment decisions.  The dataclass stays
    # frozen for geometry; this list is mutated as an immutable-container
    # ledger (items are appended, the object itself is never replaced).
    correlation_diagnostics: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Elevation detection: raw PDF geometry → ElevationOpening candidates
# ---------------------------------------------------------------------------
def _is_opening_sized(width_m: float, height_m: float) -> bool:
    """True if dimensions are within plausible door/window size ranges."""
    if width_m < _MIN_OPENING_WIDTH_M or width_m > _MAX_OPENING_WIDTH_M:
        return False
    if height_m < _MIN_OPENING_HEIGHT_M or height_m > _MAX_OPENING_HEIGHT_M:
        return False
    return True


def _word_position(word: Dict[str, Any]) -> Tuple[Optional[str], Optional[float], Optional[float]]:
    """Normalize a word record to (text, center_x, center_y).

    Accepts the current native word format ``{"text": ..., "bbox": [x0,y0,x1,y1]}``
    (v130) AND the legacy numeric-key format ``{0,1,2,3,4}``.  Returns
    (None, None, None) for an unrecognized record.
    """
    bbox = word.get("bbox")
    text = str(word.get("text") or word.get("4", "")).strip()
    if bbox and isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        try:
            x0, y0, x1, y1 = map(float, bbox[:4])
            return (text, (x0 + x1) / 2.0, (y0 + y1) / 2.0)
        except (TypeError, ValueError):
            return (text, None, None)
    if not text and "0" not in word and "1" not in word:
        return (None, None, None)
    try:
        wx = (float(word.get("0", 0)) + float(word.get("2", 0))) / 2.0
        wy = (float(word.get("1", 0)) + float(word.get("3", 0))) / 2.0
    except (TypeError, ValueError, IndexError):
        return (text, None, None)
    return (text, wx, wy)


def _is_approved_door_mark(mark: str) -> bool:
    return mark in ("D", "ED", "ID") or re.fullmatch(r"(?:D|ED|ID)\d{1,3}", mark) is not None


def _is_approved_window_mark(mark: str) -> bool:
    return mark in ("W", "EW", "IW") or re.fullmatch(r"(?:W|EW|IW)\d{1,3}", mark) is not None


def _extract_label_near_rect(
    words: Sequence[Dict[str, Any]],
    rect_bbox: Tuple[float, float, float, float],
    max_distance_px: float = 120.0,
) -> str:
    """Find a door/window mark label near (or inside) an elevation rectangle.

    Searches words within max_distance_px of the rectangle boundary.
    Distance is measured from the word center to the nearest edge of the
    rectangle (not center), since labels typically sit above/below the
    opening.  Returns the closest valid approved mark found, or empty string.

    Word records may use the current native ``{"text","bbox"}`` format or the
    legacy numeric-key format.  Marks must be an approved door/window family
    (D/ED/ID for doors; W/EW/IW for windows); junk tokens are rejected.
    """
    rx0, ry0, rx1, ry1 = rect_bbox
    r_left, r_right = min(rx0, rx1), max(rx0, rx1)
    r_top, r_bottom = min(ry0, ry1), max(ry0, ry1)
    best_label = ""
    best_dist = max_distance_px + 1.0

    for word in words:
        text, wx, wy = _word_position(word)
        if not text:
            continue
        m = _MARK_RE.search(text)
        if not m:
            continue
        mark = m.group(1).upper()
        # Must be an approved door or window family (not junk / not I / II).
        if not (_is_approved_door_mark(mark) or _is_approved_window_mark(mark)):
            continue
        if wx is None or wy is None:
            continue
        # Distance from word center to nearest edge of rectangle
        dx = max(r_left - wx, 0, wx - r_right)
        dy = max(r_top - wy, 0, wy - r_bottom)
        dist = (dx ** 2 + dy ** 2) ** 0.5
        if dist < best_dist:
            best_dist = dist
            best_label = mark

    return best_label


def detect_elevation_openings(
    elevation_page_no: int,
    elevation_side: str,
    rects: Sequence[Dict[str, Any]],
    words: Sequence[Dict[str, Any]],
    units_per_m: float,
    *,
    coord_space: str = "render_pixel",
    render_dpi: Optional[float] = None,
    source_page_id: Optional[int] = None,
    source_page_no: Optional[int] = None,
    drawing_ref: str = "",
    drawing_title: str = "",
    level: Optional[str] = None,
    wall_ref: str = "",
    calibration: Optional[Dict[str, Any]] = None,
) -> List[ElevationOpening]:
    """Detect opening-sized rectangular regions on an elevation drawing.

    Args:
        elevation_page_no: Page number of the elevation drawing.
        elevation_side: Cardinal side ("North", "South", etc.).
        rects: Rectangle dicts with keys "bbox" → [x0, y0, x1, y1] in the
               calibration's coordinate units.  May also have "confidence",
               and provenance keys ("coord_space", "level", "wall_ref",
               "drawing_ref", "calibration", ...).
        words: Word dicts from PDF extraction (positioned text).
        units_per_m: Measured calibration factor (units per metre) expressed
            in ``coord_space`` units — render pixels when coord_space is
            "render_pixel", PDF points when coord_space is "pdf_point".
            This value is NEVER described as pixels-per-metre unless
            coord_space == "render_pixel" (see Calibration.pt_per_m /
            Calibration.px_per_m for the typed accessors).
        coord_space: The explicit coordinate space of BOTH the calibration
            factor above AND the rect bboxes: "render_pixel" or "pdf_point".
        render_dpi: Render DPI when coord_space == "render_pixel".
        source_page_id / source_page_no / drawing_ref / drawing_title /
        level / wall_ref / calibration: Provenance carried onto every
            candidate.

    Coordinate-space compatibility (HARD GUARD):
        Dimensioned candidates are only produced when the rect's declared
        coordinate space is PROVEN identical to the calibration's space
        (``coord_space``).  Any rect that declares a different space than the
        calibration (e.g. a pdf_point rect against a render-pixel calibration,
        or vice versa) FAILS CLOSED — it produces NO dimensional candidate and
        is skipped.  Coordinate spaces are never mixed.

    Returns:
        List of ElevationOpening candidates.  These are NOT confirmed
        instances — they must be correlated with B1 plan data.

    Sill/head are None until an elevation datum/baseline is registered.
    """
    if units_per_m <= 0:
        return []

    # Convert physical label search radius to calibration units
    label_radius_units = _LABEL_SEARCH_RADIUS_M * units_per_m

    candidates: List[ElevationOpening] = []
    for rect in rects:
        bbox = rect.get("bbox")
        if not bbox or len(bbox) < 4:
            continue
        x0, y0, x1, y1 = [float(v) for v in bbox[:4]]

        # Hard coordinate-space guard: the rect MUST be in the same proven
        # coordinate space as the calibration before any dimension is
        # calculated.  A rect declaring a different space fails closed — it
        # contributes no dimensional candidate (spaces are never mixed).
        r_coord = rect.get("coord_space", coord_space)
        if r_coord != coord_space:
            # Dimensionally incommensurable with the calibration space:
            # fail closed, do NOT emit a candidate with invented metres.
            continue

        # Measure in metres (space is known equal to the calibration space)
        width_units = abs(x1 - x0)
        height_units = abs(y1 - y0)
        width_m = width_units / units_per_m
        height_m = height_units / units_per_m

        if not _is_opening_sized(width_m, height_m):
            continue

        # Look for a mark label near this rectangle (scale-aware radius)
        label = _extract_label_near_rect(
            words, (x0, y0, x1, y1), max_distance_px=label_radius_units
        )

        # Confidence: higher for clearly opening-sized, lower for borderline
        conf = 0.5
        if 0.6 <= width_m <= 4.0 and 0.6 <= height_m <= 3.5:
            conf = 0.65
        if label:
            conf += 0.1  # label boost
        conf = min(conf, 0.85)

        # Per-rect provenance, falling back to the function-level args so
        # rects produced by the extraction layer carry their own metadata.
        r_render = rect.get("render_dpi", render_dpi) if rect.get("render_dpi") is not None else render_dpi
        r_level = rect.get("level", level)
        r_wall = rect.get("wall_ref", wall_ref)
        r_draw = rect.get("drawing_ref", drawing_ref)
        r_calib = rect.get("calibration", calibration) or calibration or {}

        candidates.append(ElevationOpening(
            elevation_page_no=elevation_page_no,
            elevation_side=elevation_side,
            bbox_px=(x0, y0, x1, y1),
            width_m=round(width_m, 4),
            height_m=round(height_m, 4),
            label=label,
            confidence=round(conf, 2),
            coord_space=r_coord,
            render_dpi=r_render,
            source_page_id=source_page_id,
            source_page_no=source_page_no or elevation_page_no,
            drawing_ref=r_draw,
            drawing_title=drawing_title,
            level=r_level,
            wall_ref=r_wall,
            calibration=dict(r_calib),
        ))

    return candidates


# ---------------------------------------------------------------------------
# Correlation: match elevation candidates to B1/B2 plan instances
# ---------------------------------------------------------------------------
def _width_compatible(w1: Optional[float], w2: Optional[float]) -> bool:
    """True if two widths agree within cross-view tolerance."""
    if w1 is None or w2 is None:
        return True  # can't disagree if one is unknown
    return abs(w1 - w2) <= ELEVATION_WIDTH_TOLERANCE_M


def _mark_compatible(mark1: str, mark2: str) -> bool:
    """True if two type marks are compatible (exact match or both empty)."""
    if not mark1 or not mark2:
        return True  # can't disagree if one is unknown
    return mark1.upper() == mark2.upper()


def _mark_family_prefix(mark: str) -> str:
    """Return 'D' or 'W' for an approved door/window mark family.

    Uses the approved mark-family classifier, NEVER the first character:
        D / ED / ID (+ digits)  => 'D'  (door)
        W / EW / IW (+ digits)  => 'W'  (window)
    Anything else (junk, lone I/II, etc.) => ''.

    Rationale: ``ED01`` must classify as a DOOR (family 'D'), not as an
    unknown 'E'; ``IW03`` must classify as a WINDOW ('W').  Using ``mark[0]``
    would misclassify every ED/ID/EW/IW mark.
    """
    if mark and _is_approved_door_mark(mark):
        return "D"
    if mark and _is_approved_window_mark(mark):
        return "W"
    return ""


def _mark_type(mark: str) -> str:
    """Return the type prefix of a mark: 'D', 'W', or ``''``.

    Uses the approved mark-family classifier (_mark_family_prefix), so
    ED01/ID01 are 'D' (door) and EW01/IW01 are 'W' (window).
    """
    return _mark_family_prefix(mark)


def _marks_conflict(inst_mark: str, elev_label: str) -> bool:
    """True if both marks are non-empty and disagree.

    Any nonblank mismatch is a conflict — D01 vs D02, W01 vs W03,
    D01 vs W01, etc.  Type marks are type identity, not just
    door-vs-window category.
    """
    if not inst_mark or not elev_label:
        return False
    return inst_mark.upper() != elev_label.upper()


def _opening_type_conflicts(inst: OpeningEvidence, elev_label: str) -> bool:
    """True if the instance's opening_type contradicts the elevation mark.

    For example, a window instance (opening_type=window) should not
    correlate with a D01 (door) elevation label.
    """
    if not elev_label:
        return False
    elev_type = _mark_family_prefix(elev_label)
    if inst.opening_type == OPENING_TYPE_WINDOW and elev_type == "D":
        return True
    if inst.opening_type == OPENING_TYPE_DOOR and elev_type == "W":
        return True
    return False


def _correlation_assessment(
    inst: OpeningEvidence,
    elev: ElevationOpening,
) -> Tuple[float, Dict[str, Any]]:
    """Structured correlation assessment for a (plan instance, elevation) pair.

    Returns ``(score, details)``:
      - score: 0.0-1.0 (higher = better match).  0.0 for incompatible pairs
        or insufficient identity evidence — identical to ``_correlation_score``.
      - details: a structured dict of WHY the pair matched / was rejected
        (diagnostic-only, never used in the decision itself):
          rejection_reason     : str|None   — explicit hard-reject reason
          correlation_score    : float
          mark_evidence        : str|None   — exact_plan_mark_match,
                                              elevation_label_only,
                                              plan_mark_only, none, conflict
          side_match           : bool|None  — True/False/None (no side data)
          level_match          : bool|None  — True/False/None (no level data)
          width_match_m        : float|None — |plan.width - elev.width| diff
          opening_type_compatible : bool

    Qualification requires BOTH:
      - compatible width (baseline check), AND
      - at least one identity signal: exact mark match, OR
        registered side match with opening_type compatibility.

    Width alone is NOT sufficient — common 820mm doors repeat many times.
    """
    details: Dict[str, Any] = {
        "rejection_reason": None,
        "correlation_score": 0.0,
        "mark_evidence": None,
        "side_match": None,
        "level_match": None,
        "width_match_m": None,
        "opening_type_compatible": True,
    }
    score = 0.0
    has_width_signal = False
    has_identity_signal = False

    def _reject(reason: str) -> Tuple[float, Dict[str, Any]]:
        details["rejection_reason"] = reason
        details["correlation_score"] = 0.0
        return 0.0, details

    # --- Hard reject: conflicting marks (any nonblank mismatch) ---
    if _marks_conflict(inst.type_mark, elev.label):
        details["mark_evidence"] = "conflict"
        return _reject(
            "mark_conflict "
            f"(plan '{inst.type_mark}' vs elevation '{elev.label}')"
        )

    # --- Hard reject: opening_type vs mark type conflict ---
    if _opening_type_conflicts(inst, elev.label):
        details["opening_type_compatible"] = False
        return _reject(
            "opening_type_conflict "
            f"(plan '{inst.opening_type}' vs mark '{elev.label}')"
        )

    # --- Hard reject: different sides ---
    if inst.elevation_side and elev.elevation_side:
        if inst.elevation_side != elev.elevation_side:
            details["side_match"] = False
            return _reject(
                "wrong_side "
                f"(plan '{inst.elevation_side}' vs elevation '{elev.elevation_side}')"
            )
        details["side_match"] = True
    else:
        details["side_match"] = None

    # --- Hard reject: different known levels ---
    # When BOTH sources carry a reliable level, different levels hard-reject
    # the association.  An unknown level is NEUTRAL: it never becomes a
    # positive match signal and never rejects.
    inst_level = str(inst.level or "").strip()
    elev_level = str(elev.level or "").strip()
    if inst_level and elev_level:
        details["level_match"] = inst_level.upper() == elev_level.upper()
        if not details["level_match"]:
            return _reject(
                "wrong_level "
                f"(plan '{inst_level}' vs elevation '{elev_level}')"
            )
    else:
        details["level_match"] = None

    # --- Side match: contextual evidence (not sufficient alone) ---
    side_match = details["side_match"] is True
    if side_match:
        score += 0.15

    # --- Width agreement: baseline compatibility (not identity) ---
    if inst.width_m is not None:
        if not _width_compatible(inst.width_m, elev.width_m):
            details["width_match_m"] = round(inst.width_m - elev.width_m, 4)
            return _reject(
                "width_mismatch "
                f"(plan {inst.width_m:.3f}m vs elevation {elev.width_m:.3f}m, "
                f"diff {abs(inst.width_m - elev.width_m):.3f}m "
                f"over tolerance {ELEVATION_WIDTH_TOLERANCE_M:.2f}m)"
            )
        diff = abs(inst.width_m - elev.width_m)
        details["width_match_m"] = round(diff, 4)
        width_score = 0.30 * max(0, 1.0 - diff / ELEVATION_WIDTH_TOLERANCE_M)
        score += width_score
        has_width_signal = width_score > 0
    else:
        details["width_match_m"] = None

    # --- Mark match: strong identity signal ---
    if inst.type_mark and elev.label:
        if inst.type_mark.upper() == elev.label.upper():
            details["mark_evidence"] = "exact_plan_mark_match"
            score += 0.40
            has_identity_signal = True
        else:
            details["mark_evidence"] = "mark_mismatch"
    elif elev.label:
        details["mark_evidence"] = "elevation_label_only"
    elif inst.type_mark:
        details["mark_evidence"] = "plan_mark_only"
    else:
        details["mark_evidence"] = "none"

    # --- Side + width as weaker identity signal ---
    if side_match and has_width_signal and not has_identity_signal:
        has_identity_signal = True
        score += 0.15  # side+width combined is moderate identity

    # --- Require width baseline + identity signal ---
    if not has_width_signal:
        return _reject(
            "no_width_signal (plan width missing or incompatible)"
        )
    if not has_identity_signal:
        return _reject(
            "no_identity_signal (no mark match and no side+width identity)"
        )

    details["correlation_score"] = round(min(score, 1.0), 4)
    return min(score, 1.0), details


def _correlation_score(
    inst: OpeningEvidence,
    elev: ElevationOpening,
) -> float:
    """Score how well a plan instance matches an elevation candidate.

    Returns 0.0-1.0 (higher = better match).  Returns 0.0 for
    incompatible pairs or insufficient identity evidence.

    This is the score-only façade over ``_correlation_assessment``; the
    structured WHY of the decision is available via that function.
    """
    return _correlation_assessment(inst, elev)[0]


def _find_unique_best_pairs(
    pairs: List[Tuple[float, int, int]],
) -> List[Tuple[float, int, int]]:
    """Filter pairs to only those that are uniquely best for both sides.

    A pair qualifies when:
      - Its score is strictly greater than any other pair involving the same
        plan instance OR the same elevation candidate.
    Equal scores for the same elevation/plan → ambiguity → no match.
    """
    # Per plan instance: best score, and count of pairs at that score
    plan_best: Dict[int, Tuple[float, int]] = {}  # p_idx → (best_score, count_at_best)
    # Per elevation candidate: best score, and count of pairs at that score
    elev_best: Dict[int, Tuple[float, int]] = {}  # e_idx → (best_score, count_at_best)

    # Sort by score descending first to ensure we see the best first
    sorted_pairs = sorted(pairs, key=lambda x: x[0], reverse=True)

    for sc, p_idx, e_idx in sorted_pairs:
        # Track plan instance best
        if p_idx not in plan_best or sc > plan_best[p_idx][0]:
            plan_best[p_idx] = (sc, 1)
        elif sc == plan_best[p_idx][0]:
            plan_best[p_idx] = (sc, plan_best[p_idx][1] + 1)

        # Track elevation candidate best
        if e_idx not in elev_best or sc > elev_best[e_idx][0]:
            elev_best[e_idx] = (sc, 1)
        elif sc == elev_best[e_idx][0]:
            elev_best[e_idx] = (sc, elev_best[e_idx][1] + 1)

    # A pair qualifies only if it's uniquely best for BOTH sides
    qualified: List[Tuple[float, int, int]] = []
    for sc, p_idx, e_idx in sorted_pairs:
        if (plan_best[p_idx][0] == sc and plan_best[p_idx][1] == 1
                and elev_best[e_idx][0] == sc and elev_best[e_idx][1] == 1):
            qualified.append((sc, p_idx, e_idx))

    return qualified


# ---------------------------------------------------------------------------
# B3 correlation diagnostics helpers (ADDITIVE, diagnostic-only)
# ---------------------------------------------------------------------------
def _record_elev_rejection(
    elev: ElevationOpening,
    *,
    plan_instance_id: Optional[str],
    plan_mark: str = "",
    score: float = 0.0,
    reason: str,
    **extra: Any,
) -> None:
    """Record a structured rejection reason on an elevation candidate.

    Diagnostic-only ledger appended to ``ElevationOpening.correlation_diagnostics``
    so an unmatched / hard-rejected elevation explains WHY it was not matched
    (wrong level, wrong side, mark/type conflict, width mismatch, ambiguity,
    no plan instance, ...).  Never influences matching decisions.
    """
    rec: Dict[str, Any] = {
        "plan_instance_id": plan_instance_id,
        "plan_mark": plan_mark or "",
        "correlation_score": round(float(score), 4),
        "match_decided": False,
        "rejection_reason": reason,
    }
    for k, v in extra.items():
        if v is not None:
            rec[k] = v
    elev.correlation_diagnostics.append(rec)


def _record_elev_match(
    elev: ElevationOpening,
    *,
    plan_instance_id: str,
    plan_mark: str = "",
    score: float,
    **extra: Any,
) -> None:
    """Record a structured acceptance marker on an elevation candidate."""
    rec: Dict[str, Any] = {
        "plan_instance_id": plan_instance_id,
        "plan_mark": plan_mark or "",
        "correlation_score": round(float(score), 4),
        "match_decided": True,
    }
    for k, v in extra.items():
        if v is not None:
            rec[k] = v
    elev.correlation_diagnostics.append(rec)


def correlate_elevation_to_plan(
    elevation_openings: Sequence[ElevationOpening],
    plan_instances: Sequence[OpeningEvidence],
    unmatched_strategy: str = "discard",
) -> Tuple[List[OpeningEvidence], List[ElevationOpening]]:
    """Match elevation candidates to B1/B2 plan instances and enrich.

    Uses unique-best greedy assignment (order-independent, ambiguity-safe):
      1. Build ALL eligible (score, plan_idx, elev_idx) triples.
      2. Filter to pairs that are uniquely best for both the plan instance
         and the elevation candidate (no tied scores → no ambiguity).
      3. Greedily assign: each plan instance and each elevation candidate
         can be matched at most once.
      4. Matched instances are enriched with elevation height/geometry.
      5. Unmatched elevation candidates are returned separately.

    Ambiguity is rejected, not broken arbitrarily:
      - If two plan instances score equally for one elevation → unmatched.
      - If two elevations score equally for one plan → unmatched.

    B3 diagnostics (ADDITIVE): every outcome is recorded without changing
    any decision —
      - Accepted matches write a structured decision record on the enriched
        instance (``decision_reasons``) AND diagnostic keys on the elevation
        observation in ``source_observations`` (correlation score, mark/side/
        level/width evidence, dimension basis).
      - Hard-rejected / unmatched / ambiguous pairs record an explicit
        ``rejection_reason`` on the elevation candidate's
        ``correlation_diagnostics`` and a structured decision record on the
        plan instance's ``decision_reasons``.

    Args:
        elevation_openings: Detected rectangles from elevation drawings.
        plan_instances: Existing B1/B2 OpeningEvidence instances.
        unmatched_strategy: "discard" (default) — unmatched elevations
                           are not carried forward.

    Returns:
        (enriched_instances, unmatched_elevations)
        enriched_instances: All plan instances, with matched ones enriched.
        unmatched_elevations: Elevation candidates that didn't match.
    """
    if not elevation_openings:
        # No elevation evidence at all — nothing to correlate or explain.
        return list(plan_instances), list(elevation_openings)

    if not plan_instances:
        # Elevation candidates with NO plan instance: B3 NEVER creates an
        # instance.  Record that explicitly so the "why" is inspectable.
        for elev in elevation_openings:
            _record_elev_rejection(
                elev,
                plan_instance_id=None,
                reason="no_plan_instance (B3 correlates elevation evidence "
                       "to existing B1/B2 instances and NEVER creates instances)",
                instance_creation_authority=False,
            )
        return list(plan_instances), list(elevation_openings)

    # Step 1: Build ALL pairs with scores + structured assessments.
    # ``all_pairs`` retains rejected pairs too so we can record the WHY.
    all_pairs: List[Tuple[float, int, int, Dict[str, Any]]] = []
    for p_idx, inst in enumerate(plan_instances):
        for e_idx, elev in enumerate(elevation_openings):
            sc, details = _correlation_assessment(inst, elev)
            all_pairs.append((sc, p_idx, e_idx, details))

    # Eligible pairs (qualify for matching)
    eligible_triples: List[Tuple[float, int, int]] = [
        (sc, p_idx, e_idx)
        for sc, p_idx, e_idx, _details in all_pairs
        if sc >= _MIN_STRONG_SIGNAL
    ]

    # Step 1b: record structured rejections for every non-eligible pair.
    for sc, p_idx, e_idx, details in all_pairs:
        if sc >= _MIN_STRONG_SIGNAL:
            continue
        inst = plan_instances[p_idx]
        elev = elevation_openings[e_idx]
        reason = details.get("rejection_reason") or (
            f"score_below_minimum ({sc:.2f} < {_MIN_STRONG_SIGNAL:.2f})"
        )
        _record_elev_rejection(
            elev,
            plan_instance_id=inst.opening_instance_id,
            plan_mark=inst.type_mark,
            score=sc,
            reason=reason,
        )
        record_decision_reason(
            inst,
            stage="B3",
            outcome="rejected",
            source="elevation_rect",
            elevation_page_no=elev.elevation_page_no,
            elevation_side=elev.elevation_side,
            drawing_ref=elev.drawing_ref,
            coord_space=elev.coord_space,
            correlation_score=round(sc, 4),
            rejection_reason=reason,
            candidate_source=elev.extraction_method,
            deduction_authority=False,
            instance_creation_authority=False,
        )

    # Step 2: Filter to uniquely-best pairs (reject ambiguity)
    qualified = _find_unique_best_pairs(eligible_triples)

    # Step 2b: ambiguity detection — best score tied for either side.
    plan_best: Dict[int, Tuple[float, int]] = {}
    elev_best: Dict[int, Tuple[float, int]] = {}
    for sc, p_idx, e_idx in eligible_triples:
        if p_idx not in plan_best or sc > plan_best[p_idx][0]:
            plan_best[p_idx] = (sc, 1)
        elif sc == plan_best[p_idx][0]:
            plan_best[p_idx] = (sc, plan_best[p_idx][1] + 1)
        if e_idx not in elev_best or sc > elev_best[e_idx][0]:
            elev_best[e_idx] = (sc, 1)
        elif sc == elev_best[e_idx][0]:
            elev_best[e_idx] = (sc, elev_best[e_idx][1] + 1)

    # Step 3: Greedy one-to-one assignment on qualified pairs
    qualified.sort(key=lambda x: x[0], reverse=True)
    assigned_plan: set = set()
    assigned_elev: set = set()
    assignments: Dict[int, int] = {}  # plan_idx → elev_idx

    for sc, p_idx, e_idx in qualified:
        if p_idx in assigned_plan or e_idx in assigned_elev:
            continue
        assignments[p_idx] = e_idx
        assigned_plan.add(p_idx)
        assigned_elev.add(e_idx)

    # Step 3b: record ambiguity for unassigned plan instances whose best
    # eligible score was tied (they were deliberately left unmatched).
    for p_idx, inst in enumerate(plan_instances):
        if p_idx in assignments:
            continue
        if p_idx not in plan_best:
            continue  # no eligible pairs at all — rejection already recorded
        best_sc, count_at_best = plan_best[p_idx]
        if count_at_best > 1:
            tied_elevs = [
                elevation_openings[e_idx].elevation_page_no
                for sc, _p, e_idx in eligible_triples
                if _p == p_idx and sc == best_sc
            ]
            reason = (
                f"ambiguous_tie ({count_at_best} elevation candidates tied "
                f"at score {best_sc:.4f} — no unique best)"
            )
            record_decision_reason(
                inst,
                stage="B3",
                outcome="unmatched",
                source="elevation_rect",
                ambiguity_reason="ambiguous_tie",
                rejection_reason=reason,
                review_required_reason=(
                    "ambiguous elevation tie; instance left unmatched and "
                    "stays review pending manual reconciliation"
                ),
                correlation_score=round(best_sc, 4),
                tied_elevation_pages=_ordered_unique(tied_elevs),
                deduction_authority=False,
                instance_creation_authority=False,
            )

    # Step 3c: record ambiguity on unassigned elevation candidates.
    for e_idx, elev in enumerate(elevation_openings):
        if e_idx in assigned_elev:
            continue
        if e_idx not in elev_best:
            continue
        best_sc, count_at_best = elev_best[e_idx]
        if count_at_best > 1:
            reason = (
                f"ambiguous_tie ({count_at_best} plan instances tied "
                f"at score {best_sc:.4f} — no unique best)"
            )
            _record_elev_rejection(
                elev,
                plan_instance_id=None,
                score=best_sc,
                reason=reason,
                ambiguity_reason="ambiguous_tie",
                tied_plan_marks=_ordered_unique([
                    plan_instances[p_idx].type_mark
                    for sc, p_idx, _e in eligible_triples
                    if _e == e_idx and sc == best_sc
                ]),
            )

    # Step 4: Build enriched list
    enriched: List[OpeningEvidence] = []
    pair_lookup: Dict[Tuple[int, int], Tuple[float, Dict[str, Any]]] = {
        (p_idx, e_idx): (sc, details)
        for sc, p_idx, e_idx, details in all_pairs
    }
    for p_idx, inst in enumerate(plan_instances):
        if p_idx in assignments:
            e_idx = assignments[p_idx]
            elev = elevation_openings[e_idx]
            sc, details = pair_lookup[(p_idx, e_idx)]
            merged = _enrich_from_elevation(
                inst, elev, correlation_score=sc, assessment=details
            )
            record_decision_reason(
                merged,
                stage="B3",
                outcome="accepted",
                source="elevation_rect",
                purpose="height_evidence",
                elevation_page_no=elev.elevation_page_no,
                elevation_side=elev.elevation_side,
                drawing_ref=elev.drawing_ref,
                coord_space=elev.coord_space,
                correlation_score=round(sc, 4),
                mark_evidence=details.get("mark_evidence"),
                side_match=details.get("side_match"),
                level_match=details.get("level_match"),
                width_match_m=details.get("width_match_m"),
                width_evidence_m=elev.width_m,
                height_evidence_m=elev.height_m,
                candidate_source=elev.extraction_method,
                dimension_basis=DIMENSION_BASIS_UNKNOWN,
                dimension_basis_authority=(
                    "none (generic elevation_rect does not manufacture "
                    "rough_opening authority)"
                ),
                deduction_authority=False,
                instance_creation_authority=False,
            )
            _record_elev_match(
                elev,
                plan_instance_id=merged.opening_instance_id,
                plan_mark=inst.type_mark,
                score=sc,
                side_match=details.get("side_match"),
                level_match=details.get("level_match"),
                width_match_m=details.get("width_match_m"),
            )
            enriched.append(merged)
        else:
            enriched.append(inst)

    unmatched = [e for i, e in enumerate(elevation_openings) if i not in assigned_elev]
    return enriched, unmatched


def _ordered_unique(items: Sequence[Any]) -> List[Any]:
    """Order-preserving unique (used for readable diagnostic lists)."""
    return list(dict.fromkeys(items))


# ---------------------------------------------------------------------------
# Enrichment: merge elevation data into a plan instance
# ---------------------------------------------------------------------------
def _enrich_from_elevation(
    inst: OpeningEvidence,
    elev: ElevationOpening,
    *,
    correlation_score: Optional[float] = None,
    assessment: Optional[Dict[str, Any]] = None,
) -> OpeningEvidence:
    """Enrich a plan instance with elevation-derived evidence.

    Uses merge_opening_evidence() for atomic dimension bundle updates.
    Sets elevation_geometry and elevation_side as additional context.

    dimension_basis remains unknown for generic elevation_rect —
    elevation rectangles measure the visible opening (frame/leaf),
    not necessarily the wall void rough opening.  Only explicit
    wall-void evidence would upgrade the basis.

    When called from correlate_elevation_to_plan() a structured,
    ADDITIVE diagnostic record is added to the elevation observation in
    ``source_observations`` (correlation score, mark/side/level/width
    evidence, coordinate space, candidate source).  Existing keys are never
    renamed, removed, or clobbered.
    """
    # Build an elevation-sourced evidence record.
    # Preserve the existing plan type_mark — elevation labels must NOT
    # populate a blank plan mark (correlation is not semantic identity).
    elev_ev = OpeningEvidence(
        type_mark=inst.type_mark,
        width_m=elev.width_m,
        height_m=elev.height_m,
        dimension_basis=DIMENSION_BASIS_UNKNOWN,
        dimension_source="elevation_rect",
        dimension_confidence=ELEVATION_HEIGHT_CONFIDENCE,
        extraction_method="elevation_rect",
        page_no=elev.elevation_page_no,
        elevation_side=elev.elevation_side,
        elevation_geometry={
            "bbox_px": list(elev.bbox_px),
            "coord_space": elev.coord_space,
            "sill_m": elev.sill_m,
            "head_m": elev.head_m,
            "confidence": elev.confidence,
            "extraction_method": elev.extraction_method,
            "level": elev.level,
            "wall_ref": elev.wall_ref,
            "drawing_ref": elev.drawing_ref,
            "source_page_no": elev.source_page_no,
            "calibration": dict(elev.calibration),
        },
        geometry_confidence=elev.confidence,
        evidence=[f"elevation_rect page={elev.elevation_page_no} "
                  f"side={elev.elevation_side} "
                  f"{elev.width_m:.3f}x{elev.height_m:.3f}m"],
    )

    # Record ONLY the elevation observation — B1 already recorded the plan obs.
    # B3 NEVER reconstructs another source's observation.
    elev_obs = {
        "source": "elevation_rect",
        "width_m": elev.width_m,
        "height_m": elev.height_m,
        "dimension_basis": DIMENSION_BASIS_UNKNOWN,
        "dimension_confidence": ELEVATION_HEIGHT_CONFIDENCE,
        "type_mark": elev.label or "",  # preserve elevation's own label
        "page_no": elev.elevation_page_no,
        "level": elev.level,
        "wall_ref": elev.wall_ref,
        "drawing_ref": elev.drawing_ref,
        "coord_space": elev.coord_space,
        "accepted": False,  # updated after merge
    }

    # Structured correlation "why" (ADDITIVE, only when enrichment came from a
    # scored correlation pair — direct calls without an assessment stay lean).
    if assessment is not None:
        elev_obs["match_decided"] = True
        elev_obs["correlation_score"] = round(float(correlation_score or 0.0), 4)
        elev_obs["mark_evidence"] = assessment.get("mark_evidence")
        elev_obs["side_match"] = assessment.get("side_match")
        elev_obs["level_match"] = assessment.get("level_match")
        elev_obs["width_match_m"] = assessment.get("width_match_m")
        elev_obs["width_evidence_m"] = elev.width_m
        elev_obs["height_evidence_m"] = elev.height_m
        elev_obs["elevation_side"] = elev.elevation_side
        elev_obs["candidate_source"] = elev.extraction_method
        elev_obs["correlated_plan_id"] = inst.opening_instance_id
        elev_obs["correlated_plan_mark"] = inst.type_mark
        elev_obs["correlated_plan_wall_ref"] = inst.wall_ref

    # Use B0's merge logic
    merged = merge_opening_evidence(inst, elev_ev)

    # Determine if elevation won the atomic bundle
    if merged.dimension_source == "elevation_rect":
        elev_obs["accepted"] = True
    else:
        # Elevation dims arrived but the atomic bundle stayed with a prior
        # source (higher basis priority / higher same-basis confidence).
        # Record the WHY as an additive rejection reason on the observation.
        elev_obs["rejection_reason"] = (
            "dimension_bundle_not_won (existing dimension basis/confidence "
            "took precedence over elevation_rect)"
        )

    # Ensure elevation_side is set on the result (merge may not overwrite)
    if not merged.elevation_side and elev.elevation_side:
        merged.elevation_side = elev.elevation_side

    # Append ONLY the elevation observation (plan obs already exists)
    merged.source_observations = list(inst.source_observations) + [elev_obs]

    return merged
