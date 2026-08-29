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
from typing import Any, Dict, List, Optional, Sequence

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
) -> Dict[str, Any]:
    diag: Dict[str, Any] = {
        "kind": "elevation_candidate",
        "candidate_index": index,
        "source": cand.extraction_method,
        "status": status,
        "reason": reason,
        "coord_space": cand.coord_space,
        "bbox": list(cand.bbox),
        "width_m": cand.width_m,
        "height_m": cand.height_m,
        "review_status": cand.review_status,
        "drawing_ref": cand.drawing_ref,
        "elevation_side": cand.elevation_side,
        "extraction_method": cand.extraction_method,
    }
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
    return {
        "bbox": list(cand.bbox),
        "coord_space": cand.coord_space,
        "render_dpi": cand.render_dpi,
        "confidence": cand.geometry_confidence,
        "level": cand.level_band,
        "wall_ref": wall_ref,
        "drawing_ref": cand.drawing_ref,
        "extraction_method": cand.extraction_method,
        "calibration_source": cand.calibration_source or calibration_source,
        "review_status": cand.review_status,
    }


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
    return {
        "bbox": list(cand.bbox),
        "coord_space": cand.coord_space,
        "confidence": cand.geometry_confidence,
        "level": None,  # vector candidate carries no objective level band
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
                i, cand, STATUS_REJECTED, REASON_BACKEND_UNAVAILABLE, cand.notes))
            rejected += 1
            continue
        if _is_qualified(cand, calib):
            rects.append(_raster_rect_dict(cand, calibration_source, wall_ref))
            diagnostics.append(_candidate_diag(
                i, cand, STATUS_ACCEPTED, "",
                notes=[
                    "qualified geometric observation; dimension_basis=unknown; "
                    "deduction review-only (B5 requires rough_opening basis)",
                ]))
        else:
            rejected += 1
            diagnostics.append(_candidate_diag(
                i, cand, STATUS_REJECTED,
                _reject_reason(cand, calib, calib_dimensional, calib_space)))

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
                i, cand, STATUS_REJECTED, REASON_BACKEND_UNAVAILABLE, cand.notes))
            rejected += 1
            continue
        if _is_qualified(cand, calib):
            rects.append(_vector_rect_dict(cand, wall_ref))
            diagnostics.append(_candidate_diag(
                i, cand, STATUS_ACCEPTED, "",
                notes=[
                    "qualified closed-rectangle observation; dimension_basis=unknown; "
                    "deduction review-only (B5 requires rough_opening basis)",
                ]))
        else:
            rejected += 1
            diagnostics.append(_candidate_diag(
                i, cand, STATUS_REJECTED,
                _reject_reason(cand, calib, calib_dimensional, calib_space)))

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