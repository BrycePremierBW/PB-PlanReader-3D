"""PlanReader v1.6.0 native PDF filled-geometry extraction and surface evidence.

Phase B1 scope: native PDF filled polygons -> structured SurfaceEvidence
-> association with existing authoritative measured surfaces.

Does NOT implement hatch-stroke detection, raster analysis, or legend swatch
extraction.  Those are Phase B2+ work.

Critical rules:
  - Substrate/finish detection never changes authoritative m².
  - fill colour is project evidence, not universal meaning.
  - substrate and finish are separate concepts.
  - unknown calibration -> area_m2 = None, not 0.0.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

VERSION = "1.6.0"
SETTING_PREFIX = "surface_evidence_v160_"

# ---------------------------------------------------------------------------
# Calibration constants (same as Priority 1 / Priority 2)
# ---------------------------------------------------------------------------
PDF_PT_TO_MM = 25.4 / 72.0          # 1 PDF point = 0.3528 mm
MM_PER_PT = PDF_PT_TO_MM

# Material schedule code regex (same pattern as pb_material_schedule_v1222)
_FINISH_CODE_RE = re.compile(
    r"\b(?:EC\d+|FC\d+|RBL\d*|SOF\d*|CL\d+|PT\d+|PF\d+|WF\d+"
    r"|BA\d+|SCR\d*|SHD\d*|DP\d*|GD\d*|RS\d*|BC\d*"
    r"|[A-Z]{1,4}\d{1,4})\b",
    re.IGNORECASE,
)

# Codes that are explicitly finish/paint codes (subset for targeted matching)
_PAINT_CODE_RE = re.compile(
    r"\b(?:PT\d+|PF\d+|WF\d+|EC\d+|CL\d+|FC\d+|RBL\d*|SOF\d*"
    r"|BA\d+|SCR\d*|SHD\d*|DP\d*|GD\d*)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Geometry primitives
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FillPolygon:
    """A single filled polygon extracted from PDF vector geometry.

    Coordinates are in PDF points (1/72 inch).  The polygon is closed
    (last vertex connects back to first).
    """
    vertices: Tuple[Tuple[float, float], ...]
    fill: Optional[Tuple[float, float, float]] = None   # RGB 0-1
    fill_opacity: float = 1.0
    stroke: Optional[Tuple[float, float, float]] = None  # RGB 0-1
    stroke_opacity: float = 1.0
    stroke_width: float = 0.0
    close_path: bool = True
    even_odd: bool = False
    drawing_index: int = 0
    layer: str = ""
    item_types: Tuple[str, ...] = ()  # e.g. ("re",), ("l","l","l"), ("qu",)
    geometry_method: str = "native_rectangle"  # native_rectangle/native_quad/closed_line_path/bbox_fallback

    @property
    def bbox(self) -> Tuple[float, float, float, float]:
        """(x0, y0, x1, y1) bounding box."""
        if not self.vertices:
            return (0.0, 0.0, 0.0, 0.0)
        xs = [v[0] for v in self.vertices]
        ys = [v[1] for v in self.vertices]
        return (min(xs), min(ys), max(xs), max(ys))

    @property
    def area_page_pts2(self) -> float:
        """Absolute area in PDF points squared (non-negative regardless of winding)."""
        return abs(_shoelace_area(self.vertices))

    @property
    def centroid(self) -> Tuple[float, float]:
        """Approximate centroid (average of vertices)."""
        n = len(self.vertices)
        if n == 0:
            return (0.0, 0.0)
        return (
            sum(v[0] for v in self.vertices) / n,
            sum(v[1] for v in self.vertices) / n,
        )

    @property
    def width_pt(self) -> float:
        x0, y0, x1, y1 = self.bbox
        return x1 - x0

    @property
    def height_pt(self) -> float:
        x0, y0, x1, y1 = self.bbox
        return y1 - y0


# ---------------------------------------------------------------------------
# SurfaceEvidence record
# ---------------------------------------------------------------------------

@dataclass
class SurfaceEvidence:
    """Structured record linking native PDF fill geometry to surface classification.

    Geometry evidence (raw) is separate from semantic classification.
    """

    # Identity
    workspace_id: int = 0
    page_id: int = 0
    page_no: int = 0
    page_label: str = ""
    surface_id: str = ""          # e.g. "page_5:fill_3", "page_5:R04:fill_1"

    # Raw geometry evidence (from PDF)
    source_geometry_type: str = ""  # "filled_polygon", "fill_only", "fill_stroke"
    geometry_method: str = ""       # native_rectangle/native_quad/closed_line_path/bbox_fallback
    polygon_pdf_pts: Tuple[Tuple[float, float], ...] = ()
    bbox: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    fill_colour: Optional[Tuple[float, float, float]] = None
    fill_opacity: float = 1.0
    stroke_colour: Optional[Tuple[float, float, float]] = None
    source_layer: str = ""
    source_drawing_index: int = 0
    source_item_types: Tuple[str, ...] = ()

    # Area
    area_page_pts2: float = 0.0
    area_m2: Optional[float] = None  # None if uncalibrated

    # Substrate / finish classification (semantic layer)
    substrate_code: str = ""
    substrate: str = ""
    finish_code: str = ""
    finish: str = ""
    coating_system: str = ""

    # Association with measured geometry
    association_target_type: str = ""  # "room", "wall", "elevation_zone", "ceiling"
    association_target_ref: str = ""   # e.g. "R04", "W01_North"
    association_method: str = ""       # "containment", "majority_overlap", "centroid", "proximity"
    association_overlap: float = 0.0   # 0-1: overlap ratio

    # Confidence (0-1 scale)
    geometry_confidence: float = 0.0   # polygon extraction quality
    semantic_confidence: float = 0.0   # substrate/finish code certainty
    association_confidence: float = 0.0  # geometry -> target linkage

    # Status
    status: str = "unreviewed"  # confirmed/probable/needs_check/conflict/unreviewed
    evidence: List[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict for storage/transmission."""
        d = asdict(self)
        # Convert tuples to lists for JSON compatibility
        d["polygon_pdf_pts"] = [list(p) for p in self.polygon_pdf_pts]
        d["bbox"] = list(self.bbox)
        d["fill_colour"] = list(self.fill_colour) if self.fill_colour is not None else None
        d["stroke_colour"] = list(self.stroke_colour) if self.stroke_colour is not None else None
        d["source_item_types"] = list(self.source_item_types)
        return d


@dataclass
class HatchDiagnostics:
    """Hatch-stage diagnostics nested inside SurfaceProcessingDiagnostics.

    Separated so that adding hatch fields does not change B1 field positions
    in persisted JSON.  Constructed with ``**parsed`` which is backward-
    compatible because every field has a default.
    """

    strokes_extracted: int = 0
    clusters_found: int = 0
    clusters_rejected: int = 0
    regions_reconstructed: int = 0
    low_confidence_regions: int = 0
    associated: int = 0
    unassociated: int = 0
    extraction_error: str = ""   # empty = no error

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items()}


