"""PlanReader v1.6.1 — Vector hatch-stroke detection and region reconstruction.

Detects repeated stroke patterns (parallel, cross, dense) from native PDF
vector geometry and produces SurfaceEvidence records that integrate with
the v160 fill-based evidence pipeline.

Key invariants:
  - Hatch detection does NOT change authoritative m².
  - Hatch area_m2 is None unless the region polygon is geometrically
    defensible and the page is calibrated.
  - False-positive controls are conservative: Review/rejected over
    false substrate classification.
  - Reuses Priority 1 calibration chain (page_scale_info / calibrate_area_m2).
  - Reuses B1 association pipeline (associate_surface_to_target,
    associate_code_to_polygon, associate_with_measured_surfaces).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from pb_surface_evidence_v160 import (
    SurfaceEvidence,
    SurfaceProcessingResult,
    _point_in_polygon,
    _shoelace_area,
    associate_code_to_polygon,
    associate_with_measured_surfaces,
    associate_surface_to_target,
    calibrate_area_m2,
    page_scale_info,
    polygon_area_abs,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VERSION = "1.6.1"

# Minimum stroke length to consider (points)
_MIN_STROKE_LENGTH_PT = 2.0

# Minimum strokes per cluster to be considered hatch
_MIN_HATCH_STROKES = 5

# Angle tolerance for "same direction" grouping (degrees)
_ANGLE_GROUP_TOL = 5.0

# Perpendicular distance threshold for merging strokes into same cluster
_MAX_PARALLEL_DIST_PT = 35.0

# Maximum page-fraction a single stroke can span and still be hatch
_MAX_LINE_FRACTION = 0.60

# Minimum number of words near a cluster to flag dimension proximity
_DIM_WORD_MIN = 2

# Convex hull area / bbox area ratio below which we keep as is
_HULL_AREA_RATIO_THRESHOLD = 0.25

# Confidence thresholds
_HATCH_CONFIDENCE_THRESHOLD = 0.35
_RECONSTRUCTION_CONFIDENCE_THRESHOLD = 0.30

# Spacing limits (points) — reject clusters with extreme spacing
_MIN_SPACING_PT = 1.5
_MAX_SPACING_PT = 120.0

# Minimum ratio of parallel strokes to total strokes in cluster
_MIN_PARALLEL_RATIO = 0.50

# Minimum angle-entropy consistency (std dev of primary+secondary)
_MAX_ANGLE_STD = 35.0

# Bbox coverage threshold — fraction of bbox that must be "filled" by strokes
_MIN_BBOX_COVERAGE = 0.05

# Dimension text patterns
_DIM_TEXT_RE = re.compile(
    r"^[\d,]+\.?\d*\s*(mm|m|cm)?$|^\d{2,5}$|^\d{2,5}\.\d{1,3}$",
    re.IGNORECASE,
)

# Grid/ceiling pattern keywords that should be flagged
_GRID_KEYWORDS_RE = re.compile(
    r"\b(grid|ceiling\s*grid|acoustic|tile|module|batten|louvre|louver|"
    r"balustrade|stair|tread|riser|fence|decking|slat|batten|"
    r"dimension|dim|ticks?|arrow)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Stroke:
    """A single line segment extracted from PDF vector drawings."""
    x1: float
    y1: float
    x2: float
    y2: float
    width: float = 0.0
    colour: Optional[Tuple[float, float, float]] = None
    layer: str = ""
    drawing_index: int = 0
    item_index: int = 0
    dashes: Optional[Tuple[float, ...]] = None

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


@dataclass
class HatchCluster:
    """A group of strokes identified as a potential hatch pattern."""
    strokes: List[Stroke] = field(default_factory=list)
    group_id: int = 0

    # Computed metrics
    dominant_angle: float = 0.0
    secondary_angle: Optional[float] = None
    angle_tolerance: float = _ANGLE_GROUP_TOL
    stroke_count: int = 0
    stroke_length_mean: float = 0.0
    stroke_length_variance: float = 0.0
    spacing_mean_pt: float = 0.0
    spacing_variance_pt: float = 0.0
    bbox: Tuple[float, float, float, float] = (0, 0, 0, 0)
    area_bbox_pt2: float = 0.0

    # Classification
    hatch_confidence: float = 0.0
    is_cross_hatch: bool = False
    is_parallel_hatch: bool = False
    rejected: bool = False
    rejection_reason: str = ""
    notes: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Stroke extraction
# ---------------------------------------------------------------------------
def extract_strokes(pdf_page: Any) -> List[Stroke]:
    """Extract line-stroke primitives from PDF get_drawings() output.

    Only processes drawings that have a stroke (colour or width > 0).
    Skips fill-only drawings (handled by B1 fill extraction).
    Decomposes rectangles and quads into individual edges.
    Skips curves ('c') — they cannot form hatch patterns.
    """
    try:
        drawings = pdf_page.get_drawings() or []
    except Exception:
        return []

    strokes: List[Stroke] = []
    for di, d in enumerate(drawings):
        fill = d.get("fill")
        stroke_colour = d.get("color")
        stroke_width = float(d.get("width") or 0.0)
        layer = str(d.get("layer") or d.get("oc") or "")
        dashes_raw = d.get("dashes")
        dashes = None
        if dashes_raw:
            try:
                if isinstance(dashes_raw, (list, tuple)):
                    dashes = tuple(float(x) for x in dashes_raw)
                else:
                    # PyMuPDF returns dashes as string like "[2 4]"
                    s = str(dashes_raw).strip("[] ")
                    if s:
                        dashes = tuple(float(x) for x in s.split())
            except (ValueError, TypeError):
                dashes = None

        has_fill = fill is not None

        # --- False-positive filter: skip fill-only drawings ---
        # Hatch patterns are line-based and typically have no fill.
        # All fill geometry is handled by B1's extract_filled_polygons.
        if has_fill:
            continue

        for ii, item in enumerate(d.get("items") or []):
            try:
                op = item[0]
                if op == "l" and len(item) >= 3:
                    p1, p2 = item[1], item[2]
                    s = Stroke(
                        x1=float(p1.x), y1=float(p1.y),
                        x2=float(p2.x), y2=float(p2.y),
                        width=stroke_width, colour=stroke_colour,
                        layer=layer, drawing_index=di, item_index=ii,
                        dashes=dashes,
                    )
                    if s.length >= _MIN_STROKE_LENGTH_PT:
                        strokes.append(s)

                elif op == "re" and len(item) >= 2:
                    r = item[1]
                    corners = [
                        (float(r.x0), float(r.y0)),
                        (float(r.x1), float(r.y0)),
                        (float(r.x1), float(r.y1)),
                        (float(r.x0), float(r.y1)),
                    ]
                    for ci in range(4):
                        ax, ay = corners[ci]
                        bx, by = corners[(ci + 1) % 4]
                        s = Stroke(
                            x1=ax, y1=ay, x2=bx, y2=by,
                            width=stroke_width, colour=stroke_colour,
                            layer=layer, drawing_index=di, item_index=ii,
                            dashes=dashes,
                        )
                        if s.length >= _MIN_STROKE_LENGTH_PT:
                            strokes.append(s)

                elif op == "qu" and len(item) >= 2:
                    q = item[1]
                    pts = []
                    for qi in range(4):
                        pt = q[qi]
                        pts.append((float(pt.x), float(pt.y)))
                    for ci in range(4):
                        ax, ay = pts[ci]
                        bx, by = pts[(ci + 1) % 4]
                        s = Stroke(
                            x1=ax, y1=ay, x2=bx, y2=by,
                            width=stroke_width, colour=stroke_colour,
                            layer=layer, drawing_index=di, item_index=ii,
                            dashes=dashes,
                        )
                        if s.length >= _MIN_STROKE_LENGTH_PT:
                            strokes.append(s)

            except (TypeError, AttributeError, IndexError, ValueError):
                continue

    return strokes


# ---------------------------------------------------------------------------
# Angle helpers
# ---------------------------------------------------------------------------
def _angle_delta(a: float, b: float) -> float:
    """Minimal angular difference modulo 180°."""
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def _circular_mean(angles: List[float]) -> float:
    """Mean of angles treating them as directions (mod 180°)."""
    if not angles:
        return 0.0
    sin_sum = sum(math.sin(math.radians(a * 2)) for a in angles)
    cos_sum = sum(math.cos(math.radians(a * 2)) for a in angles)
    mean_rad = math.atan2(sin_sum, cos_sum) / 2.0
    return mean_rad * 180.0 / math.pi % 180.0


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def _point_line_distance(px: float, py: float,
                         x1: float, y1: float, x2: float, y2: float) -> float:
    """Perpendicular distance from point to infinite line through (x1,y1)-(x2,y2)."""
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return math.hypot(px - x1, py - y1)
    return abs(dy * px - dx * py + x2 * y1 - y2 * x1) / length


def _strokes_midpoint_distance(a: Stroke, b: Stroke) -> float:
    """Perpendicular distance between two strokes (midpoint-to-line)."""
    ca = ((a.x1 + a.x2) * 0.5, (a.y1 + a.y2) * 0.5)
    cb = ((b.x1 + b.x2) * 0.5, (b.y1 + b.y2) * 0.5)
    d1 = _point_line_distance(ca[0], ca[1], b.x1, b.y1, b.x2, b.y2)
    d2 = _point_line_distance(cb[0], cb[1], a.x1, a.y1, a.x2, a.y2)
    return (d1 + d2) * 0.5


# ---------------------------------------------------------------------------
# Union-Find for clustering
# ---------------------------------------------------------------------------
class _UnionFind:
    def __init__(self, n: int):
        self._parent = list(range(n))
        self._rank = [0] * n

    def find(self, x: int) -> int:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, x: int, y: int) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self._rank[rx] < self._rank[ry]:
            rx, ry = ry, rx
        self._parent[ry] = rx
        if self._rank[rx] == self._rank[ry]:
            self._rank[rx] += 1


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------
def _cluster_strokes(
    strokes: List[Stroke],
    angle_tol: float = _ANGLE_GROUP_TOL,
    max_dist: float = _MAX_PARALLEL_DIST_PT,
) -> List[HatchCluster]:
    """Group strokes into clusters by angle + spatial proximity using union-find.

    Two strokes are merged when:
      1. Their angle difference <= angle_tol degrees, AND
      2. Their perpendicular distance <= max_dist pt, AND
      3. Their projected intervals along the stroke direction overlap or the
         gap is small relative to average stroke length (prevents joining
         physically separate hatch regions across large empty spaces).
    """
    if len(strokes) < _MIN_HATCH_STROKES:
        return []

    n = len(strokes)
    uf = _UnionFind(n)

    # Pre-compute average stroke length for gap threshold
    avg_len = sum(s.length for s in strokes) / max(n, 1)

    # Angle-bucket acceleration: group strokes into angle buckets
    bucket_size = angle_tol * 2
    angle_buckets: Dict[int, List[int]] = {}
    for i, s in enumerate(strokes):
        bucket = int(s.angle_deg / bucket_size)
        if bucket not in angle_buckets:
            angle_buckets[bucket] = []
        angle_buckets[bucket].append(i)

    # Within each bucket (and neighbours), check proximity
    merge_count = 0
    checked: set = set()
    for bucket, indices in angle_buckets.items():
        # Check this bucket and the next (for angles near bucket boundary)
        neighbor_indices = list(indices)
        if (bucket + 1) in angle_buckets:
            neighbor_indices.extend(angle_buckets[bucket + 1])
        if (bucket - 1) in angle_buckets:
            neighbor_indices.extend(angle_buckets[bucket - 1])

        for ii, i in enumerate(indices):
            for j in neighbor_indices:
                if j <= i:
                    continue
                pair_key = (i, j)
                if pair_key in checked:
                    continue
                checked.add(pair_key)

                si, sj = strokes[i], strokes[j]
                if _angle_delta(si.angle_deg, sj.angle_deg) > angle_tol:
                    continue
                dist = _strokes_midpoint_distance(si, sj)
                if dist > max_dist:
                    continue

                # BLOCKER 1 fix: along-axis proximity check.
                # Project both strokes onto their SHARED DIRECTION axis
                # derived from the stroke angles themselves (circular mean),
                # NOT the midpoint-to-midpoint vector which may be largely
                # perpendicular to the lines for parallel strokes offset
                # in the perpendicular direction.
                mean_angle_rad = math.radians(
                    _circular_mean([si.angle_deg, sj.angle_deg])
                )
                dir_x = math.cos(mean_angle_rad)
                dir_y = math.sin(mean_angle_rad)

                # Project stroke endpoints onto direction axis
                a_start = si.x1 * dir_x + si.y1 * dir_y
                a_end = si.x2 * dir_x + si.y2 * dir_y
                if a_start > a_end:
                    a_start, a_end = a_end, a_start

                b_start = sj.x1 * dir_x + sj.y1 * dir_y
                b_end = sj.x2 * dir_x + sj.y2 * dir_y
                if b_start > b_end:
                    b_start, b_end = b_end, b_start

                # Check overlap
                overlap_start = max(a_start, b_start)
                overlap_end = min(a_end, b_end)

                if overlap_start <= overlap_end:
                    # Intervals overlap — merge
                    uf.union(i, j)
                    merge_count += 1
                else:
                    # No overlap — check gap
                    gap = b_start - a_end if b_start > a_end else a_start - b_end
                    if gap <= avg_len * 1.5:
                        uf.union(i, j)
                        merge_count += 1

    # Collect clusters
    groups: Dict[int, List[int]] = {}
    for i in range(n):
        root = uf.find(i)
        if root not in groups:
            groups[root] = []
        groups[root].append(i)

    clusters = []
    for gid, (root, members) in enumerate(groups.items()):
        cluster = HatchCluster(
            strokes=[strokes[i] for i in members],
            group_id=gid,
            stroke_count=len(members),
        )
        clusters.append(cluster)

    return clusters


@dataclass
class HatchProcessingResult:
    """Structured result from extract_hatch_evidence().

    Carries the actual detector diagnostics to the production adapter
    so that diagnostic counts (strokes, clusters, etc.) are accurate
    rather than reconstructed from final evidence count.
    """

    evidence: List[SurfaceEvidence] = field(default_factory=list)
    clusters: List[HatchCluster] = field(default_factory=list)
    strokes_extracted: int = 0
    clusters_found: int = 0
    clusters_rejected: int = 0
    regions_reconstructed: int = 0
    low_confidence_regions: int = 0
    associated: int = 0
    unassociated: int = 0
    extraction_error: str = ""


def _merge_cross_hatch_clusters(
    clusters: List[HatchCluster],
    max_merge_dist: float = 50.0,
    cross_hatch_tol: float = 12.0,
) -> List[HatchCluster]:
    """Merge spatially-overlapping clusters with perpendicular angles.

    Cross-hatch patterns produce two separate clusters (one per angle).
    This post-processing step merges them when:
      1. Their bboxes overlap (within max_merge_dist pt margin)
      2. Their dominant angles are genuinely near-perpendicular:
         abs(angle_delta - 90.0) <= cross_hatch_tol

    The _angle_delta() function returns 0-90 degrees (mod 180), so we
    require the result to be close to 90, not merely in a wide range.
    """
    if len(clusters) < 2:
        return clusters

    merged = list(range(len(clusters)))  # union-find-like: index -> cluster index
    for i in range(len(clusters)):
        for j in range(i + 1, len(clusters)):
            ci, cj = clusters[i], clusters[j]
            if merged[i] != i or merged[j] != j:
                continue  # already merged into something else

            # BLOCKER 3 fix: require genuinely near-perpendicular geometry.
            # _angle_delta returns 0-90 degrees (mod 180).
            # For true perpendicular: angle_delta should be ~90.
            angle_diff = _angle_delta(ci.dominant_angle, cj.dominant_angle)
            if abs(angle_diff - 90.0) > cross_hatch_tol:
                continue  # not perpendicular enough

            # Check spatial overlap with margin
            ix0 = max(ci.bbox[0], cj.bbox[0]) - max_merge_dist
            iy0 = max(ci.bbox[1], cj.bbox[1]) - max_merge_dist
            ix1 = min(ci.bbox[2], cj.bbox[2]) + max_merge_dist
            iy1 = min(ci.bbox[3], cj.bbox[3]) + max_merge_dist
            if ix0 > ix1 or iy0 > iy1:
                continue  # no overlap

            # Merge j into i
            merged[j] = i

    # Collect merged clusters
    groups: Dict[int, List[int]] = {}
    for i, root in enumerate(merged):
        if root not in groups:
            groups[root] = []
        groups[root].append(i)

    result = []
    for root, members in groups.items():
        if len(members) == 1:
            result.append(clusters[root])
        else:
            # Combine strokes from all merged clusters
            all_strokes = []
            member_clusters = [clusters[m] for m in members]
            for mc in member_clusters:
                all_strokes.extend(mc.strokes)
            combined = HatchCluster(
                strokes=all_strokes,
                group_id=clusters[root].group_id,
                stroke_count=len(all_strokes),
            )
            # Mark as cross-hatch when merging perpendicular clusters
            combined.is_cross_hatch = True
            combined.is_parallel_hatch = False  # cross-hatch ≠ parallel
            combined.notes.append(
                f"cross-hatch merge: {len(member_clusters)} clusters, "
                f"angles={[f'{mc.dominant_angle:.1f}' for mc in member_clusters]}"
            )
            _compute_cluster_metrics(combined)
            # Override: force cross-hatch flag after metrics (metrics may reset it)
            combined.is_cross_hatch = True
            combined.is_parallel_hatch = False
            result.append(combined)

    return result


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------
def _compute_cluster_metrics(cluster: HatchCluster) -> None:
    """Compute hatch metrics for a stroke cluster in-place."""
    strokes = cluster.strokes
    n = len(strokes)
    if n == 0:
        return

    cluster.stroke_count = n

    # Angles
    angles = [s.angle_deg for s in strokes]
    cluster.dominant_angle = _circular_mean(angles)

    # Determine if strokes are within tolerance of dominant angle
    primary_count = sum(
        1 for a in angles
        if _angle_delta(a, cluster.dominant_angle) <= _ANGLE_GROUP_TOL
    )
    cluster.is_parallel_hatch = (primary_count / n) >= _MIN_PARALLEL_RATIO

    # Look for secondary angle direction (cross-hatch)
    if n >= _MIN_HATCH_STROKES:
        residuals = []
        for a in angles:
            d = _angle_delta(a, cluster.dominant_angle)
            if d > _ANGLE_GROUP_TOL:
                residuals.append(a)
        if len(residuals) >= max(3, n * 0.2):
            sec = _circular_mean(residuals)
            # Verify it's approximately perpendicular (40–140° from primary)
            perp_delta = _angle_delta(cluster.dominant_angle, sec)
            if 40.0 <= perp_delta <= 140.0:
                cluster.secondary_angle = sec
                cluster.is_cross_hatch = True

    # Angle tolerance (spread around dominant)
    angle_diffs = [_angle_delta(a, cluster.dominant_angle) for a in angles]
    cluster.angle_tolerance = max(angle_diffs) if angle_diffs else 0.0

    # Stroke lengths
    lengths = [s.length for s in strokes]
    cluster.stroke_length_mean = sum(lengths) / n
    cluster.stroke_length_variance = (
        sum((l - cluster.stroke_length_mean) ** 2 for l in lengths) / n
    )

    # Bounding box
    all_x = [s.x1 for s in strokes] + [s.x2 for s in strokes]
    all_y = [s.y1 for s in strokes] + [s.y2 for s in strokes]
    x0, x1 = min(all_x), max(all_x)
    y0, y1 = min(all_y), max(all_y)
    cluster.bbox = (x0, y0, x1, y1)
    cluster.area_bbox_pt2 = max(0.0, (x1 - x0) * (y1 - y0))

    # Spacing: sort primary-angle strokes by perpendicular offset
    if cluster.is_parallel_hatch and n >= 2:
        _compute_spacing(cluster)


def _compute_spacing(cluster: HatchCluster) -> None:
    """Compute mean and variance of inter-stroke spacing for parallel strokes."""
    strokes = cluster.strokes
    angle_rad = math.radians(cluster.dominant_angle)

    # Project each stroke midpoint onto the perpendicular axis
    perp_axis = (-math.sin(angle_rad), math.cos(angle_rad))
    offsets = []
    for s in strokes:
        cx, cy = s.cx, s.cy
        offset = cx * perp_axis[0] + cy * perp_axis[1]
        offsets.append(offset)

    offsets.sort()
    if len(offsets) < 2:
        return

    # Compute gaps between consecutive sorted offsets
    gaps = [offsets[i + 1] - offsets[i] for i in range(len(offsets) - 1)]
    # Filter out near-zero gaps (overlapping strokes)
    gaps = [g for g in gaps if g > 0.5]
    if not gaps:
        return

    cluster.spacing_mean_pt = sum(gaps) / len(gaps)
    cluster.spacing_variance_pt = (
        sum((g - cluster.spacing_mean_pt) ** 2 for g in gaps) / len(gaps)
    )


# ---------------------------------------------------------------------------
# False-positive rejection
# ---------------------------------------------------------------------------
def _reject_false_positives(
    cluster: HatchCluster,
    page_width: float,
    page_height: float,
    words: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """Apply false-positive filters. Returns True if cluster should be rejected.

    Rejects:
      - too few strokes
      - single very long lines spanning most of the page
      - strokes with too much angle spread
      - spacing outside architectural hatch range
      - dimension-line-like patterns (arrowhead endpoints + text)
      - grid/ceiling acoustic patterns
      - clusters too sparse for their bounding box
    """
    strokes = cluster.strokes
    n = len(strokes)
    page_diag = math.hypot(page_width, page_height)

    # 1. Minimum stroke count
    if n < _MIN_HATCH_STROKES:
        cluster.rejected = True
        cluster.rejection_reason = f"too few strokes ({n} < {_MIN_HATCH_STROKES})"
        return True

    # 2. Check for single very long strokes dominating the cluster
    max_len = max(s.length for s in strokes)
    if max_len > _MAX_LINE_FRACTION * page_diag:
        cluster.rejected = True
        cluster.rejection_reason = (
            f"single stroke spans {max_len:.0f}pt "
            f"({max_len / page_diag:.0%} of page diagonal)"
        )
        return True

    # 3. Angle spread too large
    # Cross-hatch clusters have intentionally wide spread (~90° between groups).
    # Only reject if not cross-hatch and spread is too large.
    if not cluster.is_cross_hatch and cluster.angle_tolerance > _MAX_ANGLE_STD:
        cluster.rejected = True
        cluster.rejection_reason = (
            f"angle spread {cluster.angle_tolerance:.1f}° "
            f"> {_MAX_ANGLE_STD}°"
        )
        return True

    # 4. Not enough parallel strokes
    # Cross-hatch has strokes in 2 perpendicular directions; parallel_ratio
    # is naturally ~0.5. Skip this check for cross-hatch clusters.
    if not cluster.is_cross_hatch:
        primary_count = sum(
            1 for s in strokes
            if _angle_delta(s.angle_deg, cluster.dominant_angle) <= _ANGLE_GROUP_TOL
        )
        parallel_ratio = primary_count / n if n > 0 else 0.0
        if parallel_ratio < _MIN_PARALLEL_RATIO:
            cluster.rejected = True
            cluster.rejection_reason = (
                f"parallel ratio {parallel_ratio:.0%} < {_MIN_PARALLEL_RATIO:.0%}"
            )
            return True

    # 5. Spacing out of range
    if cluster.spacing_mean_pt > 0:
        if cluster.spacing_mean_pt < _MIN_SPACING_PT:
            cluster.rejected = True
            cluster.rejection_reason = (
                f"spacing {cluster.spacing_mean_pt:.1f}pt "
                f"< {_MIN_SPACING_PT}pt (too dense)"
            )
            return True
        if cluster.spacing_mean_pt > _MAX_SPACING_PT:
            cluster.rejected = True
            cluster.rejection_reason = (
                f"spacing {cluster.spacing_mean_pt:.1f}pt "
                f"> {_MAX_SPACING_PT}pt (too sparse)"
            )
            return True

    # 6. Check for dimension-line patterns: long strokes with dimension text
    if words:
        dim_words = [
            w for w in words
            if _DIM_TEXT_RE.search(str(w.get("text", "")))
        ]
        if dim_words:
            cluster_cx = sum(s.cx for s in strokes) / n
            cluster_cy = sum(s.cy for s in strokes) / n
            nearby_dim = sum(
                1 for w in dim_words
                if math.hypot(
                    (w["bbox"][0] + w["bbox"][2]) / 2 - cluster_cx,
                    (w["bbox"][1] + w["bbox"][3]) / 2 - cluster_cy,
                ) < max(cluster.area_bbox_pt2 ** 0.5, 100)
            )
            if nearby_dim >= _DIM_WORD_MIN:
                cluster.rejected = True
                cluster.rejection_reason = (
                    f"dimension-line pattern: {nearby_dim} dimension texts nearby"
                )
                return True

    # 7. Grid/ceiling/batten/louvre keywords
    if words:
        cluster_cx = sum(s.cx for s in strokes) / n
        cluster_cy = sum(s.cy for s in strokes) / n
        for w in words:
            txt = str(w.get("text", ""))
            if _GRID_KEYWORDS_RE.search(txt):
                wb = w.get("bbox", [0, 0, 0, 0])
                wcx = (wb[0] + wb[2]) / 2
                wcy = (wb[1] + wb[3]) / 2
                if math.hypot(wcx - cluster_cx, wcy - cluster_cy) < max(
                    cluster.area_bbox_pt2 ** 0.5, 100
                ):
                    cluster.rejected = True
                    cluster.rejection_reason = (
                        f"keyword '{txt.strip()}' found near cluster"
                    )
                    return True

    # 8. Line density check: strokes per unit of perpendicular extent.
    #    Hatch patterns have a characteristic density that distinguishes
    #    them from sparse architectural linework.
    if cluster.spacing_mean_pt > 0 and cluster.area_bbox_pt2 > 0:
        # Perpendicular extent of the cluster
        angle_rad = math.radians(cluster.dominant_angle)
        perp_axis = (-math.sin(angle_rad), math.cos(angle_rad))
        offsets = []
        for s in strokes:
            cx, cy = s.cx, s.cy
            offset = cx * perp_axis[0] + cy * perp_axis[1]
            offsets.append(offset)
        perp_extent = max(offsets) - min(offsets) if len(offsets) > 1 else 0
        if perp_extent > 0:
            density = n / perp_extent  # strokes per pt of perpendicular extent
            # Reject if very sparse (< 1 stroke per 40pt) — not architectural hatch
            if density < 1.0 / 40.0:
                cluster.rejected = True
                cluster.rejection_reason = (
                    f"line density {density:.4f} strokes/pt "
                    f"(< {1/40:.4f}, too sparse)"
                )
                return True

    cluster.rejected = False
    cluster.rejection_reason = ""
    return False


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------
def _compute_hatch_confidence(cluster: HatchCluster) -> float:
    """Compute 0–1 confidence that this cluster represents architectural hatch."""
    n = cluster.stroke_count
    if n < _MIN_HATCH_STROKES:
        return 0.0

    score = 0.0

    # Stroke count contribution (diminishing returns)
    count_score = min(1.0, (n - _MIN_HATCH_STROKES + 1) / 15.0)
    score += 0.25 * count_score

    # Spacing consistency (low variance = high score)
    if cluster.spacing_mean_pt > 0 and cluster.spacing_variance_pt >= 0:
        cv = (cluster.spacing_variance_pt ** 0.5) / max(
            cluster.spacing_mean_pt, 0.1
        )
        spacing_score = max(0.0, 1.0 - cv)
        score += 0.20 * spacing_score
    else:
        score += 0.05  # partial credit if spacing couldn't be computed

    # Angle consistency (low tolerance = high score)
    angle_score = max(0.0, 1.0 - cluster.angle_tolerance / 45.0)
    score += 0.20 * angle_score

    # Parallel ratio
    primary_count = sum(
        1 for s in cluster.strokes
        if _angle_delta(s.angle_deg, cluster.dominant_angle) <= _ANGLE_GROUP_TOL
    )
    parallel_ratio = primary_count / max(n, 1)
    score += 0.15 * parallel_ratio

    # Stroke length consistency (low variance = high score)
    if cluster.stroke_length_mean > 0:
        len_cv = (cluster.stroke_length_variance ** 0.5) / max(
            cluster.stroke_length_mean, 0.1
        )
        len_score = max(0.0, 1.0 - min(len_cv, 2.0) / 2.0)
        score += 0.10 * len_score

    # Cross-hatch bonus
    if cluster.is_cross_hatch:
        score += 0.10

    return min(1.0, score)


# ---------------------------------------------------------------------------
# Region reconstruction
# ---------------------------------------------------------------------------
def _reconstruct_hatch_region(cluster: HatchCluster) -> Tuple[
    Optional[List[Tuple[float, float]]],  # polygon_pdf_pts
    float,                                 # reconstruction_confidence
    str,                                   # method
]:
    """Reconstruct a closed polygon representing the hatch region.

    Returns (polygon, confidence, method_string) or (None, 0, "failed").
    """
    strokes = cluster.strokes
    n = len(strokes)
    if n < _MIN_HATCH_STROKES:
        return None, 0.0, "insufficient_strokes"

    # Method 1: Convex hull of all stroke endpoints
    points = []
    for s in strokes:
        points.append((s.x1, s.y1))
        points.append((s.x2, s.y2))

    hull = _convex_hull(points)
    if hull is None or len(hull) < 3:
        return None, 0.0, "hull_failed"

    hull_area = abs(_shoelace_area(tuple(hull)))
    bbox_area = cluster.area_bbox_pt2

    if bbox_area > 0:
        hull_ratio = hull_area / bbox_area
    else:
        hull_ratio = 0.0

    # Confidence based on hull quality
    if hull_ratio >= 0.85:
        recon_conf = 0.85
    elif hull_ratio >= 0.60:
        recon_conf = 0.65
    elif hull_ratio >= _HULL_AREA_RATIO_THRESHOLD:
        recon_conf = 0.50
    else:
        # Low hull quality — use bbox instead for safety
        recon_conf = 0.25

    # Downgrade if stroke lengths vary wildly
    if cluster.stroke_length_mean > 0:
        cv = (cluster.stroke_length_variance ** 0.5) / max(
            cluster.stroke_length_mean, 0.1
        )
        if cv > 0.6:
            recon_conf *= 0.7

    # For very sparse clusters, use bbox
    if recon_conf < _RECONSTRUCTION_CONFIDENCE_THRESHOLD:
        # Fall back to bbox
        x0, y0, x1, y1 = cluster.bbox
        if (x1 - x0) > 0 and (y1 - y0) > 0:
            bbox_poly = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            return bbox_poly, 0.25, "bbox_fallback"
        return None, 0.0, "no_valid_region"

    return hull, recon_conf, "convex_hull"


def _convex_hull(points: List[Tuple[float, float]]) -> Optional[List[Tuple[float, float]]]:
    """Compute convex hull using Andrew's monotone chain algorithm."""
    pts = sorted(set(points))
    if len(pts) < 3:
        return None

    def _cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)

    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)

    hull = lower[:-1] + upper[:-1]
    if len(hull) < 3:
        return None
    return hull


