"""PlanReader v1.7.8 Phase 2B production integration — focused regressions.

Proves the controlled, fail-closed seam that wires Phase 2A elevation
evidence (v1.7.7 extractors + v1.7.2 B3 correlation) into the real
opening-evidence production pipeline (``run_p5_native_payload`` /
``analyse_stored_page_openings``) WITHOUT granting any new deduction or
instance authority.

Pass conditions:
  1. Valid elevation evidence reaches the production evidence pipeline: a
     valid calibration + real raster rect evidence mapping to a plan
     instance that shares its geometry reaches ``source_observations`` and
     ``elevation_geometry`` (via detect_elevation_openings +
     correlate_elevation_to_plan threading).
  2. Uncalibrated evidence cannot become dimensional — invalid /
     non-dimensional calibration yields NO dimensional ElevationOpening
     (fail closed; metres are never fabricated).
  3. Coordinate-space mismatch fails closed — a render_pixel candidate
     against a pdf_point calibration (and vice versa) is dropped; spaces
     are never mixed.
  4. Wrong-level evidence is rejected — plan L1 vs elevation L2 (both
     known) yields no correlation; elevation stays unmatched, no
     enrichment.
  5. Ambiguous (tied) correlation remains review-only — equal scores stay
     unmatched; the instance deduction_status stays review.
  6. Elevation-only evidence cannot set deduct=True — basis stays unknown
     so the B5 gate still rejects.
  7. Elevation-only evidence cannot create a physical opening instance —
     instance count is unchanged after correlation.
  8. Existing B0/B1/B2/B4/B5 behaviour is unchanged with
     ``elevation_openings=None`` (no-op), and the full existing suite stays
     green.

REAL evidence is used where practical: the committed LAGO CD3001 East
elevation fixture (page 86) and its real 150-DPI render crop.  Detector
output NEVER defines truth; expected geometry is stated independently from
the fixture's independent annotations (repeated facade cell ~2.96 x 1.57 m,
glazed lights 0.773 x 1.489 m, scale bar 10 m / 28.346 pt/m).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import cv2
import pytest

import pb_opening_production_v175 as prod
import pb_elevation_production_bridge_v178 as bridge

from pb_elevation_calibration_v177 import (
    Calibration,
    calibration_from_scale_bar_positions,
    COORD_SPACE_PDF_POINT,
    COORD_SPACE_RENDER_PIXEL,
)
from pb_elevation_raster_extract_v177 import (
    ElevationRectCandidate,
    detect_raster_rect_candidates,
    opening_sized,
)
from pb_elevation_vector_extract_v177 import VectorRectCandidate
from pb_elevation_evidence_v172 import (
    ElevationOpening,
    correlate_elevation_to_plan,
)
from pb_opening_evidence_v170 import (
    DIMENSION_BASIS_UNKNOWN,
    DEDUCTION_REVIEW,
    OPENING_TYPE_DOOR,
    OPENING_TYPE_WINDOW,
    OpeningEvidence,
)

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "lago_cd3001_east_elevation_v177.json"
_CROP = Path(__file__).resolve().parent / "fixtures" / "lago_cd3001_p86_e1east_roi_150dpi.png"

_RENDER_DPI = 150.0
_PT_TO_PX = _RENDER_DPI / 72.0


# ---------------------------------------------------------------------------
# Local B1-candidate helpers (kept local so this file never imports another
# test module — importing ``tests.test_pipeline_integration_v174`` from a test
# file alters pytest's prepend-import collection order and disturbs existing
# skip/skipif evaluation in other suites).
# ---------------------------------------------------------------------------
def _b1_candidate(
    *,
    mark: str = "D01",
    wall: str = "W01",
    opening_type: str = OPENING_TYPE_DOOR,
    position: float = 1.5,
    width: float = 0.82,
    page_no: int = 1,
    geom_conf: float = 0.90,
    dim_conf: float = 0.0,
    assoc_conf: float = 0.85,
) -> OpeningEvidence:
    """Create an OpeningEvidence that mimics B1 output."""
    from pb_opening_evidence_v170 import record_plan_observation

    ev = OpeningEvidence(
        type_mark=mark,
        page_no=page_no,
        wall_ref=wall,
        opening_type=opening_type,
        width_m=width,
        height_m=None,
        dimension_basis="unknown",
        dimension_source="plan_vector",
        sill_m=0.0 if opening_type == OPENING_TYPE_DOOR else 0.9,
        position_along_wall_m=position,
        extraction_method="plan_vector",
        geometry_confidence=geom_conf,
        dimension_confidence=dim_conf,
        association_confidence=assoc_conf,
        deduction_status=DEDUCTION_REVIEW,
        evidence=["plan_vector_jamb_detection"],
    )
    ev.set_quantity(1, source="geometric")
    ev.compute_area()
    ev.compute_deduction_status()
    record_plan_observation(ev)
    return ev


def _mock_b1(candidates):
    """Create a mock PlanOpeningDetectionResult (B1)."""
    result = MagicMock()
    result.candidates = list(candidates)
    result.door_count = sum(1 for c in candidates if c.opening_type == OPENING_TYPE_DOOR)
    result.window_count = sum(1 for c in candidates if c.opening_type == OPENING_TYPE_WINDOW)
    result.gap_count = 0
    return result


def _register_facades():
    """A footprint-derived facade coregistration (shape of ``footprint_facades``).

    Registers one East facade with a single wall segment ``E01`` derived from
    the calibrated building footprint, length 8 m (station interval 0..8).
    Mirrors ``pb_elevation_registration_v135.footprint_facades``'s output shape
    so the production bridge can extent-check genuine position anchors against
    a drawing-derived registration.
    """
    return {
        "North": {"side": "North", "segments": [], "projected_width_m": 10.0,
                  "edge_length_m": 0.0, "levels": []},
        "South": {"side": "South", "segments": [], "projected_width_m": 10.0,
                  "edge_length_m": 0.0, "levels": []},
        "East": {
            "side": "East", "projected_width_m": 8.0, "edge_length_m": 8.0,
            "levels": ["Ground / unregistered"],
            "segments": [{
                "wall_ref": "E01", "side": "East",
                "a": [0.0, 0.0], "b": [8.0, 0.0], "length_m": 8.0,
                "level_name": "Ground / unregistered", "level_index": 0,
                "source_polygon": "fp-1", "confidence": "Measured plan geometry",
            }],
        },
        "West": {"side": "West", "segments": [], "projected_width_m": 8.0,
                 "edge_length_m": 0.0, "levels": []},
    }


def _reg_pos(
    wall_ref: str, station: float, *,
    origin=(0.0, 0.0), direction=(1.0, 0.0),
    segment_id="fp-1:E01", derivation="facade_registration",
):
    """A STRUCTURED registration-derived position record (R2 contract).

    Matches the E01/East segment registered by ``_register_facades`` (origin
    ``a`` = (0,0), unit direction toward ``b`` = (1,0), frame ``fp-1:E01``).
    A genuine anchor requires this record on BOTH the plan and the elevation;
    raw scalar positions alone fail closed.
    """
    return {
        "wall_ref": wall_ref,
        "segment_id": segment_id,
        "origin": list(origin),
        "direction": list(direction),
        "station_m": station,
        "derivation": derivation,
    }


def _attach_reg_position(obj, record):
    """Attach a structured registration-derived position record to an object."""
    object.__setattr__(obj, "registration_position", record)
    return obj
# Fixture helpers (real evidence; expected geometry stated independently)
# ---------------------------------------------------------------------------
def _load_fixture():
    with open(_FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def _load_real_crop():
    img = cv2.imread(str(_CROP), cv2.IMREAD_GRAYSCALE)
    assert img is not None, f"real benchmark crop not readable: {_CROP}"
    return img


def _real_render_pixel_calibration():
    """Real render_pixel calibration from the page's real 11-tick scale bar.

    The fixture independently measures the source graphic scale bar (11 ticks
    0..10 at 1 m, pdf_point positions) on page 86; the render transform
    (150 DPI) gives px positions.  Expected scale: 10 m over 283.46 pt
    -> 28.346 pt/m -> 59.055 px/m.
    """
    fx = _load_fixture()
    ticks_px = [t * _PT_TO_PX for t in fx["calibration"]["measured_bar_geometry"]["tick_positions_x_pt"]]
    cal = calibration_from_scale_bar_positions(
        ticks_px, 1.0,
        coord_space=COORD_SPACE_RENDER_PIXEL,
        render_dpi=_RENDER_DPI,
        source_page=86,
    )
    assert cal.valid
    assert cal.is_dimensional()
    assert cal.px_per_m is not None
    assert abs(cal.px_per_m - 59.055) < 1.0
    return cal, fx


def _real_crop_best_cell():
    """Run the real raster extractor and pick the best cell near the
    independently-measured repeated facade cell ~2.96 x 1.57 m."""
    cal, fx = _real_render_pixel_calibration()
    image = _load_real_crop()
    cands = detect_raster_rect_candidates(
        image, cal,
        source_filename=fx["source"]["local_source_alias"],
        source_page=86,
        drawing_ref=fx["source"]["drawing_no"],
        elevation_side=fx["source"]["elevation_side"],
        calibration_source=cal.method,
    )
    opening_cands = [c for c in cands if opening_sized(c.width_m, c.height_m)]
    assert opening_cands, "real crop must yield opening-sized dimensional candidates"
    # Independently stated expected geometry (fixture benchmark.independently_measured_cell).
    assert any(abs(c.width_m - 2.96) <= 0.15 for c in opening_cands), (
        "expected a real raster cell near the independent 2.96 m width"
    )
    assert any(abs(c.height_m - 1.57) <= 0.15 for c in opening_cands), (
        "expected a real raster cell near the independent 1.57 m height"
    )
    best = min(opening_cands, key=lambda c: abs(c.width_m - 2.96) + abs(c.height_m - 1.57))
    return cal, fx, image, best


def _raster_candidate(**extra):
    """A synthetic one-off raster candidate (for controlled fail-closed checks)."""
    base = dict(
        source_filename="synthetic.pdf",
        source_page=86,
        drawing_ref="CD3001",
        elevation_side="East",
        bbox=(2125, 1439, 2330, 1500),
        centroid=(2227.5, 1469.5),
        calibration_method="graphic_scale_bar",
        coord_space=COORD_SPACE_RENDER_PIXEL,
        render_dpi=_RENDER_DPI,
        width_m=1.0,
        height_m=2.1,
        calibration_source="graphic_scale_bar",
        dimension_basis="unknown",
        geometry_confidence=0.6,
        extraction_method="raster_rect",
        label="",
        level_band=None,
        review_status="review",
        notes=["synthetic test candidate"],
    )
    base.update(extra)
    return ElevationRectCandidate(**base)


# ---------------------------------------------------------------------------
# 1. Valid elevation evidence reaches the production evidence pipeline
# ---------------------------------------------------------------------------
def test_valid_elevation_reaches_production_evidence_pipeline():
    """Real calibration + real raster rect -> dimensional ElevationOpening ->
    production pipeline -> source_observations + elevation_geometry populated."""
    cal, fx = _real_render_pixel_calibration()
    image = _load_real_crop()

    cands = detect_raster_rect_candidates(
        image, cal,
        source_filename=fx["source"]["local_source_alias"],
        source_page=86,
        drawing_ref=fx["source"]["drawing_no"],
        elevation_side=fx["source"]["elevation_side"],
        calibration_source=cal.method,
    )
    opening_cands = [c for c in cands if opening_sized(c.width_m, c.height_m)]
    # Only a small raster rect list (ONE real candidate) is bridged, so the
    # reviewed B3 correlation has a unique best match to the plan instance.
    best = min(opening_cands, key=lambda c: abs(c.width_m - 2.96) + abs(c.height_m - 1.57))

    result = bridge.raster_openings_from_candidates(
        [best], cal,
        elevation_page_no=86,
        elevation_side=fx["source"]["elevation_side"],
        source_filename=fx["source"]["local_source_alias"],
        source_page=86,
        drawing_ref=fx["source"]["drawing_no"],
        drawing_title=fx["source"]["drawing_title"],
        level=None,
        wall_ref="E01",
        calibration_source=cal.method,
    )
    assert len(result.openings) >= 1
    opening = result.openings[0]
    # The produced opening must carry the registered wall_ref so the GENUINE
    # position anchor can be extent-validated against the facade coregistration.
    assert opening.wall_ref == "E01"
    assert opening.coord_space == COORD_SPACE_RENDER_PIXEL
    assert opening.width_m is not None and opening.height_m is not None
    # Independently-measured repeated facade cell ~2.96 x 1.57 m.
    assert abs(opening.width_m - 2.96) <= 0.15
    assert abs(opening.height_m - 1.57) <= 0.15
    assert opening.calibration and opening.source_page_no == 86
    # Accepted candidate diagnostic records WHY it qualified.
    accepted = [d for d in result.diagnostics
                if d.get("kind") == "elevation_candidate" and d["status"] == "accepted"]
    assert accepted, "qualified candidate must carry an accepted diagnostic"

    # Plan instance that shares the geometry (same width, known East side).
    plan = _b1_candidate(
        mark="", wall="E01", position=2.0, width=opening.width_m,
        opening_type=OPENING_TYPE_WINDOW, geom_conf=0.9, assoc_conf=0.85,
    )
    plan.elevation_side = "East"
    # Production strict identity: BOTH sides carry a STRUCTURED
    # registration-derived position record (same registered E01 frame, origin,
    # direction, agreeing station) — NOT a raw scalar, which would fail closed.
    _attach_reg_position(
        plan, _reg_pos("E01", plan.position_along_wall_m),
    )
    _attach_reg_position(
        opening, _reg_pos("E01", plan.position_along_wall_m),
    )

    native = {"segments": [], "words": []}
    with patch(
        "pb_plan_opening_detection_v171.plan_opening_candidates",
        return_value=_mock_b1([plan]),
    ):
        payload = prod.run_p5_native_payload(
            native, page_no=1, scale_info={"px_per_m": cal.px_per_m},
            elevation_openings=result.openings,
            elevation_diagnostics=result.diagnostics,
            elevation_provenance=result.opening_provenance,
            facade_registration=_register_facades(),
        )

    assert payload["status"] == "ok"
    # Provenance/outcomes persisted additively for the reviewer.
    assert payload["elevation_openings"]
    assert isinstance(payload["elevation_diagnostics"], list)
    assert any(d.get("kind") == "b3_correlation" for d in payload["elevation_diagnostics"])

    inst = payload["instances"][0]
    sources = [o.get("source") for o in inst["source_observations"]]
    assert "elevation_rect" in sources, "elevation observation must reach the instance"
    assert inst["elevation_geometry"] is not None
    assert inst["elevation_geometry"]["coord_space"] == COORD_SPACE_RENDER_PIXEL
    assert inst["height_m"] == pytest.approx(opening.height_m, abs=0.01)
    # Corroboration NEVER grants deduction authority / rough-opening basis.
    assert inst["dimension_basis"] == DIMENSION_BASIS_UNKNOWN
    assert inst["deduct"] is False
    assert inst["deduction_status"] == DEDUCTION_REVIEW


# ---------------------------------------------------------------------------
# 2/3. Fail-closed: uncalibrated / non-dimensional / mismatched evidence
# ---------------------------------------------------------------------------
def test_uncalibrated_evidence_cannot_become_dimensional():
    """Invalid calibration -> NO dimensional ElevationOpenings, with reasons."""
    invalid = Calibration(
        units_per_m=0.0, coord_space=COORD_SPACE_RENDER_PIXEL,
        valid=False, method="none", review_status="rejected",
        notes=["synthetic failing calibration"],
    )
    image = _load_real_crop()
    result = bridge.extract_raster_elevation_openings(
        image, invalid,
        elevation_page_no=86, elevation_side="East",
        source_filename="synthetic.pdf", source_page=86,
        drawing_ref="CD3001", calibration_source=invalid.method,
    )
    assert result.openings == []
    cand_diags = [d for d in result.diagnostics
                  if d.get("kind") == "elevation_candidate"]
    assert cand_diags, "every candidate needs a diagnostic"
    assert all(d["status"] == "rejected" for d in cand_diags)
    assert any(d["reason"] == "calibration_invalid" for d in cand_diags)
    summaries = [d for d in result.diagnostics
                 if d.get("kind") == "elevation_evidence_summary"]
    assert summaries and summaries[0]["qualified_count"] == 0

    # A direct dimensional-looking candidate is also impossible without a
    # proven dimensional calibration.
    cand = _raster_candidate(width_m=0.82, height_m=2.1)
    direct = bridge.raster_openings_from_candidates(
        [cand], invalid,
        elevation_page_no=86, elevation_side="East",
        drawing_ref="CD3001",
    )
    assert direct.openings == []
    assert any(d["reason"] == "calibration_invalid" for d in direct.diagnostics
               if d.get("kind") == "elevation_candidate")


def test_coordinate_mismatch_fails_closed():
    """render_pixel vs pdf_point (and vice versa) is ALWAYS dropped."""
    fx = _load_fixture()
    pt_cal = calibration_from_scale_bar_positions(
        fx["calibration"]["measured_bar_geometry"]["tick_positions_x_pt"], 1.0,
        coord_space=COORD_SPACE_PDF_POINT, source_page=86,
    )
    assert pt_cal.valid and pt_cal.is_dimensional() and pt_cal.pt_per_m is not None

    # render_pixel raster candidate against a pdf_point calibration -> drop.
    px_cand = _raster_candidate(width_m=1.0, height_m=2.1)
    raster = bridge.raster_openings_from_candidates(
        [px_cand], pt_cal,
        elevation_page_no=86, elevation_side="East", drawing_ref="CD3001",
    )
    assert raster.openings == []
    assert any(d["reason"] == "coordinate_space_mismatch" for d in raster.diagnostics
               if d.get("kind") == "elevation_candidate")

    # pdf_point vector candidate against a render_pixel calibration -> drop.
    rcal, _ = _real_render_pixel_calibration()
    vcand = VectorRectCandidate(
        source_filename="synthetic.pdf", source_page=86,
        drawing_ref="CD3001", elevation_side="East",
        bbox=(1100.0, 1300.0, 1300.0, 1500.0),
        centroid=(1200.0, 1400.0),
        coord_space=COORD_SPACE_PDF_POINT,
        calibration_method="graphic_scale_bar",
        width_m=1.0, height_m=2.1,
        dimension_basis="unknown",
        extraction_method="vector_line_closure",
        geometry_confidence=0.6,
        review_status="review",
        notes=["synthetic test candidate"],
    )
    vec = bridge.vector_openings_from_candidates(
        [vcand], rcal,
        elevation_page_no=86, elevation_side="East", drawing_ref="CD3001",
    )
    assert vec.openings == []
    assert any(d["reason"] == "coordinate_space_mismatch" for d in vec.diagnostics
               if d.get("kind") == "elevation_candidate")


def test_non_opening_sized_dimensional_evidence_is_rejected():
    """A dimensional candidate outside the opening geofence never qualifies."""
    cal, _ = _real_render_pixel_calibration()
    huge = _raster_candidate(width_m=50.0, height_m=50.0)
    result = bridge.raster_openings_from_candidates(
        [huge], cal,
        elevation_page_no=86, elevation_side="East", drawing_ref="CD3001",
    )
    assert result.openings == []
    assert any(d["reason"] == "not_opening_sized" for d in result.diagnostics
               if d.get("kind") == "elevation_candidate")


def test_produce_bridge_with_no_evidence_fails_closed():
    """No image and no segments -> zero openings + explicit diagnostic."""
    invalid = Calibration(units_per_m=0.0, coord_space=COORD_SPACE_RENDER_PIXEL,
                          valid=False, method="none", review_status="rejected")
    result = bridge.produce_elevation_openings(
        invalid, elevation_page_no=86, elevation_side="East",
    )
    assert result.openings == []
    summaries = [d for d in result.diagnostics
                 if d.get("kind") == "elevation_evidence_summary"]
    assert summaries and summaries[0]["reason"] == "no_elevation_evidence_supplied"


# ---------------------------------------------------------------------------
# 4. Wrong-level evidence is rejected
# ---------------------------------------------------------------------------
def test_wrong_level_elevation_evidence_is_rejected():
    """Plan L1 vs elev L2 (both known) -> no correlation, no enrichment."""
    plan = _b1_candidate(
        mark="D01", wall="W01", position=1.5, width=0.82,
        opening_type=OPENING_TYPE_DOOR, geom_conf=0.9, assoc_conf=0.85,
    )
    plan.level = "L1"
    elev = ElevationOpening(
        elevation_page_no=3, elevation_side="North",
        bbox_px=(100, 100, 200, 300), width_m=0.82, height_m=2.1,
        label="D01", confidence=0.8, level="L2",
    )
    enriched, unmatched = correlate_elevation_to_plan([elev], [plan])
    assert len(unmatched) == 1, "wrong-level elevation stays unmatched"
    assert enriched[0].elevation_geometry is None, "no enrichment on wrong level"
    assert enriched[0].height_m is None

    with patch(
        "pb_plan_opening_detection_v171.plan_opening_candidates",
        return_value=_mock_b1([plan]),
    ):
        payload = prod.run_p5_native_payload(
            {"segments": [], "words": []}, page_no=1,
            scale_info={"px_per_m": 28.3},
            elevation_openings=[elev],
        )
    inst = payload["instances"][0]
    assert inst["height_m"] is None
    assert inst["elevation_geometry"] is None
    assert inst["deduct"] is False
    assert inst["deduction_status"] == DEDUCTION_REVIEW


# ---------------------------------------------------------------------------
# 5. Ambiguous (tied) correlation remains review-only
# ---------------------------------------------------------------------------
def test_ambiguous_correlation_remains_review_only():
    """Two elevations tie for one plan -> unmatched; status stays review."""
    plan = _b1_candidate(
        mark="D01", wall="W01", position=1.5, width=0.82,
        opening_type=OPENING_TYPE_DOOR, geom_conf=0.9, assoc_conf=0.85,
    )
    e1 = ElevationOpening(
        elevation_page_no=3, elevation_side="North",
        bbox_px=(100, 100, 200, 300), width_m=0.82, height_m=2.1,
        label="D01", confidence=0.8,
    )
    e2 = ElevationOpening(
        elevation_page_no=4, elevation_side="North",
        bbox_px=(500, 100, 600, 300), width_m=0.82, height_m=2.1,
        label="D01", confidence=0.8,
    )
    enriched, unmatched = correlate_elevation_to_plan([e1, e2], [plan])
    assert len(unmatched) == 2, "tied scores must stay unmatched"
    assert enriched[0].height_m is None

    with patch(
        "pb_plan_opening_detection_v171.plan_opening_candidates",
        return_value=_mock_b1([plan]),
    ):
        payload = prod.run_p5_native_payload(
            {"segments": [], "words": []}, page_no=1,
            scale_info={"px_per_m": 28.3},
            elevation_openings=[e1, e2],
        )
    inst = payload["instances"][0]
    assert inst["elevation_geometry"] is None
    assert inst["deduction_status"] == DEDUCTION_REVIEW
    assert inst["deduct"] is False


# ---------------------------------------------------------------------------
# 6. Elevation-only evidence cannot set deduct=True
# ---------------------------------------------------------------------------
def test_elevation_only_evidence_cannot_set_deduct():
    """Height from elevation is not enough: basis stays unknown -> B5 refuses."""
    plan = _b1_candidate(
        mark="D01", wall="W01", position=1.5, width=0.82,
        opening_type=OPENING_TYPE_DOOR, geom_conf=0.95, assoc_conf=0.95,
    )
    plan.elevation_side = "North"
    elev = ElevationOpening(
        elevation_page_no=3, elevation_side="North",
        bbox_px=(100, 100, 200, 300), width_m=0.82, height_m=2.1,
        label="D01", confidence=0.9,
    )
    with patch(
        "pb_plan_opening_detection_v171.plan_opening_candidates",
        return_value=_mock_b1([plan]),
    ):
        payload = prod.run_p5_native_payload(
            {"segments": [], "words": []}, page_no=1,
            scale_info={"px_per_m": 28.3},
            elevation_openings=[elev],
        )
    inst = payload["instances"][0]
    assert "elevation_rect" in [o.get("source") for o in inst["source_observations"]]
    assert inst["height_m"] == pytest.approx(2.1, abs=0.01)  # elevation MEASURED height
    assert inst["dimension_basis"] == DIMENSION_BASIS_UNKNOWN  # no rough opening
    assert inst["deduct"] is False  # B5 gate rejects elevation-only evidence
    assert inst["deduction_status"] == DEDUCTION_REVIEW


# ---------------------------------------------------------------------------
# 7. Elevation-only evidence cannot create a physical opening instance
# ---------------------------------------------------------------------------
def test_elevation_only_evidence_cannot_create_instances():
    """B3 enriches only existing instances; elevation never spawns one."""
    plan = _b1_candidate(
        mark="D01", wall="W01", position=1.5, width=0.82,
        opening_type=OPENING_TYPE_DOOR, geom_conf=0.9, assoc_conf=0.85,
    )
    plan.elevation_side = "North"
    elev = ElevationOpening(
        elevation_page_no=3, elevation_side="North",
        bbox_px=(100, 100, 200, 300), width_m=0.82, height_m=2.1,
        label="D01", confidence=0.8,
    )
    unrelated = ElevationOpening(
        elevation_page_no=3, elevation_side="South",
        bbox_px=(900, 100, 1000, 300), width_m=5.0, height_m=5.0,
        label="", confidence=0.5,
    )
    with patch(
        "pb_plan_opening_detection_v171.plan_opening_candidates",
        return_value=_mock_b1([plan]),
    ):
        payload = prod.run_p5_native_payload(
            {"segments": [], "words": []}, page_no=1,
            scale_info={"px_per_m": 28.3},
            elevation_openings=[elev, unrelated],
        )
    assert len(payload["instances"]) == 1, "instance count unchanged after correlation"

    # Even with NO plan candidate at all, elevation cannot spawn an instance.
    with patch(
        "pb_plan_opening_detection_v171.plan_opening_candidates",
        return_value=_mock_b1([]),
    ):
        empty = prod.run_p5_native_payload(
            {"segments": [], "words": []}, page_no=1,
            scale_info={"px_per_m": 28.3},
            elevation_openings=[elev],
        )
    assert empty["instances"] == []


# ---------------------------------------------------------------------------
# 8. No-elevation default is a no-op (existing B0-B5 behaviour unchanged)
# ---------------------------------------------------------------------------
def test_no_elevation_default_is_noop():
    """elevation_openings=None/[] must produce the exact pre-Phase-2B result."""
    plan = _b1_candidate(
        mark="D01", wall="W01", position=1.5, width=0.82,
        opening_type=OPENING_TYPE_DOOR, geom_conf=0.95, assoc_conf=0.95,
    )
    native = {"segments": [], "words": []}
    with patch(
        "pb_plan_opening_detection_v171.plan_opening_candidates",
        return_value=_mock_b1([plan]),
    ):
        default = prod.run_p5_native_payload(native, page_no=1, scale_info={"px_per_m": 28.3})
        explicit_empty = prod.run_p5_native_payload(
            native, page_no=1, scale_info={"px_per_m": 28.3},
            elevation_openings=[], elevation_diagnostics=[],
        )
    assert default["instances"] == explicit_empty["instances"]
    assert default["pipeline_notes"] == explicit_empty["pipeline_notes"]
    assert default["deducted_area_m2"] == explicit_empty["deducted_area_m2"]
    # The new additive fields exist and are empty.
    assert default["elevation_openings"] == []
    assert default["elevation_diagnostics"] == []
    # No B3 stage ran for pages without elevation evidence.
    assert not any(str(n).startswith("B3:") for n in default["pipeline_notes"])
    # Instances reflect only B1 -> B4 -> B5 (deduction_status review, no deduct).
    inst = default["instances"][0]
    assert inst["deduct"] is False
    assert inst["deduction_status"] == DEDUCTION_REVIEW


# ---------------------------------------------------------------------------
# analyse_stored_page_openings threads elevation evidence too
# ---------------------------------------------------------------------------
class _FakePage:
    def get_text(self, kind: str = "words"):
        return []


class _FakePdf:
    page_count = 1

    def load_page(self, idx: int):
        return _FakePage()

    def close(self):
        pass


class _FakeFitz:
    def open(self, path):
        return _FakePdf()


def test_analyse_stored_page_openings_threads_elevation():
    """The native-bridge path also accepts and threads elevation openings."""
    cal, fx = _real_render_pixel_calibration()
    image = _load_real_crop()
    cands = detect_raster_rect_candidates(
        image, cal,
        source_filename=fx["source"]["local_source_alias"],
        source_page=86,
        drawing_ref=fx["source"]["drawing_no"],
        elevation_side=fx["source"]["elevation_side"],
        calibration_source=cal.method,
    )
    opening_cands = [c for c in cands if opening_sized(c.width_m, c.height_m)]
    best = min(opening_cands, key=lambda c: abs(c.width_m - 2.96) + abs(c.height_m - 1.57))
    result = bridge.raster_openings_from_candidates(
        [best], cal,
        elevation_page_no=86, elevation_side=fx["source"]["elevation_side"],
        source_filename=fx["source"]["local_source_alias"], source_page=86,
        drawing_ref=fx["source"]["drawing_no"],
        drawing_title=fx["source"]["drawing_title"],
        level=None, wall_ref="E01", calibration_source=cal.method,
    )
    opening = result.openings[0]
    assert opening.wall_ref == "E01"

    plan = _b1_candidate(
        mark="", wall="E01", position=2.0, width=opening.width_m,
        opening_type=OPENING_TYPE_WINDOW, geom_conf=0.9, assoc_conf=0.85,
    )
    plan.elevation_side = "East"
    # Production strict identity for the real facade cell: BOTH sides carry a
    # STRUCTURED registration-derived position record in the shared E01 frame.
    _attach_reg_position(plan, _reg_pos("E01", plan.position_along_wall_m))
    _attach_reg_position(opening, _reg_pos("E01", plan.position_along_wall_m))

    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    Path(path).write_bytes(b"%PDF-1.4\n%fake elevation-source fixture for bridge test\n")
    try:
        app = SimpleNamespace(
            fitz=_FakeFitz(),
            lquery=lambda sql, params=(): [{
                "workspace_id": 9, "page_no": 2, "path": str(path),
                "px_per_m": 0.0, "render_zoom": 1.0,
            }],
            extract_native_page_v130=lambda page: {"segments": [], "words": []},
        )
        with patch(
            "pb_plan_opening_detection_v171.plan_opening_candidates",
            return_value=_mock_b1([plan]),
        ):
            payload = prod.analyse_stored_page_openings(
                app, 5, {"scale": {"px_per_m": cal.px_per_m}},
                elevation_openings=result.openings,
                elevation_diagnostics=result.diagnostics,
                elevation_provenance=result.opening_provenance,
                facade_registration=_register_facades(),
            )
        assert payload["status"] == "ok"
        inst = payload["instances"][0]
        assert "elevation_rect" in [o.get("source") for o in inst["source_observations"]]
        assert inst["elevation_geometry"] is not None
        assert inst["deduct"] is False
        assert payload["elevation_openings"]
        assert any(d.get("kind") == "b3_correlation" for d in payload["elevation_diagnostics"])
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_bridge_accepts_textword_dataclass_words():
    """Words from the plan-vector vocabulary (``TextWord`` dataclasses) are
    normalised to the native ``{"text", "bbox"}`` format, so scale-aware label
    lookup still works instead of crashing the seam."""
    from pb_plan_opening_detection_v171 import TextWord

    cal, _ = _real_render_pixel_calibration()
    cand = _raster_candidate(width_m=0.82, height_m=2.1)
    # Rect bbox (2125,1439)-(2330,1500); a TextWord label centred INSIDE the
    # rectangle must be found at distance 0 by the scale-aware label search.
    word = TextWord(text="W01", x0=2210.0, y0=1462.0, x1=2290.0, y1=1478.0, page_no=86)
    result = bridge.raster_openings_from_candidates(
        [cand], cal,
        elevation_page_no=86, elevation_side="East",
        words=[word],
        drawing_ref="CD3001",
    )
    assert len(result.openings) == 1
    opening = result.openings[0]
    assert opening.coord_space == COORD_SPACE_RENDER_PIXEL
    assert opening.label == "W01"
    # Dimensional size is measured from the rect in the calibration space;
    # the bridge never fabricates metres (4-decimal rounding allowance).
    assert opening.width_m == pytest.approx(205.0 / cal.units_per_m, abs=1e-3)
    assert opening.height_m == pytest.approx(61.0 / cal.units_per_m, abs=1e-3)


# ---------------------------------------------------------------------------
# C1 — Level provenance: a candidate WITHOUT an objective level must not
# erase a valid caller-supplied page/elevation level.  The three-way
# distinction is: objective candidate level > caller page/elevation level >
# unknown/None — and None is never emitted as an explicit level that shadows
# a real caller level.
# ---------------------------------------------------------------------------
def _pdf_calibration():
    """A proven dimensional pdf_point calibration (28.346 pt/m)."""
    return Calibration(
        units_per_m=28.346, coord_space=COORD_SPACE_PDF_POINT, valid=True,
        method="graphic_scale_bar", source_page=86, confidence=0.95,
        review_status="accepted",
        notes=["synthetic pdf_point calibration for C1 vector path"],
    )


def _vector_candidate(width_m=1.0, height_m=2.1, **extra):
    """A synthetic pdf_point vector candidate (no objective level band)."""
    dx = width_m * 28.346
    dy = height_m * 28.346
    base = dict(
        source_filename="synthetic.pdf", source_page=86,
        drawing_ref="CD3001", elevation_side="East",
        bbox=(1100.0, 1300.0, 1100.0 + dx, 1300.0 + dy),
        centroid=(1100.0 + dx / 2.0, 1300.0 + dy / 2.0),
        coord_space=COORD_SPACE_PDF_POINT,
        calibration_method="graphic_scale_bar",
        width_m=width_m, height_m=height_m,
        dimension_basis="unknown",
        extraction_method="vector_line_closure",
        geometry_confidence=0.7,
        review_status="review",
        notes=["synthetic C1/C2 test candidate"],
    )
    base.update(extra)
    return VectorRectCandidate(**base)


def test_c1_caller_level_preserved_when_vector_candidate_has_no_objective_level():
    """A vector candidate (no objective level) must NOT erase the caller level.

    Pre-C1 the bridge emitted ``"level": None`` in the mapped rect dict, and
    ``detect_elevation_openings`` used ``rect.get("level", level)`` — so a
    valid caller-supplied ``level="L2"`` was discarded and the opening came out
    with level ``None``.  Post-C1 the level key is OMITTED when there is no
    objective level, so the caller level survives onto the opening.
    """
    result = bridge.vector_openings_from_candidates(
        [_vector_candidate()], _pdf_calibration(),
        elevation_page_no=86, elevation_side="East",
        drawing_ref="CD3001", drawing_title="E1 EAST ELEVATION",
        level="L2",
    )
    assert len(result.openings) == 1
    assert result.openings[0].level == "L2", (
        "caller-supplied level must survive when the candidate has no objective level"
    )


def test_c1_raster_caller_level_preserved_and_objective_level_wins():
    """Raster path: caller level preserved when no objective level; objective
    level wins over the caller level when present (three-way distinction)."""
    cal, _ = _real_render_pixel_calibration()

    # No objective level band (level_band=None) -> caller level survives.
    cand_blank = _raster_candidate(width_m=0.82, height_m=2.1, level_band=None)
    r1 = bridge.raster_openings_from_candidates(
        [cand_blank], cal,
        elevation_page_no=86, elevation_side="East", drawing_ref="CD3001",
        level="L2",
    )
    assert len(r1.openings) == 1
    assert r1.openings[0].level == "L2", (
        "raster candidate without an objective level must keep the caller level"
    )

    # Objective level band present -> objective level wins over caller level.
    cand_obj = _raster_candidate(width_m=0.82, height_m=2.1, level_band="L1")
    r2 = bridge.raster_openings_from_candidates(
        [cand_obj], cal,
        elevation_page_no=86, elevation_side="East", drawing_ref="CD3001",
        level="L2",
    )
    assert len(r2.openings) == 1
    assert r2.openings[0].level == "L1", (
        "objectively-derived candidate level must win over the caller level"
    )

    # No objective level and no caller level -> unknown (None), never a shadow.
    cand_none = _raster_candidate(width_m=0.82, height_m=2.1, level_band=None)
    r3 = bridge.raster_openings_from_candidates(
        [cand_none], cal,
        elevation_page_no=86, elevation_side="East", drawing_ref="CD3001",
        level=None,
    )
    assert len(r3.openings) == 1
    assert r3.openings[0].level is None


def test_c1_wrong_level_rejected_through_production_vector_path():
    """Known plan L1 vs preserved elevation L2 -> wrong-level REJECT.

    Exercises C1's level fix end-to-end: the bridge preserves the caller level
    (L2) onto the elevation opening, and the production strict correlation
    rejects the L1-vs-L2 pair (no positive match / no enrichment).
    """
    result = bridge.vector_openings_from_candidates(
        [_vector_candidate()], _pdf_calibration(),
        elevation_page_no=86, elevation_side="East",
        drawing_ref="CD3001", drawing_title="E1 EAST ELEVATION",
        level="L2",
    )
    assert len(result.openings) == 1
    elev = result.openings[0]
    assert elev.level == "L2"

    plan = _b1_candidate(
        mark="D01", wall="W01", position=1.5, width=1.0,
        opening_type=OPENING_TYPE_DOOR, geom_conf=0.9, assoc_conf=0.85,
    )
    plan.level = "L1"
    # Give the opening an exact mark so LEVEL is the ONLY reason it must reject.
    elev2 = ElevationOpening(
        elevation_page_no=86, elevation_side="East",
        bbox_px=(100, 100, 200, 300), width_m=1.0, height_m=2.1,
        label="D01", confidence=0.8, level="L2",
    )
    enriched, unmatched = bridge.correlate_elevation_to_plan_production(
        [elev2], [plan],
    )
    assert len(unmatched) == 1, "wrong level must stay unmatched"
    assert enriched[0].elevation_geometry is None, "no enrichment on wrong level"


def test_c1_unknown_level_is_neutral_and_never_a_positive_signal():
    """An unknown level is NEUTRAL: it never rejects and never, by itself,
    becomes a positive match signal.

    - Exact-mark + width with both levels unknown -> MAY correlate (level is
      neutral, not a reject); the correlation is carried by the mark anchor.
    - No strong anchor + unknown level -> NEVER correlates (level never becomes
      identity on its own).
    - Unknown elevation level vs known plan level -> never a positive signal on
      its own; without a strong anchor it stays unmatched.
    """
    # Case A: exact mark + width, both levels unknown -> correlates via mark.
    plan_a = _b1_candidate(
        mark="D01", wall="W01", position=1.5, width=0.82,
        opening_type=OPENING_TYPE_DOOR, geom_conf=0.9, assoc_conf=0.85,
    )
    elev_a = ElevationOpening(
        elevation_page_no=3, elevation_side="North",
        bbox_px=(100, 100, 200, 300), width_m=0.82, height_m=2.1,
        label="D01", confidence=0.8, level=None,
    )
    enriched_a, unmatched_a = bridge.correlate_elevation_to_plan_production(
        [elev_a], [plan_a],
    )
    assert len(unmatched_a) == 0
    assert enriched_a[0].elevation_geometry is not None

    # Case B: no strong anchor + unknown level -> never a positive signal.
    plan_b = _b1_candidate(
        mark="", wall="W01", position=1.5, width=0.82,
        opening_type=OPENING_TYPE_DOOR, geom_conf=0.9, assoc_conf=0.85,
    )
    plan_b.elevation_side = "North"
    elev_b = ElevationOpening(
        elevation_page_no=3, elevation_side="North",
        bbox_px=(100, 100, 200, 300), width_m=0.82, height_m=2.1,
        label="", confidence=0.8, level=None,
    )
    enriched_b, unmatched_b = bridge.correlate_elevation_to_plan_production(
        [elev_b], [plan_b],
    )
    assert len(unmatched_b) == 1, "side+width+unknown-level must not match"
    assert enriched_b[0].elevation_geometry is None


# ---------------------------------------------------------------------------
# C2 — Production correlation identity: side+width alone is NEVER sufficient.
# Only a stronger anchor (exact mark / validated position / proven unique
# signal) may pair a plan instance with an elevation opening; ties without a
# stronger anchor fail closed to unmatched/review.
# ---------------------------------------------------------------------------
def test_c2_two_same_width_same_side_without_strong_anchor_is_ambiguous():
    """Two same-width openings on the same side with NO stronger identity →
    production correlation must leave them ambiguous/unmatched."""
    plan = _b1_candidate(
        mark="", wall="W01", position=3.0, width=1.0,
        opening_type=OPENING_TYPE_WINDOW, geom_conf=0.9, assoc_conf=0.85,
    )
    plan.elevation_side = "North"
    e1 = ElevationOpening(
        elevation_page_no=3, elevation_side="North",
        bbox_px=(100, 100, 200, 300), width_m=1.0, height_m=1.5,
        label="", confidence=0.7,
    )
    e2 = ElevationOpening(
        elevation_page_no=3, elevation_side="North",
        bbox_px=(500, 100, 600, 300), width_m=1.0, height_m=1.5,
        label="", confidence=0.7,
    )
    enriched, unmatched = bridge.correlate_elevation_to_plan_production(
        [e1, e2], [plan],
    )
    assert len(unmatched) == 2, "side+width without a strong anchor -> unmatched"
    assert enriched[0].elevation_geometry is None

    with patch(
        "pb_plan_opening_detection_v171.plan_opening_candidates",
        return_value=_mock_b1([plan]),
    ):
        payload = prod.run_p5_native_payload(
            {"segments": [], "words": []}, page_no=1,
            scale_info={"px_per_m": 28.3},
            elevation_openings=[e1, e2],
        )
    inst = payload["instances"][0]
    assert inst["elevation_geometry"] is None
    assert inst["deduction_status"] == DEDUCTION_REVIEW


def test_c2_blank_mark_side_width_alone_does_not_correlate():
    """REPLACES the pre-C2 production behaviour where blank-mark + side + width
    was enough to correlate.  In production it must NOT correlate."""
    plan = _b1_candidate(
        mark="", wall="E1", position=2.0, width=1.0,
        opening_type=OPENING_TYPE_WINDOW, geom_conf=0.9, assoc_conf=0.85,
    )
    plan.elevation_side = "East"
    elev = ElevationOpening(
        elevation_page_no=86, elevation_side="East",
        bbox_px=(100, 100, 200, 300), width_m=1.0, height_m=1.5,
        label="", confidence=0.7,
    )
    enriched, unmatched = bridge.correlate_elevation_to_plan_production(
        [elev], [plan],
    )
    assert len(unmatched) == 1, "blank mark + side + width must NOT correlate"
    assert enriched[0].elevation_geometry is None


def test_c2_exact_mark_plus_width_may_correlate():
    """Exact compatible opening mark + width -> strong anchor -> MAY correlate."""
    plan = _b1_candidate(
        mark="D01", wall="W01", position=1.5, width=0.82,
        opening_type=OPENING_TYPE_DOOR, geom_conf=0.9, assoc_conf=0.85,
    )
    plan.elevation_side = "North"
    elev = ElevationOpening(
        elevation_page_no=3, elevation_side="North",
        bbox_px=(100, 100, 200, 300), width_m=0.82, height_m=2.1,
        label="D01", confidence=0.8,
    )
    enriched, unmatched = bridge.correlate_elevation_to_plan_production(
        [elev], [plan],
    )
    assert len(unmatched) == 0
    assert enriched[0].elevation_geometry is not None
    assert enriched[0].height_m == pytest.approx(2.1, abs=0.01)


def test_c2_footprint_validated_position_plus_width_may_correlate():
    """Shared registered frame + STRUCTURED registration-derived stations ->
    MAY correlate.

    The position anchor is GENUINE only when BOTH the plan and the elevation
    carry a structured registration-derived position record (same registered
    E01/East frame identity, origin, direction, and an agreeing station) backed
    by the calibrated ``footprint_facades`` registration.  A pair that agrees
    on raw in-range scalars alone, or falls outside the shared frame, must NOT
    anchor.
    """
    plan = _b1_candidate(
        mark="", wall="E01", position=2.0, width=1.0,
        opening_type=OPENING_TYPE_WINDOW, geom_conf=0.9, assoc_conf=0.85,
    )
    plan.elevation_side = "East"
    elev = ElevationOpening(
        elevation_page_no=86, elevation_side="East",
        bbox_px=(100, 100, 200, 300), width_m=1.0, height_m=1.5,
        label="", confidence=0.7,
        wall_ref="E01",
    )
    # Both sides carry a STRUCTURED registration-derived record in the shared
    # E01 frame (origin (0,0), direction (1,0), segment fp-1:E01).
    _attach_reg_position(plan, _reg_pos("E01", plan.position_along_wall_m))
    _attach_reg_position(elev, _reg_pos("E01", plan.position_along_wall_m))
    enriched, unmatched = bridge.correlate_elevation_to_plan_production(
        [elev], [plan], facades=_register_facades(),
    )
    assert len(unmatched) == 0
    assert enriched[0].elevation_geometry is not None

    # A position outside station tolerance must NOT anchor (wrong location),
    # even as a fully-structured pair in the same frame.
    out_tol = ElevationOpening(
        elevation_page_no=86, elevation_side="East",
        bbox_px=(100, 100, 200, 300), width_m=1.0, height_m=1.5,
        label="", confidence=0.7,
        wall_ref="E01",
    )
    _attach_reg_position(plan, _reg_pos("E01", 2.0))
    _attach_reg_position(out_tol, _reg_pos("E01", 2.0 + 5.0))
    enriched2, unmatched2 = bridge.correlate_elevation_to_plan_production(
        [out_tol], [plan], facades=_register_facades(),
    )
    assert len(unmatched2) == 1, "mismatched position must not correlate"
    _attach_reg_position(plan, _reg_pos("E01", 2.0))  # reset plan record

    # A structured station OUTSIDE the registered facade extent does NOT anchor
    # (the footprint-derived length limits where a position is valid).
    far_elev = ElevationOpening(
        elevation_page_no=86, elevation_side="East",
        bbox_px=(100, 100, 200, 300), width_m=1.0, height_m=1.5,
        label="", confidence=0.7,
        wall_ref="E01",
    )
    far_plan = _b1_candidate(
        mark="", wall="E01", position=12.0, width=1.0,
        opening_type=OPENING_TYPE_WINDOW, geom_conf=0.9, assoc_conf=0.85,
    )
    far_plan.elevation_side = "East"
    _attach_reg_position(far_plan, _reg_pos("E01", 12.0))
    _attach_reg_position(far_elev, _reg_pos("E01", 12.0))
    enriched3, unmatched3 = bridge.correlate_elevation_to_plan_production(
        [far_elev], [far_plan], facades=_register_facades(),
    )
    assert len(unmatched3) == 1, "station off the registered extent must not correlate"


def test_r2_bare_attached_anchors_are_not_enough():
    """R2: arbitrary caller-attached identity/position attributes NEVER anchor.

    A truthy ``identity_anchor`` or a bare ``wall_position_m`` — with no exact
    mark and no footprint-registered wall_ref/side (and no registered facade
    at all) — is NOT a genuinely validated anchor, so the pair must stay
    unmatched/review (fail closed).
    """
    plan = _b1_candidate(
        mark="", wall="E01", position=2.0, width=1.0,
        opening_type=OPENING_TYPE_WINDOW, geom_conf=0.9, assoc_conf=0.85,
    )
    plan.elevation_side = "East"

    # (1) Bare wall_position_m with NO registered facades at all -> no anchor.
    bare_pos = ElevationOpening(
        elevation_page_no=86, elevation_side="East",
        bbox_px=(100, 100, 200, 300), width_m=1.0, height_m=1.5,
        label="", confidence=0.7,
    )
    object.__setattr__(bare_pos, "wall_position_m", plan.position_along_wall_m)
    enriched, unmatched = bridge.correlate_elevation_to_plan_production(
        [bare_pos], [plan],
    )
    assert len(unmatched) == 1, "bare station without facades must not anchor"
    assert enriched[0].elevation_geometry is None

    # (2) Facades present but the wall_ref is UNREGISTERED (and no mark) ->
    #     still no genuine position anchor.
    unreg = ElevationOpening(
        elevation_page_no=86, elevation_side="East",
        bbox_px=(100, 100, 200, 300), width_m=1.0, height_m=1.5,
        label="", confidence=0.7,
        wall_ref="E99",
    )
    object.__setattr__(unreg, "wall_position_m", plan.position_along_wall_m)
    enriched2, unmatched2 = bridge.correlate_elevation_to_plan_production(
        [unreg], [plan], facades=_register_facades(),
    )
    assert len(unmatched2) == 1, "unregistered wall_ref must not anchor"
    assert enriched2[0].elevation_geometry is None

    # (3) Truthy identity_anchor ONLY (no mark, no registered position) -> the
    #     permissive unique-signal free pass is removed in production.
    unique_only = ElevationOpening(
        elevation_page_no=86, elevation_side="East",
        bbox_px=(100, 100, 200, 300), width_m=1.0, height_m=1.5,
        label="", confidence=0.7,
        wall_ref="E01",
    )
    object.__setattr__(unique_only, "identity_anchor", "schedule-LAGO-D01")
    object.__setattr__(unique_only, "wall_position_m", 99.0)  # disagrees anyway
    enriched3, unmatched3 = bridge.correlate_elevation_to_plan_production(
        [unique_only], [plan], facades=_register_facades(),
    )
    assert len(unmatched3) == 1, "identity_anchor alone must not anchor"
    assert enriched3[0].elevation_geometry is None

    # (4) Sanity: the SAME evidence with a non-blank EXACT mark DOES correlate,
    #     proving the gate rejects the bare attributes, not the pair itself.
    marked = ElevationOpening(
        elevation_page_no=86, elevation_side="East",
        bbox_px=(100, 100, 200, 300), width_m=0.82, height_m=2.1,
        label="D01", confidence=0.7,
    )
    plan2 = _b1_candidate(
        mark="D01", wall="E01", position=2.0, width=0.82,
        opening_type=OPENING_TYPE_DOOR, geom_conf=0.9, assoc_conf=0.85,
    )
    plan2.elevation_side = "East"
    enriched4, unmatched4 = bridge.correlate_elevation_to_plan_production(
        [marked], [plan2], facades=_register_facades(),
    )
    assert len(unmatched4) == 0
    assert enriched4[0].elevation_geometry is not None


def test_r2_wrong_wall_never_anchors():
    """R2: MISMATCHED plan/elevation wall references never anchor — even when
    the attached ``wall_position_m`` numerically lies within a registered extent.

    The elevation opening sits on registered East wall ``E01`` (extent 0..8)
    with an in-extent station, but the PLAN instance is on a DIFFERENT wall
    (``W02``).  Without a MATCHING plan wall_ref, the position cannot be a
    registration-derived anchor: same stations on different walls are two
    different physical openings, so the pair fails closed to review.
    """
    # Plan on West wall W02; elevation on East wall E01 (registered, extent 0..8).
    plan = _b1_candidate(
        mark="", wall="W02", position=2.0, width=1.0,
        opening_type=OPENING_TYPE_WINDOW, geom_conf=0.9, assoc_conf=0.85,
    )
    plan.elevation_side = "West"
    elev = ElevationOpening(
        elevation_page_no=86, elevation_side="East",
        bbox_px=(100, 100, 200, 300), width_m=1.0, height_m=1.5,
        label="", confidence=0.7,
        wall_ref="E01",
    )
    # The attached station (2.0) is well WITHIN E01's registered extent — but
    # the plan is on a different wall, so this must NOT qualify by itself.
    object.__setattr__(elev, "wall_position_m", 2.0)
    enriched, unmatched = bridge.correlate_elevation_to_plan_production(
        [elev], [plan], facades=_register_facades(),
    )
    assert len(unmatched) == 1, "wrong wall must never anchor (wrong-wall regression)"
    assert enriched[0].elevation_geometry is None


def test_r2_unproven_position_never_anchors():
    """R2: an attached in-extent ``wall_position_m`` does NOT qualify BY ITSELF —
    it also needs a REGISTRATION-DERIVED plan station on the same segment.

    Plan and elevation share registered East wall ``E01`` and the attached
    station (2.0) is inside the 0..8 extent — but the plan carries NO
    ``position_along_wall_m`` (its position is UNPROVEN).  There is no derived
    plan station to agree with, so the opening's own number is just an
    arbitrary caller-attached scalar and must fail closed to ambiguity/review.
    """
    plan = _b1_candidate(
        mark="", wall="E01", width=1.0,
        opening_type=OPENING_TYPE_WINDOW, geom_conf=0.9, assoc_conf=0.85,
    )
    plan.elevation_side = "East"
    # Deliberately drop the plan's position: it is NOT registration-derived.
    plan.position_along_wall_m = None
    elev = ElevationOpening(
        elevation_page_no=86, elevation_side="East",
        bbox_px=(100, 100, 200, 300), width_m=1.0, height_m=1.5,
        label="", confidence=0.7,
        wall_ref="E01",
    )
    object.__setattr__(elev, "wall_position_m", 2.0)  # in E01 extent 0..8
    enriched, unmatched = bridge.correlate_elevation_to_plan_production(
        [elev], [plan], facades=_register_facades(),
    )
    assert len(unmatched) == 1, (
        "attached in-extent position must not qualify without a derived plan station"
    )
    assert enriched[0].elevation_geometry is None


def test_r2_registration_derived_position_still_correlates():
    """R2 regression (positive): a genuinely registration-derived pair in the
    SAME registered frame DOES anchor.

    Both plan and elevation carry a structured registration-derived position
    record for the shared E01/East frame — same segment identity ``fp-1:E01``,
    same origin ``(0,0)``, same direction ``(1,0)``, agreeing station 2.0,
    derivation source ``facade_registration`` — so the pair shares a genuine
    physical location and correlates.  This is the positive counterpart to the
    raw-scalar and different-frame regressions.
    """
    plan = _b1_candidate(
        mark="", wall="E01", position=2.0, width=1.0,
        opening_type=OPENING_TYPE_WINDOW, geom_conf=0.9, assoc_conf=0.85,
    )
    plan.elevation_side = "East"
    elev = ElevationOpening(
        elevation_page_no=86, elevation_side="East",
        bbox_px=(100, 100, 200, 300), width_m=1.0, height_m=1.5,
        label="", confidence=0.7,
        wall_ref="E01",
    )
    _attach_reg_position(plan, _reg_pos("E01", 2.0))
    _attach_reg_position(elev, _reg_pos("E01", 2.0))
    enriched, unmatched = bridge.correlate_elevation_to_plan_production(
        [elev], [plan], facades=_register_facades(),
    )
    assert len(unmatched) == 0
    assert enriched[0].elevation_geometry is not None


def test_r2_matching_wall_in_range_raw_scalars_reject():
    """R2 regression: matching wall refs + arbitrary IN-RANGE raw scalars reject.

    The plan and elevation share wall_ref ``E01``, the raw plan station and the
    dynamically attached ``wall_position_m`` are BOTH numerically inside the
    registered 0..8 extent and agree exactly — but neither side carries a
    STRUCTURED registration-derived position record.  Range-checking two raw
    scalars does NOT establish registration provenance or a shared origin/
    direction, so the pair must FAIL CLOSED (ambiguous -> review).
    """
    plan = _b1_candidate(
        mark="", wall="E01", position=2.0, width=1.0,
        opening_type=OPENING_TYPE_WINDOW, geom_conf=0.9, assoc_conf=0.85,
    )
    plan.elevation_side = "East"
    elev = ElevationOpening(
        elevation_page_no=86, elevation_side="East",
        bbox_px=(100, 100, 200, 300), width_m=1.0, height_m=1.5,
        label="", confidence=0.7,
        wall_ref="E01",
    )
    # Raw scalars only: matching refs, in-range, agreeing values, yet NO
    # registration-derived record on either side.
    object.__setattr__(elev, "wall_position_m", plan.position_along_wall_m)
    enriched, unmatched = bridge.correlate_elevation_to_plan_production(
        [elev], [plan], facades=_register_facades(),
    )
    assert len(unmatched) == 1, (
        "raw in-range scalars must not establish a validated position anchor"
    )
    assert enriched[0].elevation_geometry is None


def test_r2_different_registration_origin_or_direction_rejects():
    """R2 regression: DIFFERENT registration origins or directions reject — the
    same station in two different frames is NOT a shared physical position.

    Both sides carry structured records with the same station 2.0 and the same
    wall_ref, but one claims a different REGISTERED frame than the other —
    instead of the segment's true origin (0,0)/direction (1,0) a fabricated
    frame is supplied.  Without the SAME registered origin and direction the
    two records are not derived in a common frame, so the pair must reject.
    """
    plan = _b1_candidate(
        mark="", wall="E01", position=2.0, width=1.0,
        opening_type=OPENING_TYPE_WINDOW, geom_conf=0.9, assoc_conf=0.85,
    )
    plan.elevation_side = "East"
    elev = ElevationOpening(
        elevation_page_no=86, elevation_side="East",
        bbox_px=(100, 100, 200, 300), width_m=1.0, height_m=1.5,
        label="", confidence=0.7,
        wall_ref="E01",
    )
    # Plan is genuinely in the registered E01 frame.
    _attach_reg_position(plan, _reg_pos("E01", 2.0))

    # (a) Elevation claims a DIFFERENT ORIGIN (translated frame) for the same
    #     wall/station.
    elev_wrong_origin = ElevationOpening(
        elevation_page_no=86, elevation_side="East",
        bbox_px=(100, 100, 200, 300), width_m=1.0, height_m=1.5,
        label="", confidence=0.7, wall_ref="E01",
    )
    _attach_reg_position(
        elev_wrong_origin,
        _reg_pos("E01", 2.0, origin=(100.0, 0.0)),  # NOT the registered origin
    )
    enriched_a, unmatched_a = bridge.correlate_elevation_to_plan_production(
        [elev_wrong_origin], [plan], facades=_register_facades(),
    )
    assert len(unmatched_a) == 1, "different origin must reject (different frame)"

    # (b) Elevation claims a DIFFERENT DIRECTION (rotated frame) for the same
    #     wall/station.
    elev_wrong_dir = ElevationOpening(
        elevation_page_no=86, elevation_side="East",
        bbox_px=(100, 100, 200, 300), width_m=1.0, height_m=1.5,
        label="", confidence=0.7, wall_ref="E01",
    )
    _attach_reg_position(
        elev_wrong_dir,
        _reg_pos("E01", 2.0, direction=(0.0, 1.0)),  # NOT the registered direction
    )
    enriched_b, unmatched_b = bridge.correlate_elevation_to_plan_production(
        [elev_wrong_dir], [plan], facades=_register_facades(),
    )
    assert len(unmatched_b) == 1, "different direction must reject (different frame)"


def test_c2_wrong_mark_rejects():
    """Wrong opening mark -> reject (no correlation even with width/side)."""
    plan = _b1_candidate(
        mark="D01", wall="W01", position=1.5, width=0.82,
        opening_type=OPENING_TYPE_DOOR, geom_conf=0.9, assoc_conf=0.85,
    )
    plan.elevation_side = "North"
    elev = ElevationOpening(
        elevation_page_no=3, elevation_side="North",
        bbox_px=(100, 100, 200, 300), width_m=0.82, height_m=2.1,
        label="D02", confidence=0.8,  # D02 != D01
    )
    # Give a validated position so the ONLY disqualifier is the wrong mark.
    object.__setattr__(elev, "wall_position_m", plan.position_along_wall_m)
    enriched, unmatched = bridge.correlate_elevation_to_plan_production(
        [elev], [plan],
    )
    assert len(unmatched) == 1, "conflicting mark must reject"
    assert enriched[0].elevation_geometry is None


def test_c2_wrong_side_rejects():
    """Wrong elevation side -> reject (no correlation)."""
    plan = _b1_candidate(
        mark="D01", wall="W01", position=1.5, width=0.82,
        opening_type=OPENING_TYPE_DOOR, geom_conf=0.9, assoc_conf=0.85,
    )
    plan.elevation_side = "North"
    elev = ElevationOpening(
        elevation_page_no=3, elevation_side="South",  # wrong side
        bbox_px=(100, 100, 200, 300), width_m=0.82, height_m=2.1,
        label="D01", confidence=0.8,
    )
    object.__setattr__(elev, "wall_position_m", plan.position_along_wall_m)
    enriched, unmatched = bridge.correlate_elevation_to_plan_production(
        [elev], [plan],
    )
    assert len(unmatched) == 1, "different sides must reject"
    assert enriched[0].elevation_geometry is None


def test_c2_wrong_level_rejects():
    """Wrong known level -> reject (no correlation)."""
    plan = _b1_candidate(
        mark="D01", wall="W01", position=1.5, width=0.82,
        opening_type=OPENING_TYPE_DOOR, geom_conf=0.9, assoc_conf=0.85,
    )
    plan.level = "L1"
    plan.elevation_side = "North"
    elev = ElevationOpening(
        elevation_page_no=3, elevation_side="North",
        bbox_px=(100, 100, 200, 300), width_m=0.82, height_m=2.1,
        label="D01", confidence=0.8, level="L2",  # known L2 vs plan L1
    )
    object.__setattr__(elev, "wall_position_m", plan.position_along_wall_m)
    enriched, unmatched = bridge.correlate_elevation_to_plan_production(
        [elev], [plan],
    )
    assert len(unmatched) == 1, "different known levels must reject"
    assert enriched[0].elevation_geometry is None


# ---------------------------------------------------------------------------
# C3 — Source provenance survives into persisted elevation provenance.
# A reviewer must be able to trace accepted elevation evidence back to its
# original filename, page, drawing ref/title, coordinate space, calibration
# source+state, elevation side, and level (when known).  No invented
# provenance: every field is carried from the actual bridge inputs.
# ---------------------------------------------------------------------------
def test_c3_provenance_traceability_persisted_in_payload():
    """Accepted elevation evidence carries the full traceable provenance set.

    Asserts the production payload's persisted elevation diagnostics (and
    opening payload) expose: original source filename; source page; drawing
    ref/title; coordinate space; calibration source (method) and state
    (review_status/valid); elevation side; and level when known.
    """
    cal, fx = _real_render_pixel_calibration()
    image = _load_real_crop()
    cands = detect_raster_rect_candidates(
        image, cal,
        source_filename=fx["source"]["local_source_alias"],
        source_page=86,
        drawing_ref=fx["source"]["drawing_no"],
        elevation_side=fx["source"]["elevation_side"],
        calibration_source=cal.method,
    )
    opening_cands = [c for c in cands if opening_sized(c.width_m, c.height_m)]
    best = min(opening_cands, key=lambda c: abs(c.width_m - 2.96) + abs(c.height_m - 1.57))

    src_filename = "lago_cd3001_p86_e1east_roi_150dpi.png"
    drawing_title = "E1 EAST ELEVATION"
    result = bridge.raster_openings_from_candidates(
        [best], cal,
        elevation_page_no=86,
        elevation_side=fx["source"]["elevation_side"],
        source_filename=src_filename,
        source_page=86,
        drawing_ref=fx["source"]["drawing_no"],
        drawing_title=drawing_title,
        level="L2",
        wall_ref="E1",
        calibration_source=cal.method,
    )
    assert len(result.openings) >= 1
    opening = result.openings[0]
    assert opening.level == "L2"

    plan = _b1_candidate(
        mark="", wall="E01", position=2.0, width=opening.width_m,
        opening_type=OPENING_TYPE_WINDOW, geom_conf=0.9, assoc_conf=0.85,
    )
    plan.elevation_side = "East"
    object.__setattr__(opening, "wall_position_m", plan.position_along_wall_m)

    with patch(
        "pb_plan_opening_detection_v171.plan_opening_candidates",
        return_value=_mock_b1([plan]),
    ):
        payload = prod.run_p5_native_payload(
            {"segments": [], "words": []}, page_no=1,
            scale_info={"px_per_m": cal.px_per_m},
            elevation_openings=result.openings,
            elevation_diagnostics=result.diagnostics,
        )

    assert payload["status"] == "ok"
    assert any(d.get("kind") == "b3_correlation" for d in payload["elevation_diagnostics"])

    # --- The accepted-candidate diagnostic is fully traceable (C3) ---------
    accepted = [
        d for d in payload["elevation_diagnostics"]
        if d.get("kind") == "elevation_candidate" and d.get("status") == "accepted"
    ]
    assert accepted, "accepted candidate diagnostic must be persisted"
    d = accepted[0]
    # The real raster candidate carries its OWN source filename (the actual PDF
    # alias passed to the extractor) — it must survive into the diagnostic.
    assert d["source_filename"] == fx["source"]["local_source_alias"], (
        "original filename must survive"
    )
    assert d["source_page"] == 86, "source page must survive"
    assert d["drawing_ref"] == fx["source"]["drawing_no"], "drawing ref must survive"
    assert d["drawing_title"] == drawing_title, "drawing title must survive"
    assert d["coord_space"] == COORD_SPACE_RENDER_PIXEL, "coordinate space must survive"
    assert d["elevation_side"] == fx["source"]["elevation_side"], "elevation side must survive"
    assert d["level"] == "L2", "known level must survive"
    assert d["calibration"] and d["calibration"]["method"] == cal.method, (
        "calibration source must survive"
    )
    assert d["calibration"]["review_status"] == cal.review_status, (
        "calibration state must survive"
    )

    # --- The elevation opening payload carries the same traceable fields ----
    o = payload["elevation_openings"][0]
    assert o["level"] == "L2"
    assert o["coord_space"] == COORD_SPACE_RENDER_PIXEL
    assert o["drawing_ref"] == fx["source"]["drawing_no"]
    assert o["elevation_side"] == fx["source"]["elevation_side"]
    assert o["calibration"]["method"] == cal.method
    assert o["calibration"]["review_status"] == cal.review_status

    # --- The evidence summary is also traceable ------------------------------
    summary = [
        s for s in payload["elevation_diagnostics"]
        if s.get("kind") == "elevation_evidence_summary"
    ]
    assert summary and summary[0]["source_filename"] == src_filename


def test_r1_opening_provenance_is_index_aligned_and_consistent():
    """R1: persisted opening provenance is index-aligned with openings and
    NEVER disagrees with the accepted candidate's diagnostic provenance.

    Every accepted opening gets a provenance entry with the SAME resolved
    source (filename, page, drawing ref/title, coordinate space, calibration
    source+state, elevation side, level) as its accepted diagnostic — a single
    authoritative resolver, so the openings and diagnostics can never diverge.
    The production payload then persists ``elevation_provenance`` aligned with
    ``elevation_openings`` so a reviewer can trace each persisted opening to its
    original source.
    """
    cal, fx = _real_render_pixel_calibration()
    cands = detect_raster_rect_candidates(
        _load_real_crop(), cal,
        source_filename=fx["source"]["local_source_alias"],
        source_page=86,
        drawing_ref=fx["source"]["drawing_no"],
        elevation_side=fx["source"]["elevation_side"],
        calibration_source=cal.method,
    )
    opening_cands = [c for c in cands if opening_sized(c.width_m, c.height_m)]
    best = min(opening_cands, key=lambda c: abs(c.width_m - 2.96) + abs(c.height_m - 1.57))
    result = bridge.raster_openings_from_candidates(
        [best], cal,
        elevation_page_no=86,
        elevation_side=fx["source"]["elevation_side"],
        source_filename=fx["source"]["local_source_alias"],
        source_page=86,
        drawing_ref=fx["source"]["drawing_no"],
        drawing_title=fx["source"]["drawing_title"],
        level=None,
        wall_ref="E01",
        calibration_source=cal.method,
    )

    assert result.openings, "need at least one accepted opening"
    # Index-aligned: one provenance entry per opening.
    assert len(result.opening_provenance) == len(result.openings)

    accepted = [
        d for d in result.diagnostics
        if d.get("kind") == "elevation_candidate" and d.get("status") == "accepted"
    ]
    assert len(accepted) == len(result.openings)

    for opening, prov, diag in zip(
        result.openings, result.opening_provenance, accepted
    ):
        # The persisted provenance carries the real source traceability.
        assert prov["source_filename"] == fx["source"]["local_source_alias"]
        assert prov["source_page"] == 86
        assert prov["drawing_ref"] == fx["source"]["drawing_no"]
        assert prov["drawing_title"] == fx["source"]["drawing_title"]
        assert prov["coord_space"] == COORD_SPACE_RENDER_PIXEL
        assert prov["elevation_side"] == fx["source"]["elevation_side"]
        assert prov["calibration_source"]
        assert prov["calibration_state"]["method"] == cal.method
        # SINGLE authoritative resolver: the diagnostic's provenance is the
        # SAME as the persisted provenance for that opening.
        for key in ("source_filename", "source_page", "drawing_ref",
                    "drawing_title", "elevation_side", "coord_space",
                    "calibration_source"):
            assert prov[key] == diag[key], f"provenance mismatch on {key}"

    # --- Production payload persists elevation_provenance aligned ------------
    plan = _b1_candidate(
        mark="", wall="E01", position=2.0, width=result.openings[0].width_m,
        opening_type=OPENING_TYPE_WINDOW, geom_conf=0.9, assoc_conf=0.85,
    )
    plan.elevation_side = "East"
    # Genuine registration-derived position on BOTH sides so the pair has a
    # strong anchor (single authoritative provenance, reused for correlation).
    _attach_reg_position(plan, _reg_pos("E01", plan.position_along_wall_m))
    _attach_reg_position(
        result.openings[0], _reg_pos("E01", plan.position_along_wall_m),
    )
    with patch(
        "pb_plan_opening_detection_v171.plan_opening_candidates",
        return_value=_mock_b1([plan]),
    ):
        payload = prod.run_p5_native_payload(
            {"segments": [], "words": []}, page_no=1,
            scale_info={"px_per_m": cal.px_per_m},
            elevation_openings=result.openings,
            elevation_diagnostics=result.diagnostics,
            elevation_provenance=result.opening_provenance,
            facade_registration=_register_facades(),
        )

    assert payload["status"] == "ok"
    persisted = payload["elevation_provenance"]
    assert persisted, "elevation_provenance must be persisted in the payload"
    assert len(persisted) == len(payload["elevation_openings"])
    assert persisted[0]["source_filename"] == fx["source"]["local_source_alias"]
    assert persisted[0]["source_page"] == 86


def test_c3_bridge_level_source_filename_survives_when_candidate_has_none():
    """The ``source_filename`` parameter accepted by the bridge survives into
    the persisted diagnostics even when the candidate itself carries none.

    Proves the bridge B ground-truths the reviewer's traceability: a
    caller-supplied original filename is never dropped by the seam.
    """
    cal, _ = _real_render_pixel_calibration()
    # Candidate with NO source filename of its own (explicitly blank) — the
    # bridge-level source_filename must win.
    cand = _raster_candidate(width_m=0.82, height_m=2.1, source_filename="")
    result = bridge.raster_openings_from_candidates(
        [cand], cal,
        elevation_page_no=86, elevation_side="East",
        source_filename="my_source_drawing.pdf",
        source_page=86,
        drawing_ref="CD3001",
        drawing_title="E1 EAST ELEVATION",
        level="L1",
        wall_ref="E1",
        calibration_source=cal.method,
    )
    assert len(result.openings) == 1
    accepted = [
        d for d in result.diagnostics
        if d.get("kind") == "elevation_candidate" and d.get("status") == "accepted"
    ]
    assert accepted
    assert accepted[0]["source_filename"] == "my_source_drawing.pdf", (
        "bridge-supplied source_filename must survive when candidate has none"
    )
    assert accepted[0]["drawing_title"] == "E1 EAST ELEVATION"
    assert accepted[0]["level"] == "L1"
    assert accepted[0]["coord_space"] == COORD_SPACE_RENDER_PIXEL
    assert accepted[0]["calibration"]["method"] == cal.method
    assert accepted[0]["calibration"]["review_status"] == cal.review_status
    summary = [
        s for s in result.diagnostics if s.get("kind") == "elevation_evidence_summary"
    ]
    assert summary and summary[0]["source_filename"] == "my_source_drawing.pdf"


def test_r1_objective_level_beats_caller_level():
    """R1 precedence: the CANDIDATE-OBJECTIVE level wins over a conflicting
    bridge-supplied ``level``.

    The candidate objectively resolved level ``L1`` (via ``level_band``); the
    caller passes ``L2``.  Provenance resolution is candidate-objective ->
    bridge-context -> unknown, so the opening AND its diagnostic must both
    carry ``L1`` — never ``L2`` — and the identical record is reused for both
    (single authoritative resolve).
    """
    cal, _ = _real_render_pixel_calibration()
    cand = _raster_candidate(width_m=0.82, height_m=2.1, level_band="L1")
    result = bridge.raster_openings_from_candidates(
        [cand], cal,
        elevation_page_no=86, elevation_side="East",
        source_filename="synthetic.pdf", source_page=86,
        drawing_ref="CD3001",
        drawing_title="E1 EAST ELEVATION",
        level="L2",           # conflicting caller level
        wall_ref="E1",
        calibration_source=cal.method,
    )
    assert len(result.openings) == 1
    accepted = [
        d for d in result.diagnostics
        if d.get("kind") == "elevation_candidate" and d.get("status") == "accepted"
    ]
    assert len(accepted) == 1

    # Candidate-objective level beats the caller context.
    prov = result.opening_provenance[0]
    assert prov["level"] == "L1", "candidate-objective level must beat caller L2"
    # The diagnostic reuses the IDENTICAL resolved record (R1 single resolve).
    assert prov["level"] == accepted[0]["level"]
    assert result.openings[0].level == "L1"


def test_r1_blank_candidate_falls_back_to_caller_context():
    """R1 precedence: a BLANK candidate falls back to the bridge-supplied
    context (candidate-objective -> BRIDGE-CONTEXT -> unknown).

    The candidate carries NO own level / source_filename; the caller's ``L2``
    and filename flow through to the opening and its diagnostic — proving the
    bridge-context tier of the precedence chain.
    """
    cal, _ = _real_render_pixel_calibration()
    cand = _raster_candidate(
        width_m=0.82, height_m=2.1,
        source_filename="", level_band=None,   # blank candidate-objective tier
    )
    result = bridge.raster_openings_from_candidates(
        [cand], cal,
        elevation_page_no=86, elevation_side="East",
        source_filename="BRIDGE_SOURCE.pdf",
        source_page=86,
        drawing_ref="CD3001",
        drawing_title="BRIDGE TITLE",
        level="L2",
        wall_ref="E1",
        calibration_source=cal.method,
    )
    assert len(result.openings) == 1
    prov = result.opening_provenance[0]
    # Bridge context fills the blank candidate slots (candidate-objective empty
    # -> bridge-context -> unknown).
    assert prov["level"] == "L2", "blank candidate level falls back to caller L2"
    assert prov["drawing_title"] == "BRIDGE TITLE"
    assert prov["source_filename"] == "BRIDGE_SOURCE.pdf"
    assert result.openings[0].level == "L2"
    accepted = [
        d for d in result.diagnostics
        if d.get("kind") == "elevation_candidate" and d.get("status") == "accepted"
    ]
    assert len(accepted) == 1
    assert prov["level"] == accepted[0]["level"]
    assert prov["drawing_title"] == accepted[0]["drawing_title"]
    assert prov["source_filename"] == accepted[0]["source_filename"]