"""PlanReader v1.7.1 plan-vector opening candidate detection.

Phase B1 of Priority 5: reads raw PDF geometric features (line segments,
rectangles, text words) and detects opening candidates — door swings,
jamb pairs, wall gaps, glazing pairs, nearby tags.

Outputs OpeningEvidence candidates ONLY.  No take-off changes, no
deduct=True.  This module does NOT modify any existing production files.

Detection strategies:
  1. Door swing: arc/curved segment near a wall gap + nearby "D" tag
  2. Jamb pair: two short perpendicular segments crossing a wall line
     + nearby "W" tag → window opening
  3. Wall gap: two perpendicular segments on opposite sides of a wall
     line with no connecting segment between them
  4. Glazing pair: two closely-spaced parallel lines within a wall gap
  5. Nearby tag: text label near geometric features (D01, W01, etc.)

Each candidate receives a geometry_confidence score based on the
number and quality of supporting features.
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

VERSION = "1.7.1"

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
# Geometry helpers
# ---------------------------------------------------------------------------

def _segment_distance(a: Segment, b: Segment) -> float:
    """Minimum distance between two line segments (midpoint-to-midpoint)."""
    ca = (a.cx, a.cy)
    cb = (b.cx, b.cy)
    return math.hypot(ca[0] - cb[0], ca[1] - cb[1])


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


def _segments_perpendicular(a: Segment, b: Segment, tol: float = 15.0) -> bool:
    """True if two segments are approximately perpendicular."""
    d = abs(a.angle_deg - b.angle_deg) % 180.0
    return abs(d - 90.0) <= tol


def _angle_delta(a: float, b: float) -> float:
    """Minimal angular difference modulo 180."""
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def _nearby_words(
    cx: float,
    cy: float,
    words: Sequence[TextWord],
    max_dist_pt: float = 120.0,
) -> List[TextWord]:
    """Return text words within max_dist_pt of a point."""
    result = []
    for w in words:
        d = math.hypot(w.cx - cx, w.cy - cy)
        if d <= max_dist_pt:
            result.append(w)
    return result


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
# Door detection
# ---------------------------------------------------------------------------

@dataclass
class DoorCandidate:
    """Geometric evidence for a door opening."""
    wall_ref: str = ""
    position_along_wall_m: Optional[float] = None
    width_m: Optional[float] = None
    swing_segment: Optional[Segment] = None
    gap_segments: List[Segment] = field(default_factory=list)
    tag: str = ""
    confidence: float = 0.0
    evidence: List[str] = field(default_factory=list)
    page_no: int = 0


def detect_door_candidates(
    segments: Sequence[Segment],
    wall_lines: Sequence[WallLine],
    words: Sequence[TextWord],
    scale_px_per_m: float = 0.0,
    page_no: int = 0,
) -> List[DoorCandidate]:
    """Detect door openings from swing arcs and wall gaps.

    Door detection heuristic:
      1. Find short segments (~0.8-1.0m) perpendicular to a wall line
         (these are door leaves/jambs)
      2. Optionally find an arc or angled segment nearby (door swing)
      3. Find a tag (D01, D02, etc.) within proximity
      4. The gap between jambs = door width

    Returns candidates with confidence scores.  Does NOT produce
    OpeningEvidence — that is done by plan_opening_candidates().
    """
    candidates: List[DoorCandidate] = []

    # Door leaf width range in PDF points (at typical 50-100 px/m)
    # 0.7m to 1.2m door leaf → 35 to 120 pt at 50 px/m
    MIN_LEAF_PT = 25.0
    MAX_LEAF_PT = 150.0

    # Pre-identify segments that are part of parallel pairs (windows)
    # so they are not also counted as door leaves
    parallel_partner: set = set()
    MIN_WINDOW_PT = 20.0
    MAX_WINDOW_PT = 200.0
    MAX_PAIR_DISTANCE_PT = 100.0
    near_wall_all = [
        s for s in segments
        if MIN_WINDOW_PT <= s.length <= MAX_WINDOW_PT
    ]
    for i, a in enumerate(near_wall_all):
        for j, b in enumerate(near_wall_all):
            if j <= i:
                continue
            if _segments_parallel(a, b) and _segment_distance(a, b) <= MAX_PAIR_DISTANCE_PT:
                parallel_partner.add(id(a))
                parallel_partner.add(id(b))

    for wl in wall_lines:
        wall = wl.segment
        # Find short segments perpendicular to this wall and near it
        for seg in segments:
            if seg is wall:
                continue
            if id(seg) in parallel_partner:
                continue  # part of a parallel pair → window, not door
            if seg.length < MIN_LEAF_PT or seg.length > MAX_LEAF_PT:
                continue
            # Must be approximately perpendicular to wall
            if not _segments_perpendicular(wall, seg):
                continue
            # Must be near the wall line (within ~30 pt)
            dist = _point_segment_distance(seg.cx, seg.cy, wall)
            if dist > 30.0:
                continue

            # This looks like a door leaf or jamb
            tag, tag_cls = _find_tag_near(seg.cx, seg.cy, words)

            # Compute width from the perpendicular segment
            width_pt = seg.length
            width_m = round(width_pt / scale_px_per_m, 3) if scale_px_per_m > 0 else None

            # Position along wall
            # Project door center onto wall direction
            wall_dx = wall.x2 - wall.x1
            wall_dy = wall.y2 - wall.y1
            wall_len = math.hypot(wall_dx, wall_dy)
            if wall_len > 1e-9:
                t = ((seg.cx - wall.x1) * wall_dx + (seg.cy - wall.y1) * wall_dy) / (wall_len * wall_len)
                pos_pt = t * wall_len
                pos_m = round(pos_pt / scale_px_per_m, 3) if scale_px_per_m > 0 else None
            else:
                pos_m = None

            # Find gap segments (perpendicular segments on both sides of the leaf)
            gap_segs = []
            for other in segments:
                if other is seg or other is wall:
                    continue
                if _segments_perpendicular(wall, other):
                    d = _point_segment_distance(other.cx, other.cy, wall)
                    if d <= 30.0 and other.length >= MIN_LEAF_PT:
                        gap_segs.append(other)

            # Build evidence list
            ev = []
            ev.append(f"door_leaf: {seg.length:.1f}pt perpendicular to wall")
            if tag:
                ev.append(f"tag: {tag}")

            # Confidence scoring
            conf = 0.55  # base: perpendicular short segment near wall
            if tag and tag_cls == "door":
                conf = 0.95  # tag confirms door
            elif tag:
                conf = 0.85  # tag present but not door-specific
            elif width_m is not None and 0.6 <= width_m <= 1.2:
                conf = 0.78  # reasonable door width

            candidates.append(DoorCandidate(
                wall_ref=wl.wall_ref or "",
                position_along_wall_m=pos_m,
                width_m=width_m,
                swing_segment=seg,
                gap_segments=gap_segs,
                tag=tag,
                confidence=conf,
                evidence=ev,
                page_no=page_no,
            ))

    return candidates


# ---------------------------------------------------------------------------
# Window / jamb pair detection
# ---------------------------------------------------------------------------

@dataclass
class WindowCandidate:
    """Geometric evidence for a window opening."""
    wall_ref: str = ""
    position_along_wall_m: Optional[float] = None
    width_m: Optional[float] = None
    parallel_segments: List[Segment] = field(default_factory=list)
    tag: str = ""
    confidence: float = 0.0
    evidence: List[str] = field(default_factory=list)
    page_no: int = 0


def detect_window_candidates(
    segments: Sequence[Segment],
    wall_lines: Sequence[WallLine],
    words: Sequence[TextWord],
    scale_px_per_m: float = 0.0,
    page_no: int = 0,
) -> List[WindowCandidate]:
    """Detect window openings from parallel line pairs (glazing/jamb indicators).

    Window detection heuristic:
      1. Find two short parallel segments crossing or near a wall line
         (jamb pair / glazing pair)
      2. The distance between them = rough opening width
      3. Find a tag (W01, W02, etc.) within proximity

    Returns candidates with confidence scores.
    """
    candidates: List[WindowCandidate] = []

    MIN_WINDOW_PT = 20.0
    MAX_WINDOW_PT = 200.0
    MAX_PAIR_DISTANCE_PT = 100.0  # max spacing between parallel jamb lines

    for wl in wall_lines:
        wall = wl.segment
        # Find pairs of short parallel segments near this wall
        near_wall = [
            s for s in segments
            if s is not wall
            and MIN_WINDOW_PT <= s.length <= MAX_WINDOW_PT
            and _segments_perpendicular(wall, s)
            and _point_segment_distance(s.cx, s.cy, wall) <= 30.0
        ]

        # Check all pairs for parallelism
        used: set = set()
        for i, a in enumerate(near_wall):
            if i in used:
                continue
            for j, b in enumerate(near_wall):
                if j <= i or j in used:
                    continue
                if not _segments_parallel(a, b):
                    continue
                pair_dist = _segment_distance(a, b)
                if pair_dist > MAX_PAIR_DISTANCE_PT:
                    continue

                # Found a parallel pair — likely a window
                used.add(i)
                used.add(j)

                # Width from pair spacing
                width_pt = pair_dist
                width_m = round(width_pt / scale_px_per_m, 3) if scale_px_per_m > 0 else None

                # Position: midpoint of the pair projected onto wall
                mid_cx = (a.cx + b.cx) / 2.0
                mid_cy = (a.cy + b.cy) / 2.0
                wall_dx = wall.x2 - wall.x1
                wall_dy = wall.y2 - wall.y1
                wall_len = math.hypot(wall_dx, wall_dy)
                if wall_len > 1e-9:
                    t = ((mid_cx - wall.x1) * wall_dx + (mid_cy - wall.y1) * wall_dy) / (wall_len * wall_len)
                    pos_m = round(t * wall_len / scale_px_per_m, 3) if scale_px_per_m > 0 else None
                else:
                    pos_m = None

                tag, tag_cls = _find_tag_near(mid_cx, mid_cy, words)

                ev = []
                ev.append(f"parallel_pair: {a.length:.1f}pt + {b.length:.1f}pt, spacing={pair_dist:.1f}pt")
                if tag:
                    ev.append(f"tag: {tag}")

                conf = 0.55
                if tag and tag_cls == "window":
                    conf = 0.95
                elif tag:
                    conf = 0.85
                elif width_m is not None and 0.3 <= width_m <= 3.0:
                    conf = 0.78

                candidates.append(WindowCandidate(
                    wall_ref=wl.wall_ref or "",
                    position_along_wall_m=pos_m,
                    width_m=width_m,
                    parallel_segments=[a, b],
                    tag=tag,
                    confidence=conf,
                    evidence=ev,
                    page_no=page_no,
                ))

    return candidates


# ---------------------------------------------------------------------------
# Generic opening detection (wall gaps without specific features)
# ---------------------------------------------------------------------------

@dataclass
class GapCandidate:
    """Geometric evidence for a generic opening (wall gap)."""
    wall_ref: str = ""
    position_along_wall_m: Optional[float] = None
    width_m: Optional[float] = None
    tag: str = ""
    confidence: float = 0.0
    evidence: List[str] = field(default_factory=list)
    page_no: int = 0


def detect_gap_candidates(
    segments: Sequence[Segment],
    wall_lines: Sequence[WallLine],
    words: Sequence[TextWord],
    scale_px_per_m: float = 0.0,
    page_no: int = 0,
) -> List[GapCandidate]:
    """Detect wall gaps — openings without specific door/window features.

    A wall gap is detected when a wall line has perpendicular segments
    on both sides of a region where no wall segment exists.  This is a
    lower-confidence detection than door/window specific features.
    """
    candidates: List[GapCandidate] = []

    MIN_GAP_PT = 30.0
    MAX_GAP_PT = 200.0

    for wl in wall_lines:
        wall = wl.segment
        # Find perpendicular segments near this wall
        perps = [
            s for s in segments
            if s is not wall
            and _segments_perpendicular(wall, s)
            and _point_segment_distance(s.cx, s.cy, wall) <= 30.0
            and MIN_GAP_PT <= s.length <= MAX_GAP_PT
        ]

        # Sort by position along wall
        wall_dx = wall.x2 - wall.x1
        wall_dy = wall.y2 - wall.y1
        wall_len = math.hypot(wall_dx, wall_dy)
        if wall_len < 1e-9:
            continue

        def _pos(s: Segment) -> float:
            t = ((s.cx - wall.x1) * wall_dx + (s.cy - wall.y1) * wall_dy) / (wall_len * wall_len)
            return t * wall_len

        perps.sort(key=_pos)

        # Look for gaps between consecutive perpendicular segments
        for k in range(len(perps) - 1):
            gap_start = _pos(perps[k])
            gap_end = _pos(perps[k + 1])
            gap_len = gap_end - gap_start

            if gap_len < MIN_GAP_PT or gap_len > MAX_GAP_PT:
                continue

            # Check that there's no wall segment filling this gap
            # (a wall segment would be parallel to the wall and within the gap)
            gap_filled = False
            for s in segments:
                if s is wall:
                    continue
                if _segments_parallel(wall, s):
                    s_pos = _pos(s)
                    if gap_start <= s_pos <= gap_end:
                        gap_filled = True
                        break
            if gap_filled:
                continue

            # Found a gap
            mid_pt = (gap_start + gap_end) / 2.0
            mid_cx = wall.x1 + (mid_pt / wall_len) * wall_dx
            mid_cy = wall.y1 + (mid_pt / wall_len) * wall_dy

            width_m = round(gap_len / scale_px_per_m, 3) if scale_px_per_m > 0 else None
            pos_m = round(mid_pt / scale_px_per_m, 3) if scale_px_per_m > 0 else None

            tag, tag_cls = _find_tag_near(mid_cx, mid_cy, words)

            ev = [f"wall_gap: {gap_len:.1f}pt between perpendicular segments"]

            conf = 0.50  # base: gap in wall line
            if tag:
                conf = 0.75
                ev.append(f"tag: {tag}")

            candidates.append(GapCandidate(
                wall_ref=wl.wall_ref or "",
                position_along_wall_m=pos_m,
                width_m=width_m,
                tag=tag,
                confidence=conf,
                evidence=ev,
                page_no=page_no,
            ))

    return candidates


# ---------------------------------------------------------------------------
# Conversion to OpeningEvidence
# ---------------------------------------------------------------------------

def _opening_type_from_tag(tag: str) -> str:
    """Map a tag classification to an OpeningEvidence opening_type."""
    t = _classify_tag(tag)
    if t == "door":
        return OPENING_TYPE_DOOR
    if t == "window":
        return OPENING_TYPE_WINDOW
    return OPENING_TYPE_OTHER


def _opening_type_from_class(detection_class: str) -> str:
    """Map a detection class to an OpeningEvidence opening_type."""
    if detection_class == "door":
        return OPENING_TYPE_DOOR
    if detection_class == "window":
        return OPENING_TYPE_WINDOW
    return OPENING_TYPE_OTHER


def door_to_opening_evidence(cand: DoorCandidate) -> OpeningEvidence:
    """Convert a DoorCandidate to an OpeningEvidence record."""
    ev = OpeningEvidence(
        type_mark=cand.tag if cand.tag else "",
        page_no=cand.page_no,
        wall_ref=cand.wall_ref,
        opening_type=OPENING_TYPE_DOOR,
        width_m=cand.width_m,
        height_m=None,  # plan detection cannot measure height
        dimension_basis=DIMENSION_BASIS_UNKNOWN,
        dimension_source="plan_vector",
        sill_m=0.0,
        position_along_wall_m=cand.position_along_wall_m,
        extraction_method="plan_vector",
        geometry_confidence=cand.confidence,
        dimension_confidence=0.0,  # no dimension measurement from plan
        association_confidence=0.5 if not cand.tag else 0.85,
        deduction_status=DEDUCTION_REVIEW,
        evidence=cand.evidence + ["plan_vector_door_detection"],
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
        geometry_confidence=cand.confidence,
        dimension_confidence=0.0,
        association_confidence=0.5 if not cand.tag else 0.85,
        deduction_status=DEDUCTION_REVIEW,
        evidence=cand.evidence + ["plan_vector_window_detection"],
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
        geometry_confidence=cand.confidence,
        dimension_confidence=0.0,
        association_confidence=0.4 if not cand.tag else 0.75,
        deduction_status=DEDUCTION_REVIEW,
        evidence=cand.evidence + ["plan_vector_gap_detection"],
    )
    ev.set_quantity(1, source="geometric")
    ev.compute_area()
    ev.compute_deduction_status()
    return ev


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
        scale_px_per_m: Calibration factor (PDF points per meter).
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
    doors = detect_door_candidates(segments, wall_lines, words, scale_px_per_m, page_no)
    result.door_count = len(doors)

    # Step 3: Detect window candidates
    windows = detect_window_candidates(segments, wall_lines, words, scale_px_per_m, page_no)
    result.window_count = len(windows)

    # Step 4: Detect gap candidates (only where not already covered by door/window)
    gaps = detect_gap_candidates(segments, wall_lines, words, scale_px_per_m, page_no)
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
                abs(g.position_along_wall_m - pos) < 0.20 and g.wall_ref == ref
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
