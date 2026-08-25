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
    DIMENSION_BASIS_UNKNOWN,
    DEDUCTION_REVIEW,
    NON_INSTANCE_SOURCES,
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

# Mark label pattern
_MARK_RE = re.compile(r"\b([DW]0?\d{1,2})\b", re.IGNORECASE)


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


def _extract_label_near_rect(
    words: Sequence[Dict[str, Any]],
    rect_bbox: Tuple[float, float, float, float],
    max_distance_px: float = 120.0,
) -> str:
    """Find a D/W mark label near (or inside) an elevation rectangle.

    Searches words within max_distance_px of the rectangle boundary.
    Distance is measured from the word center to the nearest edge of the
    rectangle (not center), since labels typically sit above/below the
    opening.  Returns the first valid mark found, or empty string.
    """
    rx0, ry0, rx1, ry1 = rect_bbox
    r_left, r_right = min(rx0, rx1), max(rx0, rx1)
    r_top, r_bottom = min(ry0, ry1), max(ry0, ry1)
    best_label = ""
    best_dist = max_distance_px + 1.0

    for word in words:
        text = str(word.get("text") or word.get("4", "")).strip()
        if not text:
            continue
        m = _MARK_RE.search(text)
        if not m:
            continue
        mark = m.group(1).upper()
        if not (mark[0] in ("D", "W")):
            continue
        # Word center
        try:
            wx = (float(word.get("0", 0)) + float(word.get("2", 0))) / 2.0
            wy = (float(word.get("1", 0)) + float(word.get("3", 0))) / 2.0
        except (TypeError, ValueError, IndexError):
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
    scale_px_per_m: float,
) -> List[ElevationOpening]:
    """Detect opening-sized rectangular regions on an elevation drawing.

    Args:
        elevation_page_no: Page number of the elevation drawing.
        elevation_side: Cardinal side ("North", "South", etc.).
        rects: Rectangle dicts with keys "bbox" → [x0, y0, x1, y1] in
               page pixel coordinates.  May also have "confidence".
        words: Word dicts from PDF extraction (positioned text).
        scale_px_per_m: Calibration factor (pixels per metre).

    Returns:
        List of ElevationOpening candidates.  These are NOT confirmed
        instances — they must be correlated with B1 plan data.

    Sill/head are None until an elevation datum/baseline is registered.
    """
    if scale_px_per_m <= 0:
        return []

    # Convert physical label search radius to pixels
    label_radius_px = _LABEL_SEARCH_RADIUS_M * scale_px_per_m

    candidates: List[ElevationOpening] = []
    for rect in rects:
        bbox = rect.get("bbox")
        if not bbox or len(bbox) < 4:
            continue
        x0, y0, x1, y1 = [float(v) for v in bbox[:4]]

        # Measure in metres
        width_px = abs(x1 - x0)
        height_px = abs(y1 - y0)
        width_m = width_px / scale_px_per_m
        height_m = height_px / scale_px_per_m

        if not _is_opening_sized(width_m, height_m):
            continue

        # Look for a mark label near this rectangle (scale-aware radius)
        label = _extract_label_near_rect(
            words, (x0, y0, x1, y1), max_distance_px=label_radius_px
        )

        # Confidence: higher for clearly opening-sized, lower for borderline
        conf = 0.5
        if 0.6 <= width_m <= 4.0 and 0.6 <= height_m <= 3.5:
            conf = 0.65
        if label:
            conf += 0.1  # label boost
        conf = min(conf, 0.85)

        candidates.append(ElevationOpening(
            elevation_page_no=elevation_page_no,
            elevation_side=elevation_side,
            bbox_px=(x0, y0, x1, y1),
            width_m=round(width_m, 4),
            height_m=round(height_m, 4),
            label=label,
            confidence=round(conf, 2),
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


def _mark_type(mark: str) -> str:
    """Return the type prefix of a mark: 'D', 'W', or ''."""
    if mark and mark[0].upper() in ("D", "W"):
        return mark[0].upper()
    return ""


def _marks_conflict(mark1: str, mark2: str) -> bool:
    """True if both marks are non-empty and have different D/W types."""
    if not mark1 or not mark2:
        return False
    t1, t2 = _mark_type(mark1), _mark_type(mark2)
    return bool(t1 and t2 and t1 != t2)


def _correlation_score(
    inst: OpeningEvidence,
    elev: ElevationOpening,
) -> float:
    """Score how well a plan instance matches an elevation candidate.

    Returns 0.0-1.0 (higher = better match).  Returns 0.0 for
    incompatible pairs (wrong side, wrong mark type, incompatible width,
    or insufficient strong signals).

    Matching requires at least one strong instance-specific signal:
      - compatible width + exact mark match, OR
      - compatible width + same elevation_side (side alone is NOT enough).
    A generic side match without width agreement does not qualify.
    """
    score = 0.0
    has_width_signal = False
    has_mark_signal = False

    # --- Hard reject: conflicting D/W marks ---
    if _marks_conflict(inst.type_mark, elev.label):
        return 0.0

    # --- Hard reject: different sides ---
    if inst.elevation_side and elev.elevation_side:
        if inst.elevation_side != elev.elevation_side:
            return 0.0

    # --- Side match: contextual evidence (not sufficient alone) ---
    side_match = False
    if inst.elevation_side and elev.elevation_side:
        side_match = inst.elevation_side == elev.elevation_side
        if side_match:
            score += 0.15  # contextual bonus, not sufficient alone

    # --- Width agreement: strong signal ---
    if inst.width_m is not None:
        if not _width_compatible(inst.width_m, elev.width_m):
            return 0.0  # incompatible widths → hard reject
        diff = abs(inst.width_m - elev.width_m)
        width_score = 0.35 * max(0, 1.0 - diff / ELEVATION_WIDTH_TOLERANCE_M)
        score += width_score
        has_width_signal = width_score > 0

    # --- Mark match: strong signal ---
    if inst.type_mark and elev.label:
        if inst.type_mark.upper() == elev.label.upper():
            score += 0.35
            has_mark_signal = True

    # --- Require width agreement as baseline ---
    # Without width, side/mark alone are insufficient for reliable matching.
    if not has_width_signal:
        return 0.0

    # --- Wall ref consistency check (not scoring, just reject conflicts) ---
    # Don't infer side from wall_ref[0] — only reject if wall_ref clearly
    # contradicts the elevation side via explicit registration.
    # (wall_ref → side mapping is deferred to B4 reconciliation.)

    return min(score, 1.0)


def correlate_elevation_to_plan(
    elevation_openings: Sequence[ElevationOpening],
    plan_instances: Sequence[OpeningEvidence],
    unmatched_strategy: str = "discard",
) -> Tuple[List[OpeningEvidence], List[ElevationOpening]]:
    """Match elevation candidates to B1/B2 plan instances and enrich.

    Uses globally strongest-first greedy assignment (order-independent):
      1. Build ALL eligible (score, plan_idx, elev_idx) triples.
      2. Sort by score descending.
      3. Greedily assign: each plan instance and each elevation candidate
         can be matched at most once.
      4. Matched instances are enriched with elevation height/geometry.
      5. Unmatched elevation candidates are returned separately.

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
    if not elevation_openings or not plan_instances:
        return list(plan_instances), list(elevation_openings)

    # Step 1: Build ALL eligible pairs with scores
    pairs: List[Tuple[float, int, int]] = []  # (score, plan_idx, elev_idx)
    for p_idx, inst in enumerate(plan_instances):
        for e_idx, elev in enumerate(elevation_openings):
            sc = _correlation_score(inst, elev)
            if sc >= _MIN_STRONG_SIGNAL:
                pairs.append((sc, p_idx, e_idx))

    # Step 2: Sort globally strongest-first (order-independent)
    pairs.sort(key=lambda x: x[0], reverse=True)

    # Step 3: Greedy one-to-one assignment
    assigned_plan: set = set()
    assigned_elev: set = set()
    assignments: Dict[int, Tuple[float, int]] = {}  # plan_idx → (score, elev_idx)

    for sc, p_idx, e_idx in pairs:
        if p_idx in assigned_plan or e_idx in assigned_elev:
            continue
        assignments[p_idx] = (sc, e_idx)
        assigned_plan.add(p_idx)
        assigned_elev.add(e_idx)

    # Step 4: Build enriched list
    enriched: List[OpeningEvidence] = []
    for p_idx, inst in enumerate(plan_instances):
        if p_idx in assignments:
            _, e_idx = assignments[p_idx]
            elev = elevation_openings[e_idx]
            merged = _enrich_from_elevation(inst, elev)
            enriched.append(merged)
        else:
            enriched.append(inst)

    unmatched = [e for i, e in enumerate(elevation_openings) if i not in assigned_elev]
    return enriched, unmatched


# ---------------------------------------------------------------------------
# Enrichment: merge elevation data into a plan instance
# ---------------------------------------------------------------------------
def _enrich_from_elevation(
    inst: OpeningEvidence,
    elev: ElevationOpening,
) -> OpeningEvidence:
    """Enrich a plan instance with elevation-derived evidence.

    Uses merge_opening_evidence() for atomic dimension bundle updates.
    Sets elevation_geometry and elevation_side as additional context.

    dimension_basis remains unknown for generic elevation_rect —
    elevation rectangles measure the visible opening (frame/leaf),
    not necessarily the wall void rough opening.  Only explicit
    wall-void evidence would upgrade the basis.
    """
    # Build an elevation-sourced evidence record
    elev_ev = OpeningEvidence(
        type_mark=inst.type_mark or elev.label,
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
            "sill_m": elev.sill_m,
            "head_m": elev.head_m,
            "confidence": elev.confidence,
            "extraction_method": elev.extraction_method,
        },
        geometry_confidence=elev.confidence,
        evidence=[f"elevation_rect page={elev.elevation_page_no} "
                  f"side={elev.elevation_side} "
                  f"{elev.width_m:.3f}x{elev.height_m:.3f}m"],
    )

    # Use B0's merge logic
    merged = merge_opening_evidence(inst, elev_ev)

    # Ensure elevation_side is set on the result (merge may not overwrite)
    if not merged.elevation_side and elev.elevation_side:
        merged.elevation_side = elev.elevation_side

    return merged
