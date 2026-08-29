"""PlanReader v1.7.8 production bridge — Phase 2B elevation evidence wiring.

Connects the reviewed Phase 2A elevation-extraction capability (v1.7.7
``Calibration`` / raster / vector extractors) into the opening-evidence
production path as a controlled, fail-closed CORROBORATION seam.

This module NEVER grants deduction or instance authority:
  - Elevation evidence NEVER creates a physical opening instance (B1 controls
    the physical-candidate count; the pipeline enumerates only B1 candidates).
  - Elevation evidence NEVER sets ``deduct=True`` (B5 is the sole deduction
    authority and requires a ``rough_opening`` dimension basis; generic
    elevation observations always keep ``dimension_basis="unknown"``).
  - A dimensional elevation observation is produced ONLY when the page's
    v1.7.7 Calibration is PROVEN valid / dimensional in the candidate's OWN
    coordinate space.  Invalid / non-dimensional / coordinate-space mismatched
    evidence FAILS CLOSED — metres are never fabricated and spaces are never
    mixed.
  - Every candidate carries a structured diagnostic entry recording WHY it
    was accepted or rejected, so a consuming reviewer can audit the seam.

The qualified ``ElevationOpening`` objects returned here are exactly what the
production entry points (``run_p5_native_payload`` /
``analyse_stored_page_openings``) thread into
``run_opening_pipeline(elevation_openings=...)``, whose reviewed B3 stage
(correlate_elevation_to_plan) performs the real correlation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pb_elevation_calibration_v177 import (
    Calibration,
    COORD_SPACE_PDF_POINT,
    COORD_SPACE_RENDER_PIXEL,
)
from pb_elevation_raster_extract_v177 import (
    ElevationRectCandidate,
    detect_raster_rect_candidates,
    opening_sized,
)
from pb_elevation_vector_extract_v177 import (
    VectorRectCandidate,
    recover_vector_rects,
)
from pb_elevation_evidence_v172 import (
    ElevationOpening,
    detect_elevation_openings,
    _correlation_score,
    _enrich_from_elevation,
    _find_unique_best_pairs,
)

VERSION = "1.7.8"

# Diagnostic statuses
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"

# Rejection reasons (structured, so a reviewer can granulate a summary)
REASON_CALIBRATION_INVALID = "calibration_invalid"            # no proven dimensional calibration
REASON_COORDINATE_MISMATCH = "coordinate_space_mismatch"      # cand space != calibration space
REASON_NON_DIMENSIONAL = "non_dimensional_candidate"          # extractor never produced metres
REASON_NOT_OPENING_SIZED = "not_opening_sized"                # outside the opening-size geofence
REASON_BACKEND_UNAVAILABLE = "backend_unavailable"            # cv2/numpy not available
REASON_NO_EVIDENCE = "no_elevation_evidence_supplied"

# ---------------------------------------------------------------------------
# Production B3 correlation identity (C2)
# ---------------------------------------------------------------------------
# Generic "same elevation side + compatible width" is NOT sufficient instance
# identity for PRODUCTION correlation — repeated same-width windows/doors on
# one elevation make it too weak.  Production matching requires a stronger
# instance-specific anchor.  These constants describe the anchor rules.
PROD_POSITION_TOLERANCE_M = 0.25   # validated position/location correspondence tolerance


@dataclass(frozen=True)
class ElevationBridgeResult:
    """Fail-closed bridge output: qualified openings + full diagnostics.

    ``openings`` are the dimensional, coordinate-compatible, opening-sized
    observations that may be threaded into B3 correlation.  ``diagnostics``
    record the accept/reject decision and provenance for every candidate so a
    reviewer can see WHY each candidate did or did not qualify (and that
    qualified candidates remain deduction review-only).
    """
    openings: List[ElevationOpening]
    diagnostics: List[Dict[str, Any]]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "openings": [asdict(o) for o in self.openings],
            "diagnostics": list(self.diagnostics),
        }


# ---------------------------------------------------------------------------
# Calibration state (explicit, serialisable) and per-candidate diagnostics
# ---------------------------------------------------------------------------
def calibration_state(calibration: Optional[Calibration]) -> Dict[str, Any]:
    """Describe a calibration for diagnostics — never substitutes for it."""
    if calibration is None:
        return {
            "valid": False,
            "dimensional": False,
            "reason": "no_calibration",
            "coord_space": "",
            "units_per_m": 0.0,
            "pt_per_m": None,
            "px_per_m": None,
            "method": "",
            "confidence": 0.0,
            "review_status": "rejected",
            "render_dpi": None,
        }
    return {
        "valid": bool(calibration.valid),
        "dimensional": calibration.is_dimensional(),
        "coord_space": calibration.coord_space,
        "units_per_m": calibration.units_per_m,
        "pt_per_m": calibration.pt_per_m,
        "px_per_m": calibration.px_per_m,
        "method": calibration.method,
        "confidence": calibration.confidence,
        "review_status": calibration.review_status,
        "render_dpi": calibration.render_dpi,
        "source_page": calibration.source_page,
        "notes": list(calibration.notes),
    }


def _candidate_diag(
    index: int,
    cand: Any,
    status: str,
    reason: str,
    notes: Optional[Sequence[str]] = None,
    *,
    drawing_title: str = "",
    level: Optional[str] = None,
    calibration: Optional[Calibration] = None,
    source_filename: str = "",
) -> Dict[str, Any]:
    """Per-candidate diagnostic carrying full reviewer traceability (C3).

    Traces an accepted/rejected elevation candidate back to: original source
    filename, source page, drawing ref/title, coordinate space, calibration
    source+state, elevation side, and level (when known).
    """
    diag: Dict[str, Any] = {
        "kind": "elevation_candidate",
        "candidate_index": index,
        "source": cand.extraction_method,
        "status": status,
        "reason": reason,
        "source_filename": str(
            getattr(cand, "source_filename", None) or source_filename
        ),
        "source_page": getattr(cand, "source_page", None),
        "coord_space": cand.coord_space,
        "bbox": list(cand.bbox),
        "width_m": cand.width_m,
        "height_m": cand.height_m,
        "review_status": cand.review_status,
        "drawing_ref": cand.drawing_ref,
        "drawing_title": drawing_title,
        "elevation_side": cand.elevation_side,
        "level": level,
        "extraction_method": cand.extraction_method,
    }
    if calibration is not None:
        diag["calibration"] = calibration_state(calibration)
    label = getattr(cand, "label", "")
    if label:
        diag["label"] = label
    if notes:
        diag["notes"] = list(notes)
    return diag


def _is_backend_unavailable(cand: Any) -> bool:
    return (
        cand.review_status == "rejected"
        and tuple(cand.bbox) == (0, 0, 0, 0)
        and any("backend unavailable" in n for n in cand.notes)
    )


def _reject_reason(
    cand: Any,
    calib: Optional[Calibration],
    calib_dimensional: bool,
    calib_space: str,
) -> str:
    if _is_backend_unavailable(cand):
        return REASON_BACKEND_UNAVAILABLE
    if calib is None or not calib_dimensional:
        return REASON_CALIBRATION_INVALID
    if cand.coord_space != calib_space:
        return REASON_COORDINATE_MISMATCH
    if cand.width_m is None or cand.height_m is None:
        return REASON_NON_DIMENSIONAL
    return REASON_NOT_OPENING_SIZED


def _evidence_summary(
    *,
    calibration: Optional[Calibration],
    elevation_page_no: int,
    elevation_side: str,
    drawing_ref: str,
    drawing_title: str,
    path: str,
    source_filename: str = "",
    total: int,
    qualified: int,
    rejected: int,
) -> Dict[str, Any]:
    if total == 0:
        status = "review" if not qualified else "accepted"
    elif qualified and rejected == 0:
        status = "accepted"
    elif qualified:
        status = "review"   # some qualified + some rejected
    else:
        status = "rejected"
    return {
        "kind": "elevation_evidence_summary",
        "module": VERSION,
        "status": status,
        "path": path,
        "source_filename": source_filename,
        "elevation_page_no": elevation_page_no,
        "elevation_side": elevation_side,
        "drawing_ref": drawing_ref,
        "drawing_title": drawing_title,
        "candidates_total": total,
        "qualified_count": qualified,
        "rejected_count": rejected,
        "dimension_basis": "unknown",
        "deduction_authority": False,   # elevation never grants deduction authority
        "instance_creation": False,     # elevation never creates instances
        "calibration": calibration_state(calibration),
    }


# ---------------------------------------------------------------------------
# Candidate mapping — v177 candidates -> detect_elevation_openings rect dicts
# ---------------------------------------------------------------------------
def map_raster_candidates(
    candidates: Sequence[ElevationRectCandidate],
    *,
    wall_ref: str = "",
    calibration_source: str = "",
) -> List[Dict[str, Any]]:
    """Map v177 raster candidates onto ``detect_elevation_openings`` rects.

    Coordinate-space identity is PRESERVED: every rect is declared
    ``render_pixel`` — the space the raster extractor always measures in — so
    dimensional output is only possible against a proven ``render_pixel``
    calibration.  A ``pdf_point`` calibration can never convert these.
    """
    rects: List[Dict[str, Any]] = []
    for cand in candidates or []:
        rects.append(_raster_rect_dict(cand, calibration_source, wall_ref))
    return rects


def _raster_rect_dict(
    cand: ElevationRectCandidate,
    calibration_source: str,
    wall_ref: str,
) -> Dict[str, Any]:
    """Map a raster candidate onto a ``detect_elevation_openings`` rect dict.

    Level provenance rule (C1): the ``"level"`` key is emitted ONLY when the
    candidate carries an OBJECTIVELY-derived level band.  When there is no
    objective level (``cand.level_band`` is None) the key is OMITTED entirely
    so downstream ``rect.get("level", level)`` falls back to the caller-supplied
    page/elevation level instead of being shadowed by ``None``.  ``None`` is
    never emitted as an explicit level that erases a real caller level.
    """
    rect: Dict[str, Any] = {
        "bbox": list(cand.bbox),
        "coord_space": cand.coord_space,
        "render_dpi": cand.render_dpi,
        "confidence": cand.geometry_confidence,
        "wall_ref": wall_ref,
        "drawing_ref": cand.drawing_ref,
        "extraction_method": cand.extraction_method,
        "calibration_source": cand.calibration_source or calibration_source,
        "review_status": cand.review_status,
    }
    if cand.level_band is not None:
        rect["level"] = cand.level_band
    return rect


def map_vector_candidates(
    candidates: Sequence[VectorRectCandidate],
    *,
    wall_ref: str = "",
) -> List[Dict[str, Any]]:
    """Map v177 vector candidates onto ``detect_elevation_openings`` rects.

    Coordinate-space identity is PRESERVED: every rect is declared
    ``pdf_point`` — the space ``recover_vector_rects`` works in — so
    dimensional output is only possible against a proven ``pdf_point``
    calibration.  A ``render_pixel`` calibration can never convert these.
    """
    rects: List[Dict[str, Any]] = []
    for cand in candidates or []:
        rects.append(_vector_rect_dict(cand, wall_ref))
    return rects


def _vector_rect_dict(
    cand: VectorRectCandidate,
    wall_ref: str,
) -> Dict[str, Any]:
    """Map a vector candidate onto a ``detect_elevation_openings`` rect dict.

    Level provenance rule (C1): vector candidates carry no objective level band,
    so the ``"level"`` key is OMITTED (never emitted as ``None``).  Downstream
    ``rect.get("level", level)`` therefore falls back to the caller-supplied
    page/elevation level, preserving the three-way distinction:
    objectively-derived candidate level (if present) > caller page/elevation
    level > unknown/None — with ``None`` never shadowing a real caller level.
    """
    return {
        "bbox": list(cand.bbox),
        "coord_space": cand.coord_space,
        "confidence": cand.geometry_confidence,
        "wall_ref": wall_ref,
        "drawing_ref": cand.drawing_ref,
        "extraction_method": cand.extraction_method,
        "review_status": cand.review_status,
    }


def _is_qualified(cand: Any, calib: Optional[Calibration]) -> bool:
    """True only when the candidate can safely receive dimensional metres.

    Requires a proven dimensional calibration in the candidate's OWN
    coordinate space, metres already produced by the extractor, and an
    opening-sized result.
    """
    if calib is None or not calib.is_dimensional():
        return False
    if cand.coord_space != calib.coord_space:
        return False
    if cand.width_m is None or cand.height_m is None:
        return False
    return opening_sized(cand.width_m, cand.height_m)


def _word_dicts(words: Sequence[Any]) -> List[Dict[str, Any]]:
    """Normalise elevation words to the native ``{"text", "bbox"}`` dict format.

    ``detect_elevation_openings`` labels rectangles by scanning word records
    (``_word_position`` accepts the v130 native ``{"text", "bbox"}`` format and
    the legacy numeric-key format).  The plan-vector vocabulary uses ``TextWord``
    dataclasses (``text, x0, y0, x1, y1, page_no``) that follow neither format,
    so they are converted to the native bbox format here.  Plain dicts are
    passed through untouched so v1.7.2 keeps decoding them, and any record that
    cannot be normalised is dropped (labels are optional corroboration and must
    never crash the bridge).
    """
    result: List[Dict[str, Any]] = []
    for w in words or []:
        if isinstance(w, dict):
            result.append(dict(w))
            continue
        try:
            x0 = float(w.x0)
            y0 = float(w.y0)
            x1 = float(w.x1)
            y1 = float(w.y1)
        except (AttributeError, TypeError, ValueError):
            try:
                result.append(dict(w))
            except (TypeError, ValueError):
                pass  # unrecognised record: drop it, never crash the seam
            continue
        result.append({
            "text": str(getattr(w, "text", "") or ""),
            "bbox": [x0, y0, x1, y1],
        })
    return result


# ---------------------------------------------------------------------------
# Raster path
# ---------------------------------------------------------------------------
def raster_openings_from_candidates(
    candidates: Sequence[ElevationRectCandidate],
    calibration: Optional[Calibration],
    *,
    elevation_page_no: int,
    elevation_side: str,
    words: Sequence[Dict[str, Any]] = (),
    source_filename: str = "",
    source_page: int = 0,
    drawing_ref: str = "",
    drawing_title: str = "",
    source_page_id: Optional[int] = None,
    level: Optional[str] = None,
    wall_ref: str = "",
    calibration_source: str = "",
) -> ElevationBridgeResult:
    """Bridge pre-extracted v177 raster candidates to qualified openings.

    Fail-closed rules (every candidate gets an explicit diagnostic):
      - No / invalid / non-dimensional calibration -> rejected with
        ``calibration_invalid``; NO dimensional openings are produced.
      - Coordinator-space mismatch (render_pixel candidate against a
        pdf_point calibration) -> rejected with
        ``coordinate_space_mismatch``; spaces are never mixed.
      - Extractors may report non-dimensional candidates (``width_m``/``height_m``
        None) -> rejected with ``non_dimensional_candidate``.
      - Dimensional but outside the opening-size geofence -> rejected with
        ``not_opening_sized`` (never inferred).
    """
    calib = calibration
    calib_dimensional = calib is not None and calib.is_dimensional()
    calib_space = calib.coord_space if calib is not None else ""

    diagnostics: List[Dict[str, Any]] = []
    rects: List[Dict[str, Any]] = []
    rejected = 0

    for i, cand in enumerate(candidates or []):
        if _is_backend_unavailable(cand):
            diagnostics.append(_candidate_diag(
                i, cand, STATUS_REJECTED, REASON_BACKEND_UNAVAILABLE, cand.notes,
                drawing_title=drawing_title, level=level,
                calibration=calib, source_filename=source_filename))
            rejected += 1
            continue
        if _is_qualified(cand, calib):
            rects.append(_raster_rect_dict(cand, calibration_source, wall_ref))
            diagnostics.append(_candidate_diag(
                i, cand, STATUS_ACCEPTED, "",
                notes=[
                    "qualified geometric observation; dimension_basis=unknown; "
                    "deduction review-only (B5 requires rough_opening basis)",
                ],
                drawing_title=drawing_title, level=level,
                calibration=calib, source_filename=source_filename))
        else:
            rejected += 1
            diagnostics.append(_candidate_diag(
                i, cand, STATUS_REJECTED,
                _reject_reason(cand, calib, calib_dimensional, calib_space),
                drawing_title=drawing_title, level=level,
                calibration=calib, source_filename=source_filename))

    openings: List[ElevationOpening] = []
    if rects and calib_dimensional:
        openings = detect_elevation_openings(
            elevation_page_no=elevation_page_no,
            elevation_side=elevation_side,
            rects=rects,
            words=_word_dicts(words),
            units_per_m=calib.units_per_m,
            coord_space=calib.coord_space,
            render_dpi=calib.render_dpi,
            source_page_id=source_page_id,
            source_page_no=elevation_page_no or (source_page or None),
            drawing_ref=drawing_ref,
            drawing_title=drawing_title,
            level=level,
            wall_ref=wall_ref,
            calibration=calib.as_dict(),
        )

    diagnostics.append(_evidence_summary(
        calibration=calib,
        elevation_page_no=elevation_page_no,
        elevation_side=elevation_side,
        drawing_ref=drawing_ref,
        drawing_title=drawing_title,
        path="raster",
        source_filename=source_filename,
        total=len(candidates or []),
        qualified=len(openings),
        rejected=rejected,
    ))

    return ElevationBridgeResult(openings=list(openings), diagnostics=diagnostics)


def extract_raster_elevation_openings(
    image: Any,
    calibration: Optional[Calibration],
    *,
    elevation_page_no: int,
    elevation_side: str,
    words: Sequence[Dict[str, Any]] = (),
    source_filename: str = "",
    source_page: int = 0,
    drawing_ref: str = "",
    drawing_title: str = "",
    source_page_id: Optional[int] = None,
    level: Optional[str] = None,
    wall_ref: str = "",
    drawing_region: Optional[Sequence[int]] = None,
    dark_threshold: int = 160,
    min_side_px: int = 8,
    min_area_px: int = 0,
    calibration_source: str = "",
) -> ElevationBridgeResult:
    """Run the v1.7.7 raster extractor, then bridge (fail closed).

    The raster extractor is always render_pixel; dimensional openings are only
    possible against a proven render_pixel calibration.  A pdf_point or invalid
    calibration produces NON-dimensional geometric observations at most (and
    they are dropped from the dimensional result with explicit diagnostics).
    """
    candidates = detect_raster_rect_candidates(
        image,
        calibration if calibration is not None else Calibration(
            units_per_m=0.0,
            coord_space=COORD_SPACE_RENDER_PIXEL,
            valid=False,
            method="none",
            notes=["no calibration supplied"],
        ),
        source_filename=source_filename,
        source_page=source_page,
        drawing_ref=drawing_ref,
        elevation_side=elevation_side,
        dark_threshold=dark_threshold,
        min_side_px=min_side_px,
        min_area_px=min_area_px,
        drawing_region=drawing_region,
        calibration_source=calibration_source,
    )
    return raster_openings_from_candidates(
        candidates,
        calibration,
        elevation_page_no=elevation_page_no,
        elevation_side=elevation_side,
        words=words,
        source_filename=source_filename,
        source_page=source_page,
        drawing_ref=drawing_ref,
        drawing_title=drawing_title,
        source_page_id=source_page_id,
        level=level,
        wall_ref=wall_ref,
        calibration_source=calibration_source,
    )


# ---------------------------------------------------------------------------
# Vector path (PDF-point secondary / navigation cross-check)
# ---------------------------------------------------------------------------
def vector_openings_from_candidates(
    candidates: Sequence[VectorRectCandidate],
    calibration: Optional[Calibration],
    *,
    elevation_page_no: int,
    elevation_side: str,
    words: Sequence[Dict[str, Any]] = (),
    source_filename: str = "",
    source_page: int = 0,
    drawing_ref: str = "",
    drawing_title: str = "",
    source_page_id: Optional[int] = None,
    level: Optional[str] = None,
    wall_ref: str = "",
) -> ElevationBridgeResult:
    """Bridge pre-extracted v177 vector candidates to qualified openings.

    Same fail-closed discipline as the raster path, but in ``pdf_point``
    space: dimensional openings are only possible against a proven
    ``pdf_point`` calibration.  A render_pixel calibration FAILS CLOSED
    (coordinate_space_mismatch) — 28.346 pt/m is never described as px/m.
    """
    calib = calibration
    calib_dimensional = calib is not None and calib.is_dimensional()
    calib_space = calib.coord_space if calib is not None else ""

    diagnostics: List[Dict[str, Any]] = []
    rects: List[Dict[str, Any]] = []
    rejected = 0

    for i, cand in enumerate(candidates or []):
        if _is_backend_unavailable(cand):
            diagnostics.append(_candidate_diag(
                i, cand, STATUS_REJECTED, REASON_BACKEND_UNAVAILABLE, cand.notes,
                drawing_title=drawing_title, level=level,
                calibration=calib, source_filename=source_filename))
            rejected += 1
            continue
        if _is_qualified(cand, calib):
            rects.append(_vector_rect_dict(cand, wall_ref))
            diagnostics.append(_candidate_diag(
                i, cand, STATUS_ACCEPTED, "",
                notes=[
                    "qualified closed-rectangle observation; dimension_basis=unknown; "
                    "deduction review-only (B5 requires rough_opening basis)",
                ],
                drawing_title=drawing_title, level=level,
                calibration=calib, source_filename=source_filename))
        else:
            rejected += 1
            diagnostics.append(_candidate_diag(
                i, cand, STATUS_REJECTED,
                _reject_reason(cand, calib, calib_dimensional, calib_space),
                drawing_title=drawing_title, level=level,
                calibration=calib, source_filename=source_filename))

    openings: List[ElevationOpening] = []
    if rects and calib_dimensional:
        openings = detect_elevation_openings(
            elevation_page_no=elevation_page_no,
            elevation_side=elevation_side,
            rects=rects,
            words=_word_dicts(words),
            units_per_m=calib.units_per_m,
            coord_space=calib.coord_space,
            render_dpi=calib.render_dpi,
            source_page_id=source_page_id,
            source_page_no=elevation_page_no or (source_page or None),
            drawing_ref=drawing_ref,
            drawing_title=drawing_title,
            level=level,
            wall_ref=wall_ref,
            calibration=calib.as_dict(),
        )

    diagnostics.append(_evidence_summary(
        calibration=calib,
        elevation_page_no=elevation_page_no,
        elevation_side=elevation_side,
        drawing_ref=drawing_ref,
        drawing_title=drawing_title,
        path="vector",
        source_filename=source_filename,
        total=len(candidates or []),
        qualified=len(openings),
        rejected=rejected,
    ))

    return ElevationBridgeResult(openings=list(openings), diagnostics=diagnostics)


def extract_vector_elevation_openings(
    segments: Sequence[Dict[str, Any]],
    calibration: Optional[Calibration],
    *,
    elevation_page_no: int,
    elevation_side: str,
    words: Sequence[Dict[str, Any]] = (),
    source_filename: str = "",
    source_page: int = 0,
    drawing_ref: str = "",
    drawing_title: str = "",
    source_page_id: Optional[int] = None,
    level: Optional[str] = None,
    wall_ref: str = "",
    angle_tol_deg: float = 5.0,
    min_side_pt: float = 4.0,
    max_cells: int = 20000,
) -> ElevationBridgeResult:
    """Run the v1.7.7 vector sidecar, then bridge (fail closed)."""
    candidates = recover_vector_rects(
        segments,
        calibration if calibration is not None else Calibration(
            units_per_m=0.0,
            coord_space=COORD_SPACE_PDF_POINT,
            valid=False,
            method="none",
            notes=["no calibration supplied"],
        ),
        source_filename=source_filename,
        source_page=source_page,
        drawing_ref=drawing_ref,
        elevation_side=elevation_side,
        angle_tol_deg=angle_tol_deg,
        min_side_pt=min_side_pt,
        max_cells=max_cells,
    )
    return vector_openings_from_candidates(
        candidates,
        calibration,
        elevation_page_no=elevation_page_no,
        elevation_side=elevation_side,
        words=words,
        source_filename=source_filename,
        source_page=source_page,
        drawing_ref=drawing_ref,
        drawing_title=drawing_title,
        source_page_id=source_page_id,
        level=level,
        wall_ref=wall_ref,
    )


# ---------------------------------------------------------------------------
# Combined production entry
# ---------------------------------------------------------------------------
def produce_elevation_openings(
    calibration: Optional[Calibration],
    *,
    elevation_page_no: int,
    elevation_side: str,
    raster_image: Any = None,
    segments: Optional[Sequence[Dict[str, Any]]] = None,
    words: Sequence[Dict[str, Any]] = (),
    source_filename: str = "",
    source_page: int = 0,
    drawing_ref: str = "",
    drawing_title: str = "",
    source_page_id: Optional[int] = None,
    level: Optional[str] = None,
    wall_ref: str = "",
    drawing_region: Optional[Sequence[int]] = None,
    dark_threshold: int = 160,
    min_side_px: int = 8,
    min_area_px: int = 0,
    calibration_source: str = "",
    angle_tol_deg: float = 5.0,
    min_side_pt: float = 4.0,
    max_cells: int = 20000,
) -> ElevationBridgeResult:
    """Full controlled production seam over whatever evidence is supplied.

    Runs each supplied evidence path (raster image and/or vector segments),
    merges their qualified openings and diagnostics.  With NO raster image and
    NO segments the bridge FAILS CLOSED with a ``no_elevation_evidence_supplied``
    diagnostic and zero openings — the production default (no elevation
    evidence) is an exact no-op.

    When BOTH paths are supplied for the same sheet, candidate lists are NOT
    cross-space deduplicated on purpose: a raster (render_pixel) and vector
    (pdf_point) observation of the same physical opening stay distinct and the
    reviewed B3 correlation resolves them (ties fail closed to review).
    """
    openings: List[ElevationOpening] = []
    diagnostics: List[Dict[str, Any]] = []
    ran = False

    if raster_image is not None:
        ran = True
        r = extract_raster_elevation_openings(
            raster_image,
            calibration,
            elevation_page_no=elevation_page_no,
            elevation_side=elevation_side,
            words=words,
            source_filename=source_filename,
            source_page=source_page,
            drawing_ref=drawing_ref,
            drawing_title=drawing_title,
            source_page_id=source_page_id,
            level=level,
            wall_ref=wall_ref,
            drawing_region=drawing_region,
            dark_threshold=dark_threshold,
            min_side_px=min_side_px,
            min_area_px=min_area_px,
            calibration_source=calibration_source,
        )
        openings.extend(r.openings)
        diagnostics.extend(r.diagnostics)

    if segments:
        ran = True
        v = extract_vector_elevation_openings(
            segments,
            calibration,
            elevation_page_no=elevation_page_no,
            elevation_side=elevation_side,
            words=words,
            source_filename=source_filename,
            source_page=source_page,
            drawing_ref=drawing_ref,
            drawing_title=drawing_title,
            source_page_id=source_page_id,
            level=level,
            wall_ref=wall_ref,
            angle_tol_deg=angle_tol_deg,
            min_side_pt=min_side_pt,
            max_cells=max_cells,
        )
        openings.extend(v.openings)
        diagnostics.extend(v.diagnostics)

    if not ran:
        diagnostics.append({
            "kind": "elevation_evidence_summary",
            "module": VERSION,
            "status": "none",
            "path": "none",
            "reason": REASON_NO_EVIDENCE,
            "source_filename": source_filename,
            "elevation_page_no": elevation_page_no,
            "elevation_side": elevation_side,
            "drawing_ref": drawing_ref,
            "drawing_title": drawing_title,
            "candidates_total": 0,
            "qualified_count": 0,
            "rejected_count": 0,
            "dimension_basis": "unknown",
            "deduction_authority": False,
            "instance_creation": False,
            "calibration": calibration_state(calibration),
        })

    return ElevationBridgeResult(openings=openings, diagnostics=diagnostics)


# ---------------------------------------------------------------------------
# Production B3 correlation (C2): strict instance identity
# ---------------------------------------------------------------------------
def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        if result != result or abs(result) == float("inf"):
            return default
        return result
    except (TypeError, ValueError):
        return default


def _exact_mark_anchor(inst: Any, elev: ElevationOpening) -> bool:
    """Strong anchor (a): an exact compatible opening mark on both sides.

    Both the plan instance and the elevation opening must carry a non-blank
    mark and agree exactly (case-insensitive).  This is instance-specific:
    an 820 mm door with mark ``D01`` is identified by its mark, not by its
    width.
    """
    inst_mark = str(getattr(inst, "type_mark", "") or "").strip()
    elev_mark = str(getattr(elev, "label", "") or "").strip()
    return bool(inst_mark and elev_mark and inst_mark.upper() == elev_mark.upper())


def _validated_position_anchor(inst: Any, elev: ElevationOpening) -> bool:
    """Strong anchor (b): validated position/location correspondence.

    A production elevation opening may carry an explicit, independently
    validated wall position in metres (``wall_position_m``) that must agree
    with the plan instance's ``position_along_wall_m`` within tolerance.
    Position is instance-specific (two same-width windows on one elevation sit
    at different stations along the wall).
    """
    inst_pos = _num(getattr(inst, "position_along_wall_m", None), float("nan"))
    elev_pos = _num(getattr(elev, "wall_position_m", None), float("nan"))
    if inst_pos != inst_pos or elev_pos != elev_pos:
        return False  # either position unknown -> no validated correspondence
    return abs(inst_pos - elev_pos) <= PROD_POSITION_TOLERANCE_M


def _proven_unique_anchor(elev: ElevationOpening) -> bool:
    """Strong anchor (c): another independently-proven unique identity signal.

    An elevation opening may declare an explicit identity anchor (e.g. a
    proven unique opening reference / owner-verified correspondence) via the
    ``identity_anchor`` attribute.  A truthy, non-blank value counts as a
    strong, independently-proven unique identity signal; side/width do NOT.
    """
    anchor = getattr(elev, "identity_anchor", None)
    if anchor is None:
        return False
    if isinstance(anchor, (dict, list)):
        return bool(anchor)
    return bool(str(anchor).strip())


def _has_production_identity_anchor(inst: Any, elev: ElevationOpening) -> bool:
    """Return True when a (plan, elevation) pair shares a strong anchor.

    Production matching REQUIRES at least one of:
      (a) an exact compatible opening mark, OR
      (b) validated position/location correspondence, OR
      (c) another independently-proven unique identity signal.

    Side and width SUPPORT a match but never independently identify a physical
    opening, so a pair that only agrees on side+width has NO strong anchor and
    may not correlate (fail-closed ambiguity → review).
    """
    return (
        _exact_mark_anchor(inst, elev)
        or _validated_position_anchor(inst, elev)
        or _proven_unique_anchor(elev)
    )


def _production_correlation_score(inst: Any, elev: ElevationOpening) -> float:
    """Production B3 score: v172 compatibility, plus the strong-anchor gate.

    The reviewed v1.7.2 ``_correlation_score`` already enforces the hard
    rejects (conflicting marks, opening-type conflicts, different sides,
    different known levels, incompatible width) and grades width/side/mark
    agreement.  Production additionally REQUIRES a strong instance-specific
    identity anchor: without one, the pair is forced to 0.0 (no match) so a
    generic "same side + compatible width" agreement can never, by itself,
    identify a physical opening.

    Level note (C1): an unknown level is NEUTRAL — it never becomes a positive
    signal and never rejects; the strong-anchor gate governs elevation's
    correlation eligibility independently of level.
    """
    base = _correlation_score(inst, elev)
    if base <= 0.0:
        return 0.0
    if not _has_production_identity_anchor(inst, elev):
        # Side + width agreement alone is insufficient production identity.
        return 0.0
    return base


def correlate_elevation_to_plan_production(
    elevation_openings: Sequence[ElevationOpening],
    plan_instances: Sequence[Any],
) -> Tuple[List[Any], List[ElevationOpening]]:
    """Strict production B3 correlation: (enriched, unmatched).

    Same ambiguity-safe unique-best assignment and enrichment as the reviewed
    v1.7.2 B3 stage, but with the production identity gate: only pairs sharing
    a strong instance anchor (exact mark / validated position / proven unique
    signal) may correlate.  Pairs without a strong anchor are NEVER matched —
    they fail closed to unmatched/review, exactly like a tie.

    Returns ``(enriched_instances, unmatched_elevations)`` mirroring
    ``pb_elevation_evidence_v172.correlate_elevation_to_plan``'s contract, so
    the production payload can carry the same reviewer-facing shapes.
    """
    if not elevation_openings or not plan_instances:
        return list(plan_instances), list(elevation_openings)

    pairs: List[Tuple[float, int, int]] = []
    for p_idx, inst in enumerate(plan_instances):
        for e_idx, elev in enumerate(elevation_openings):
            sc = _production_correlation_score(inst, elev)
            if sc > 0.0:
                pairs.append((sc, p_idx, e_idx))

    qualified = _find_unique_best_pairs(pairs)
    qualified.sort(key=lambda x: x[0], reverse=True)
    assigned_plan: set = set()
    assigned_elev: set = set()
    assignments: Dict[int, int] = {}

    for sc, p_idx, e_idx in qualified:
        if p_idx in assigned_plan or e_idx in assigned_elev:
            continue
        assignments[p_idx] = e_idx
        assigned_plan.add(p_idx)
        assigned_elev.add(e_idx)

    enriched: List[Any] = []
    for p_idx, inst in enumerate(plan_instances):
        if p_idx in assignments:
            enriched.append(_enrich_from_elevation(inst, elevation_openings[assignments[p_idx]]))
        else:
            enriched.append(inst)

    unmatched = [e for i, e in enumerate(elevation_openings) if i not in assigned_elev]
    return enriched, unmatched
