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


# ---------------------------------------------------------------------------
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
        wall_ref="E1",
        calibration_source=cal.method,
    )
    assert len(result.openings) >= 1
    opening = result.openings[0]
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
        mark="", wall="E1", position=2.0, width=opening.width_m,
        opening_type=OPENING_TYPE_WINDOW, geom_conf=0.9, assoc_conf=0.85,
    )
    plan.elevation_side = "East"

    native = {"segments": [], "words": []}
    with patch(
        "pb_plan_opening_detection_v171.plan_opening_candidates",
        return_value=_mock_b1([plan]),
    ):
        payload = prod.run_p5_native_payload(
            native, page_no=1, scale_info={"px_per_m": cal.px_per_m},
            elevation_openings=result.openings,
            elevation_diagnostics=result.diagnostics,
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
        level=None, wall_ref="E1", calibration_source=cal.method,
    )
    opening = result.openings[0]

    plan = _b1_candidate(
        mark="", wall="E1", position=2.0, width=opening.width_m,
        opening_type=OPENING_TYPE_WINDOW, geom_conf=0.9, assoc_conf=0.85,
    )
    plan.elevation_side = "East"

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