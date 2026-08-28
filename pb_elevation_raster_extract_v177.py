"""PlanReader v1.7.7 raster elevation opening-candidate extractor (Phase 2A).

Primary extraction path for elevation sheets whose openings are best read
from the RENDERED PIXEL geometry (works for the LAGO CD300x elevation pages,
which are native vector polyline drawings WITHOUT discrete 're' rectangle
objects — so a raster + computer-vision read is the robust primary path).

Purpose
-------
Produce conservative GEOMETRIC elevation opening OBSERVATIONS from real
drawing geometry.  Phase 2A is extraction + benchmark only: this module NEVER
creates a physical B1 instance and NEVER sets deduct=True.

Safety contract
---------------
  - Input geometry is read from a page RENDER (pixels), not decoded PDF
    text/schedules.
  - Every candidate is a geometric OBSERVATION carrying: source info, page,
    side, bbox (original pixels), centroid, measured visible width/height
    ONLY when calibration is valid, calibration source, geometry confidence,
    extraction method, possible label only when genuinely supported, review/
    ambiguity status.
  - GENERIC rectangles remain dimension_basis="unknown".  Nothing here
    manufactures rough-opening (wall-void) authority.
  - False-positive controls: reject rectangles that are not plausibly
    opening-sized; do NOT infer an opening merely from proportions; use
    repetition/grid geometry only as SUPPORTING evidence, never as proof.
  - When calibration is invalid the page is non-dimensional and measured
    width/height are NOT reported (None).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pb_elevation_calibration_v177 import (
    Calibration,
    COORD_SPACE_RENDER_PIXEL,
)

VERSION = "1.7.7"

# Geofence: only accept candidate rectangles within these pixel size limits
# when calibration is available (in metres, matching B3's opening-size band).
_OPENING_MIN_WIDTH_M = 0.30
_OPENING_MAX_WIDTH_M = 6.0
_OPENING_MIN_HEIGHT_M = 0.30
_OPENING_MAX_HEIGHT_M = 5.0

# Review status values
STATUS_ACCEPTED = "accepted"
STATUS_REVIEW = "review"
STATUS_REJECTED = "rejected"


@dataclass(frozen=True)
class ElevationRectCandidate:
    """A conservative rectangular geometry observation from an elevation.

    Geometry ONLY — never a physical instance, never deductive authority.
    """
    source_filename: str
    source_page: int
    drawing_ref: str
    elevation_side: str
    bbox: Tuple[int, int, int, int]       # original pixel bbox (x0,y0,x1,y1)
    centroid: Tuple[float, float]         # pixel centroid
    calibration_method: str
    coord_space: str
    render_dpi: Optional[float] = None
    width_m: Optional[float] = None       # measured visible width (None if non-dimensional)
    height_m: Optional[float] = None      # measured visible height (None if non-dimensional)
    calibration_source: str = ""
    dimension_basis: str = "unknown"
    geometry_confidence: float = 0.5
    extraction_method: str = "raster_rect"
    label: str = ""                       # only when genuinely supported
    level_band: Optional[str] = None      # only when objectively derived
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
            "width_m": self.width_m,
            "height_m": self.height_m,
            "dimension_basis": self.dimension_basis,
            "calibration_method": self.calibration_method,
            "calibration_source": self.calibration_source,
            "coord_space": self.coord_space,
            "render_dpi": self.render_dpi,
            "geometry_confidence": self.geometry_confidence,
            "extraction_method": self.extraction_method,
            "label": self.label,
            "level_band": self.level_band,
            "review_status": self.review_status,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Geometry helpers (import-light: numpy/cv2 imported lazily so the module
# still imports in environments without them, and unit tests can run pure
# geometry paths)
# ---------------------------------------------------------------------------
def _area_ratio(contour_area: float, bbox_area: float) -> float:
    """How much of the bbox the contour's area fills (0..1)."""
    if bbox_area <= 0:
        return 0.0
    return max(0.0, min(1.0, contour_area / bbox_area))


