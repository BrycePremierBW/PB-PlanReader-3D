"""PlanReader v1.7.3 plan-vector opening candidate detection.

Phase B1 of Priority 5: reads raw PDF geometric features (line segments,
text words) and detects opening candidates — door jambs/leaves,
window jamb pairs, wall discontinuities, nearby tags.

Outputs OpeningEvidence candidates ONLY.  No take-off changes, no
deduct=True.  This module does NOT modify any existing production files.

Detection strategies:
  1. Door jamb/leaf: short perpendicular segment near a wall line,
     corroborated by a nearby door tag or wall discontinuity
  2. Window jamb pair: two short parallel segments perpendicular to a
     wall, wall-local (not global hatch detection), corroborated by window tag
  3. Wall discontinuity: two collinear wall segments with a gap between
     their endpoints — a real opening in the wall fabric
  4. Nearby tag: text label near geometric features (D01/W01/ED01/ID01/EW01)
     provides TYPE/semantic evidence only, not geometry confidence

Each candidate carries three independent confidence channels:
  - geometry_confidence: strength of the geometric signal
  - association_confidence: quality of wall/position evidence
  - semantic_confidence: strength of the tag/label evidence

Tag proximity sets type_mark and semantic confidence but must NOT
inflate geometry or association confidence.

Tag/type compatibility:
  - door geometry may only assign Dxx/EDxx/IDxx marks
  - window geometry may only assign Wxx/EWxx/IWxx marks
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
    record_plan_observation,
)

VERSION = "1.7.3"

@dataclass(frozen=True)
class Segment:
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


# Real project tags use optional E/I scope prefixes before D/W.
_TAG_DOOR_RE = re.compile(r"^(?:D|ED|ID)\d{1,3}$", re.IGNORECASE)
_TAG_WINDOW_RE = re.compile(r"^(?:W|EW|IW)\d{1,3}$", re.IGNORECASE)


def _classify_tag(text: str) -> str:
    t = text.strip().upper()
    if _TAG_DOOR_RE.fullmatch(t):
        return "door"
    if _TAG_WINDOW_RE.fullmatch(t):
        return "window"
    return ""


def _resolve_scale(scale_info: Optional[Dict[str, Any]],
                   scale_px_per_m: float = 0.0) -> Tuple[float, float]:
    if scale_info and isinstance(scale_info, dict):
        px_per_m = float(scale_info.get("px_per_m") or 0.0)
        render_zoom = float(scale_info.get("render_zoom") or 1.0)
        if render_zoom <= 0:
            render_zoom = 1.0
        if px_per_m > 0:
            pt_per_m = px_per_m / render_zoom
            m_per_pt = 1.0 / pt_per_m if pt_per_m > 0 else 0.0
            return pt_per_m, m_per_pt
    if scale_px_per_m > 0:
        return scale_px_per_m, 1.0 / scale_px_per_m
    return 0.0, 0.0


def _pt_to_m(pt: float, m_per_pt: float) -> Optional[float]:
    if m_per_pt <= 0:
        return None
    return round(pt * m_per_pt, 3)


def _segment_distance(a: Segment, b: Segment) -> float:
    return math.hypot(a.cx - b.cx, a.cy - b.cy)


def _point_segment_distance(px: float, py: float, s: Segment) -> float:
    dx, dy = s.x2 - s.x1, s.y2 - s.y1
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-12:
        return math.hypot(px - s.x1, py - s.y1)
    t = max(0.0, min(1.0, ((px - s.x1) * dx + (py - s.y1) * dy) / length_sq))
    proj_x = s.x1 + t * dx
    proj_y = s.y1 + t * dy
    return math.hypot(px - proj_x, py - proj_y)


def _segments_parallel(a: Segment, b: Segment, tol: float = 5.0) -> bool:
    d = abs(a.angle_deg - b.angle_deg) % 180.0
    return min(d, 180.0 - d) <= tol


def _line_perp_distance(px: float, py: float, s: Segment) -> float:
    dx, dy = s.x2 - s.x1, s.y2 - s.y1
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-12:
        return math.hypot(px - s.x1, py - s.y1)
    return abs((dy * px - dx * py + s.x2 * s.y1 - s.y2 * s.x1) / math.sqrt(length_sq))


def _segments_perpendicular(a: Segment, b: Segment, tol: float = 15.0) -> bool:
    d = abs(a.angle_deg - b.angle_deg) % 180.0
    return abs(d - 90.0) <= tol


def _find_tag_near(
    cx: float,
    cy: float,
    words: Sequence[TextWord],
    max_dist_pt: float = 120.0,
) -> Tuple[str, str]:
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


def _canonical_wall_direction(seg: Segment) -> Tuple[float, float, float, float]:
    angle = seg.angle_deg
    if angle < 45.0 or angle >= 135.0:
        if seg.x1 <= seg.x2:
            return (seg.x2 - seg.x1, seg.y2 - seg.y1, seg.x1, seg.y1)
        return (seg.x1 - seg.x2, seg.y1 - seg.y2, seg.x2, seg.y2)
    if seg.y1 <= seg.y2:
        return (seg.x2 - seg.x1, seg.y2 - seg.y1, seg.x1, seg.y1)
    return (seg.x1 - seg.x2, seg.y1 - seg.y2, seg.x2, seg.y2)


def _position_along_wall(px: float, py: float,
                         wall: Segment) -> Optional[float]:
    dx, dy, ox, oy = _canonical_wall_direction(wall)
    wall_len_sq = dx * dx + dy * dy
    if wall_len_sq < 1e-12:
        return None
    t = ((px - ox) * dx + (py - oy) * dy) / wall_len_sq
    return t * math.sqrt(wall_len_sq)


@dataclass
class WallLine:
    segment: Segment
    wall_ref: str = ""
    side: str = ""


def detect_wall_lines(
    segments: Sequence[Segment],
    min_length_pt: float = 200.0,
) -> List[WallLine]:
    long_segs = [s for s in segments if s.length >= min_length_pt]
    long_segs.sort(key=lambda s: s.length, reverse=True)
    return [WallLine(segment=s) for s in long_segs]


def _resolve_wall_ref(wl_a: WallLine, wl_b: WallLine) -> str:
    a_ref = wl_a.wall_ref.strip()
    b_ref = wl_b.wall_ref.strip()
    if a_ref and b_ref:
        return a_ref if a_ref == b_ref else ""
    return a_ref or b_ref


@dataclass
class DoorCandidate:
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


def _physical_pt(physical_m: float, pt_per_m: float, fallback: float) -> float:
    return physical_m * pt_per_m if pt_per_m > 0 else fallback


def detect_door_candidates(
    segments: Sequence[Segment],
    wall_lines: Sequence[WallLine],
    words: Sequence[TextWord],
    scale_info: Optional[Dict[str, Any]] = None,
    scale_px_per_m: float = 0.0,
    page_no: int = 0,
) -> List[DoorCandidate]:
    candidates: List[DoorCandidate] = []
    pt_per_m, m_per_pt = _resolve_scale(scale_info, scale_px_per_m)
    MIN_LEAF_PT = _physical_pt(0.3, pt_per_m, 25.0)
    MAX_LEAF_PT = _physical_pt(1.5, pt_per_m, 150.0)
    WALL_PROXIMITY_PT = _physical_pt(0.6, pt_per_m, 30.0)
    DOOR_TAG_RADIUS_PT = _physical_pt(0.6, pt_per_m, 120.0)

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
                if _segments_parallel(a, b) and _segment_distance(a, b) <= _physical_pt(2.0, pt_per_m, 100.0):
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
                continue
            if seg.length < MIN_LEAF_PT or seg.length > MAX_LEAF_PT:
                continue
            if not _segments_perpendicular(wall, seg):
                continue
            dist = _point_segment_distance(seg.cx, seg.cy, wall)
            if dist > WALL_PROXIMITY_PT:
                continue

            tag, tag_cls = _find_tag_near(seg.cx, seg.cy, words, max_dist_pt=DOOR_TAG_RADIUS_PT)
            assigned_tag = tag if tag and tag_cls == "door" else ""
            width_m = _pt_to_m(seg.length, m_per_pt)
            pos_pt = ((seg.cx - wall_ox) * wall_dx + (seg.cy - wall_oy) * wall_dy) / wall_len
            pos_m = _pt_to_m(pos_pt, m_per_pt)

            geom_conf = 0.45
            if width_m is not None and 0.6 <= width_m <= 1.2:
                geom_conf = 0.60
            assoc_conf = 0.70 if wl.wall_ref else 0.30
            sem_conf = 0.0
            if tag and tag_cls == "door":
                sem_conf = 0.95
            elif tag and tag_cls == "window":
                sem_conf = 0.30
            elif tag:
                sem_conf = 0.60

            ev = [f"jamb_leaf: {seg.length:.1f}pt perpendicular to wall"]
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


@dataclass
class WindowCandidate:
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


def _global_tag_assignment(
    surviving: List[Tuple],
    words: Sequence[TextWord],
    tag_radius_pt: float,
) -> Dict[int, str]:
    w_tag_indices = [i for i, w in enumerate(words)
                     if _classify_tag(w.text) == "window"]
    pair_cx_cy = []
    for (i, j, a, b), _, _ in surviving:
        pair_cx_cy.append(((a.cx + b.cx) / 2.0, (a.cy + b.cy) / 2.0))
    tag_pair_matches: List[Tuple[float, int, int]] = []
    for pi, (pcx, pcy) in enumerate(pair_cx_cy):
        for ti in w_tag_indices:
            d = math.hypot(pcx - words[ti].cx, pcy - words[ti].cy)
            if d <= tag_radius_pt:
                tag_pair_matches.append((d, ti, pi))
    tag_pair_matches.sort(key=lambda x: x[0])
    used_tags_idx: set = set()
    used_pairs: set = set()
    pair_assigned_tag: Dict[int, str] = {}
    for dist, ti, pi in tag_pair_matches:
        if ti in used_tags_idx or pi in used_pairs:
            continue
        pair_assigned_tag[pi] = words[ti].text
        used_tags_idx.add(ti)
        used_pairs.add(pi)
    return pair_assigned_tag


def _compute_hatch_and_tags(
    pairs: List[Tuple[int, int, Segment, Segment]],
    wall_dx: float, wall_dy: float,
    wall_ox: float, wall_oy: float,
    wall_len: float,
    pt_per_m: float,
) -> Tuple[bool, List[Tuple[float, float]]]:
    is_hatch = False
    pair_cx_cy: List[Tuple[float, float]] = []
    if len(pairs) < 2:
        return is_hatch, pair_cx_cy
    pair_midpoints = []
    pair_jamb_spacings = []
    for _, _, a, b in pairs:
        mcx = (a.cx + b.cx) / 2.0
        mcy = (a.cy + b.cy) / 2.0
        pair_midpoints.append(((mcx - wall_ox) * wall_dx + (mcy - wall_oy) * wall_dy) / wall_len)
        pair_jamb_spacings.append(_segment_distance(a, b))
        pair_cx_cy.append((mcx, mcy))
    indexed_pairs = sorted(enumerate(pair_midpoints), key=lambda x: x[1])
    pair_midpoints_sorted = [pair_midpoints[orig_i] for orig_i, _ in indexed_pairs]
    gaps_between = [pair_midpoints_sorted[k + 1] - pair_midpoints_sorted[k]
                    for k in range(len(pair_midpoints_sorted) - 1)]
    mean_jamb = sum(pair_jamb_spacings) / len(pair_jamb_spacings)
    if gaps_between and mean_jamb > 0:
        max_gap = max(gaps_between)
        gap_ratio = max_gap / mean_jamb
        scale_min_gap_pt = 1.5 * pt_per_m if pt_per_m > 0 else 0
        if len(gaps_between) >= 2:
            if gap_ratio <= 2.5:
                if not (scale_min_gap_pt > 0 and max_gap >= scale_min_gap_pt):
                    is_hatch = True
        else:
            if scale_min_gap_pt > 0 and max_gap < scale_min_gap_pt:
                is_hatch = True
            elif scale_min_gap_pt <= 0 and gap_ratio <= 2.5:
                is_hatch = True
    return is_hatch, pair_cx_cy


def detect_window_candidates(
    segments: Sequence[Segment],
    wall_lines: Sequence[WallLine],
    words: Sequence[TextWord],
    scale_info: Optional[Dict[str, Any]] = None,
    scale_px_per_m: float = 0.0,
    page_no: int = 0,
) -> List[WindowCandidate]:
    candidates: List[WindowCandidate] = []
    pt_per_m, m_per_pt = _resolve_scale(scale_info, scale_px_per_m)
    MIN_WINDOW_PT = _physical_pt(0.3, pt_per_m, 25.0)
    MAX_WINDOW_PT = _physical_pt(3.0, pt_per_m, 200.0)
    MAX_PAIR_DISTANCE_PT = _physical_pt(1.2, pt_per_m, 100.0)
    WALL_PROXIMITY_PT = _physical_pt(0.6, pt_per_m, 30.0)
    TAG_RADIUS_PT = _physical_pt(2.4, pt_per_m, 120.0)
    all_surviving: List[Tuple] = []

    for wl in wall_lines:
        wall = wl.segment
        near_wall = [
            s for s in segments
            if s is not wall
            and MIN_WINDOW_PT <= s.length <= MAX_WINDOW_PT
            and _segments_perpendicular(wall, s)
            and _point_segment_distance(s.cx, s.cy, wall) <= WALL_PROXIMITY_PT
        ]
        if not near_wall:
            continue
        wall_dx, wall_dy, wall_ox, wall_oy = _canonical_wall_direction(wall)
        wall_len = math.hypot(wall_dx, wall_dy)
        if wall_len < 1e-9:
            continue
        def _pos(s: Segment) -> float:
            return ((s.cx - wall_ox) * wall_dx + (s.cy - wall_oy) * wall_dy) / wall_len
        sorted_segs = sorted(near_wall, key=_pos)
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
                used.add(i)
                used.add(j)
                pairs.append((i, j, a, b))
                break
        is_hatch, _ = _compute_hatch_and_tags(
            pairs, wall_dx, wall_dy, wall_ox, wall_oy, wall_len, pt_per_m,
        )
        for pair in pairs:
            all_surviving.append((pair, wl, is_hatch))

    global_assigned = _global_tag_assignment(all_surviving, words, TAG_RADIUS_PT)
    surviving_final = [
        (orig_idx, pair, wl, is_hatch_multi)
        for orig_idx, (pair, wl, is_hatch_multi) in enumerate(all_surviving)
        if not is_hatch_multi or global_assigned.get(orig_idx, "") != ""
    ]

    for _si, (orig_idx, pair, wl, _is_hatch_multi) in enumerate(surviving_final):
        i, j, a, b = pair
        pair_dist = _segment_distance(a, b)
        width_m = _pt_to_m(pair_dist, m_per_pt)
        mid_cx = (a.cx + b.cx) / 2.0
        mid_cy = (a.cy + b.cy) / 2.0
        wall = wl.segment
        wall_dx, wall_dy, wall_ox, wall_oy = _canonical_wall_direction(wall)
        wall_len = math.hypot(wall_dx, wall_dy)
        pos_pt = ((mid_cx - wall_ox) * wall_dx + (mid_cy - wall_oy) * wall_dy) / wall_len if wall_len > 1e-9 else 0
        pos_m = _pt_to_m(pos_pt, m_per_pt)
        assigned_tag = global_assigned.get(orig_idx, "")
        tag_cls = _classify_tag(assigned_tag) if assigned_tag else ""
        raw_tag = ""
        raw_tag_cls = ""
        if not assigned_tag:
            raw_tag, raw_tag_cls = _find_tag_near(mid_cx, mid_cy, words,
                                                  max_dist_pt=TAG_RADIUS_PT)
        geom_conf = 0.60
        if width_m is not None and 0.3 <= width_m <= 3.0:
            geom_conf = 0.70
        assoc_conf = 0.70 if wl.wall_ref else 0.30
        sem_conf = 0.0
        if assigned_tag and tag_cls == "window":
            sem_conf = 0.95
        elif assigned_tag and tag_cls == "door":
            sem_conf = 0.30
        elif assigned_tag:
            sem_conf = 0.60
        elif raw_tag and raw_tag_cls == "door":
            sem_conf = 0.30
        ev = [f"jamb_pair: {a.length:.1f}pt + {b.length:.1f}pt, spacing={pair_dist:.1f}pt"]
        if assigned_tag:
            ev.append(f"tag: {assigned_tag} (window-compatible)")
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


@dataclass
class GapCandidate:
    wall_ref: str = ""
    position_along_wall_m: Optional[float] = None
    width_m: Optional[float] = None
    tag: str = ""
    geometry_confidence: float = 0.0
    association_confidence: float = 0.0
    semantic_confidence: float = 0.0
    evidence: List[str] = field(default_factory=list)
    page_no: int = 0
    centroid_x: Optional[float] = None
    centroid_y: Optional[float] = None


def detect_gap_candidates(
    segments: Sequence[Segment],
    wall_lines: Sequence[WallLine],
    words: Sequence[TextWord],
    scale_info: Optional[Dict[str, Any]] = None,
    scale_px_per_m: float = 0.0,
    page_no: int = 0,
) -> List[GapCandidate]:
    candidates: List[GapCandidate] = []
    pt_per_m, m_per_pt = _resolve_scale(scale_info, scale_px_per_m)
    MIN_GAP_PT = _physical_pt(0.5, pt_per_m, 30.0)
    MAX_GAP_PT = _physical_pt(2.0, pt_per_m, 200.0)
    GAP_TAG_RADIUS_PT = _physical_pt(0.6, pt_per_m, 120.0)
    COLLINEAR_TOL_DEG = 10.0
    COLLINEAR_OFFSET_PT = 15.0
    ENDPOINT_PROXIMITY_PT = 5.0

    for i, wl_a in enumerate(wall_lines):
        a = wl_a.segment
        for j, wl_b in enumerate(wall_lines):
            if j <= i:
                continue
            b = wl_b.segment
            if not _segments_parallel(a, b, tol=COLLINEAR_TOL_DEG):
                continue
            perp_dist = _line_perp_distance(b.cx, b.cy, a)
            if perp_dist > COLLINEAR_OFFSET_PT:
                continue
            gap_wall_ref = _resolve_wall_ref(wl_a, wl_b)
            a_angle = a.angle_deg
            cos_a = math.cos(math.radians(a_angle))
            sin_a = math.sin(math.radians(a_angle))
            a_starts = [(a.x1 * cos_a + a.y1 * sin_a), (a.x2 * cos_a + a.y2 * sin_a)]
            b_starts = [(b.x1 * cos_a + b.y1 * sin_a), (b.x2 * cos_a + b.y2 * sin_a)]
            shared_min_t = min(a_starts + b_starts)
            shared_origin_x = shared_min_t * cos_a
            shared_origin_y = shared_min_t * sin_a
            wall_dx = cos_a
            wall_dy = sin_a
            wall_ox = shared_origin_x
            wall_oy = shared_origin_y
            wall_len = a.length + b.length

            def _project(seg: Segment) -> Tuple[float, float]:
                t1 = ((seg.x1 - wall_ox) * wall_dx + (seg.y1 - wall_oy) * wall_dy) / wall_len
                t2 = ((seg.x2 - wall_ox) * wall_dx + (seg.y2 - wall_oy) * wall_dy) / wall_len
                return (t1 * wall_len, t2 * wall_len)

            a_proj = _project(a)
            b_proj = _project(b)
            a_min, a_max = min(a_proj), max(a_proj)
            b_min, b_max = min(b_proj), max(b_proj)
            if a_max < b_min:
                gap_start_pt = a_max
                gap_end_pt = b_min
            elif b_max < a_min:
                gap_start_pt = b_max
                gap_end_pt = a_min
            else:
                continue
            gap_len_pt = gap_end_pt - gap_start_pt
            if gap_len_pt < MIN_GAP_PT or gap_len_pt > MAX_GAP_PT:
                continue
            gap_filled = False
            for s in segments:
                if s is a or s is b:
                    continue
                if _segments_parallel(a, s, tol=COLLINEAR_TOL_DEG):
                    s_proj = _project(s)
                    s_min, s_max = min(s_proj), max(s_proj)
                    if s_min <= gap_start_pt + ENDPOINT_PROXIMITY_PT and s_max >= gap_end_pt - ENDPOINT_PROXIMITY_PT:
                        gap_filled = True
                        break
            if gap_filled:
                continue
            if a_max < b_min:
                a_end = max(a_proj)
                b_end = min(b_proj)
            else:
                a_end = min(a_proj)
                b_end = max(b_proj)

            def _interp(seg: Segment, t_proj: float) -> Tuple[float, float]:
                t1 = ((seg.x1 - shared_origin_x) * cos_a + (seg.y1 - shared_origin_y) * sin_a)
                t2 = ((seg.x2 - shared_origin_x) * cos_a + (seg.y2 - shared_origin_y) * sin_a)
                if abs(t2 - t1) < 1e-9:
                    return (seg.cx, seg.cy)
                frac = (t_proj - t1) / (t2 - t1)
                frac = max(0.0, min(1.0, frac))
                return (seg.x1 + frac * (seg.x2 - seg.x1),
                        seg.y1 + frac * (seg.y2 - seg.y1))

            end_a_2d = _interp(a, a_end)
            end_b_2d = _interp(b, b_end)
            mid_cx = (end_a_2d[0] + end_b_2d[0]) / 2.0
            mid_cy = (end_a_2d[1] + end_b_2d[1]) / 2.0
            width_m = _pt_to_m(gap_len_pt, m_per_pt)
            mid_pt = (gap_start_pt + gap_end_pt) / 2.0
            pos_m = _pt_to_m(mid_pt, m_per_pt)
            tag, tag_cls = _find_tag_near(mid_cx, mid_cy, words,
                                          max_dist_pt=GAP_TAG_RADIUS_PT)
            geom_conf = 0.75
            assoc_conf = 0.70 if gap_wall_ref else 0.30
            sem_conf = 0.80 if tag else 0.0
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
                centroid_x=mid_cx,
                centroid_y=mid_cy,
            ))
    return candidates


def door_to_opening_evidence(cand: DoorCandidate) -> OpeningEvidence:
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
    if cand.jamb_segment is not None:
        cx = (cand.jamb_segment.x1 + cand.jamb_segment.x2) / 2.0
        cy = (cand.jamb_segment.y1 + cand.jamb_segment.y2) / 2.0
        ev.compute_plan_geometry_signature(centroid_x=cx, centroid_y=cy)
    record_plan_observation(ev)
    return ev


def window_to_opening_evidence(cand: WindowCandidate) -> OpeningEvidence:
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
    if cand.parallel_segments:
        midpoints_x = []
        midpoints_y = []
        for seg in cand.parallel_segments:
            midpoints_x.append((seg.x1 + seg.x2) / 2.0)
            midpoints_y.append((seg.y1 + seg.y2) / 2.0)
        cx = sum(midpoints_x) / len(midpoints_x)
        cy = sum(midpoints_y) / len(midpoints_y)
        ev.compute_plan_geometry_signature(centroid_x=cx, centroid_y=cy)
    record_plan_observation(ev)
    return ev


def gap_to_opening_evidence(cand: GapCandidate) -> OpeningEvidence:
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
    if cand.centroid_x is not None and cand.centroid_y is not None:
        ev.compute_plan_geometry_signature(
            centroid_x=cand.centroid_x,
            centroid_y=cand.centroid_y,
        )
    record_plan_observation(ev)
    return ev


def _opening_type_from_tag(tag: str) -> str:
    t = _classify_tag(tag)
    if t == "door":
        return OPENING_TYPE_DOOR
    if t == "window":
        return OPENING_TYPE_WINDOW
    return OPENING_TYPE_OTHER


@dataclass
class PlanOpeningDetectionResult:
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
    result = PlanOpeningDetectionResult()
    result.total_features = len(segments)
    if wall_lines is None:
        wall_lines = detect_wall_lines(segments, min_length_pt=min_wall_length_pt)
    result.wall_lines_found = len(wall_lines)
    if not wall_lines:
        result.notes.append("No wall lines detected — cannot find openings without wall context")
        return result

    doors = detect_door_candidates(
        segments, wall_lines, words,
        scale_info=scale_info, scale_px_per_m=scale_px_per_m,
        page_no=page_no,
    )
    result.door_count = len(doors)
    windows = detect_window_candidates(
        segments, wall_lines, words,
        scale_info=scale_info, scale_px_per_m=scale_px_per_m,
        page_no=page_no,
    )
    result.window_count = len(windows)
    gaps = detect_gap_candidates(
        segments, wall_lines, words,
        scale_info=scale_info, scale_px_per_m=scale_px_per_m,
        page_no=page_no,
    )

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
