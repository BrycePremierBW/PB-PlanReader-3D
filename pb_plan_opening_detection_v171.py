"""PlanReader v1.7.3 plan-vector opening candidate detection.

Phase B1 of Priority 5: reads raw PDF geometric features (line segments,
text words) and detects opening candidates — door jambs/leaves,
window jamb pairs, wall discontinuities, nearby tags.

Outputs OpeningEvidence candidates ONLY.  No take-off changes, no
deduct=True.  This module does NOT modify any existing production files.

Detection strategies:
  1. Door jamb/leaf: short perpendicular segment near a wall line,
     corroborated by a nearby D tag or wall discontinuity
  2. Window jamb pair: two short parallel segments perpendicular to a
     wall, wall-local (not global hatch detection), corroborated by W tag
  3. Wall discontinuity: two collinear wall segments with a gap between
     their endpoints — a real opening in the wall fabric
  4. Nearby tag: text label near geometric features (D01, W01, etc.)
     provides TYPE/semantic evidence only, not geometry confidence

Each candidate carries three independent confidence channels:
  - geometry_confidence: strength of the geometric signal
  - association_confidence: quality of wall/position evidence
  - semantic_confidence: strength of the tag/label evidence

Tag proximity sets type_mark and semantic confidence but must NOT
inflate geometry or association confidence.

Tag/type compatibility:
  - door geometry may only assign Dxx marks
  - window geometry may only assign Wxx marks
  - conflicting tags are recorded as evidence but not as type_mark
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pb_opening_evidence_v170 import (
    DEDUCTION_REVIEW,
    DIMENSION_BASIS_UNKNOWN,
    OPENING_TYPE_DOOR,
    OPENING_TYPE_GLAZED,
    OPENING_TYPE_OTHER,
    OPENING_TYPE_WINDOW,
    OpeningEvidence,
)

VERSION = "1.7.3"

# ---------------------------------------------------------------------------
# Geometry primitives for input
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Segment:
    """A line segment in PDF point coordinates."""
    x1: float
    y1: float
    x2: float
    y2: float
    layer: str = ""
    drawing_index: int = 0

    @property
    def length(self) -> float:
        return math.hypot(self.x2 - self.x1, self.y2 - self.y1)

    @property
    def angle_deg(self) -> float:
        return math.degrees(math.atan2(self.y2 - self.y1, self.x2 - self.x1)) % 180.0

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) * 0.5

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) * 0.5

    @property
    def bbox(self) -> Tuple[float, float, float, float]:
        return (min(self.x1, self.x2), min(self.y1, self.y2),
                max(self.x1, self.x2), max(self.y1, self.y2))


@dataclass(frozen=True)
class TextWord:
    """A positioned text word from PDF extraction."""
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    page_no: int = 0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) * 0.5

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) * 0.5

    @property
    def bbox(self) -> Tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


# ---------------------------------------------------------------------------
# Tag patterns
# ---------------------------------------------------------------------------

_TAG_DOOR_RE = re.compile(r"^D\d{1,3}$", re.IGNORECASE)
_TAG_WINDOW_RE = re.compile(r"^W\d{1,3}$", re.IGNORECASE)


def _classify_tag(text: str) -> str:
    """Classify a text tag as door, window, or unknown."""
    t = text.strip().upper()
    if _TAG_DOOR_RE.match(t):
        return "door"
    if _TAG_WINDOW_RE.match(t):
        return "window"
    return ""


# ---------------------------------------------------------------------------
# Scale / calibration helpers
# ---------------------------------------------------------------------------

def _resolve_scale(scale_info: Optional[Dict[str, Any]],
                   scale_px_per_m: float = 0.0) -> Tuple[float, float]:
    """Resolve PDF-points-per-metre from a Priority 1 scale_info dict.

    Priority 1 calibration returns:
      px_per_m: rendered pixels per metre (after render_zoom)
      render_zoom: zoom factor applied during rendering

    PDF points per metre = px_per_m / render_zoom

    Returns (pdf_pt_per_m, m_per_pdf_pt).
    """
    if scale_info and isinstance(scale_info, dict):
        px_per_m = float(scale_info.get("px_per_m") or 0.0)
        render_zoom = float(scale_info.get("render_zoom") or 1.0)
        if render_zoom <= 0:
            render_zoom = 1.0
        if px_per_m > 0:
            pt_per_m = px_per_m / render_zoom
            m_per_pt = 1.0 / pt_per_m if pt_per_m > 0 else 0.0
            return pt_per_m, m_per_pt

    # Fallback: use raw scale_px_per_m as PDF pt/m (legacy callers)
    if scale_px_per_m > 0:
        return scale_px_per_m, 1.0 / scale_px_per_m
    return 0.0, 0.0


def _pt_to_m(pt: float, m_per_pt: float) -> Optional[float]:
    """Convert PDF points to metres. Returns None if no scale."""
    if m_per_pt <= 0:
        return None
    return round(pt * m_per_pt, 3)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _segment_distance(a: Segment, b: Segment) -> float:
    """Midpoint-to-midpoint distance between two segments."""
    return math.hypot(a.cx - b.cx, a.cy - b.cy)


def _point_segment_distance(px: float, py: float, s: Segment) -> float:
    """Distance from point to line segment."""
    dx, dy = s.x2 - s.x1, s.y2 - s.y1
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-12:
        return math.hypot(px - s.x1, py - s.y1)
    t = max(0.0, min(1.0, ((px - s.x1) * dx + (py - s.y1) * dy) / length_sq))
    proj_x = s.x1 + t * dx
    proj_y = s.y1 + t * dy
    return math.hypot(px - proj_x, py - proj_y)


def _segments_parallel(a: Segment, b: Segment, tol: float = 5.0) -> bool:
    """True if two segments are approximately parallel."""
    d = abs(a.angle_deg - b.angle_deg) % 180.0
    return min(d, 180.0 - d) <= tol


def _line_perp_distance(px: float, py: float, s: Segment) -> float:
    """Perpendicular distance from a point to the INFINITE line through a segment.

    Unlike _point_segment_distance, this does NOT clamp to the segment extent.
    Used for collinearity checks where the other segment may be far along the
    wall direction but on the same line.
    """
    dx, dy = s.x2 - s.x1, s.y2 - s.y1
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-12:
        return math.hypot(px - s.x1, py - s.y1)
    return abs((dy * px - dx * py + s.x2 * s.y1 - s.y2 * s.x1) / math.sqrt(length_sq))


def _segments_perpendicular(a: Segment, b: Segment, tol: float = 15.0) -> bool:
    """True if two segments are approximately perpendicular."""
    d = abs(a.angle_deg - b.angle_deg) % 180.0
    return abs(d - 90.0) <= tol


def _find_tag_near(
    cx: float,
    cy: float,
    words: Sequence[TextWord],
    max_dist_pt: float = 120.0,
) -> Tuple[str, str]:
    """Find the closest D/W tag near a point. Returns (tag_text, classification)."""
    best_dist = max_dist_pt + 1.0
    best_tag = ""
    best_class = ""
    for w in words:
        cls = _classify_tag(w.text)
        if not cls:
            continue
        d = math.hypot(w.cx - cx, w.cy - cy)
        if d < best_dist:
            best_dist = d
            best_tag = w.text.strip().upper()
            best_class = cls
    return best_tag, best_class


# ---------------------------------------------------------------------------
# Canonical wall direction
# ---------------------------------------------------------------------------

def _canonical_wall_direction(seg: Segment) -> Tuple[float, float, float, float]:
    """Return (dx, dy, origin_x, origin_y) with a stable, orientation-independent
    direction for a wall segment.

    The canonical direction is determined by the segment's angle:
      - angle < 45° or >= 135° → horizontal → left-to-right (lower x first)
      - otherwise → vertical → bottom-to-top (lower y first)

    Reversing the input segment endpoints produces the SAME direction and origin.
    """
    angle = seg.angle_deg
    if angle < 45.0 or angle >= 135.0:
        # Horizontal-ish: canonical left-to-right
        if seg.x1 <= seg.x2:
            return (seg.x2 - seg.x1, seg.y2 - seg.y1, seg.x1, seg.y1)
        else:
            return (seg.x1 - seg.x2, seg.y1 - seg.y2, seg.x2, seg.y2)
    else:
        # Vertical-ish: canonical bottom-to-top
        if seg.y1 <= seg.y2:
            return (seg.x2 - seg.x1, seg.y2 - seg.y1, seg.x1, seg.y1)
        else:
            return (seg.x1 - seg.x2, seg.y1 - seg.y2, seg.x2, seg.y2)


def _position_along_wall(px: float, py: float,
                         wall: Segment) -> Optional[float]:
    """Project a point onto a wall's canonical axis and return the signed
    distance from the canonical origin.  Always >= 0 for valid projections.
    """
    dx, dy, ox, oy = _canonical_wall_direction(wall)
    wall_len_sq = dx * dx + dy * dy
    if wall_len_sq < 1e-12:
        return None
    t = ((px - ox) * dx + (py - oy) * dy) / wall_len_sq
    wall_len = math.sqrt(wall_len_sq)
    return t * wall_len  # can be negative if outside the canonical segment


# ---------------------------------------------------------------------------
# Wall line detection
# ---------------------------------------------------------------------------

@dataclass
class WallLine:
    """A detected wall line (long segment forming a wall boundary)."""
    segment: Segment
    wall_ref: str = ""
    side: str = ""  # North/South/East/West


def detect_wall_lines(
    segments: Sequence[Segment],
    min_length_pt: float = 200.0,
) -> List[WallLine]:
    """Identify long segments as likely wall lines.

    Wall lines are typically the longest segments in a floor plan.
    Returns segments sorted by length (longest first).
    """
    long_segs = [s for s in segments if s.length >= min_length_pt]
    long_segs.sort(key=lambda s: s.length, reverse=True)
    return [WallLine(segment=s) for s in long_segs]


# ---------------------------------------------------------------------------
# Wall-ref conflict resolution
# ---------------------------------------------------------------------------

def _resolve_wall_ref(wl_a: WallLine, wl_b: WallLine) -> str:
    """Resolve wall_ref from two collinear wall segments.

    If both have non-empty wall_ref and they disagree, return "" (conflict).
    If one has a ref and the other is empty, return the non-empty one.
    If both empty, return "".
    """
    a_ref = wl_a.wall_ref.strip()
    b_ref = wl_b.wall_ref.strip()
    if a_ref and b_ref:
        return a_ref if a_ref == b_ref else ""  # conflict → blank
    return a_ref or b_ref


# ---------------------------------------------------------------------------
# Door detection (jambs/leaves — NOT swing arcs)
# ---------------------------------------------------------------------------

@dataclass
class DoorCandidate:
    """Geometric evidence for a door opening."""
    wall_ref: str = ""
    position_along_wall_m: Optional[float] = None
    width_m: Optional[float] = None
    jamb_segment: Optional[Segment] = None
    tag: str = ""
    geometry_confidence: float = 0.0
    association_confidence: float = 0.0
    semantic_confidence: float = 0.0
    evidence: List[str] = field(default_factory=list)
    page_no: int = 0


def detect_door_candidates(
    segments: Sequence[Segment],
    wall_lines: Sequence[WallLine],
    words: Sequence[TextWord],
    scale_info: Optional[Dict[str, Any]] = None,
    scale_px_per_m: float = 0.0,
    page_no: int = 0,
) -> List[DoorCandidate]:
    """Detect door openings from perpendicular jamb segments near walls.

    Door detection heuristic:
      1. Find short segments perpendicular to a wall line (jamb/leaf)
      2. The perpendicular segment alone is WEAK geometry evidence
      3. A nearby D tag provides semantic (type) evidence only
      4. Higher geometry confidence requires additional corroboration
         (reasonable width, or being part of a discontinuity)

    Tag compatibility: door geometry may only assign Dxx marks.
    A nearby W tag is recorded as evidence but NOT as type_mark.

    Returns candidates with THREE independent confidence channels.
    Tags set semantic_confidence, NOT geometry_confidence.
    """
    candidates: List[DoorCandidate] = []

    pt_per_m, m_per_pt = _resolve_scale(scale_info, scale_px_per_m)

    MIN_LEAF_PT = 25.0
    MAX_LEAF_PT = 150.0
    WALL_PROXIMITY_PT = 30.0

    # Pre-identify segments that are part of wall-local parallel pairs (windows)
    # Only exclude if the pair is near the SAME wall
    wall_local_parallel: set = set()
    for wl in wall_lines:
        wall = wl.segment
        near_this_wall = [
            s for s in segments
            if s is not wall
            and MIN_LEAF_PT <= s.length <= MAX_LEAF_PT
            and _segments_perpendicular(wall, s)
            and _point_segment_distance(s.cx, s.cy, wall) <= WALL_PROXIMITY_PT
        ]
        for i, a in enumerate(near_this_wall):
            for j, b in enumerate(near_this_wall):
                if j <= i:
                    continue
                if _segments_parallel(a, b) and _segment_distance(a, b) <= 100.0:
                    wall_local_parallel.add(id(a))
                    wall_local_parallel.add(id(b))

    for wl in wall_lines:
        wall = wl.segment
        wall_dx, wall_dy, wall_ox, wall_oy = _canonical_wall_direction(wall)
        wall_len = math.hypot(wall_dx, wall_dy)
        if wall_len < 1e-9:
            continue

        for seg in segments:
            if seg is wall:
                continue
            if id(seg) in wall_local_parallel:
                continue  # part of a parallel pair on this wall → window
            if seg.length < MIN_LEAF_PT or seg.length > MAX_LEAF_PT:
                continue
            if not _segments_perpendicular(wall, seg):
                continue
            dist = _point_segment_distance(seg.cx, seg.cy, wall)
            if dist > WALL_PROXIMITY_PT:
                continue

            # Found a perpendicular jamb/leaf segment
            tag, tag_cls = _find_tag_near(seg.cx, seg.cy, words)

            # Tag/type compatibility: door geometry may only assign Dxx marks
            assigned_tag = ""
            if tag and tag_cls == "door":
                assigned_tag = tag  # compatible: D tag for door geometry
            elif tag and tag_cls == "window":
                # Conflicting: W tag near door geometry — record as evidence
                pass  # assigned_tag stays ""
            elif tag:
                pass  # unknown tag type — don't assign

            # Width from the perpendicular segment
            width_m = _pt_to_m(seg.length, m_per_pt)

            # Position along wall (canonical, orientation-independent)
            pos_pt = ((seg.cx - wall_ox) * wall_dx + (seg.cy - wall_oy) * wall_dy) / wall_len
            pos_m = _pt_to_m(pos_pt, m_per_pt)

            # --- Three independent confidence channels ---

            # GEOMETRY confidence: based purely on geometric signal.
            # A single perpendicular line near a wall is weak evidence —
            # it could be a partition return, frame line, or annotation.
            geom_conf = 0.45  # base: single perpendicular segment
            if width_m is not None and 0.6 <= width_m <= 1.2:
                geom_conf = 0.60  # reasonable door width supports geometry

            # ASSOCIATION confidence: based on wall/position evidence.
            assoc_conf = 0.70 if wl.wall_ref else 0.30

            # SEMANTIC confidence: based on tag/label evidence.
            # Only D tags contribute to door semantic confidence.
            sem_conf = 0.0
            if tag and tag_cls == "door":
                sem_conf = 0.95
            elif tag and tag_cls == "window":
                sem_conf = 0.30  # conflicting tag — weak semantic
            elif tag:
                sem_conf = 0.60  # unknown tag but present

            ev = []
            ev.append(f"jamb_leaf: {seg.length:.1f}pt perpendicular to wall")
            if assigned_tag:
                ev.append(f"tag: {tag} (door-compatible)")
            elif tag:
                ev.append(f"tag: {tag} (conflicting — not assigned as type_mark)")

            candidates.append(DoorCandidate(
                wall_ref=wl.wall_ref or "",
                position_along_wall_m=pos_m,
                width_m=width_m,
                jamb_segment=seg,
                tag=assigned_tag,
                geometry_confidence=geom_conf,
                association_confidence=assoc_conf,
                semantic_confidence=sem_conf,
                evidence=ev,
                page_no=page_no,
            ))

    return candidates


# ---------------------------------------------------------------------------
# Window / jamb pair detection (wall-local, cluster-based hatch filter)
# ---------------------------------------------------------------------------

@dataclass
class WindowCandidate:
    """Geometric evidence for a window opening."""
    wall_ref: str = ""
    position_along_wall_m: Optional[float] = None
    width_m: Optional[float] = None
    parallel_segments: List[Segment] = field(default_factory=list)
    tag: str = ""
    geometry_confidence: float = 0.0
    association_confidence: float = 0.0
    semantic_confidence: float = 0.0
    evidence: List[str] = field(default_factory=list)
    page_no: int = 0


def detect_window_candidates(
    segments: Sequence[Segment],
    wall_lines: Sequence[WallLine],
    words: Sequence[TextWord],
    scale_info: Optional[Dict[str, Any]] = None,
    scale_px_per_m: float = 0.0,
    page_no: int = 0,
) -> List[WindowCandidate]:
    """Detect window openings from wall-local parallel jamb pairs.

    Window detection is WALL-LOCAL: only segments near the SAME wall
    are considered as potential jamb pairs.

    Hatch/grid filter uses PAIR-LOCAL analysis:
      1. First find all parallel pairs within window-distance range
      2. Then check unpaired segments — if many remain and form regular
         spacing patterns (hatch/battens), reject the whole wall
      3. Legitimate multi-window walls have distinct pairs with gaps
         between them, not a continuous run of equally-spaced lines

    A pair of parallel segments that are both perpendicular to a wall
    and close together indicates a window jamb pair.  The distance
    between them is the rough opening width.
    """
    candidates: List[WindowCandidate] = []

    pt_per_m, m_per_pt = _resolve_scale(scale_info, scale_px_per_m)

    MIN_WINDOW_PT = 25.0
    MAX_WINDOW_PT = 200.0
    MAX_PAIR_DISTANCE_PT = 100.0
    WALL_PROXIMITY_PT = 30.0

    for wl in wall_lines:
        wall = wl.segment
        # Find short segments perpendicular to THIS wall and near it
        near_wall = [
            s for s in segments
            if s is not wall
            and MIN_WINDOW_PT <= s.length <= MAX_WINDOW_PT
            and _segments_perpendicular(wall, s)
            and _point_segment_distance(s.cx, s.cy, wall) <= WALL_PROXIMITY_PT
        ]

        if not near_wall:
            continue

        # Step 1: Sort by position along wall for cluster analysis
        wall_dx, wall_dy, wall_ox, wall_oy = _canonical_wall_direction(wall)
        wall_len = math.hypot(wall_dx, wall_dy)
        if wall_len < 1e-9:
            continue

        def _pos(s: Segment) -> float:
            return ((s.cx - wall_ox) * wall_dx + (s.cy - wall_oy) * wall_dy) / wall_len

        sorted_segs = sorted(near_wall, key=_pos)

        # Step 2: Find parallel pairs within window-distance range
        used: set = set()
        pairs: List[Tuple[int, int, Segment, Segment]] = []

        for i, a in enumerate(sorted_segs):
            if i in used:
                continue
            for j, b in enumerate(sorted_segs):
                if j <= i or j in used:
                    continue
                if not _segments_parallel(a, b):
                    continue
                pair_dist = _segment_distance(a, b)
                if pair_dist > MAX_PAIR_DISTANCE_PT:
                    continue
                # Found a pair
                used.add(i)
                used.add(j)
                pairs.append((i, j, a, b))
                break  # each segment belongs to at most one pair

        # Step 3: Hatch filter — scale-aware with tag override
        # After pairing, check if pairs represent separate windows or hatch.
        #
        # Hatch: pairs are uniformly spaced with gap ≈ jamb spacing (ratio ≈ 2.0).
        # Windows: gap is much larger than jamb spacing (ratio > 2.5).
        #
        # When pairs have W tags, they are evidence-corroborated windows and
        # override the geometric hatch suspicion — a W tag is strong semantic
        # evidence that the pair represents a real window, not a hatch element.
        is_hatch = False
        if len(pairs) >= 2:
            pair_midpoints = []
            pair_jamb_spacings = []
            pair_has_w_tag = []
            for _, _, a, b in pairs:
                mcx = (a.cx + b.cx) / 2.0
                mcy = (a.cy + b.cy) / 2.0
                pair_midpoints.append(((mcx - wall_ox) * wall_dx + (mcy - wall_oy) * wall_dy) / wall_len)
                pair_jamb_spacings.append(_segment_distance(a, b))
                tag, _ = _find_tag_near(mcx, mcy, words)
                pair_has_w_tag.append(_classify_tag(tag) == "window")
            pair_midpoints.sort()

            # Gaps between consecutive pair midpoints
            gaps_between = [
                pair_midpoints[k + 1] - pair_midpoints[k]
                for k in range(len(pair_midpoints) - 1)
            ]
            mean_jamb = sum(pair_jamb_spacings) / len(pair_jamb_spacings)

            if gaps_between and mean_jamb > 0:
                max_gap = max(gaps_between)
                gap_ratio = max_gap / mean_jamb

                # Scale-aware minimum gap: 1.5 m between pair centres
                # (minimum wall section between separate windows)
                MIN_GAP_M = 1.5
                scale_min_gap_pt = MIN_GAP_M * pt_per_m if pt_per_m > 0 else 0

                if len(gaps_between) >= 2:
                    # Multiple gaps: ratio test is reliable
                    if gap_ratio <= 2.5:
                        if scale_min_gap_pt > 0 and max_gap >= scale_min_gap_pt:
                            pass
                        else:
                            is_hatch = True
                else:
                    # Single gap (2 pairs): ratio test ambiguous
                    # Use scale threshold, but W tags override
                    if not any(pair_has_w_tag):
                        if scale_min_gap_pt > 0 and max_gap < scale_min_gap_pt:
                            is_hatch = True
                        elif scale_min_gap_pt <= 0 and gap_ratio <= 2.5:
                            is_hatch = True

        if is_hatch:
            continue

        # Step 4: Process each pair as a window candidate
        for i, j, a, b in pairs:
            pair_dist = _segment_distance(a, b)
            width_m = _pt_to_m(pair_dist, m_per_pt)

            # Position: midpoint projected onto wall (canonical)
            mid_cx = (a.cx + b.cx) / 2.0
            mid_cy = (a.cy + b.cy) / 2.0
            pos_pt = ((mid_cx - wall_ox) * wall_dx + (mid_cy - wall_oy) * wall_dy) / wall_len
            pos_m = _pt_to_m(pos_pt, m_per_pt)

            tag, tag_cls = _find_tag_near(mid_cx, mid_cy, words)

            # Tag/type compatibility: window geometry may only assign Wxx marks
            assigned_tag = ""
            if tag and tag_cls == "window":
                assigned_tag = tag  # compatible: W tag for window geometry
            elif tag and tag_cls == "door":
                pass  # conflicting: D tag near window — don't assign
            elif tag:
                pass  # unknown tag type

            # --- Three independent confidence channels ---

            # GEOMETRY: parallel pair near wall is moderate evidence
            geom_conf = 0.60
            if width_m is not None and 0.3 <= width_m <= 3.0:
                geom_conf = 0.70  # reasonable window width

            # ASSOCIATION
            assoc_conf = 0.70 if wl.wall_ref else 0.30

            # SEMANTIC: only W tags contribute to window semantic confidence
            sem_conf = 0.0
            if tag and tag_cls == "window":
                sem_conf = 0.95
            elif tag and tag_cls == "door":
                sem_conf = 0.30  # conflicting tag
            elif tag:
                sem_conf = 0.60

            ev = []
            ev.append(f"jamb_pair: {a.length:.1f}pt + {b.length:.1f}pt, spacing={pair_dist:.1f}pt")
            if assigned_tag:
                ev.append(f"tag: {tag} (window-compatible)")
            elif tag:
                ev.append(f"tag: {tag} (conflicting — not assigned as type_mark)")

            candidates.append(WindowCandidate(
                wall_ref=wl.wall_ref or "",
                position_along_wall_m=pos_m,
                width_m=width_m,
                parallel_segments=[a, b],
                tag=assigned_tag,
                geometry_confidence=geom_conf,
                association_confidence=assoc_conf,
                semantic_confidence=sem_conf,
                evidence=ev,
                page_no=page_no,
            ))

    return candidates


# ---------------------------------------------------------------------------
# Wall discontinuity detection (real gaps in wall fabric)
# ---------------------------------------------------------------------------

@dataclass
class GapCandidate:
    """Geometric evidence for a generic opening (wall discontinuity)."""
    wall_ref: str = ""
    position_along_wall_m: Optional[float] = None
    width_m: Optional[float] = None
    tag: str = ""
    geometry_confidence: float = 0.0
    association_confidence: float = 0.0
    semantic_confidence: float = 0.0
    evidence: List[str] = field(default_factory=list)
    page_no: int = 0


def detect_gap_candidates(
    segments: Sequence[Segment],
    wall_lines: Sequence[WallLine],
    words: Sequence[TextWord],
    scale_info: Optional[Dict[str, Any]] = None,
    scale_px_per_m: float = 0.0,
    page_no: int = 0,
) -> List[GapCandidate]:
    """Detect wall discontinuities — real gaps in the wall fabric.

    A wall discontinuity is detected when two COLLINEAR wall segments
    terminate with a gap between their endpoints.  This is fundamentally
    different from finding perpendicular intersections along a continuous
    wall — those are partition returns, not openings.

    A continuous wall line running through a region does NOT create a gap.
    Only actual termination of wall linework creates a gap.

    Position is computed using a canonical wall direction (orientation-independent).
    Conflicting wall_refs from two segments result in a blank wall_ref.
    """
    candidates: List[GapCandidate] = []

    pt_per_m, m_per_pt = _resolve_scale(scale_info, scale_px_per_m)

    MIN_GAP_PT = 30.0
    MAX_GAP_PT = 200.0
    COLLINEAR_TOL_DEG = 10.0
    COLLINEAR_OFFSET_PT = 15.0  # max perpendicular offset for "same line"
    ENDPOINT_PROXIMITY_PT = 5.0  # gap = space between non-adjacent endpoints

    # Find collinear wall segments (same angle, close perpendicular offset)
    for i, wl_a in enumerate(wall_lines):
        a = wl_a.segment
        for j, wl_b in enumerate(wall_lines):
            if j <= i:
                continue
            b = wl_b.segment

            # Must be approximately parallel (collinear)
            if not _segments_parallel(a, b, tol=COLLINEAR_TOL_DEG):
                continue

            # Must be close in the perpendicular direction (same wall line)
            perp_dist = _line_perp_distance(b.cx, b.cy, a)
            if perp_dist > COLLINEAR_OFFSET_PT:
                continue

            # Resolve wall_ref (conflicting → blank)
            gap_wall_ref = _resolve_wall_ref(wl_a, wl_b)

            # Use canonical wall direction for stable projection
            # Use a shared origin from both segments to ensure the projection
            # is stable regardless of which segment is 'a' vs 'b'
            a_angle = a.angle_deg
            cos_a = math.cos(math.radians(a_angle))
            sin_a = math.sin(math.radians(a_angle))

            a_starts = [(a.x1 * cos_a + a.y1 * sin_a),
                        (a.x2 * cos_a + a.y2 * sin_a)]
            b_starts = [(b.x1 * cos_a + b.y1 * sin_a),
                        (b.x2 * cos_a + b.y2 * sin_a)]
            shared_min_t = min(a_starts + b_starts)
            shared_origin_x = shared_min_t * cos_a
            shared_origin_y = shared_min_t * sin_a

            wall_dx = cos_a
            wall_dy = sin_a
            wall_ox = shared_origin_x
            wall_oy = shared_origin_y
            wall_len = a.length + b.length  # approximate; sufficient for projection

            def _project(seg: Segment) -> Tuple[float, float]:
                """Project segment endpoints onto canonical wall axis (in points)."""
                t1 = ((seg.x1 - wall_ox) * wall_dx + (seg.y1 - wall_oy) * wall_dy) / wall_len
                t2 = ((seg.x2 - wall_ox) * wall_dx + (seg.y2 - wall_oy) * wall_dy) / wall_len
                return (t1 * wall_len, t2 * wall_len)

            a_proj = _project(a)
            b_proj = _project(b)

            a_min, a_max = min(a_proj), max(a_proj)
            b_min, b_max = min(b_proj), max(b_proj)

            # Check for gap: segments don't overlap and have space between them
            if a_max < b_min:
                gap_start_pt = a_max
                gap_end_pt = b_min
            elif b_max < a_min:
                gap_start_pt = b_max
                gap_end_pt = a_min
            else:
                continue  # overlapping — no gap

            gap_len_pt = gap_end_pt - gap_start_pt
            if gap_len_pt < MIN_GAP_PT or gap_len_pt > MAX_GAP_PT:
                continue

            # Verify the gap is not filled by another wall segment
            gap_filled = False
            for s in segments:
                if s is a or s is b:
                    continue
                if _segments_parallel(a, s, tol=COLLINEAR_TOL_DEG):
                    s_proj = _project(s)
                    s_min, s_max = min(s_proj), max(s_proj)
                    # Fills the gap if it overlaps the gap region
                    if s_min <= gap_start_pt + ENDPOINT_PROXIMITY_PT and s_max >= gap_end_pt - ENDPOINT_PROXIMITY_PT:
                        gap_filled = True
                        break
            if gap_filled:
                continue

            # Found a genuine wall discontinuity
            mid_pt = (gap_start_pt + gap_end_pt) / 2.0
            # mid_pt is in points along wall direction (unit vector cos_a, sin_a)
            # Actual position = origin + mid_pt * direction
            mid_cx = wall_ox + mid_pt * wall_dx
            mid_cy = wall_oy + mid_pt * wall_dy

            width_m = _pt_to_m(gap_len_pt, m_per_pt)
            pos_m = _pt_to_m(mid_pt, m_per_pt)

            tag, tag_cls = _find_tag_near(mid_cx, mid_cy, words)

            # --- Three independent confidence channels ---

            # GEOMETRY: two wall segments terminating = strong evidence
            geom_conf = 0.75

            # ASSOCIATION
            assoc_conf = 0.70 if gap_wall_ref else 0.30

            # SEMANTIC
            sem_conf = 0.0
            if tag:
                sem_conf = 0.80

            ev = [f"wall_discontinuity: {gap_len_pt:.1f}pt gap between collinear wall segments"]
            if tag:
                ev.append(f"tag: {tag} (semantic evidence)")

            candidates.append(GapCandidate(
                wall_ref=gap_wall_ref,
                position_along_wall_m=pos_m,
                width_m=width_m,
                tag=tag,
                geometry_confidence=geom_conf,
                association_confidence=assoc_conf,
                semantic_confidence=sem_conf,
                evidence=ev,
                page_no=page_no,
            ))

    return candidates


# ---------------------------------------------------------------------------
# Conversion to OpeningEvidence
# ---------------------------------------------------------------------------

def door_to_opening_evidence(cand: DoorCandidate) -> OpeningEvidence:
    """Convert a DoorCandidate to an OpeningEvidence record.

    geometry_confidence → cand.geometry_confidence (geometric signal)
    association_confidence → cand.association_confidence (wall evidence)
    dimension_confidence → 0.0 (plan cannot measure rough_opening dims)
    """
    ev = OpeningEvidence(
        type_mark=cand.tag if cand.tag else "",
        page_no=cand.page_no,
        wall_ref=cand.wall_ref,
        opening_type=OPENING_TYPE_DOOR,
        width_m=cand.width_m,
        height_m=None,
        dimension_basis=DIMENSION_BASIS_UNKNOWN,
        dimension_source="plan_vector",
        sill_m=0.0,
        position_along_wall_m=cand.position_along_wall_m,
        extraction_method="plan_vector",
        geometry_confidence=cand.geometry_confidence,
        dimension_confidence=0.0,
        association_confidence=cand.association_confidence,
        deduction_status=DEDUCTION_REVIEW,
        evidence=cand.evidence + ["plan_vector_jamb_detection"],
    )
    ev.set_quantity(1, source="geometric")
    ev.compute_area()
    ev.compute_deduction_status()
    return ev


def window_to_opening_evidence(cand: WindowCandidate) -> OpeningEvidence:
    """Convert a WindowCandidate to an OpeningEvidence record."""
    ev = OpeningEvidence(
        type_mark=cand.tag if cand.tag else "",
        page_no=cand.page_no,
        wall_ref=cand.wall_ref,
        opening_type=OPENING_TYPE_WINDOW,
        width_m=cand.width_m,
        height_m=None,
        dimension_basis=DIMENSION_BASIS_UNKNOWN,
        dimension_source="plan_vector",
        sill_m=0.9,
        position_along_wall_m=cand.position_along_wall_m,
        extraction_method="plan_vector",
        geometry_confidence=cand.geometry_confidence,
        dimension_confidence=0.0,
        association_confidence=cand.association_confidence,
        deduction_status=DEDUCTION_REVIEW,
        evidence=cand.evidence + ["plan_vector_jamb_pair_detection"],
    )
    ev.set_quantity(1, source="geometric")
    ev.compute_area()
    ev.compute_deduction_status()
    return ev


def gap_to_opening_evidence(cand: GapCandidate) -> OpeningEvidence:
    """Convert a GapCandidate to an OpeningEvidence record."""
    opening_type = _opening_type_from_tag(cand.tag)
    ev = OpeningEvidence(
        type_mark=cand.tag if cand.tag else "",
        page_no=cand.page_no,
        wall_ref=cand.wall_ref,
        opening_type=opening_type,
        width_m=cand.width_m,
        height_m=None,
        dimension_basis=DIMENSION_BASIS_UNKNOWN,
        dimension_source="plan_vector",
        sill_m=0.0 if opening_type == OPENING_TYPE_DOOR else 0.9,
        position_along_wall_m=cand.position_along_wall_m,
        extraction_method="plan_vector",
        geometry_confidence=cand.geometry_confidence,
        dimension_confidence=0.0,
        association_confidence=cand.association_confidence,
        deduction_status=DEDUCTION_REVIEW,
        evidence=cand.evidence + ["plan_vector_discontinuity_detection"],
    )
    ev.set_quantity(1, source="geometric")
    ev.compute_area()
    ev.compute_deduction_status()
    return ev


def _opening_type_from_tag(tag: str) -> str:
    """Map a tag classification to an OpeningEvidence opening_type."""
    t = _classify_tag(tag)
    if t == "door":
        return OPENING_TYPE_DOOR
    if t == "window":
        return OPENING_TYPE_WINDOW
    return OPENING_TYPE_OTHER


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

@dataclass
class PlanOpeningDetectionResult:
    """Result from plan_opening_candidates()."""
    candidates: List[OpeningEvidence] = field(default_factory=list)
    door_count: int = 0
    window_count: int = 0
    gap_count: int = 0
    total_features: int = 0
    wall_lines_found: int = 0
    notes: List[str] = field(default_factory=list)


def plan_opening_candidates(
    segments: Sequence[Segment],
    words: Sequence[TextWord],
    wall_lines: Optional[Sequence[WallLine]] = None,
    scale_info: Optional[Dict[str, Any]] = None,
    scale_px_per_m: float = 0.0,
    page_no: int = 0,
    min_wall_length_pt: float = 200.0,
) -> PlanOpeningDetectionResult:
    """Detect opening candidates from plan-vector geometry.

    This is the main B1 entry point.  It takes raw PDF geometric
    features and produces OpeningEvidence candidates.

    Args:
        segments: Line segments from PDF vector extraction.
        words: Positioned text words from PDF text extraction.
        wall_lines: Optional pre-detected wall lines. If None, auto-detected.
        scale_info: Priority 1 calibration dict (preferred). Keys: px_per_m,
            render_zoom.
        scale_px_per_m: Legacy calibration factor. Used only if scale_info
            is not provided.
        page_no: Page number for provenance.
        min_wall_length_pt: Minimum segment length to be a wall line.

    Returns:
        PlanOpeningDetectionResult with OpeningEvidence candidates.
    """
    result = PlanOpeningDetectionResult()
    result.total_features = len(segments)

    # Step 1: Detect wall lines if not provided
    if wall_lines is None:
        wall_lines = detect_wall_lines(segments, min_length_pt=min_wall_length_pt)
    result.wall_lines_found = len(wall_lines)

    if not wall_lines:
        result.notes.append("No wall lines detected — cannot find openings without wall context")
        return result

    # Step 2: Detect door candidates
    doors = detect_door_candidates(
        segments, wall_lines, words,
        scale_info=scale_info, scale_px_per_m=scale_px_per_m,
        page_no=page_no,
    )
    result.door_count = len(doors)

    # Step 3: Detect window candidates
    windows = detect_window_candidates(
        segments, wall_lines, words,
        scale_info=scale_info, scale_px_per_m=scale_px_per_m,
        page_no=page_no,
    )
    result.window_count = len(windows)

    # Step 4: Detect gap candidates (wall discontinuities)
    gaps = detect_gap_candidates(
        segments, wall_lines, words,
        scale_info=scale_info, scale_px_per_m=scale_px_per_m,
        page_no=page_no,
    )

    # Filter gaps that overlap with door/window positions
    used_positions: List[Tuple[float, str]] = []
    for d in doors:
        if d.position_along_wall_m is not None:
            used_positions.append((d.position_along_wall_m, d.wall_ref))
    for w in windows:
        if w.position_along_wall_m is not None:
            used_positions.append((w.position_along_wall_m, w.wall_ref))

    filtered_gaps = []
    for g in gaps:
        if g.position_along_wall_m is not None:
            overlaps = any(
                abs(g.position_along_wall_m - pos) < 0.20
                and g.wall_ref != "" and ref != ""
                and g.wall_ref == ref
                for pos, ref in used_positions
            )
            if overlaps:
                continue
        filtered_gaps.append(g)
    gaps = filtered_gaps
    result.gap_count = len(gaps)

    # Step 5: Convert to OpeningEvidence
    candidates: List[OpeningEvidence] = []
    for d in doors:
        candidates.append(door_to_opening_evidence(d))
    for w in windows:
        candidates.append(window_to_opening_evidence(w))
    for g in gaps:
        candidates.append(gap_to_opening_evidence(g))

    result.candidates = candidates

    if not candidates:
        result.notes.append("No opening candidates detected from plan geometry")

    return result
