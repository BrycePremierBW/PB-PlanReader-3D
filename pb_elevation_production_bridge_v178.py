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

import math
from dataclasses import asdict, dataclass, field
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

# Explicit marker when a provenance field has neither candidate-own evidence
# nor bridge-supplied context.  Provenance is NEVER invented; unresolved
# fields are flagged "unknown" (strings) or None (page/level).
PROVENANCE_UNKNOWN = "unknown"

# ---------------------------------------------------------------------------
# Production B3 correlation identity (C2)
# ---------------------------------------------------------------------------
# Generic "same elevation side + compatible width" is NOT sufficient instance
# identity for PRODUCTION correlation — repeated same-width windows/doors on
# one elevation make it too weak.  Production matching requires a stronger
# instance-specific anchor.  These constants describe the anchor rules.
PROD_POSITION_TOLERANCE_M = 0.25   # validated position/location correspondence tolerance
_FRAME_EPS = 1e-6                  # tolerance when matching a registered frame's origin/direction


@dataclass(frozen=True)
class ElevationBridgeResult:
    """Fail-closed bridge output: qualified openings + full diagnostics.

    ``openings`` are the dimensional, coordinate-compatible, opening-sized
    observations that may be threaded into B3 correlation.  ``diagnostics``
    record the accept/reject decision and provenance for every candidate so a
    reviewer can see WHY each candidate did or did not qualify (and that
    qualified candidates remain deduction review-only).

    ``opening_provenance`` is index-aligned with ``openings``: element i carries
    the SAME resolved provenance as the matching accepted opening, so a reviewer
    can trace each persisted opening back to its original source filename, page,
    drawing ref/title, coordinate space, calibration source+state, elevation
    side, and level.  Provenance is resolved through a SINGLE authoritative
    helper so the openings and their diagnostics can never disagree.
    """
    openings: List[ElevationOpening]
    diagnostics: List[Dict[str, Any]]
    opening_provenance: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "openings": [asdict(o) for o in self.openings],
            "diagnostics": list(self.diagnostics),
            "opening_provenance": [dict(p) for p in self.opening_provenance],
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


