"""PlanReader v1.7.7 elevation calibration — measured, provenance-carrying scale.

Phase 2A (extraction + benchmark only — NOT wired into production B4/B5).

Purpose
-------
Produce a trustworthy pixels-per-metre calibration for an elevation page
from REAL drawing evidence (a graphic scale bar), never from an assumed
96-DPI heuristic.

Safety contract
---------------
  - Calibration is only authoritative when it can be MEASURED from drawing
    geometry and its provenance recorded.
  - If calibration cannot be proven, the page remains NON-DIMENSIONAL and
    FAILS CLOSED (valid=False, px_per_m=0.0).
  - Calibration does NOT by itself enable any deduction (B5 remains the sole
    deduct authority and is not touched here).
  - The coordinate space (rendered-pixel vs PDF-point) must be explicit and
    carried alongside the calibration; the two must never be mixed.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

VERSION = "1.7.7"

# ---------------------------------------------------------------------------
# Coordinate-space identifiers
# ---------------------------------------------------------------------------
COORD_SPACE_PDF_POINT = "pdf_point"        # 1 pt = 1/72 inch
COORD_SPACE_RENDER_PIXEL = "render_pixel"  # px at a documented render DPI
COORD_SPACES = (COORD_SPACE_PDF_POINT, COORD_SPACE_RENDER_PIXEL)

# Health / confidence thresholds
_MIN_METRE_SPAN_M = 1.0      # a credible scale bar must span at least 1 m
_MIN_DIVISION_COUNT = 2      # at least two labelled divisions required
_MAX_SPACING_REL_DIFF = 0.05  # 5% max relative deviation between measured
                               # division spacings before the bar is suspect


@dataclass(frozen=True)
class Calibration:
    """A measured, provenance-carrying elevation calibration.

    Attributes:
        px_per_m: Measured pixels-per-metre in the page's coordinate space.
            Zero when calibration could not be proven (fail-closed).
        valid: True only when measurement is trustworthy AND coordinate
            space is explicit.
        method: How the calibration was derived (e.g. "graphic_scale_bar").
        source_page: 0-based or 1-based page identifier for the elevation.
        coord_space: One of COORD_SPACE_* — the units of px_per_m.
        render_dpi: Render DPI when coord_space == "render_pixel" else None.
        calibration_geometry: The measured drawing evidence (division
            positions, spans) used to derive the result.
        confidence: 0.0-1.0 estimate of calibration trustworthiness.
        review_status: "accepted" | "review" | "rejected".
        notes: Human-readable provenance / reasoning.
    """
    px_per_m: float
    coord_space: str
    valid: bool
    method: str = ""
    source_page: Optional[int] = None
    render_dpi: Optional[float] = None
    calibration_geometry: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    review_status: str = "rejected"
    notes: List[str] = field(default_factory=list)

    def is_dimensional(self) -> bool:
        """True only when a proven, valid calibration exists."""
        return self.valid and self.px_per_m > 0.0

    def to_meters(self, px: float) -> Optional[float]:
        """Convert a length in the calibration's coordinate space to metres.

        Returns None (not a number) when the page is not dimensional —
        callers must treat None as NON-DIMENSIONAL and fail closed.
        """
        if not self.is_dimensional():
            return None
        return px / self.px_per_m

    def as_dict(self) -> Dict[str, Any]:
        """Serialize for provenance / fixtures / pipelines."""
        return {
            "version": VERSION,
            "px_per_m": self.px_per_m,
            "valid": self.valid,
            "is_dimensional": self.is_dimensional(),
            "method": self.method,
            "source_page": self.source_page,
            "coord_space": self.coord_space,
            "render_dpi": self.render_dpi,
            "calibration_geometry": self.calibration_geometry,
            "confidence": self.confidence,
            "review_status": self.review_status,
            "notes": list(self.notes),
        }


def _fail_closed(reason: str, coord_space: str, source_page: Optional[int] = None,
                 render_dpi: Optional[float] = None) -> Calibration:
    """Return a non-dimensional, rejected calibration (fail-closed)."""
    return Calibration(
        px_per_m=0.0,
        coord_space=coord_space,
        valid=False,
        method="none",
        source_page=source_page,
        render_dpi=render_dpi,
        calibration_geometry={},
        confidence=0.0,
        review_status="rejected",
        notes=[reason],
    )


def measured_calibration_from_divisions(
    division_positions: Sequence[float],
    metre_per_division: float,
    *,
    coord_space: str,
    method: str,
    source_page: Optional[int] = None,
    render_dpi: Optional[float] = None,
    labels: Optional[Sequence[str]] = None,
    division_scatter_tol: Optional[float] = None,
) -> Calibration:
    """Derive a calibration from measured scale-bar division positions.

    This is the authoritative entry point for measured calibration.  The
    caller locates the graphic scale bar divisions (their positions in the
    page's coordinate space) and states how many metres each division
    represents (from the bar's own labels / annotation).  The result carries
    the measured px_per_m and full provenance.

    Args:
        division_positions: Positions (monotonic) of scale-bar division
            boundaries, e.g. the centres of labels 0..10 on a 10 m bar.
        metre_per_division: Real-world metres each full division represents.
        coord_space: COORD_SPACE_PDF_POINT or COORD_SPACE_RENDER_PIXEL.
        method: Provenance, e.g. "graphic_scale_bar".
        source_page: Page identifier of the elevation sheet.
        render_dpi: Required when coord_space == COORD_SPACE_RENDER_PIXEL.
        labels: Optional labels attached to each division position (for
            provenance only).
        division_scatter_tol: Relative tolerance (fraction) on division
            spacing consistency; defaults to _MAX_SPACING_REL_DIFF.

    Returns:
        A Calibration.  If the measurement is not provable/consistent, the
        result FAILS CLOSED (valid=False, px_per_m=0.0).
    """
    if coord_space not in COORD_SPACES:
        return _fail_closed(
            f"unknown coordinate space {coord_space!r}", coord_space,
            source_page, render_dpi)

    if coord_space == COORD_SPACE_RENDER_PIXEL and not render_dpi:
        return _fail_closed(
            "render_pixel calibration requires an explicit render_dpi",
            coord_space, source_page, render_dpi)

    if metre_per_division <= 0:
        return _fail_closed(
            "metre_per_division must be > 0", coord_space, source_page,
            render_dpi)

    pos = [float(p) for p in division_positions]
    # Positions must be monotonic increasing (no reordering).
    if any(b <= a for a, b in zip(pos, pos[1:])):
        return _fail_closed(
            "division positions are not strictly increasing",
            coord_space, source_page, render_dpi)

    # Measured division spacings (in coordinate-space units).
    spacings = [pos[i + 1] - pos[i] for i in range(len(pos) - 1)]

    # A trustworthy scale bar needs multiple FULL divisions so its
    # uniformity can be assessed; a single division is too weak evidence.
    if len(spacings) < _MIN_DIVISION_COUNT:
        return _fail_closed(
            f"need at least {_MIN_DIVISION_COUNT} full divisions "
            f"(got {len(spacings)})", coord_space, source_page, render_dpi)
    if any(s <= 0 for s in spacings):
        return _fail_closed(
            "non-positive division spacing", coord_space, source_page,
            render_dpi)

    mean_spacing = sum(spacings) / len(spacings)
    tol = division_scatter_tol if division_scatter_tol is not None \
        else _MAX_SPACING_REL_DIFF

    # Consistency check: the division spacings must agree with each other
    # (a clean graphic scale bar has uniform divisions).  A wildly uneven
    # bar is not trustworthy.
    max_rel_dev = max(abs(s - mean_spacing) / mean_spacing for s in spacings)
    if max_rel_dev > tol:
        return _fail_closed(
            f"scale bar divisions are not uniform "
            f"(max rel deviation {max_rel_dev:.3f} > {tol})",
            coord_space, source_page, render_dpi)

    metre_span = metre_per_division * len(spacings)
    if metre_span < _MIN_METRE_SPAN_M:
        return _fail_closed(
            f"scale bar metre span {metre_span:.2f} m below credible "
            f"minimum {_MIN_METRE_SPAN_M} m",
            coord_space, source_page, render_dpi)

    px_per_m_value = mean_spacing / metre_per_division
    if px_per_m_value <= 0.0 or not math.isfinite(px_per_m_value):
        return _fail_closed(
            "invalid px_per_m derived from measurements",
            coord_space, source_page, render_dpi)

    # Confidence: penalise some residual scatter, reward a valid measurement.
    confidence = max(0.0, 1.0 - max_rel_dev / tol) * 0.9 + 0.05
    confidence = min(confidence, 1.0)

    geometry: Dict[str, Any] = {
        "division_positions": [round(p, 6) for p in pos],
        "division_spacings": [round(s, 6) for s in spacings],
        "mean_division_spacing": round(mean_spacing, 6),
        "max_rel_deviation": round(max_rel_dev, 6),
        "metre_per_division": metre_per_division,
        "metre_span": round(metre_span, 6),
        "division_count": len(spacings),
        "labelled_divisions": list(labels) if labels else [],
    }

    return Calibration(
        px_per_m=round(px_per_m_value, 6),
        coord_space=coord_space,
        valid=True,
        method=method,
        source_page=source_page,
        render_dpi=render_dpi,
        calibration_geometry=geometry,
        confidence=round(confidence, 3),
        review_status="accepted",
        notes=[
            f"measured {len(spacings)} divisions at mean spacing "
            f"{mean_spacing:.3f} {coord_space} units each representing "
            f"{metre_per_division} m → px_per_m={px_per_m_value:.4f}",
        ],
    )


# ---------------------------------------------------------------------------
# PDF-point sample: build the scale-bar probe the sheets use
# ---------------------------------------------------------------------------
# The LAGO CD300x elevation sheets carry a graphic scale bar whose labelled
# divisions 0..10 are spaced evenly.  A sheet-specific probe inserts the
# measured division positions (in the chosen coordinate space) plus the
# nominal metres per division (from the bar's annotation) and defers to
# measured_calibration_from_divisions().

def calibration_from_scale_bar_positions(
    division_positions: Sequence[float],
    metre_per_division: float,
    *,
    coord_space: str,
    source_page: Optional[int] = None,
    render_dpi: Optional[float] = None,
    labels: Optional[Sequence[str]] = None,
) -> Calibration:
    """Calibrate from a graphic scale bar's measured division positions.

    Thin convenience wrapper over measured_calibration_from_divisions()
    with method tag set to "graphic_scale_bar".
    """
    return measured_calibration_from_divisions(
        division_positions,
        metre_per_division,
        coord_space=coord_space,
        method="graphic_scale_bar",
        source_page=source_page,
        render_dpi=render_dpi,
        labels=labels,
    )
