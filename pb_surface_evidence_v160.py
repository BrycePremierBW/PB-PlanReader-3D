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
        """Signed area in PDF points squared (Shoelace formula)."""
        return _shoelace_area(self.vertices)

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
                ))

        # Case 4: Mixed items or curves with fill -> Review (deferred)
        # Only emit for drawings that have non-line items (curves, quads)
        # Pure line items that didn't close are open paths — skip them.
        elif fill is not None:
            non_line_kinds = [k for k in kinds if k not in ("l",)]
            has_curves = any(k == "c" for k in kinds)
            has_quads = any(k == "qu" for k in kinds)
            # Only emit bbox fallback if there are curves, quads, or rects
            # Pure unmatched line items = open path -> skip
            if not non_line_kinds:
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

    Uses the hierarchy:
      1. Full containment (100% of fill inside target) -> strongest
      2. Majority overlap (>50% of fill inside target) -> strong
      3. Significant intersection (>20%) -> moderate
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

    # Compute what fraction of fill polygon is inside target
    overlap = _polygon_overlap_ratio(fill_verts, target_polygon)

    # Compute intersection area for additional signal
    fill_area = polygon_area_abs(fill_verts)
    target_area = polygon_area_abs(target_polygon)

    # Method 1: Full containment (>95% overlap — allow for sampling noise)
    if overlap >= 0.95:
        evidence_parts.append(f"Fill fully contained in target ({overlap:.0%} overlap)")
        return AssociationResult(
            target_type=target_type, target_ref=target_ref,
            method="containment", overlap_ratio=overlap,
            confidence=0.95,
            evidence=evidence_parts,
        )

    # Method 2: Majority overlap (>50%)
    if overlap >= 0.50:
        evidence_parts.append(f"Majority overlap ({overlap:.0%} of fill inside target)")
        return AssociationResult(
            target_type=target_type, target_ref=target_ref,
            method="majority_overlap", overlap_ratio=overlap,
            confidence=0.75 + 0.20 * (overlap - 0.50),  # 0.75-0.95
            evidence=evidence_parts,
        )

    # Method 3: Significant intersection (>20%)
    if overlap >= 0.20:
        evidence_parts.append(f"Partial intersection ({overlap:.0%} overlap)")
        return AssociationResult(
            target_type=target_type, target_ref=target_ref,
            method="intersection", overlap_ratio=overlap,
            confidence=0.50 + 0.25 * (overlap - 0.20),  # 0.50-0.75
            evidence=evidence_parts,
        )

    # Method 4: Centroid containment
    cx, cy = fill_polygon.centroid
    if _point_in_polygon(cx, cy, target_polygon):
        evidence_parts.append(f"Centroid inside target (low overlap: {overlap:.0%})")
        return AssociationResult(
            target_type=target_type, target_ref=target_ref,
            method="centroid", overlap_ratio=overlap,
            confidence=0.40,
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
            confidence=conf,
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
    """
    results = []
    for idx, fp in enumerate(fill_polygons):
        area_pts2 = fp.area_page_pts2
        area_m2 = calibrate_area_m2(fp.vertices, scale_info) if scale_info else None

        # Determine source_geometry_type
        has_fill = fp.fill is not None
        has_stroke = fp.stroke is not None
        if has_fill and has_stroke:
            geom_type = "fill_stroke"
        elif has_fill:
            geom_type = "fill_only"
        else:
            geom_type = "stroke_only"

        # Geometry confidence based on polygon quality
        n_verts = len(fp.vertices)
        if n_verts >= 3 and fp.close_path:
            geo_conf = 0.90
        elif n_verts >= 3:
            geo_conf = 0.75
        else:
            geo_conf = 0.50

        surface_id = f"page_{page_id}:fill_{idx}" if page_id else f"fill_{idx}"

        ev = SurfaceEvidence(
            workspace_id=workspace_id,
            page_id=page_id,
            page_no=page_no,
            page_label=page_label,
            surface_id=surface_id,
            source_geometry_type=geom_type,
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
            status="unreviewed",
            evidence=[f"Extracted from drawing {fp.drawing_index}, items={fp.item_types}"],
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
                FillPolygon(vertices=sev.polygon_pdf_pts),
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

    # Phase 2: code association
    if code_occurrences:
        for sev in evidence_list:
            if not sev.polygon_pdf_pts:
                continue
            fp = FillPolygon(vertices=sev.polygon_pdf_pts, fill=sev.fill_colour)
            associated_codes: List[str] = []
            for code_occ in code_occurrences:
                code_bbox = code_occ.get("bbox")
                if not code_bbox:
                    continue
                result = associate_code_to_polygon(code_bbox, fp)
                if result["associated"]:
                    associated_codes.append(code_occ.get("code", ""))

            # Apply code evidence
            if len(associated_codes) == 1:
                code = associated_codes[0].upper()
                sev.finish_code = code
                sev.semantic_confidence = 0.70  # Code present but substrate unknown
                sev.evidence.append(f"Finish code {code} found inside polygon")
                if not sev.substrate:
                    sev.substrate = "To confirm"
                    sev.status = "needs_check"
            elif len(associated_codes) > 1:
                # Conflict: multiple codes inside same polygon
                sev.status = "conflict"
                sev.semantic_confidence = 0.0
                sev.notes = f"Multiple codes found: {', '.join(associated_codes)}"
                sev.evidence.append(
                    f"CONFLICT: {len(associated_codes)} codes inside polygon: "
                    + ", ".join(associated_codes)
                )

    return evidence_list


# ---------------------------------------------------------------------------
# Apply function (monkey-patch pattern)
# ---------------------------------------------------------------------------

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
