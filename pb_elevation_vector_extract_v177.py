"""PlanReader v1.7.7 vector elevation rectangle-closure sidecar (Phase 2A).

Secondary / NAVIGATION cross-check path for elevation sheets.  The LAGO
CD300x elevation pages are NATIVE VECTOR POLYLINE drawings with NO discrete
're' rectangle objects.  This sidecar RECOVERS candidate closed rectangles by
REASSEMBLING them from axis-aligned line segments (2 horizontal + 2 vertical
sides), working in PDF-POINT coordinates.

Purpose
-------
Provide a vector-geometry estimate of rectangular openings that can be
cross-checked against the raster (rendered-pixel) primary path at the same
physical scale.  It is an OBSERVATION layer: it never creates a B1 instance
and never sets deduct=True.

Safety contract
---------------
  - Works in PDF-POINT space; calibration must be in pdf_point space for
    metred output, and must NEVER be mixed with a render_pixel calibration.
  - Rectangles are only recovered where the four sides are actually present
    as drawn linework (structural closure), never inferred from proportions.
  - Generic recovered rectangles stay dimension_basis="unknown".
  - When calibration is invalid the page is non-dimensional and measured
    width/height are not reported (None).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pb_elevation_calibration_v177 import (
    Calibration,
    COORD_SPACE_PDF_POINT,
)

VERSION = "1.7.7"

STATUS_REVIEW = "review"
STATUS_REJECTED = "rejected"


@dataclass(frozen=True)
class VectorRectCandidate:
    """A closed rectangle recovered from vector line segments (points).

    Geometry observation ONLY.
    """
    source_filename: str
    source_page: int
    drawing_ref: str
    elevation_side: str
    bbox: Tuple[float, float, float, float]   # (x0,y0,x1,y1) in PDF points
    centroid: Tuple[float, float]
    coord_space: str
    calibration_method: str
    width_m: Optional[float] = None
    height_m: Optional[float] = None
    dimension_basis: str = "unknown"
    extraction_method: str = "vector_line_closure"
    geometry_confidence: float = 0.6
    label: str = ""
    review_status: str = STATUS_REVIEW
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source_filename": self.source_filename,
            "source_page": self.source_page,
            "drawing_ref": self.drawing_ref,
            "elevation_side": self.elevation_side,
            "bbox": list(self.bbox),
            "centroid": list(self.centroid),
            "coord_space": self.coord_space,
            "width_m": self.width_m,
            "height_m": self.height_m,
            "dimension_basis": self.dimension_basis,
            "calibration_method": self.calibration_method,
            "extraction_method": self.extraction_method,
            "geometry_confidence": self.geometry_confidence,
            "label": self.label,
            "review_status": self.review_status,
            "notes": list(self.notes),
        }


def _classify_segment(
    seg: Dict[str, Any], angle_tol_deg: float = 5.0, min_len_pt: float = 2.0
) -> Optional[Tuple[str, float, float, float]]:
    """Classify a segment as horizontal or vertical and return its extent.

    Returns (axis, fixed_coord, min_other, max_other) or None if not
    axis-aligned or too short.
    """
    x1, y1, x2, y2 = (float(seg.get("x1", 0)), float(seg.get("y1", 0)),
                      float(seg.get("x2", 0)), float(seg.get("y2", 0)))
    if math.hypot(x2 - x1, y2 - y1) < min_len_pt:
        return None
    dx, dy = x2 - x1, y2 - y1
    if abs(dy) <= abs(dx) * math.tan(math.radians(angle_tol_deg)):
        # horizontal-ish: fixed y
        return ("h", (y1 + y2) / 2.0, min(x1, x2), max(x1, x2))
    if abs(dx) <= abs(dy) * math.tan(math.radians(angle_tol_deg)):
        # vertical-ish: fixed x
        return ("v", (x1 + x2) / 2.0, min(y1, y2), max(y1, y2))
    return None


# Gap tolerance (PDF points): collinear segments are merged into ONE
# continuous line ONLY when they overlap or touch within this distance.
# A real gap between two collinear fragments means they are SEPARATE sides —
# they must NOT be bridged into an invented continuous line (which could let
# two separated fragments form a rectangle side that does not actually exist).
_GAP_TOLERANCE_PT = 2.0


def _cluster_lines(extents: Sequence[Tuple[float, float, float]],
                   tol: float = 1.5) -> List[Tuple[float, float, float]]:
    """Cluster collinear segments into continuous lines by fixed-coordinate proximity.

    extents: (fixed_coord, lo, hi) axis-aligned segments.

    Segments are first grouped into bands by fixed-coordinate proximity
    (within ``tol``).  Within a band, intervals are merged ONLY when they
    overlap or touch within ``_GAP_TOLERANCE_PT``.  Spatially separated
    collinear fragments (a real gap wider than the tolerance) are kept as
    SEPARATE lines — they are never bridged into an invented continuous side.

    Returns merged (fixed_coord, min_lo, max_hi) lines; each returned line is
    guaranteed to be a contiguous coverage (no internal gap beyond tolerance).
    """
    # 1) Bucket by fixed-coordinate proximity.
    buckets: List[List[Tuple[float, float, float]]] = []
    for fixed, lo, hi in sorted(extents, key=lambda e: (e[0], e[1])):
        placed = False
        for b in buckets:
            if abs(b[0][0] - fixed) <= tol:
                b.append((fixed, lo, hi))
                placed = True
                break
        if not placed:
            buckets.append([(fixed, lo, hi)])

    # 2) Within each band, merge only intervals that overlap/touch (gap-safe).
    result: List[Tuple[float, float, float]] = []
    for bucket in buckets:
        fixed_rep = bucket[0][0]
        intervals = sorted((lo, hi) for _, lo, hi in bucket)
        cur_lo, cur_hi = intervals[0]
        for lo, hi in intervals[1:]:
            if lo - cur_hi <= _GAP_TOLERANCE_PT:
                # overlap or touch (within documented tolerance) → contiguous
                if hi > cur_hi:
                    cur_hi = hi
            else:
                # real gap: separated fragments → separate continuous lines
                result.append((fixed_rep, cur_lo, cur_hi))
                cur_lo, cur_hi = lo, hi
        result.append((fixed_rep, cur_lo, cur_hi))
    return result


def recover_vector_rects(
    segments: Sequence[Dict[str, Any]],
    calibration: Calibration,
    *,
    source_filename: str = "",
    source_page: int = 0,
    drawing_ref: str = "",
    elevation_side: str = "",
    angle_tol_deg: float = 5.0,
    min_side_pt: float = 4.0,
    max_cells: int = 20000,
) -> List[VectorRectCandidate]:
    """Recover closed rectangles from axis-aligned line segments.

    Only returns cells whose four sides are present as drawn linework
    (closure).  Calibration MUST be in pdf_point coordinate space; if it is
    not, measured widths/heights are reported as None to avoid mixing
    coordinate spaces.

    Args:
        segments: list of segment dicts with x1,y1,x2,y2 (PDF-point units).
        calibration: Calibration for this page.
        ...provenance fields...
        angle_tol_deg: allow segments within this angle of axis-aligned.
        min_side_pt: drop cells with any side shorter than this.
        max_cells: safety cap on cells examined.

    Returns:
        List of VectorRectCandidate.
    """
    # Coordinate-space guard: vector geometry is in PDF points; we only
    # convert to metres when the calibration is in pdf_point space.
    dimensional = (calibration.valid and calibration.coord_space == COORD_SPACE_PDF_POINT)
    pt_per_m = calibration.pt_per_m if dimensional else 0.0

    h_extents: List[Tuple[float, float, float]] = []
    v_extents: List[Tuple[float, float, float]] = []
    for seg in segments:
        axis, fixed, lo, hi = _classify_segment(
            seg, angle_tol_deg=angle_tol_deg, min_len_pt=min_side_pt) or (None, 0, 0, 0)
        if axis == "h":
            h_extents.append((fixed, lo, hi))
        elif axis == "v":
            v_extents.append((fixed, lo, hi))

    h_lines = _cluster_lines(h_extents)
    v_lines = _cluster_lines(v_extents)
    if not h_lines or not v_lines:
        return []

    # To form a cell [x_l, x_r] x [y_b, y_t]:
    #   - a horizontal line at y_t spans include x_l..x_r
    #   - a horizontal line at y_b spans include x_l..x_r
    #   - a vertical line at x_l spans include y_b..y_t
    #   - a vertical line at x_r spans include y_b..y_t
    # Iterate over horizontal pairs, then index vertical lines by x for O(1)
    # lookups.  Cap the work to avoid pathological pages.
    v_by_x: Dict[float, Tuple[float, float]] = {}
    for fixed, lo, hi in v_lines:
        v_by_x.setdefault(round(fixed, 3), (lo, hi))

    y_coords = sorted(set(round(h, 3) for h, _, _ in h_lines))
    h_by_y: Dict[float, List[Tuple[float, float]]] = {}
    for fixed, lo, hi in h_lines:
        h_by_y.setdefault(round(fixed, 3), []).append((lo, hi))

    cells: List[Tuple[float, float, float, float]] = []
    truncated = False  # explicit truncation/review signal (never return raw tuples)
    y_pairs = list(zip(y_coords, y_coords[1:]))
    cells_run = 0
    for y_b, y_t in y_pairs:
        if y_t - y_b < min_side_pt:
            continue
        # candidate x boundaries must be spanned by BOTH horizontal lines
        top_lines = h_by_y.get(y_t)
        bot_lines = h_by_y.get(y_b)
        if not top_lines or not bot_lines:
            continue
        # gather all x boundaries where both horizontals actually span
        xs = set()
        for (t_lo, t_hi) in top_lines:
            # vertical lines at x within [t_lo, t_hi] - but we need them to
            # also be spanned by the bottom horizontal; collect candidate xs
            for x_fixed in v_by_x:
                if (t_lo - 1e-6 <= x_fixed <= t_hi + 1e-6):
                    xs.add(x_fixed)
        for (b_lo, b_hi) in bot_lines:
            for x_fixed in v_by_x:
                if (b_lo - 1e-6 <= x_fixed <= b_hi + 1e-6):
                    xs.add(x_fixed)

        x_sorted = sorted(xs)
        for x_l, x_r in zip(x_sorted, x_sorted[1:]):
            if x_r - x_l < min_side_pt:
                continue
            cells_run += 1
            if cells_run > max_cells:
                # Work cap hit: stop collecting, flag truncation.  We do NOT
                # return raw tuples here — we always return a properly
                # constructed List[VectorRectCandidate] with an explicit
                # truncation/review signal (see below).
                truncated = True
                break
            # both verticals must span y_b..y_t AND both horizontals must
            # span x_l..x_r (already ensured via xs membership + the bot/top
            # loops enforce horizontal span for their own line, but we must
            # confirm the PAIR shares both xs).
            vl = v_by_x.get(round(x_l, 3))
            vr = v_by_x.get(round(x_r, 3))
            if not vl or not vr:
                continue
            if not (vl[0] - 1e-6 <= y_b and y_t <= vl[1] + 1e-6):
                continue
            if not (vr[0] - 1e-6 <= y_b and y_t <= vr[1] + 1e-6):
                continue
            # confirm both horizontals span x_l..x_r
            spans_both_h = False
            if any((tl - 1e-6 <= x_l and x_r <= th + 1e-6) for tl, th in top_lines) and \
               any((bl - 1e-6 <= x_l and x_r <= bh + 1e-6) for bl, bh in bot_lines):
                spans_both_h = True
            if not spans_both_h:
                continue
            cells.append((x_l, y_b, x_r, y_t))
        if truncated:
            break

    candidates: List[VectorRectCandidate] = []
    base_notes = ["closed rectangle recovered from drawn linework; generic"]
    if truncated:
        base_notes.append(
            f"WORK-CAP HIT at {max_cells} cells — result TRUNCATED; review required")
    for (x_l, y_b, x_r, y_t) in cells:
        width_m = height_m = None
        if dimensional:
            width_m = round((x_r - x_l) / pt_per_m, 4)
            height_m = round((y_t - y_b) / pt_per_m, 4)
        candidates.append(VectorRectCandidate(
            source_filename=source_filename,
            source_page=source_page,
            drawing_ref=drawing_ref,
            elevation_side=elevation_side,
            bbox=(x_l, y_b, x_r, y_t),
            centroid=((x_l + x_r) / 2.0, (y_b + y_t) / 2.0),
            coord_space=COORD_SPACE_PDF_POINT,
            calibration_method=calibration.method,
            width_m=width_m,
            height_m=height_m,
            dimension_basis="unknown",
            extraction_method="vector_line_closure",
            geometry_confidence=0.6,
            review_status=STATUS_REVIEW,
            notes=list(base_notes),
        ))
    return candidates