# ---------------------------------------------------------------------------
# Main detection function
# ---------------------------------------------------------------------------
def detect_hatch_patterns(
    pdf_page: Any,
    scale_info: Optional[Dict[str, Any]] = None,
    words: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[
    List[SurfaceEvidence],     # hatch evidence
    List[HatchCluster],        # all clusters (for diagnostics)
    Dict[str, Any],            # hatch diagnostics
]:
    """Detect repeated stroke patterns and produce SurfaceEvidence records.

    Args:
        pdf_page: PyMuPDF page object.
        scale_info: Calibration dict from page_scale_info() (None = uncalibrated).
        words: Positioned text words for dimension detection.

    Returns:
        (evidence_list, all_clusters, hatch_diagnostics_dict)
    """
    from pb_surface_evidence_v160 import FillPolygon

    page_rect = pdf_page.rect
    page_width = float(page_rect.width)
    page_height = float(page_rect.height)

    hatch_diag: Dict[str, Any] = {
        "strokes_extracted": 0,
        "clusters_found": 0,
        "clusters_rejected": 0,
        "regions_reconstructed": 0,
        "low_confidence_regions": 0,
        "associated": 0,
        "unassociated": 0,
        "extraction_error": "",
    }

    # Step 1: Extract strokes
    try:
        strokes = extract_strokes(pdf_page)
    except Exception as exc:
        hatch_diag["extraction_error"] = f"{type(exc).__name__}: {exc}"
        return [], [], hatch_diag

    hatch_diag["strokes_extracted"] = len(strokes)

    if len(strokes) < _MIN_HATCH_STROKES:
        return [], [], hatch_diag

    # Step 2: Cluster strokes
    clusters = _cluster_strokes(strokes)

    # Step 2b: Compute initial metrics for cross-hatch merge detection
    for cluster in clusters:
        _compute_cluster_metrics(cluster)

    # Step 2c: Merge perpendicular clusters that overlap spatially
    clusters = _merge_cross_hatch_clusters(clusters)

    hatch_diag["clusters_found"] = len(clusters)

    # Step 3: Compute final metrics + reject false positives
    all_clusters: List[HatchCluster] = []
    for cluster in clusters:
        _compute_cluster_metrics(cluster)
        _reject_false_positives(cluster, page_width, page_height, words)
        all_clusters.append(cluster)

    rejected_count = sum(1 for c in all_clusters if c.rejected)
    hatch_diag["clusters_rejected"] = rejected_count

    # Step 4: Reconstruct regions + build evidence
    evidence_list: List[SurfaceEvidence] = []
    hatches_reconstructed = 0
    low_conf_count = 0

    for cluster in all_clusters:
        if cluster.rejected:
            continue

        # Compute confidence
        cluster.hatch_confidence = _compute_hatch_confidence(cluster)

        if cluster.hatch_confidence < _HATCH_CONFIDENCE_THRESHOLD:
            cluster.rejected = True
            cluster.rejection_reason = (
                f"hatch confidence {cluster.hatch_confidence:.2f} "
                f"< {_HATCH_CONFIDENCE_THRESHOLD}"
            )
            continue

        # Reconstruct region
        polygon, recon_conf, method = _reconstruct_hatch_region(cluster)
        cluster.notes.append(f"reconstruction: {method}")

        if polygon is None or recon_conf < _RECONSTRUCTION_CONFIDENCE_THRESHOLD:
            low_conf_count += 1
            # Still produce evidence but with bbox fallback
            x0, y0, x1, y1 = cluster.bbox
            polygon = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            recon_conf = max(recon_conf, 0.20)
            method = "bbox_fallback"

        hatches_reconstructed += 1

        # Build SurfaceEvidence
        poly_tuples = tuple(tuple(p) for p in polygon)
        area_pts2 = polygon_area_abs(poly_tuples)
        area_m2 = calibrate_area_m2(poly_tuples, scale_info) if scale_info else None
        # Bbox fallback: never trust area
        if method == "bbox_fallback":
            area_m2 = None

        # Collect unique layers
        layers = sorted({s.layer for s in cluster.strokes if s.layer})
        drawing_indices = sorted({s.drawing_index for s in cluster.strokes})

        hatch_type = "cross_hatch" if cluster.is_cross_hatch else "parallel_hatch"

        # Import constants for canonical surface-evidence type identifiers
        try:
            from pb_surface_evidence_v160 import (
                SURFACE_TYPE_HATCH, GEOMETRY_METHOD_VECTOR_HATCH,
            )
        except ImportError:
            SURFACE_TYPE_HATCH = "hatch_region"
            GEOMETRY_METHOD_VECTOR_HATCH = "vector_hatch_region"

        ev = SurfaceEvidence(
            source_geometry_type=SURFACE_TYPE_HATCH,
            geometry_method=GEOMETRY_METHOD_VECTOR_HATCH,
            polygon_pdf_pts=poly_tuples,
            bbox=cluster.bbox,
            area_page_pts2=area_pts2,
            area_m2=area_m2,
            geometry_confidence=min(cluster.hatch_confidence, recon_conf),
            status="needs_check" if recon_conf < 0.50 else "unreviewed",
            source_layer=layers[0] if layers else "",
            source_drawing_index=drawing_indices[0] if drawing_indices else 0,
            source_item_types=(hatch_type,),
            evidence=[
                f"Vector hatch: {cluster.stroke_count} strokes, "
                f"{hatch_type}, dominant_angle={cluster.dominant_angle:.1f}°, "
                f"spacing={cluster.spacing_mean_pt:.1f}pt, "
                f"hatch_conf={cluster.hatch_confidence:.2f}, "
                f"recon={method} ({recon_conf:.2f})"
            ],
        )

        # Hatch-specific metadata stored in notes as structured JSON-like string
        hatch_meta = (
            f"hatch_angles=[{cluster.dominant_angle:.1f}"
            + (f", {cluster.secondary_angle:.1f}" if cluster.secondary_angle else "")
            + f"], spacing_pt={cluster.spacing_mean_pt:.1f}, "
            f"stroke_count={cluster.stroke_count}, "
            f"stroke_length_mean={cluster.stroke_length_mean:.1f}, "
            f"reconstruction_method={method}"
        )
        ev.notes = hatch_meta

        evidence_list.append(ev)

    hatch_diag["regions_reconstructed"] = hatches_reconstructed
    hatch_diag["low_confidence_regions"] = low_conf_count
    hatch_diag["associated"] = 0  # filled after association
    hatch_diag["unassociated"] = len(evidence_list)

    return evidence_list, all_clusters, hatch_diag


# ---------------------------------------------------------------------------
# Production adapter
# ---------------------------------------------------------------------------
def extract_hatch_evidence(
    pdf_page: Any,
    page_id: int = 0,
    page_no: int = 0,
    page_label: str = "",
    workspace_id: int = 0,
    scale_info: Optional[Dict[str, Any]] = None,
    words: Optional[List[Dict[str, Any]]] = None,
    measured_surfaces: Optional[List[Dict[str, Any]]] = None,
    code_occurrences: Optional[List[Dict[str, Any]]] = None,
) -> HatchProcessingResult:
    """High-level hatch extraction for production adapter.

    Detects hatch patterns, builds evidence, associates with measured surfaces,
    and associates positioned codes.  Returns a HatchProcessingResult carrying
    the actual detector diagnostics so production counts are accurate.
    """
    evidence_list, clusters, hatch_diag = detect_hatch_patterns(
        pdf_page, scale_info, words
    )

    result = HatchProcessingResult(
        evidence=evidence_list,
        clusters=clusters,
        strokes_extracted=hatch_diag.get("strokes_extracted", 0),
        clusters_found=hatch_diag.get("clusters_found", 0),
        clusters_rejected=hatch_diag.get("clusters_rejected", 0),
        regions_reconstructed=hatch_diag.get("regions_reconstructed", 0),
        low_confidence_regions=hatch_diag.get("low_confidence_regions", 0),
        extraction_error=hatch_diag.get("extraction_error", ""),
    )

    if not evidence_list:
        return result

    # Assign page metadata
    for idx, ev in enumerate(evidence_list):
        ev.workspace_id = workspace_id
        ev.page_id = page_id
        ev.page_no = page_no
        ev.page_label = page_label
        ev.surface_id = f"page_{page_id}:hatch_{idx}"

    # Associate with measured surfaces
    if measured_surfaces:
        evidence_list = associate_with_measured_surfaces(
            evidence_list, measured_surfaces, code_occurrences
        )
        result.evidence = evidence_list

    # Count associations
    associated = sum(1 for e in evidence_list if e.association_method
                     and e.association_method != "none")
    result.associated = associated
    result.unassociated = len(evidence_list) - associated

    return result


# ---------------------------------------------------------------------------
# Monkey-patch integration
# ---------------------------------------------------------------------------
def apply(app: Any) -> None:
    """Wire hatch detection into the PlanReader app.

    Follows the established apply() monkey-patch pattern.
    """
    if getattr(app, "_pb_hatch_detection_v160_applied", False):
        return

    app.extract_hatch_evidence_v160 = extract_hatch_evidence
    app.detect_hatch_patterns_v160 = detect_hatch_patterns
    app.HatchProcessingResult = HatchProcessingResult

    app._pb_hatch_detection_v160_applied = True