def _is_rectangular(contour_area: float, bbox_area: float,
                    fill_threshold: float = 0.35) -> bool:
    """True when a contour roughly fills its bounding box (rectangular).

    A ring/frame outline fills much less than a solid box; openings in
    elevations are usually bounded thin rectangles.  We accept frames whose
    dark perimeter is present but not necessarily solid-filled, so a low
    fill threshold plus bbox matching is used.  The caller is responsible
    for the opening-size geofence.
    """
    # A true rectangle has a high bounding-box match; reject thin slivers.
    return bbox_area > 0 and _area_ratio(contour_area, bbox_area) >= fill_threshold


def detect_raster_rect_candidates(
    image: Any,
    calibration: Calibration,
    *,
    source_filename: str = "",
    source_page: int = 0,
    drawing_ref: str = "",
    elevation_side: str = "",
    dark_threshold: int = 160,
    min_side_px: int = 8,
    min_area_px: int = 0,
    drawing_region: Optional[Sequence[int]] = None,
    calibration_source: str = "",
) -> List[ElevationRectCandidate]:
    """Detect conservative rectangular opening candidates in a rendered page.

    Args:
        image: A numpy HxWx3 (BGR) or HxW grayscale uint8 image of the page
            render.  Every emitted candidate's bbox/centroid are in THIS
            image's PIXEL space, so coord_space is ALWAYS render_pixel —
            regardless of the calibration object supplied.
        calibration: Calibration for this page.  Metred output (width_m /
            height_m) is produced ONLY when the calibration is a proven,
            dimensional render_pixel calibration (px_per_m is not None).  A
            pdf_point calibration (pt/m only) or any invalid calibration
            still permits non-dimensional geometric observations: the bbox is
            correctly labelled render_pixel while width_m / height_m stay
            None.
        source_filename / source_page / drawing_ref / elevation_side /
            calibration_source: Provenance attached to every candidate.  This
            must come from the CALLER's context (never hard-coded to a
            specific sheet/project).
        dark_threshold: Grayscale value below which a pixel counts as dark
            linework.
        min_side_px: Drop candidates smaller than this side length.
        min_area_px: Drop candidates whose pixel area is below this
            (conservative false-positive control for micro text/hatch).
        drawing_region: Optional [x0, y0, x1, y1] pixel crop restricting
            detection to a drawing region (excludes title block / logos /
            annotation).  Coordinates in the same pixel space as ``image``.

    Returns:
        List of ElevationRectCandidate (geometry observations only).
    """
    try:
        import numpy as np
        import cv2
    except Exception as exc:  # pragma: no cover - env-dependent
        return [_unavailable_candidate(str(exc))]

    arr = np.asarray(image)
    if arr.ndim == 3:
        gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
    else:
        gray = arr
    if gray.dtype != np.uint8:
        gray = np.clip(gray, 0, 255).astype(np.uint8)

    h, w = gray.shape
    if w == 0 or h == 0:
        return [ElevationRectCandidate(
            source_filename=source_filename, source_page=source_page,
            drawing_ref=drawing_ref, elevation_side=elevation_side,
            bbox=(0, 0, 0, 0), centroid=(0.0, 0.0),
            calibration_method=calibration.method,
            calibration_source=calibration_source,
            coord_space=COORD_SPACE_RENDER_PIXEL,
            render_dpi=calibration.render_dpi,
            review_status=STATUS_REJECTED,
            notes=["empty image"])]

    # Restrict detection to a drawing region (exclude title block / logos /
    # annotation).  Detected bboxes are re-expressed in FULL-PAGE pixels by
    # adding back the crop offset, so downstream code stays in one space.
    ox = oy = 0
    if drawing_region is not None and len(drawing_region) >= 4:
        rx0, ry0, rx1, ry1 = (int(drawing_region[0]), int(drawing_region[1]),
                              int(drawing_region[2]), int(drawing_region[3]))
        rx0, ry0 = max(0, min(rx0, w - 1)), max(0, min(ry0, h - 1))
        rx1, ry1 = max(rx0 + 1, min(rx1, w)), max(ry0 + 1, min(ry1, h))
        ox, oy = rx0, ry0
        gray = gray[ry0:ry1, rx0:rx1]
        h, w = gray.shape
        if w == 0 or h == 0:
            return [_unavailable_candidate("empty drawing region")]

    dimensional = calibration.is_dimensional() and calibration.px_per_m is not None
    # Hard coordinate discipline: metred output only when the calibration is
    # genuinely in the SAME render-pixel space as the supplied image.  A
    # pdf_point calibration (pt/m only, px_per_m is None) must NOT be applied
    # to a raster image — that would silently describe 28.346 pt/m as px/m.
    px_per_m = calibration.px_per_m if dimensional else 0.0

    # Segment dark linework.
    _, binimg = cv2.threshold(gray, dark_threshold, 255, cv2.THRESH_BINARY_INV)

    # Slight dilation to connect thin frame segments into closed outlines.
    kernel3 = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    binimg = cv2.dilate(binimg, kernel3, iterations=1)

    contours, _ = cv2.findContours(binimg, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    candidates: List[ElevationRectCandidate] = []
    for cnt in contours:
        x, y, cw, ch = cv2.boundingRect(cnt)
        if cw < min_side_px or ch < min_side_px:
            continue
        if min_area_px > 0 and cw * ch < min_area_px:
            continue
        if not _is_rectangular(float(cv2.contourArea(cnt)), float(cw * ch)):
            continue

        # Re-express in full-page pixel coordinates (crop offset added back).
        fx, fy = x + ox, y + oy
        bbox = (int(fx), int(fy), int(fx + cw), int(fy + ch))
        centroid = (float(fx + cw / 2.0), float(fy + ch / 2.0))

        width_m = None
        height_m = None
        if dimensional:
            width_m = round(cw / px_per_m, 4)
            height_m = round(ch / px_per_m, 4)

        cand = ElevationRectCandidate(
            source_filename=source_filename,
            source_page=source_page,
            drawing_ref=drawing_ref,
            elevation_side=elevation_side,
            bbox=bbox,
            centroid=centroid,
            calibration_method=calibration.method,
            calibration_source=calibration_source,
            coord_space=COORD_SPACE_RENDER_PIXEL,
            render_dpi=calibration.render_dpi,
            width_m=width_m,
            height_m=height_m,
            dimension_basis="unknown",
            geometry_confidence=round(min(0.5 + 0.2, 0.75), 3),
            extraction_method="raster_rect",
            review_status=STATUS_REVIEW,
            notes=["geometric rectangle only; generic basis=unknown"],
        )
        candidates.append(cand)

    return candidates


def _unavailable_candidate(msg: str) -> ElevationRectCandidate:
    return ElevationRectCandidate(
        source_filename="", source_page=0, drawing_ref="", elevation_side="",
        bbox=(0, 0, 0, 0), centroid=(0.0, 0.0), calibration_method="",
        calibration_source="", coord_space=COORD_SPACE_RENDER_PIXEL,
        review_status=STATUS_REJECTED,
        notes=[f"raster backend unavailable: {msg}"],
    )


# ---------------------------------------------------------------------------
# Size geofence for opening plausibility (metres)
# ---------------------------------------------------------------------------
def opening_sized(width_m: Optional[float], height_m: Optional[float]) -> bool:
    """True only when measured dims fall within opening-size bands.

    Returns False if either dimension is None (non-dimensional page → cannot
    confirm opening size → NOT treated as an opening-sized observation).
    """
    if width_m is None or height_m is None:
        return False
    if not (_OPENING_MIN_WIDTH_M <= width_m <= _OPENING_MAX_WIDTH_M):
        return False
    if not (_OPENING_MIN_HEIGHT_M <= height_m <= _OPENING_MAX_HEIGHT_M):
        return False
    return True