@dataclass
class SurfaceProcessingDiagnostics:
    """Structured diagnostics for one page's surface processing pipeline.

    Records the outcome of every pipeline stage so that failures are visible
    rather than silently folded into a normal-looking result.
    """

    # Stage outcomes (boolean = succeeded, str = error message if failed)
    page_lookup_ok: bool = False
    pdf_open_ok: bool = False
    fills_extracted_count: int = 0
    positioned_words_extracted_count: int = 0
    positioned_words_extraction_error: str = ""  # empty = no error
    finish_codes_found_count: int = 0
    text_only_codes_found_count: int = 0
    measured_room_targets_count: int = 0
    measured_wall_targets_count: int = 0
    room_extraction_error: str = ""   # empty = no error
    wall_extraction_error: str = ""   # empty = no error
    associated_count: int = 0
    unassociated_count: int = 0
    storage_ok: bool = False
    storage_error: str = ""           # empty = no error

    # B2 hatch diagnostics (nested — backward-compatible via default)
    hatch_diag: HatchDiagnostics = field(default_factory=HatchDiagnostics)

    def __post_init__(self):
        """Ensure nested hatch_diag is always a HatchDiagnostics instance."""
        if isinstance(self.hatch_diag, dict):
            self.hatch_diag = HatchDiagnostics(**self.hatch_diag)

    def to_dict(self) -> Dict[str, Any]:
        d = {k: v for k, v in asdict(self).items()}
        # Ensure nested hatch_diag serialises cleanly
        if isinstance(d.get("hatch_diag"), dict):
            pass  # asdict already flattened it
        elif hasattr(self.hatch_diag, "to_dict"):
            d["hatch_diag"] = self.hatch_diag.to_dict()
        return d


@dataclass
class SurfaceProcessingResult:
    """Return type for process_page_surface_evidence().

    Contains the evidence list plus structured diagnostics so callers can
    distinguish "no evidence found" from "pipeline stage failed".
    """

    evidence: List[SurfaceEvidence] = field(default_factory=list)
    diagnostics: SurfaceProcessingDiagnostics = field(
        default_factory=SurfaceProcessingDiagnostics,
    )
    status: str = "ok"  # "ok" | "partial" | "error" | "no_fills"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evidence": [ev.to_dict() for ev in self.evidence],
            "diagnostics": self.diagnostics.to_dict(),
            "status": self.status,
        }


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _shoelace_area(vertices: Sequence[Tuple[float, float]]) -> float:
    """Signed area via Shoelace formula.  Positive = CCW winding."""
    n = len(vertices)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += vertices[i][0] * vertices[j][1]
        area -= vertices[j][0] * vertices[i][1]
    return area / 2.0


def polygon_area_abs(vertices: Sequence[Tuple[float, float]]) -> float:
    """Absolute area in PDF points squared."""
    return abs(_shoelace_area(vertices))


def _point_in_polygon(px: float, py: float, polygon: Sequence[Tuple[float, float]]) -> bool:
    """Ray-cast point-in-polygon test (even-odd rule)."""
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _all_vertices_inside(
    inner: Sequence[Tuple[float, float]],
    outer: Sequence[Tuple[float, float]],
) -> bool:
    """Deterministic check: are ALL vertices of inner polygon inside outer?"""
    return all(_point_in_polygon(px, py, outer) for px, py in inner)


def _edges_cross_outside(
    inner: Sequence[Tuple[float, float]],
    outer: Sequence[Tuple[float, float]],
    sample_count_per_edge: int = 5,
) -> bool:
    """Check if any edge of inner polygon crosses outside outer polygon.

    Samples intermediate points along each edge and tests containment.
    This catches cases where vertices are inside but edges bulge outside
    (e.g., a rectangle with a protrusion).
    """
    n = len(inner)
    if n < 3:
        return False
    for i in range(n):
        x1, y1 = inner[i]
        x2, y2 = inner[(i + 1) % n]
        for k in range(1, sample_count_per_edge):
            t = k / sample_count_per_edge
            sx = x1 + t * (x2 - x1)
            sy = y1 + t * (y2 - y1)
            if not _point_in_polygon(sx, sy, outer):
                return True
    return False


def _deterministic_containment(
    inner: Sequence[Tuple[float, float]],
    outer: Sequence[Tuple[float, float]],
) -> bool:
    """Deterministic full-containment test.

    Returns True only if:
      1. All vertices of inner are inside outer, AND
      2. No edge of inner crosses outside outer (edge sampling).

    This is strict — a rectangle with a small protrusion will NOT pass.
    """
    if not inner or not outer:
        return False
    if not _all_vertices_inside(inner, outer):
        return False
    if _edges_cross_outside(inner, outer):
        return False
    return True