def _resolve_provenance(
    cand: Any,
    *,
    source_filename: str = "",
    source_page: Optional[int] = None,
    drawing_ref: str = "",
    drawing_title: str = "",
    elevation_side: str = "",
    level: Optional[str] = None,
    wall_ref: str = "",
    calibration: Optional[Calibration] = None,
    calibration_source: str = "",
) -> Dict[str, Any]:
    """SINGLE authoritative provenance resolution for elevation evidence.

    Every field follows the SAME precedence (candidate-objective first, then
    the bridge-supplied page/elevation context, else an explicit
    ``PROVENANCE_UNKNOWN``/``None``):
      - source filename / page / drawing ref / drawing title / level /
        wall_ref: candidate's own evidence, else the bridge arg
      - elevation side / coordinate space: candidate's own, else bridge arg
      - calibration source: candidate's own ``calibration_source``, else the
        bridge arg, else the calibration's ``method``

    The returned record is the CANONICAL resolution for that candidate: callers
    resolve ONCE per candidate and reuse the identical dict for BOTH its
    diagnostic and its persisted opening provenance, so the two can never
    disagree.  No provenance is ever invented: unresolved fields are "unknown"
    or None.
    """
    own_filename = str(getattr(cand, "source_filename", None) or "").strip()
    bridge_filename = str(source_filename or "").strip()
    resolved_filename = own_filename or bridge_filename

    own_page = getattr(cand, "source_page", None)
    resolved_page = own_page if own_page is not None else source_page

    own_ref = str(getattr(cand, "drawing_ref", None) or "").strip()
    resolved_ref = own_ref or str(drawing_ref or "").strip()

    own_title = str(getattr(cand, "drawing_title", None) or "").strip()
    resolved_title = own_title or str(drawing_title or "").strip()

    own_side = str(getattr(cand, "elevation_side", None) or "").strip()
    resolved_side = own_side or str(elevation_side or "").strip()

    own_wall = str(getattr(cand, "wall_ref", None) or "").strip()
    resolved_wall = own_wall or str(wall_ref or "").strip()

    # Candidate-OBJECTIVE level: raster candidates expose it as ``level_band``
    # (only when objectively derived), other candidates may carry a ``level``
    # attribute.  Either beats the bridge-supplied caller ``level``.
    own_level = getattr(cand, "level_band", None)
    if own_level is None:
        own_level = getattr(cand, "level", None)
    resolved_level = own_level if own_level is not None else level

    cal_method = ""
    cal_state: Dict[str, Any] = {}
    if calibration is not None:
        cal_state = calibration_state(calibration)
        cal_method = str(calibration.method or "").strip()
    own_cal_src = str(getattr(cand, "calibration_source", None) or "").strip()
    resolved_cal_source = own_cal_src or str(calibration_source or "").strip() or cal_method

    return {
        "source_filename": resolved_filename or PROVENANCE_UNKNOWN,
        "source_page": resolved_page,
        "drawing_ref": resolved_ref or PROVENANCE_UNKNOWN,
        "drawing_title": resolved_title or PROVENANCE_UNKNOWN,
        "elevation_side": resolved_side or PROVENANCE_UNKNOWN,
        "coord_space": str(getattr(cand, "coord_space", None) or "").strip()
                        or PROVENANCE_UNKNOWN,
        "level": resolved_level,
        "wall_ref": resolved_wall,
        "calibration_source": resolved_cal_source or PROVENANCE_UNKNOWN,
        "calibration_state": cal_state,
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
    calibration_source: str = "",
    wall_ref: str = "",
    resolved: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Per-candidate diagnostic carrying full reviewer traceability (C3).

    Traces an accepted/rejected elevation candidate back to: original source
    filename, source page, drawing ref/title, coordinate space, calibration
    source+state, elevation side, and level (when known).  Provenance is the
    SAME canonical record the caller resolved ONCE via ``_resolve_provenance``
    (``resolved``) — reused verbatim here and for the persisted opening
    provenance so the two can never disagree.  When no ``resolved`` record is
    supplied (rare non-bridge callers), it is resolved here as a fallback.
    """
    if resolved is None:
        resolved = _resolve_provenance(
            cand,
            source_filename=source_filename,
            source_page=getattr(cand, "source_page", None) or None,
            drawing_ref=str(getattr(cand, "drawing_ref", None) or ""),
            drawing_title=drawing_title,
            elevation_side=str(getattr(cand, "elevation_side", None) or ""),
            level=level,
            wall_ref=wall_ref,
            calibration=calibration,
            calibration_source=calibration_source,
        )
    diag: Dict[str, Any] = {
        "kind": "elevation_candidate",
        "candidate_index": index,
        "source": cand.extraction_method,
        "status": status,
        "reason": reason,
        "source_filename": resolved["source_filename"],
        "source_page": resolved["source_page"],
        "coord_space": resolved["coord_space"],
        "bbox": list(cand.bbox),
        "width_m": cand.width_m,
        "height_m": cand.height_m,
        "review_status": cand.review_status,
        "drawing_ref": resolved["drawing_ref"],
        "drawing_title": resolved["drawing_title"],
        "elevation_side": resolved["elevation_side"],
        "level": resolved["level"],
        "wall_ref": resolved["wall_ref"],
        "calibration_source": resolved["calibration_source"],
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
    accepted_provenance: List[Dict[str, Any]] = []
    rejected = 0

    for i, cand in enumerate(candidates or []):
        # Resolve provenance ONCE per candidate; the identical record feeds both
        # the diagnostic and (if accepted) the persisted opening provenance, so
        # the two can never disagree.
        prov = _resolve_provenance(
            cand,
            source_filename=source_filename,
            source_page=source_page,
            drawing_ref=drawing_ref,
            drawing_title=drawing_title,
            elevation_side=elevation_side,
            level=level,
            wall_ref=wall_ref,
            calibration=calib,
            calibration_source=calibration_source,
        )
        if _is_backend_unavailable(cand):
            diagnostics.append(_candidate_diag(
                i, cand, STATUS_REJECTED, REASON_BACKEND_UNAVAILABLE, cand.notes,
                calibration=calib, source_filename=source_filename,
                calibration_source=calibration_source, wall_ref=wall_ref,
                resolved=prov))
            rejected += 1
            continue
        if _is_qualified(cand, calib):
            rects.append(_raster_rect_dict(cand, calibration_source, wall_ref))
            accepted_provenance.append(prov)
            diagnostics.append(_candidate_diag(
                i, cand, STATUS_ACCEPTED, "",
                notes=[
                    "qualified geometric observation; dimension_basis=unknown; "
                    "deduction review-only (B5 requires rough_opening basis)",
                ],
                calibration=calib, source_filename=source_filename,
                calibration_source=calibration_source, wall_ref=wall_ref,
                resolved=prov))
        else:
            rejected += 1
            diagnostics.append(_candidate_diag(
                i, cand, STATUS_REJECTED,
                _reject_reason(cand, calib, calib_dimensional, calib_space),
                calibration=calib, source_filename=source_filename,
                calibration_source=calibration_source, wall_ref=wall_ref,
                resolved=prov))

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

    # opening_provenance is index-aligned with openings: guard the per-accepted-
    # rect provenance to exactly len(openings) so alignment is never violated.
    opening_provenance = accepted_provenance[:len(openings)]

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

    return ElevationBridgeResult(
        openings=list(openings),
        diagnostics=diagnostics,
        opening_provenance=opening_provenance,
    )


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
    accepted_provenance: List[Dict[str, Any]] = []
    rejected = 0

    for i, cand in enumerate(candidates or []):
        # Resolve provenance ONCE per candidate (identical for diagnostic and,
        # if accepted, the persisted opening provenance).  The vector path has
        # no explicit calibration_source argument; resolution falls back to the
        # calibration's own method via ``_resolve_provenance``.
        prov = _resolve_provenance(
            cand,
            source_filename=source_filename,
            source_page=source_page,
            drawing_ref=drawing_ref,
            drawing_title=drawing_title,
            elevation_side=elevation_side,
            level=level,
            wall_ref=wall_ref,
            calibration=calib,
        )
        if _is_backend_unavailable(cand):
            diagnostics.append(_candidate_diag(
                i, cand, STATUS_REJECTED, REASON_BACKEND_UNAVAILABLE, cand.notes,
                calibration=calib, source_filename=source_filename,
                wall_ref=wall_ref,
                resolved=prov))
            rejected += 1
            continue
        if _is_qualified(cand, calib):
            rects.append(_vector_rect_dict(cand, wall_ref))
            accepted_provenance.append(prov)
            diagnostics.append(_candidate_diag(
                i, cand, STATUS_ACCEPTED, "",
                notes=[
                    "qualified closed-rectangle observation; dimension_basis=unknown; "
                    "deduction review-only (B5 requires rough_opening basis)",
                ],
                calibration=calib, source_filename=source_filename,
                wall_ref=wall_ref,
                resolved=prov))
        else:
            rejected += 1
            diagnostics.append(_candidate_diag(
                i, cand, STATUS_REJECTED,
                _reject_reason(cand, calib, calib_dimensional, calib_space),
                calibration=calib, source_filename=source_filename,
                wall_ref=wall_ref,
                resolved=prov))

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

    opening_provenance = accepted_provenance[:len(openings)]

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

    return ElevationBridgeResult(
        openings=list(openings),
        diagnostics=diagnostics,
        opening_provenance=opening_provenance,
    )


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
    opening_provenance: List[Dict[str, Any]] = []
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
        opening_provenance.extend(r.opening_provenance)
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
        opening_provenance.extend(v.opening_provenance)
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

    return ElevationBridgeResult(
        openings=openings,
        diagnostics=diagnostics,
        opening_provenance=opening_provenance,
    )


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


def _registered_wall_segment(
    facades: Any, side: str, wall_ref: str
) -> Optional[Dict[str, Any]]:
    """Return the registered facade segment for (side, wall_ref_ref).

    ``facades`` is the shape produced by ``footprint_facades`` (keyed by
    cardinal side, each with a ``segments`` list of ``{wall_ref, a, b,
    length_m, side, source_polygon, ...}``) — i.e. wall segments derived from
    the CALIBRATED building footprint.  The returned segment is the SINGLE
    source of truth for that wall's location frame: its ``a`` (origin), the
    direction toward ``b``, and its ``length_m`` define the along-wall station
    axis, and its ``source_polygon`` + ``wall_ref`` define the frame identity.

    Returns None when the side/wall is not registered, or when no facade
    coregistration is supplied at all — a caller-attached string can never be
    a genuine anchor.
    """
    if not facades or not wall_ref:
        return None
    for seg in (facades.get(side) or {}).get("segments") or []:
        if str(seg.get("wall_ref") or "").strip().upper() == wall_ref.upper():
            length = _num(seg.get("length_m"), None)
            if length is None or length <= 0.0:
                return None
            return seg
    return None


def _vec2(value: Any) -> Optional[Tuple[float, float]]:
    """Normalise a 2D vector from ``[x, y]``/``(x, y)``; None if malformed."""
    try:
        x = float(value[0])
        y = float(value[1])
    except (TypeError, ValueError, IndexError):
        return None
    if x != x or y != y or abs(x) == float("inf") or abs(y) == float("inf"):
        return None
    return (x, y)


def _segment_frame(segment: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Canonical location frame derived from a REGISTERED facade segment.

    Computed directly from the segment's own geometry: origin ``a``, unit
    direction toward ``b``, length, and a frame identity ``<source_polygon>:
    <wall_ref>``.  Returns None when the segment lacks usable geometry.
    """
    a = _vec2(segment.get("a"))
    b = _vec2(segment.get("b"))
    if a is None or b is None:
        return None
    vx, vy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(vx, vy)
    if length <= 0.0:
        return None
    wall_ref = str(segment.get("wall_ref") or "").strip().upper()
    source = str(segment.get("source_polygon") or "").strip()
    return {
        "origin": a,
        "direction": (vx / length, vy / length),
        "length_m": length,
        "segment_id": f"{source}:{wall_ref}" if source else wall_ref,
    }


def _position_record(obj: Any) -> Optional[Dict[str, Any]]:
    """Extract+normalise a STRUCTURED registration-derived position record.

    Only a dict stored on the object (``registration_position``) counts.  A raw
    scalar (``position_along_wall_m`` / ``wall_position_m``) carries no frame
    identity, origin, direction, or derivation source and therefore can never
    establish registration provenance — such scalar-only positions return None
    and FAIL CLOSED (they are indistinguishable from an arbitrary number).

    Canonical record: ``{wall_ref, segment_id, origin, direction, station_m,
    derivation}``.  Returns None for any missing/malformed field so an
    incomplete or fabricated record can never anchor.
    """
    rec = getattr(obj, "registration_position", None)
    if not isinstance(rec, dict):
        return None
    wall_ref = str(rec.get("wall_ref") or "").strip().upper()
    segment_id = str(rec.get("segment_id") or "").strip()
    origin = _vec2(rec.get("origin"))
    direction = _vec2(rec.get("direction"))
    station = _num(rec.get("station_m"), None)
    derivation = str(rec.get("derivation") or "").strip()
    if not wall_ref or not segment_id or origin is None or direction is None:
        return None
    if station is None or station != station:
        return None
    if not derivation:
        return None
    dlen = math.hypot(direction[0], direction[1])
    if dlen <= 0.0:
        return None
    return {
        "wall_ref": wall_ref,
        "segment_id": segment_id,
        "origin": origin,
        "direction": (direction[0] / dlen, direction[1] / dlen),
        "station_m": station,
        "derivation": derivation,
    }


def _record_in_registered_frame(
    rec: Dict[str, Any], frame: Dict[str, Any]
) -> bool:
    """True when a position record was derived in the SAME registered frame.

    The record's frame identity, ORIGIN and DIRECTION must each match the
    registered facade segment's frame, and its station must fall on that
    segment.  A different origin or direction (even with an agreeing station)
    means the two sides are NOT in a shared registration frame -> reject.
    """
    if rec["segment_id"] != frame["segment_id"]:
        return False  # different frame identity
    if (abs(rec["origin"][0] - frame["origin"][0]) > _FRAME_EPS
            or abs(rec["origin"][1] - frame["origin"][1]) > _FRAME_EPS):
        return False  # different origin
    if (abs(rec["direction"][0] - frame["direction"][0]) > _FRAME_EPS
            or abs(rec["direction"][1] - frame["direction"][1]) > _FRAME_EPS):
        return False  # different direction
    if rec["derivation"] != "facade_registration":
        return False  # not derived from the facade registration
    return 0.0 <= rec["station_m"] <= frame["length_m"]


def _validated_position_anchor(
    inst: Any, elev: ElevationOpening, facades: Any = None
) -> bool:
    """Strong anchor (b): GENUINELY registration-derived position correspondence.

    Production position identity is ONLY accepted when BOTH the plan instance
    and the elevation opening carry a STRUCTURED registration-derived position
    record that (a) names the SAME wall reference, (b) was derived in the SAME
    registered facade segment frame — same segment identity, ORIGIN and
    DIRECTION computed from that segment's ``a``/``b`` geometry, with derivation
    source ``facade_registration`` — and (c) agree on the along-wall station.

    Raw scalar-only positions (``position_along_wall_m`` +
    ``wall_position_m``) are NEVER accepted: an arbitrary in-range number
    carries no frame identity, origin, direction, or derivation source, so even
    with matching wall refs and values inside the registered extent it is
    indistinguishable from a fabricated scalar and FAILS CLOSED.  Different
    registration origins or directions likewise reject — same station in two
    different frames is not a shared position.
    """
    plan_rec = _position_record(inst)
    elev_rec = _position_record(elev)
    if plan_rec is None or elev_rec is None:
        # Raw scalar-only / missing structural record -> cannot establish
        # registration provenance; fail closed to ambiguity/review.
        return False
    if plan_rec["wall_ref"] != elev_rec["wall_ref"]:
        return False  # WRONG WALL: references must match before position anchors
    segment = _registered_wall_segment(
        facades, str(getattr(elev, "elevation_side", "") or "").strip(),
        plan_rec["wall_ref"],
    )
    if segment is None:
        # Wall not registered from the footprint (or no facades) -> the position
        # is NOT registration-derived and cannot be a genuine anchor.
        return False
    frame = _segment_frame(segment)
    if frame is None:
        return False
    if not _record_in_registered_frame(plan_rec, frame):
        return False  # plan not in the shared registered frame
    if not _record_in_registered_frame(elev_rec, frame):
        return False  # elevation not in the shared registered frame
    if plan_rec["segment_id"] != elev_rec["segment_id"]:
        return False  # different frame identity
    # Both sides derived in the SAME registered origin/direction frame; their
    # stations must agree on a physical opening.
    return abs(plan_rec["station_m"] - elev_rec["station_m"]) <= PROD_POSITION_TOLERANCE_M


def _proven_unique_anchor(elev: ElevationOpening) -> bool:
    """Strong anchor (c): an independently-proven UNIQUE identity signal.

    A truthy, caller-attached ``identity_anchor`` attribute is NOT accepted as
    an anchor: it is not independently validated and would let generic side +
    width evidence masquerade as a unique match.  Production relies on the
    genuine exact-mark anchor (a) and the genuine validated-position anchor (b)
    instead.  This predicate is therefore always False — kept as an explicit
    statement that arbitrary attached identity signals are never sufficient.
    """
    return False


def _has_production_identity_anchor(
    inst: Any, elev: ElevationOpening, facades: Any = None
) -> bool:
    """Return True when a (plan, elevation) pair shares a strong anchor.

    Production matching REQUIRES at least one GENUINELY VALIDATED anchor:
      (a) an exact compatible opening mark (non-blank both sides, equal), OR
      (b) a validated position/location correspondence backed by a
          footprint-registered facade wall_ref (extent-checked against
          ``facades``).

    Side and width SUPPORT a match but never independently identify a physical
    opening, and arbitrary caller-attached identifiers/positions are never
    sufficient.  A pair that only agrees on side+width (or only on an attached
    ``identity_anchor``/bare position) has NO strong anchor and may not
    correlate (fail-closed ambiguity → review).
    """
    return (
        _exact_mark_anchor(inst, elev)
        or _validated_position_anchor(inst, elev, facades)
    )


def _production_correlation_score(
    inst: Any, elev: ElevationOpening, facades: Any = None
) -> float:
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
    if not _has_production_identity_anchor(inst, elev, facades):
        # Side + width agreement alone is insufficient production identity.
        return 0.0
    return base


def correlate_elevation_to_plan_production(
    elevation_openings: Sequence[ElevationOpening],
    plan_instances: Sequence[Any],
    facades: Any = None,
) -> Tuple[List[Any], List[ElevationOpening]]:
    """Strict production B3 correlation: (enriched, unmatched).

    Same ambiguity-safe unique-best assignment and enrichment as the reviewed
    v1.7.2 B3 stage, but with the production identity gate: only pairs sharing
    a GENUINELY VALIDATED strong instance anchor may correlate.  Genuine anchors
    are (a) an exact compatible opening mark, or (b) a validated position backed
    by a facade wall segment REGISTERED from the calibrated building footprint
    (``facades`` from ``footprint_facades``), extent-checked so a station off
    the registered wall can never anchor.  Pairs without such an anchor —
    including side+width-only agreement and arbitrary caller-attached
    identifiers/positions — are NEVER matched; they fail closed to
    unmatched/review, exactly like a tie.

    Returns ``(enriched_instances, unmatched_elevations)`` mirroring
    ``pb_elevation_evidence_v172.correlate_elevation_to_plan``'s contract, so
    the production payload can carry the same reviewer-facing shapes.
    """
    if not elevation_openings or not plan_instances:
        return list(plan_instances), list(elevation_openings)

    pairs: List[Tuple[float, int, int]] = []
    for p_idx, inst in enumerate(plan_instances):
        for e_idx, elev in enumerate(elevation_openings):
            sc = _production_correlation_score(inst, elev, facades)
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
