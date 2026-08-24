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
    app: Any, page_id: int, workspace_id: int
) -> List[Dict[str, Any]]:
    """Retrieve existing measured surfaces (rooms/walls) for a page.

    Returns list of dicts with polygon, ref, type, area_m2 — compatible
    with associate_with_measured_surfaces() input contract.
    """
    surfaces: List[Dict[str, Any]] = []

    # Get room face takeoff data (rooms with calibrated polygons)
    try:
        room_data = app.get_room_face_takeoff and app.get_room_face_takeoff()
        if room_data:
            for room in room_data:
                if room.get("page_id") == page_id:
                    poly = room.get("polygon") or room.get("calibrated_polygon")
                    if poly:
                        surfaces.append({
                            "polygon": poly,
                            "ref": room.get("room_ref") or room.get("label", ""),
                            "type": "room",
                            "area_m2": room.get("area_m2"),
                        })
    except Exception:
        pass

    # Get registered wall data
    try:
        wall_data = app.get_registered_walls and app.get_registered_walls()
        if wall_data:
            for wall in wall_data:
                if wall.get("page_id") == page_id:
                    # Walls may have bbox but not polygon
                    bbox = wall.get("bbox")
                    if bbox:
                        x0, y0, x1, y1 = bbox
                        surfaces.append({
                            "bbox": [x0, y0, x1, y1],
                            "ref": wall.get("wall_ref") or wall.get("label", ""),
                            "type": "wall",
                            "area_m2": wall.get("gross_m2") or wall.get("area_m2"),
                        })
    except Exception:
        pass

    # Get takeoff rows as fallback measured surfaces
    try:
        rows = app.lexecute(
            "SELECT id, location, substrate, quantity, unit, notes "
            "FROM takeoff_rows WHERE workspace_id=? AND source_page=?",
            (workspace_id, page_id),
        )
        if rows:
            for row in rows:
                # Only include rows that have an associated polygon in notes
                # (e.g., from room face takeoff or floor mapper)
                surfaces.append({
                    "ref": row[1] or "",
                    "type": "takeoff_row",
                    "area_m2": row[3] if row[4] == "m²" else None,
                })
    except Exception:
        pass

    return surfaces


def process_page_surface_evidence(
    app: Any,
    page_id: int,
    workspace_id: int,
) -> List[SurfaceEvidence]:
    """Production adapter: process one page through the full SurfaceEvidence chain.

    Steps:
      1. Retrieve the PDF page object from the database
      2. Extract filled polygons via get_drawings()
      3. Build SurfaceEvidence with page calibration
      4. Extract positioned finish codes from page text
      5. Retrieve existing measured surfaces for this page
      6. Associate SurfaceEvidence with measured surfaces
      7. Store results in workspace settings
      8. Return evidence list

    This is the narrow production integration. It does NOT rewrite the
    takeoff pipeline — it only adds classification metadata.
    """
    import json

    # Step 1: Get page data and PDF object
    try:
        pages = app.lexecute(
            "SELECT id, page_no, label, px_per_m, render_zoom, scale_text "
            "FROM pages WHERE id=?", (page_id,)
        )
        if not pages:
            return []
        page_row = pages[0]
    except Exception:
        return []

    page_dict = {
        "id": page_row[0],
        "page_no": page_row[1],
        "label": page_row[2] or "",
        "px_per_m": page_row[3],
        "render_zoom": page_row[4],
        "scale_text": page_row[5] or "",
    }
    page_no = page_dict["page_no"]
    page_label = page_dict["label"]

    # Step 2: Get PDF page object (PyMuPDF)
    pdf_page = None
    try:
        if hasattr(app, "get_pdf_page"):
            pdf_page = app.get_pdf_page(page_id)
        elif hasattr(app, "lexecute"):
            blobs = app.lexecute(
                "SELECT pdf_blob FROM pages WHERE id=?", (page_id,)
            )
            if blobs and blobs[0][0]:
                import fitz
                doc = fitz.open(stream=blobs[0][0], filetype="pdf")
                pdf_page = doc[0]
    except Exception:
        pass

    if pdf_page is None:
        return []

    # Step 3: Extract filled polygons
    fill_polygons = extract_filled_polygons(pdf_page)

    # Step 4: Build SurfaceEvidence with calibration
    scale = page_scale_info(page_dict)
    evidence_list = build_surface_evidence(
        fill_polygons,
        page_id=page_id,
        page_no=page_no,
        page_label=page_label,
        workspace_id=workspace_id,
        scale_info=scale,
    )

    # Step 5: Extract positioned finish codes from page text
    code_occurrences: List[Dict[str, Any]] = []
    try:
        if hasattr(pdf_page, "get_text"):
            text = pdf_page.get_text("text") or ""
            code_occurrences = extract_finish_codes_from_text(
                text, page_id=page_id, page_no=page_no, page_label=page_label
            )
    except Exception:
        pass

    # Also try positioned word extraction
    try:
        if hasattr(app, "extract_words_with_positions"):
            words = app.extract_words_with_positions(page_id)
            if words:
                positioned_codes = extract_finish_codes_from_positions(
                    words, page_id=page_id, page_no=page_no, page_label=page_label
                )
                # Merge positioned codes (prefer positioned over text-based)
                code_occurrences = positioned_codes or code_occurrences
    except Exception:
        pass

    # Step 6: Get existing measured surfaces for this page
    measured = _get_measured_surfaces_for_page(app, page_id, workspace_id)

    # Step 7: Associate
    evidence_list = associate_with_measured_surfaces(
        evidence_list, measured, code_occurrences=code_occurrences
    )

    # Step 8: Store results in workspace settings
    try:
        setting_key = f"surface_evidence_v160_page_{page_id}"
        records = [ev.to_dict() for ev in evidence_list]
        app.lexecute(
            "INSERT OR REPLACE INTO workspace_settings "
            "(workspace_id, setting_key, setting_value, updated_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (workspace_id, setting_key, json.dumps(records)),
        )
    except Exception:
        pass

    return evidence_list


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
    }

    # Expose key functions as app-level callables
    app.extract_filled_polygons = extract_filled_polygons
    app.build_surface_evidence_v160 = build_surface_evidence
    app.associate_surface_evidence_v160 = associate_with_measured_surfaces
    app.process_page_surface_evidence_v160 = process_page_surface_evidence