def _polygon_overlap_ratio(
    poly_a: Sequence[Tuple[float, float]],
    poly_b: Sequence[Tuple[float, float]],
    sample_count: int = 200,
) -> float:
    """Estimate what fraction of poly_a's area overlaps poly_b.

    Uses Monte Carlo sampling within poly_a's bounding box.
    Returns 0.0 to 1.0.
    """
    if not poly_a or not poly_b:
        return 0.0
    ax0, ay0, ax1, ay1 = (
        min(v[0] for v in poly_a), min(v[1] for v in poly_a),
        max(v[0] for v in poly_a), max(v[1] for v in poly_a),
    )
    area_a = polygon_area_abs(poly_a)
    if area_a <= 0:
        return 0.0
    # Deterministic grid sampling (not truly random — reproducible)
    cols = int(math.sqrt(sample_count))
    rows = max(1, sample_count // cols)
    inside_a = 0
    inside_both = 0
    dx = (ax1 - ax0) / max(cols, 1)
    dy = (ay1 - ay0) / max(rows, 1)
    for ci in range(cols):
        for ri in range(rows):
            sx = ax0 + (ci + 0.5) * dx
            sy = ay0 + (ri + 0.5) * dy
            if _point_in_polygon(sx, sy, poly_a):
                inside_a += 1
                if _point_in_polygon(sx, sy, poly_b):
                    inside_both += 1
    if inside_a == 0:
        return 0.0
    return inside_both / inside_a


def _polygon_intersection_area(
    poly_a: Sequence[Tuple[float, float]],
    poly_b: Sequence[Tuple[float, float]],
    sample_count: int = 400,
) -> float:
    """Estimate intersection area of two polygons in PDF points squared.

    Uses grid sampling within the union of both bounding boxes.
    """
    if not poly_a or not poly_b:
        return 0.0
    # Union bounding box
    all_pts = list(poly_a) + list(poly_b)
    ux0 = min(v[0] for v in all_pts)
    uy0 = min(v[1] for v in all_pts)
    ux1 = max(v[0] for v in all_pts)
    uy1 = max(v[1] for v in all_pts)
    cols = int(math.sqrt(sample_count))
    rows = max(1, sample_count // cols)
    dx = (ux1 - ux0) / max(cols, 1)
    dy = (uy1 - uy0) / max(rows, 1)
    cell_area = dx * dy if (dx > 0 and dy > 0) else 0.0
    count = 0
    for ci in range(cols):
        for ri in range(rows):
            sx = ux0 + (ci + 0.5) * dx
            sy = uy0 + (ri + 0.5) * dy
            if _point_in_polygon(sx, sy, poly_a) and _point_in_polygon(sx, sy, poly_b):
                count += 1
    return count * cell_area


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def _scale_factor_m_per_pt(scale_info: Dict[str, Any]) -> Optional[float]:
    """Metres per PDF point from scale_info dict.

    Returns None if calibration is unavailable (not 0.0).
    """
    rpm = scale_info.get("real_metres_per_page_mm")
    if rpm is not None and rpm > 0:
        return MM_PER_PT * rpm
    return None


def calibrate_area_m2(
    polygon: Sequence[Tuple[float, float]],
    scale_info: Dict[str, Any],
) -> Optional[float]:
    """Convert polygon area from PDF points^2 to real-world m^2.

    Returns None if calibration is unavailable.
    """
    sf = _scale_factor_m_per_pt(scale_info)
    if sf is None:
        return None
    area_pts2 = polygon_area_abs(polygon)
    return area_pts2 * sf * sf


def page_scale_info(page: Dict[str, Any]) -> Dict[str, Any]:
    """Derive authoritative page calibration from a PlanReader page dict.

    Same conversion rule as pb_room_face_takeoff.page_scale_info():
        real_metres_per_page_mm = render_zoom * 2.834646 / px_per_m
    """
    px_per_m = _num(page.get("px_per_m"), 0.0)
    render_zoom = _num(page.get("render_zoom"), 1.0)
    if px_per_m <= 0:
        return {"real_metres_per_page_mm": None, "px_per_m": 0.0, "render_zoom": render_zoom, "scale_text": ""}
    rpm = render_zoom * 2.834646 / px_per_m
    return {
        "real_metres_per_page_mm": rpm,
        "px_per_m": px_per_m,
        "render_zoom": render_zoom,
        "scale_text": str(page.get("scale_text") or ""),
    }


def _num(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Fill polygon extraction from PDF drawings
# ---------------------------------------------------------------------------

def _extract_point(item: Any, idx: int) -> Optional[Tuple[float, float]]:
    """Extract a point from a PyMuPDF drawing item."""
    try:
        p = item[idx]
        return (float(p.x), float(p.y))
    except (AttributeError, IndexError):
        try:
            p = item[idx]
            return (float(p[0]), float(p[1]))
        except (TypeError, IndexError):
            return None


def _extract_rect(item: Any) -> Optional[Tuple[float, float, float, float]]:
    """Extract rect from a PyMuPDF 're' item."""
    try:
        r = item[1]
        return (float(r.x0), float(r.y0), float(r.x1), float(r.y1))
    except (AttributeError, IndexError):
        return None


def _extract_quad(item: Any) -> Optional[Tuple[Tuple[float, float], ...]]:
    """Extract quad corners from a PyMuPDF 'qu' item."""
    try:
        q = item[1]
        return (
            (float(q.ul.x), float(q.ul.y)),
            (float(q.ur.x), float(q.ur.y)),
            (float(q.lr.x), float(q.lr.y)),
            (float(q.ll.x), float(q.ll.y)),
        )
    except (AttributeError, IndexError):
        return None


def _items_are_closed_lines(items: Sequence) -> bool:
    """Check if a sequence of 'l' items forms a closed path.

    A closed path has each line's endpoint matching the next line's startpoint,
    and the last endpoint matching the first startpoint.
    """
    line_items = [it for it in items if it[0] == "l"]
    if len(line_items) < 3:
        return False
    pts = []
    for it in line_items:
        p1 = _extract_point(it, 1)
        p2 = _extract_point(it, 2)
        if p1 is None or p2 is None:
            return False
        pts.append(p1)
    # Check last endpoint -> first startpoint closure
    last_p2 = _extract_point(line_items[-1], 2)
    first_p1 = _extract_point(line_items[0], 1)
    if last_p2 is None or first_p1 is None:
        return False
    tol = 0.5  # PDF points tolerance for endpoint matching
    if math.hypot(last_p2[0] - first_p1[0], last_p2[1] - first_p1[1]) > tol:
        return False
    # Check each line endpoint -> next line startpoint
    for i in range(len(line_items) - 1):
        end = _extract_point(line_items[i], 2)
        start = _extract_point(line_items[i + 1], 1)
        if end is None or start is None:
            return False
        if math.hypot(end[0] - start[0], end[1] - start[1]) > tol:
            return False
    return True


def _closed_line_vertices(items: Sequence) -> Tuple[Tuple[float, float], ...]:
    """Extract polygon vertices from a sequence of closed 'l' items."""
    pts = []
    for it in items:
        if it[0] != "l":
            continue
        p1 = _extract_point(it, 1)
        if p1 is not None:
            pts.append(p1)
    return tuple(pts)


def extract_filled_polygons(pdf_page: Any) -> List[FillPolygon]:
    """Extract all filled polygons from a PDF page's vector drawings.

    Processes get_drawings() and identifies:
      - filled rectangles (single 're' item with fill)
      - filled quads (single 'qu' item with fill)
      - closed line paths (sequence of 'l' items with fill that form a closed path)

    Does NOT process curves ('c') as polygons.

    Returns FillPolygon objects with raw PDF-point coordinates.
    """
    drawings = pdf_page.get_drawings() or []
    results: List[FillPolygon] = []

    for draw_idx, drawing in enumerate(drawings):
        if not isinstance(drawing, dict):
            continue

        fill = drawing.get("fill")
        fill_opacity = _num(drawing.get("fill_opacity"), 1.0)
        stroke = drawing.get("color")
        stroke_opacity = _num(drawing.get("stroke_opacity"), 1.0)
        stroke_width = _num(drawing.get("width"), 0.0)
        close_path = bool(drawing.get("closePath", False))
        even_odd = bool(drawing.get("even_odd", False))
        layer = str(drawing.get("layer") or drawing.get("oc") or "")
        items = drawing.get("items", [])

        if not items:
            continue

        # Only process drawings that have a fill
        if fill is None:
            continue

        # Classify by item types
        kinds = tuple(str(it[0]) for it in items)

        # Case 1: Single rectangle with fill
        if len(items) == 1 and items[0][0] == "re":
            rect = _extract_rect(items[0])
            if rect is None:
                continue
            x0, y0, x1, y1 = rect
            verts = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
            results.append(FillPolygon(
                vertices=verts,
                fill=tuple(fill) if fill else None,
                fill_opacity=fill_opacity,
                stroke=tuple(stroke) if stroke else None,
                stroke_opacity=stroke_opacity,
                stroke_width=stroke_width,
                close_path=True,
                even_odd=even_odd,
                drawing_index=draw_idx,
                layer=layer,
                item_types=kinds,
                geometry_method="native_rectangle",
            ))

        # Case 2: Single quad with fill
        elif len(items) == 1 and items[0][0] == "qu":
            quad = _extract_quad(items[0])
            if quad is None:
                continue
            results.append(FillPolygon(
                vertices=quad,
                fill=tuple(fill) if fill else None,
                fill_opacity=fill_opacity,
                stroke=tuple(stroke) if stroke else None,
                stroke_opacity=stroke_opacity,
                stroke_width=stroke_width,
                close_path=True,
                even_odd=even_odd,
                drawing_index=draw_idx,
                layer=layer,
                item_types=kinds,
                geometry_method="native_quad",
            ))

        # Case 3: Sequence of 'l' items forming a closed path with fill
        elif all(k == "l" for k in kinds) and _items_are_closed_lines(items):
            verts = _closed_line_vertices(items)
            if len(verts) >= 3:
                results.append(FillPolygon(
                    vertices=verts,
                    fill=tuple(fill) if fill else None,
                    fill_opacity=fill_opacity,
                    stroke=tuple(stroke) if stroke else None,
                    stroke_opacity=stroke_opacity,
                    stroke_width=stroke_width,
                    close_path=True,
                    even_odd=even_odd,
                    drawing_index=draw_idx,
                    layer=layer,
                    item_types=kinds,
                    geometry_method="closed_line_path",
                ))

        # Case 4: Mixed items or curves with fill -> Review (deferred)
        # Emit bbox fallback for drawings with curves, or mixed item types.
        # Pure line items that didn't close are open paths — skip them.
        elif fill is not None:
            has_curves = any(k == "c" for k in kinds)
            has_non_l = any(k != "l" for k in kinds)
            # Emit bbox fallback if there are curves, or mixed (non-homogeneous) items
            if not has_non_l:
                # All items are lines but didn't form closed path -> open path, skip
                continue
            all_pts: List[Tuple[float, float]] = []
            for it in items:
                if it[0] == "re":
                    rect = _extract_rect(it)
                    if rect:
                        x0, y0, x1, y1 = rect
                        all_pts.extend([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
                elif it[0] == "qu":
                    quad = _extract_quad(it)
                    if quad:
                        all_pts.extend(quad)
                elif it[0] == "l":
                    p1 = _extract_point(it, 1)
                    p2 = _extract_point(it, 2)
                    if p1:
                        all_pts.append(p1)
                    if p2:
                        all_pts.append(p2)
                elif it[0] == "c":
                    # Bezier curve: extract all 4 control points for bbox
                    for idx in range(1, 5):
                        pt = _extract_point(it, idx)
                        if pt:
                            all_pts.append(pt)
            if all_pts:
                xs = [p[0] for p in all_pts]
                ys = [p[1] for p in all_pts]
                bbox = (min(xs), min(ys), max(xs), max(ys))
                # Emit as a degenerate polygon (just bbox corners) for Review
                verts = (
                    (bbox[0], bbox[1]), (bbox[2], bbox[1]),
                    (bbox[2], bbox[3]), (bbox[0], bbox[3]),
                )
                results.append(FillPolygon(
                    vertices=verts,
                    fill=tuple(fill) if fill else None,
                    fill_opacity=fill_opacity,
                    stroke=tuple(stroke) if stroke else None,
                    stroke_opacity=stroke_opacity,
                    stroke_width=stroke_width,
                    close_path=True,
                    even_odd=even_odd,
                    drawing_index=draw_idx,
                    layer=layer,
                    item_types=kinds,
                    geometry_method="bbox_fallback",
                ))

    return results


# ---------------------------------------------------------------------------
# Code extraction from page text
# ---------------------------------------------------------------------------

def extract_finish_codes_from_text(
    text: str,
    page_id: int = 0,
    page_no: int = 0,
    page_label: str = "",
) -> List[Dict[str, Any]]:
    """Extract finish code occurrences from page text.

    Returns list of dicts with code, text position, and page context.
    This is a text-based extraction; spatial association with polygons
    is done separately via the association pipeline.
    """
    if not text:
        return []
    results = []
    for m in _PAINT_CODE_RE.finditer(text):
        code = m.group(0).upper()
        # Get surrounding context (50 chars either side)
        start = max(0, m.start() - 50)
        end = min(len(text), m.end() + 50)
        context = text[start:end].replace("\n", " ").strip()
        results.append({
            "code": code,
            "start": m.start(),
            "end": m.end(),
            "context": context,
            "page_id": page_id,
            "page_no": page_no,
            "page_label": page_label,
        })
    return results


# ---------------------------------------------------------------------------
# Positioned code extraction (from WordBox / positioned text)
# ---------------------------------------------------------------------------

def extract_finish_codes_from_positions(
    words: Sequence[Any],
    page_id: int = 0,
    page_no: int = 0,
    page_label: str = "",
) -> List[Dict[str, Any]]:
    """Extract finish code occurrences from positioned text words.

    Words should have .text and .bbox attributes (WordBox) or be dicts
    with 'text' and 'bbox' keys.
    """
    results = []
    for w in words:
        text = getattr(w, "text", None) or (w.get("text", "") if isinstance(w, dict) else "")
        text = str(text).strip()
        if not text:
            continue
        # Check if the word itself is a finish code
        m = _PAINT_CODE_RE.fullmatch(text.upper())
        if not m:
            # Also check for codes embedded in multi-word text
            m2 = _PAINT_CODE_RE.search(text)
            if m2:
                code = m2.group(0).upper()
            else:
                continue
        else:
            code = m.group(0).upper()

        bbox = getattr(w, "bbox", None) or (w.get("bbox", (0, 0, 0, 0)) if isinstance(w, dict) else (0, 0, 0, 0))
        try:
            x0, y0, x1, y1 = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
        except (TypeError, IndexError, ValueError):
            continue
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0

        results.append({
            "code": code,
            "bbox": (x0, y0, x1, y1),
            "centroid": (cx, cy),
            "text": text,
            "page_id": page_id,
            "page_no": page_no,
            "page_label": page_label,
        })
    return results


# ---------------------------------------------------------------------------
# Geometry association
# ---------------------------------------------------------------------------

@dataclass
class AssociationResult:
    """Result of associating a SurfaceEvidence with a measured geometry target."""
    target_type: str = ""      # "room", "wall", "elevation_zone", "ceiling"
    target_ref: str = ""       # e.g. "R04", "W01_North"
    method: str = ""           # "containment", "majority_overlap", "centroid", "proximity"
    overlap_ratio: float = 0.0  # 0-1
    confidence: float = 0.0    # 0-1
    evidence: List[str] = field(default_factory=list)


def associate_surface_to_target(
    fill_polygon: FillPolygon,
    target_polygon: Sequence[Tuple[float, float]],
    target_type: str = "",
    target_ref: str = "",
    proximity_threshold_pt: float = 50.0,
) -> AssociationResult:
    """Associate a fill polygon with a measured geometry target.

    Uses a strict hierarchy:
      1. Deterministic full containment (all vertices + no edge crossings) -> strongest
      2. Sampled majority overlap (>50%) -> strong
      3. Sampled significant intersection (>20%) -> moderate
      4. Centroid containment -> moderate
      5. Proximity (centroid within threshold) -> weak / Review

    Returns AssociationResult with method, overlap ratio, and confidence.
    """
    fill_verts = fill_polygon.vertices
    if not fill_verts or not target_polygon:
        return AssociationResult(
            target_type=target_type, target_ref=target_ref,
            method="none", confidence=0.0,
            evidence=["No geometry to associate"],
        )

    evidence_parts: List[str] = []

    # Method 1: Deterministic full containment
    # Requires all vertices inside AND no edge crossings.
    # A rectangle with a small protrusion will NOT pass this test.
    is_deterministic_containment = _deterministic_containment(fill_verts, target_polygon)

    # Sampled overlap (for non-containment classification)
    overlap = _polygon_overlap_ratio(fill_verts, target_polygon)

    # Cap max confidence for bbox_fallback geometry — it can never look authoritative
    is_bbox_fallback = fill_polygon.geometry_method == "bbox_fallback"
    max_geo_confidence = 0.40 if is_bbox_fallback else 1.0

    if is_deterministic_containment:
        evidence_parts.append(
            "All fill vertices inside target, no edge crossings (deterministic containment)"
        )
        return AssociationResult(
            target_type=target_type, target_ref=target_ref,
            method="containment", overlap_ratio=overlap,
            confidence=min(0.95, max_geo_confidence),
            evidence=evidence_parts,
        )

    # Method 2: Majority overlap (>50%) — sampled
    if overlap >= 0.50:
        base_conf = 0.75 + 0.20 * (overlap - 0.50)  # 0.75-0.95
        evidence_parts.append(f"Majority overlap ({overlap:.0%} of fill inside target, sampled)")
        return AssociationResult(
            target_type=target_type, target_ref=target_ref,
            method="majority_overlap", overlap_ratio=overlap,
            confidence=min(base_conf, max_geo_confidence),
            evidence=evidence_parts,
        )

    # Method 3: Significant intersection (>20%) — sampled
    if overlap >= 0.20:
        base_conf = 0.50 + 0.25 * (overlap - 0.20)  # 0.50-0.75
        evidence_parts.append(f"Partial intersection ({overlap:.0%} overlap, sampled)")
        return AssociationResult(
            target_type=target_type, target_ref=target_ref,
            method="intersection", overlap_ratio=overlap,
            confidence=min(base_conf, max_geo_confidence),
            evidence=evidence_parts,
        )

    # Method 4: Centroid containment
    cx, cy = fill_polygon.centroid
    if _point_in_polygon(cx, cy, target_polygon):
        evidence_parts.append(f"Centroid inside target (low overlap: {overlap:.0%})")
        return AssociationResult(
            target_type=target_type, target_ref=target_ref,
            method="centroid", overlap_ratio=overlap,
            confidence=min(0.40, max_geo_confidence),
            evidence=evidence_parts,
        )

    # Method 5: Proximity
    tcx = sum(v[0] for v in target_polygon) / len(target_polygon)
    tcy = sum(v[1] for v in target_polygon) / len(target_polygon)
    dist = math.hypot(cx - tcx, cy - tcy)
    if dist <= proximity_threshold_pt:
        conf = max(0.1, 0.35 * (1.0 - dist / proximity_threshold_pt))
        evidence_parts.append(f"Centroid proximity ({dist:.0f}pt from target centre)")
        return AssociationResult(
            target_type=target_type, target_ref=target_ref,
            method="proximity", overlap_ratio=overlap,
            confidence=min(conf, max_geo_confidence),
            evidence=evidence_parts,
        )

    # No association
    return AssociationResult(
        target_type=target_type, target_ref=target_ref,
        method="none", overlap_ratio=overlap,
        confidence=0.0,
        evidence=[f"No association (overlap={overlap:.0%}, dist={dist:.0f}pt)"],
    )


# ---------------------------------------------------------------------------
# Code-to-polygon spatial association
# ---------------------------------------------------------------------------

def associate_code_to_polygon(
    code_bbox: Tuple[float, float, float, float],
    fill_polygon: FillPolygon,
    proximity_threshold_pt: float = 30.0,
) -> Dict[str, Any]:
    """Check if a code occurrence (from positioned text) falls inside or near a fill polygon.

    Returns dict with 'associated' (bool), 'method', 'confidence'.
    """
    cx = (code_bbox[0] + code_bbox[2]) / 2.0
    cy = (code_bbox[1] + code_bbox[3]) / 2.0

    # Check if code centroid is inside the polygon
    if _point_in_polygon(cx, cy, fill_polygon.vertices):
        return {
            "associated": True,
            "method": "centroid_containment",
            "confidence": 0.85,
        }

    # Check proximity to polygon centroid
    pcx, pcy = fill_polygon.centroid
    dist = math.hypot(cx - pcx, cy - pcy)
    if dist <= proximity_threshold_pt:
        return {
            "associated": True,
            "method": "proximity",
            "confidence": max(0.2, 0.6 * (1.0 - dist / proximity_threshold_pt)),
        }

    return {
        "associated": False,
        "method": "none",
        "confidence": 0.0,
    }


# ---------------------------------------------------------------------------
# Build SurfaceEvidence records
# ---------------------------------------------------------------------------

def build_surface_evidence(
    fill_polygons: List[FillPolygon],
    page_id: int = 0,
    page_no: int = 0,
    page_label: str = "",
    workspace_id: int = 0,
    scale_info: Optional[Dict[str, Any]] = None,
) -> List[SurfaceEvidence]:
    """Convert extracted FillPolygons into SurfaceEvidence records.

    Applies calibration if scale_info is provided.
    Bbox-fallback records get low confidence and area_m2=None.
    """
    # Confidence by geometry extraction method
    _METHOD_CONFIDENCE = {
        "native_rectangle": 0.90,
        "native_quad": 0.85,
        "closed_line_path": 0.80,
        "bbox_fallback": 0.30,
    }

    results = []
    for idx, fp in enumerate(fill_polygons):
        area_pts2 = fp.area_page_pts2
        is_bbox_fallback = fp.geometry_method == "bbox_fallback"

        # Bbox fallback: never trust the area as real fill area
        if is_bbox_fallback:
            area_m2 = None
        elif scale_info:
            area_m2 = calibrate_area_m2(fp.vertices, scale_info)
        else:
            area_m2 = None

        # Determine source_geometry_type
        has_fill = fp.fill is not None
        has_stroke = fp.stroke is not None
        if has_fill and has_stroke:
            geom_type = "fill_stroke"
        elif has_fill:
            geom_type = "fill_only"
        else:
            geom_type = "stroke_only"

        # Geometry confidence based on extraction method
        geo_conf = _METHOD_CONFIDENCE.get(fp.geometry_method, 0.50)

        # Status: bbox_fallback always needs check
        status = "needs_check" if is_bbox_fallback else "unreviewed"

        surface_id = f"page_{page_id}:fill_{idx}" if page_id else f"fill_{idx}"

        ev = SurfaceEvidence(
            workspace_id=workspace_id,
            page_id=page_id,
            page_no=page_no,
            page_label=page_label,
            surface_id=surface_id,
            source_geometry_type=geom_type,
            geometry_method=fp.geometry_method,
            polygon_pdf_pts=fp.vertices,
            bbox=fp.bbox,
            fill_colour=fp.fill,
            fill_opacity=fp.fill_opacity,
            stroke_colour=fp.stroke,
            source_layer=fp.layer,
            source_drawing_index=fp.drawing_index,
            source_item_types=fp.item_types,
            area_page_pts2=area_pts2,
            area_m2=area_m2,
            geometry_confidence=geo_conf,
            status=status,
            evidence=[f"Extracted from drawing {fp.drawing_index}, method={fp.geometry_method}, items={fp.item_types}"],
        )
        results.append(ev)

    return results


# ---------------------------------------------------------------------------
# Production integration: associate SurfaceEvidence with measured surfaces
# ---------------------------------------------------------------------------

def associate_with_measured_surfaces(
    evidence_list: List[SurfaceEvidence],
    measured_surfaces: Sequence[Dict[str, Any]],
    code_occurrences: Optional[List[Dict[str, Any]]] = None,
) -> List[SurfaceEvidence]:
    """Associate SurfaceEvidence records with existing measured surfaces.

    Measured surfaces are dicts with at minimum:
      - polygon (list of [x,y] PDF points) or bbox
      - ref (e.g. "R04", "W01")
      - type (e.g. "room", "wall", "ceiling")
      - area_m2 (authoritative quantity — never modified)

    Code occurrences are positioned finish codes from extract_finish_codes_from_positions().

    For each SurfaceEvidence, finds the best matching measured surface and
    applies code association if codes overlap.
    """
    for sev in evidence_list:
        if not sev.polygon_pdf_pts:
            continue

        best_result: Optional[AssociationResult] = None

        for target in measured_surfaces:
            target_poly = target.get("polygon") or []
            if not target_poly:
                # Try bbox
                bbox = target.get("bbox")
                if bbox and len(bbox) >= 4:
                    x0, y0, x1, y1 = bbox
                    target_poly = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
                else:
                    continue

            # Convert target polygon to tuple format
            target_verts = tuple((float(p[0]), float(p[1])) for p in target_poly)

            result = associate_surface_to_target(
                FillPolygon(vertices=sev.polygon_pdf_pts, geometry_method=sev.geometry_method),
                target_verts,
                target_type=target.get("type", ""),
                target_ref=target.get("ref", ""),
            )

            if best_result is None or result.confidence > best_result.confidence:
                best_result = result

        if best_result and best_result.confidence > 0:
            sev.association_target_type = best_result.target_type
            sev.association_target_ref = best_result.target_ref
            sev.association_method = best_result.method
            sev.association_overlap = best_result.overlap_ratio
            sev.association_confidence = best_result.confidence
            sev.evidence.extend(best_result.evidence)
            # Update status based on confidence
            if best_result.confidence >= 0.85:
                sev.status = "probable"
            elif best_result.confidence >= 0.50:
                sev.status = "needs_check"
            else:
                sev.status = "needs_check"

    # Phase 2: code association (deduplicate by normalised code)
    if code_occurrences:
        for sev in evidence_list:
            if not sev.polygon_pdf_pts:
                continue
            fp = FillPolygon(vertices=sev.polygon_pdf_pts, fill=sev.fill_colour, geometry_method=sev.geometry_method)
            associated_raw: List[str] = []
            for code_occ in code_occurrences:
                code_bbox = code_occ.get("bbox")
                if not code_bbox:
                    continue
                result = associate_code_to_polygon(code_bbox, fp)
                if result["associated"]:
                    associated_raw.append(code_occ.get("code", ""))

            # Deduplicate: same code in multiple positions is NOT a conflict
            distinct_codes: List[str] = sorted({c.upper() for c in associated_raw if c})
            occurrence_count = len(associated_raw)

            # Apply code evidence
            if len(distinct_codes) == 1:
                code = distinct_codes[0]
                sev.finish_code = code
                sev.semantic_confidence = 0.70  # Code present but substrate unknown
                sev.evidence.append(
                    f"Finish code {code} found inside polygon"
                    + (f" ({occurrence_count} occurrences)" if occurrence_count > 1 else "")
                )
                if not sev.substrate:
                    sev.substrate = "To confirm"
                    sev.status = "needs_check"
            elif len(distinct_codes) > 1:
                # Conflict: multiple DISTINCT codes inside same polygon
                sev.status = "conflict"
                sev.semantic_confidence = 0.0
                sev.notes = f"Multiple distinct codes found: {', '.join(distinct_codes)}"
                sev.evidence.append(
                    f"CONFLICT: {len(distinct_codes)} distinct codes inside polygon: "
                    + ", ".join(distinct_codes)
                )

    return evidence_list


# ---------------------------------------------------------------------------
# Production adapter: process page through full chain and store results
# ---------------------------------------------------------------------------

def _get_measured_surfaces_for_page(
    app: Any, page_id: int, workspace_id: int, page: Dict[str, Any],
    diagnostics: SurfaceProcessingDiagnostics,
) -> List[Dict[str, Any]]:
    """Retrieve existing measured surfaces (rooms/walls) for a page.

    Uses REAL production interfaces:
      - extract_room_faces_from_page() from pb_room_face_takeoff (Priority 2)
      - registered_wall_records_v135() from pb_elevation_registration_v135

    Errors are captured into diagnostics rather than silently swallowed.

    Returns list of dicts with polygon, ref, type, area_m2 — compatible
    with associate_with_measured_surfaces() input contract.
    """
    surfaces: List[Dict[str, Any]] = []

    # 1. Room face polygons from Priority 2
    try:
        from pb_room_face_takeoff import extract_room_faces_from_page
        room_faces = extract_room_faces_from_page(app, page)
        for rf in room_faces:
            poly = rf.polygon_pdf_pts
            if poly and len(poly) >= 3:
                surfaces.append({
                    "polygon": [(float(p[0]), float(p[1])) for p in poly],
                    "ref": str(rf.room_ref or rf.label or ""),
                    "type": "room",
                    "area_m2": rf.floor_area_m2,
                })
        diagnostics.measured_room_targets_count = len([
            s for s in surfaces if s["type"] == "room"
        ])
    except Exception as exc:
        diagnostics.room_extraction_error = f"{type(exc).__name__}: {exc}"

    # 2. Registered wall data from v135 (only if source_polygon is real coords)
    try:
        if hasattr(app, "registered_wall_records_v135"):
            walls = app.registered_wall_records_v135(int(workspace_id))
            for wall in walls:
                source_poly = wall.get("source_polygon")
                # source_polygon may be a string ID or actual coords
                if isinstance(source_poly, (list, tuple)) and len(source_poly) >= 3:
                    surfaces.append({
                        "polygon": [(float(p[0]), float(p[1])) for p in source_poly],
                        "ref": str(wall.get("wall_ref") or ""),
                        "type": "wall",
                        "area_m2": wall.get("net_m2"),
                    })
        diagnostics.measured_wall_targets_count = len([
            s for s in surfaces if s["type"] == "wall"
        ])
    except Exception as exc:
        diagnostics.wall_extraction_error = f"{type(exc).__name__}: {exc}"

    return surfaces


def process_page_surface_evidence(
    app: Any,
    page_id: int,
    workspace_id: int,
) -> SurfaceProcessingResult:
    """Production adapter: process one page through the full SurfaceEvidence chain.

    Uses ONLY real PlanReader production interfaces:
      - pages table: id, document_id, page_no, page_label, page_type, px_per_m
      - documents table: path (PDF file location)
      - fitz.open(documents.path) -> pdf[page_no - 1]
      - pdf_page.get_text("words") for positioned text
      - extract_room_faces_from_page() for Priority 2 measured surfaces
      - app.set_workspace_setting() for storage
      - app.registered_wall_records_v135() for wall data

    Returns a SurfaceProcessingResult containing:
      - evidence: list of SurfaceEvidence records
      - diagnostics: structured pipeline stage outcomes
      - status: "ok" | "partial" | "error" | "no_fills"

    Diagnostics record every stage outcome so failures are never silently
    hidden behind a normal-looking result.
    """
    import json
    from pathlib import Path

    diag = SurfaceProcessingDiagnostics()

    # ------------------------------------------------------------------
    # Step 1: Query real pages + documents schema
    # ------------------------------------------------------------------
    try:
        rows = app.lquery(
            "SELECT p.id, p.document_id, p.page_no, p.page_label, "
            "p.page_type, p.px_per_m, p.render_zoom, p.scale_text "
            "FROM pages p WHERE p.id=?",
            (page_id,),
        )
        if not rows:
            return SurfaceProcessingResult(diagnostics=diag, status="error")
        r = rows[0]
        diag.page_lookup_ok = True
    except Exception as exc:
        diag.page_lookup_ok = False
        return SurfaceProcessingResult(diagnostics=diag, status="error")

    doc_id = int(r.get("document_id") or 0)
    page_no = int(r.get("page_no") or 1)
    page_label = str(r.get("page_label") or "")
    page_type = str(r.get("page_type") or "")

    page_dict = {
        "id": page_id,
        "document_id": doc_id,
        "page_no": page_no,
        "page_label": page_label,
        "page_type": page_type,
        "px_per_m": r.get("px_per_m"),
        "render_zoom": r.get("render_zoom"),
        "scale_text": str(r.get("scale_text") or ""),
    }

    # ------------------------------------------------------------------
    # Step 2: Open PDF via documents.path (real production path)
    # ------------------------------------------------------------------
    pdf_page = None
    _pdf_doc = None  # keep reference for cleanup
    try:
        import fitz as _fitz
        doc_rows = app.lquery(
            "SELECT path FROM documents WHERE id=?", (doc_id,)
        )
        if not doc_rows:
            return SurfaceProcessingResult(diagnostics=diag, status="error")
        pdf_path = Path(str(doc_rows[0].get("path") or ""))
        if pdf_path.suffix.lower() != ".pdf" or not pdf_path.is_file():
            return SurfaceProcessingResult(diagnostics=diag, status="error")
        _pdf_doc = _fitz.open(pdf_path)
        page_idx = page_no - 1
        if page_idx < 0 or page_idx >= len(_pdf_doc):
            return SurfaceProcessingResult(diagnostics=diag, status="error")
        pdf_page = _pdf_doc[page_idx]
        diag.pdf_open_ok = True
    except Exception:
        return SurfaceProcessingResult(diagnostics=diag, status="error")

    if pdf_page is None:
        return SurfaceProcessingResult(diagnostics=diag, status="error")

    try:
        # ------------------------------------------------------------------
        # Step 3: Extract positioned words FIRST (shared by hatch + fills)
        #
        # BLOCKER 2 fix: words must be available BEFORE hatch detection
        # so false-positive text filters (GRID, BATTEN, dimensions, etc.)
        # work in production, not only in isolated tests.
        # ------------------------------------------------------------------
        positioned_words: List[Dict[str, Any]] = []
        positioned_code_occurrences: List[Dict[str, Any]] = []
        text_only_codes: List[Dict[str, Any]] = []

        try:
            words_raw = pdf_page.get_text("words") or []
            for w in words_raw:
                if len(w) < 5:
                    continue
                try:
                    x0, y0, x1, y1 = float(w[0]), float(w[1]), float(w[2]), float(w[3])
                except (TypeError, ValueError):
                    continue
                text = str(w[4]).strip()
                if text:
                    positioned_words.append({"text": text, "bbox": [x0, y0, x1, y1]})

            diag.positioned_words_extracted_count = len(positioned_words)

            if positioned_words:
                positioned_code_occurrences = extract_finish_codes_from_positions(
                    positioned_words, page_id=page_id, page_no=page_no,
                    page_label=page_label,
                )
        except Exception as exc:
            diag.positioned_words_extraction_error = (
                f"{type(exc).__name__}: {exc}"
            )

        diag.finish_codes_found_count = len(positioned_code_occurrences)

        # ------------------------------------------------------------------
        # Step 4: Extract filled polygons
        # ------------------------------------------------------------------
        fill_polygons = extract_filled_polygons(pdf_page)
        diag.fills_extracted_count = len(fill_polygons)

        # ------------------------------------------------------------------
        # Step 5: Hatch extraction (B2) — WITH positioned words for
        #         false-positive text filters
        # ------------------------------------------------------------------
        hatch_result = None
        try:
            from pb_hatch_detection_v160 import extract_hatch_evidence
            scale_for_hatch = page_scale_info(page_dict)
            hatch_result = extract_hatch_evidence(
                pdf_page, page_id=page_id, page_no=page_no,
                page_label=page_label, workspace_id=workspace_id,
                scale_info=scale_for_hatch,
                words=positioned_words,  # BLOCKER 2 fix: words for FP filters
            )
        except Exception as exc:
            diag.hatch_diag.extraction_error = (
                f"{type(exc).__name__}: {exc}"
            )

        # BLOCKER 4 fix: propagate actual detector diagnostics
        hatch_evidence_list: List[SurfaceEvidence] = []
        if hatch_result is not None:
            hatch_evidence_list = hatch_result.evidence
            diag.hatch_diag.strokes_extracted = hatch_result.strokes_extracted
            diag.hatch_diag.clusters_found = hatch_result.clusters_found
            diag.hatch_diag.clusters_rejected = hatch_result.clusters_rejected
            diag.hatch_diag.regions_reconstructed = hatch_result.regions_reconstructed
            diag.hatch_diag.low_confidence_regions = hatch_result.low_confidence_regions
            diag.hatch_diag.extraction_error = hatch_result.extraction_error

        # ------------------------------------------------------------------
        # Failure-state requirement: distinguish genuinely empty from failed
        # ------------------------------------------------------------------
        has_hatch_error = bool(diag.hatch_diag.extraction_error)
        if not fill_polygons and not hatch_evidence_list:
            if has_hatch_error:
                # Hatch stage failed — do NOT return normal "no_fills"
                return SurfaceProcessingResult(
                    diagnostics=diag, status="partial",
                )
            return SurfaceProcessingResult(
                diagnostics=diag, status="no_fills",
            )

        # ------------------------------------------------------------------
        # Step 6: Build SurfaceEvidence with calibration
        # ------------------------------------------------------------------
        scale = page_scale_info(page_dict)
        evidence_list = build_surface_evidence(
            fill_polygons,
            page_id=page_id,
            page_no=page_no,
            page_label=page_label,
            workspace_id=workspace_id,
            scale_info=scale,
        )

        # Append hatch evidence (B2) — these carry their own surface_ids
        # and were already calibrated inside extract_hatch_evidence.
        evidence_list.extend(hatch_evidence_list)

        # ------------------------------------------------------------------
        # Text-only fallback: extract codes WITHOUT spatial info.
        # These are retained for metadata but NOT used for polygon association.
        # ------------------------------------------------------------------
        try:
            text_str = pdf_page.get_text("text") or ""
            if text_str:
                text_only_codes = extract_finish_codes_from_text(
                    text_str, page_id=page_id, page_no=page_no,
                    page_label=page_label,
                )
        except Exception:
            pass

        diag.text_only_codes_found_count = len(text_only_codes)

        # Use positioned codes for spatial association (have bbox).
        # Text-only codes are metadata only -- never pretend they have coordinates.
        code_occurrences = positioned_code_occurrences

        # ------------------------------------------------------------------
        # Step 6: Get measured surfaces via real Priority 2 / v135 interfaces
        # ------------------------------------------------------------------
        measured = _get_measured_surfaces_for_page(
            app, page_id, workspace_id, page_dict, diag,
        )

        # ------------------------------------------------------------------
        # Step 7: Associate
        # ------------------------------------------------------------------
        evidence_list = associate_with_measured_surfaces(
            evidence_list, measured, code_occurrences=code_occurrences,
        )

        # Count associated vs unassociated
        diag.associated_count = sum(
            1 for ev in evidence_list
            if ev.association_method and ev.association_method != "none"
        )
        diag.unassociated_count = len(evidence_list) - diag.associated_count

        # ------------------------------------------------------------------
        # Accuracy-status requirement:
        # If fills or hatches were extracted but measured-surface extraction
        # failed, evidence must NOT look like a normal successful result.
        # ------------------------------------------------------------------
        measured_extraction_failed = bool(
            diag.room_extraction_error or diag.wall_extraction_error
        )
        has_any_geometry = (
            diag.fills_extracted_count > 0
            or diag.hatch_diag.regions_reconstructed > 0
        )
        if measured_extraction_failed and has_any_geometry:
            for sev in evidence_list:
                if not sev.association_method or sev.association_method == "none":
                    sev.status = "needs_check"
                    reasons = []
                    if diag.room_extraction_error:
                        reasons.append(
                            f"room extraction failed: {diag.room_extraction_error}"
                        )
                    if diag.wall_extraction_error:
                        reasons.append(
                            f"wall extraction failed: {diag.wall_extraction_error}"
                        )
                    sev.evidence.append(
                        "Measured-surface association unavailable: "
                        + "; ".join(reasons)
                    )

        # Distinguish "no code found" from "code extraction unavailable"
        if diag.positioned_words_extraction_error:
            for sev in evidence_list:
                if not sev.finish_code:
                    sev.evidence.append(
                        "Finish code extraction unavailable: "
                        + diag.positioned_words_extraction_error
                    )

        # Attach text-only code evidence as metadata (no spatial association)
        if text_only_codes and not positioned_code_occurrences:
            for sev in evidence_list:
                codes_found = sorted({c["code"] for c in text_only_codes})
                if codes_found:
                    sev.evidence.append(
                        f"Text-only codes on page (no spatial position): "
                        + ", ".join(codes_found)
                    )

        # Update hatch association diagnostics from actual detector result
        if hatch_result is not None:
            diag.hatch_diag.associated = hatch_result.associated
            diag.hatch_diag.unassociated = hatch_result.unassociated

        # ------------------------------------------------------------------
        # Step 8: Store results via app.set_workspace_setting()
        #
        # Order matters: set diagnostic fields BEFORE serialising, so the
        # persisted JSON accurately reflects the storage outcome.
        # ------------------------------------------------------------------
        evidence_stored = False
        try:
            setting_key = f"surface_evidence_v160_page_{page_id}"
            records = [ev.to_dict() for ev in evidence_list]
            payload = json.dumps(records, separators=(",", ":"))
            if hasattr(app, "set_workspace_setting"):
                app.set_workspace_setting(int(workspace_id), setting_key, payload)
            else:
                # Test/compatibility fallback only — production always has
                # set_workspace_setting.
                app.lexecute(
                    "INSERT OR REPLACE INTO workspace_settings "
                    "(workspace_id, setting_key, setting_value, updated_at) "
                    "VALUES (?, ?, ?, datetime('now'))",
                    (workspace_id, setting_key, payload),
                )
            evidence_stored = True
        except Exception as exc:
            diag.storage_error = f"evidence: {type(exc).__name__}: {exc}"

        # Store diagnostics — serialise AFTER updating storage fields so
        # the persisted JSON accurately reflects the evidence storage outcome.
        diagnostics_stored = False
        diag.storage_ok = evidence_stored  # accurate for persisted diag JSON
        try:
            diag_key = f"surface_evidence_v160_diag_page_{page_id}"
            diag_payload = json.dumps(diag.to_dict(), separators=(",", ":"))
            if hasattr(app, "set_workspace_setting"):
                app.set_workspace_setting(int(workspace_id), diag_key, diag_payload)
            else:
                app.lexecute(
                    "INSERT OR REPLACE INTO workspace_settings "
                    "(workspace_id, setting_key, setting_value, updated_at) "
                    "VALUES (?, ?, ?, datetime('now'))",
                    (workspace_id, diag_key, diag_payload),
                )
            diagnostics_stored = True
        except Exception as exc:
            # Diagnostics storage failed — we still have the in-memory copy
            # but the persisted record may disagree.  Record this.
            diag.storage_error += (
                f" | diagnostics: {type(exc).__name__}: {exc}"
            )

        # Update storage_ok to reflect BOTH storage operations for the
        # in-memory result (persisted JSON already has accurate evidence-
        # storage status from the assignment above).
        diag.storage_ok = evidence_stored and diagnostics_stored

        # Determine overall status
        any_storage_error = bool(diag.storage_error)
        if not evidence_stored:
            overall_status = "error"
        elif any_storage_error or measured_extraction_failed:
            overall_status = "partial"
        else:
            overall_status = "ok"

        return SurfaceProcessingResult(
            evidence=evidence_list,
            diagnostics=diag,
            status=overall_status,
        )

    finally:
        # Clean up PDF document
        try:
            if _pdf_doc is not None:
                _pdf_doc.close()
        except Exception:
            pass


def get_surface_evidence_diagnostics_v160(
    app: Any, page_id: int, workspace_id: int,
) -> Optional[SurfaceProcessingDiagnostics]:
    """Retrieve stored diagnostics for a page, or None if not found."""
    try:
        setting_key = f"surface_evidence_v160_diag_page_{page_id}"
        if hasattr(app, "workspace_setting"):
            raw = app.workspace_setting(int(workspace_id), setting_key, "{}")
        else:
            rows = app.lexecute(
                "SELECT setting_value FROM workspace_settings "
                "WHERE workspace_id=? AND setting_key=?",
                (workspace_id, setting_key),
            )
            raw = rows[0][0] if rows else "{}"
        parsed = json.loads(str(raw or "{}"))
        return SurfaceProcessingDiagnostics(**parsed)
    except Exception:
        return None


def apply(app: Any) -> None:
    """Wire SurfaceEvidence extraction into the PlanReader app.

    Follows the established apply() monkey-patch pattern.
    """
    if getattr(app, "_pb_surface_evidence_v160_applied", False):
        return
    app._pb_surface_evidence_v160_applied = True

    # Expose module on app object
    app.surface_evidence_v160 = {
        "version": VERSION,
        "extract_filled_polygons": extract_filled_polygons,
        "build_surface_evidence": build_surface_evidence,
        "associate_with_measured_surfaces": associate_with_measured_surfaces,
        "extract_finish_codes_from_text": extract_finish_codes_from_text,
        "extract_finish_codes_from_positions": extract_finish_codes_from_positions,
        "associate_code_to_polygon": associate_code_to_polygon,
        "associate_surface_to_target": associate_surface_to_target,
        "page_scale_info": page_scale_info,
        "calibrate_area_m2": calibrate_area_m2,
        "SurfaceProcessingResult": SurfaceProcessingResult,
        "SurfaceProcessingDiagnostics": SurfaceProcessingDiagnostics,
    }

    # Expose key functions as app-level callables
    app.extract_filled_polygons = extract_filled_polygons
    app.build_surface_evidence_v160 = build_surface_evidence
    app.associate_surface_evidence_v160 = associate_with_measured_surfaces
    app.process_page_surface_evidence_v160 = process_page_surface_evidence
    app.get_surface_evidence_diagnostics_v160 = get_surface_evidence_diagnostics_v160
