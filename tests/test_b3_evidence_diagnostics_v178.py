"""Tests for PB PlanReader B3 evidence diagnostics / explainability (WS3).

Locks the ADDITIVE, diagnostic-only decision ledger introduced for B3
opening-evidence explainability:

  - ``OpeningEvidence.decision_reasons`` — additive field with an
    empty-list default; recorded by B3/B4/B5 so every accepted / rejected /
    review-only outcome is inspectable as STRUCTURED data (never free-text
    truth, never a decision input).
  - ``pb_elevation_evidence_v172`` correlation + enrichment records a
    structured WHY (correlation score, mark / side / level / width evidence,
    dimension basis) on the instance, on the elevation observation in
    ``source_observations``, and on the elevation candidate's
    ``correlation_diagnostics`` ledger.

Foundation rules under test:
  1. Structured data over free-text; no UI redesign (tiny read-only helpers
     ``render_decision_reasons`` / ``decision_reasons_summary`` only).
  2. Diagnostics NEVER define truth and NEVER imply authority —
     ``deduct`` stays False, and ``deduction_authority`` /
     ``instance_creation_authority`` remain False unless a real authority
     gate granted them.
  3. Serialization compatibility: legacy constructors, legacy dicts and
     persisted records WITHOUT ``decision_reasons`` still work; the
     production consumer ``_is_authorised_b5_automatic`` tolerates the
     extra ``asdict()`` key and authorizes identically.
  4. The dataclass equality domain is DOCUMENTED as intentionally extended
     (identical legacy fields remain equal; differing ``decision_reasons``
     now compare unequal) — behaviour is tested, not hidden.
  5. Existing ``source_observations`` keys are preserved byte-identical;
     existing ``deduction_status`` strings and ``.notes`` are untouched.

Detector output NEVER defines truth in any assertion below.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from pb_opening_evidence_v170 import (
    OpeningEvidence,
    merge_opening_evidence,
    record_decision_reason,
    render_decision_reasons,
    decision_reasons_summary,
    DIMENSION_BASIS_UNKNOWN,
    DIMENSION_BASIS_ROUGH_OPENING,
    DEDUCTION_REVIEW,
    DEDUCTION_AUTO_ELIGIBLE,
    DEDUCTION_DERIVED_ELIGIBLE,
)
from pb_elevation_evidence_v172 import (
    ElevationOpening,
    correlate_elevation_to_plan,
    _enrich_from_elevation,
    _correlation_assessment,
    _MIN_STRONG_SIGNAL,
    ELEVATION_WIDTH_TOLERANCE_M,
)
from pb_opening_production_v175 import _is_authorised_b5_automatic


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _window_plan(**overrides) -> OpeningEvidence:
    """B1-style plan instance for a window (EW01) with plan-only evidence."""
    kw = dict(
        opening_instance_id="plan-EW01",
        type_mark="EW01",
        wall_ref="E01",
        width_m=0.80,
        height_m=None,
        dimension_basis=DIMENSION_BASIS_UNKNOWN,
        dimension_source="plan_vector",
        elevation_side="East",
        level="Ground",
        extraction_method="plan_vector",
        geometry_confidence=0.7,
        dimension_confidence=0.0,
        notes="keep-me",
    )
    kw.update(overrides)
    return OpeningEvidence(**kw)


def _door_plan(**overrides) -> OpeningEvidence:
    """B1-style plan instance for a door (D01) with plan-only evidence."""
    kw = dict(
        opening_instance_id="plan-D01",
        type_mark="D01",
        wall_ref="L08",
        width_m=0.82,
        height_m=None,
        dimension_basis=DIMENSION_BASIS_UNKNOWN,
        dimension_source="plan_vector",
        elevation_side="East",
        level="Ground",
        extraction_method="plan_vector",
        geometry_confidence=0.7,
        dimension_confidence=0.0,
    )
    kw.update(overrides)
    return OpeningEvidence(**kw)


def _elev(**overrides) -> ElevationOpening:
    """ElevationOpening candidate that matches _window_plan strongly."""
    kw = dict(
        elevation_page_no=86,
        elevation_side="East",
        bbox_px=(100, 100, 300, 500),
        width_m=0.82,
        height_m=1.5,
        label="EW01",
        level="Ground",
        drawing_ref="CD3001",
        coord_space="pdf_point",
    )
    kw.update(overrides)
    return ElevationOpening(**kw)


def _door_elev(**overrides) -> ElevationOpening:
    """ElevationOpening candidate that matches _door_plan strongly."""
    return _elev(label="D01", width_m=0.82, bbox_px=(100, 100, 300, 500), **overrides)


def _assessment_details() -> dict:
    """Structured assessment details for a strong EW01 match."""
    return {
        "rejection_reason": None,
        "mark_evidence": "exact_plan_mark_match",
        "side_match": True,
        "level_match": True,
        "width_match_m": 0.02,
    }


def _load_elev_fixture() -> dict:
    p = Path(__file__).resolve().parent / "fixtures" / "lago_cd3001_east_elevation_v177.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _load_door_fixture() -> dict:
    p = Path(__file__).resolve().parent / "fixtures" / "lago_b1_ga08_ed04_cluster.json"
    return json.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. Accepted enrichment carries a structured "why" (round-trip safe)
# ---------------------------------------------------------------------------
def test_accepted_enrichment_records_structured_why_round_trip_safe():
    inst = _window_plan()
    elev = _elev()
    enriched, unmatched = correlate_elevation_to_plan([elev], [inst])

    assert unmatched == []
    m = enriched[0]
    # identity preserved through the merge-enrichment
    assert m.opening_instance_id == inst.opening_instance_id
    # instance WAS enriched (height arrived from elevation)
    assert m.height_m == elev.height_m
    assert m.dimension_source == "elevation_rect"
    assert m.dimension_basis == DIMENSION_BASIS_UNKNOWN

    accepted = [r for r in m.decision_reasons if r.get("outcome") == "accepted"]
    assert len(accepted) == 1
    rec = accepted[0]
    assert rec["stage"] == "B3"
    assert rec["correlation_score"] > 0
    assert rec["correlation_score"] >= _MIN_STRONG_SIGNAL
    assert rec["mark_evidence"] == "exact_plan_mark_match"
    assert rec["side_match"] is True
    assert rec["level_match"] is True
    assert isinstance(rec["width_match_m"], float)
    assert rec["dimension_basis"] == DIMENSION_BASIS_UNKNOWN
    assert rec["deduction_authority"] is False
    assert rec["instance_creation_authority"] is False

    # the elevation observation in source_observations carries the same WHY
    obs = m.source_observations[-1]
    assert obs["source"] == "elevation_rect"
    assert obs["accepted"] is True
    assert obs["match_decided"] is True
    assert obs["correlation_score"] == rec["correlation_score"]
    assert obs["mark_evidence"] == "exact_plan_mark_match"
    assert obs["width_match_m"] == rec["width_match_m"]
    assert obs["dimension_basis"] == DIMENSION_BASIS_UNKNOWN

    # elevation candidate side ledger
    assert elev.correlation_diagnostics
    assert elev.correlation_diagnostics[-1]["match_decided"] is True
    assert elev.correlation_diagnostics[-1]["plan_instance_id"] == m.opening_instance_id

    # serialization round-trip: asdict -> reconstruct preserves diagnostics
    payload = asdict(m)
    clone = OpeningEvidence(**payload)
    assert clone == m
    assert clone.decision_reasons == m.decision_reasons
    assert any(r.get("outcome") == "accepted" for r in clone.decision_reasons)


# ---------------------------------------------------------------------------
# 2. Hard rejections record an explicit structured rejection_reason and do
#    NOT enrich the instance
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("mutator", "reason_part"),
    [
        ({"level": "First"}, "wrong_level"),
        ({"elevation_side": "South"}, "wrong_side"),
    ],
)
def test_hard_rejections_record_rejection_reason_and_do_not_enrich(mutator, reason_part):
    inst = _window_plan(level="Ground", elevation_side="East")
    elev = _elev(**mutator)

    assert _correlation_assessment(inst, elev)[0] == 0.0

    enriched, unmatched = correlate_elevation_to_plan([elev], [inst])
    m = enriched[0]
    # unchanged object — NOT enriched
    assert m is inst
    assert m.height_m is None
    assert m.dimension_source == "plan_vector"
    assert unmatched == [elev]

    rejected = [r for r in m.decision_reasons if r.get("outcome") == "rejected"]
    assert len(rejected) == 1
    rec = rejected[0]
    assert reason_part in rec["rejection_reason"]
    assert rec["correlation_score"] == 0.0
    assert rec["deduction_authority"] is False
    assert rec["instance_creation_authority"] is False

    # elevation candidate ledger records the same explicit rejection
    assert elev.correlation_diagnostics[-1]["match_decided"] is False
    assert reason_part in elev.correlation_diagnostics[-1]["rejection_reason"]


def test_conflicting_mark_hard_rejects_with_structured_reason():
    inst = _door_plan()
    elev = _elev(label="W01", width_m=0.82)  # D01 plan vs W01 elevation
    enriched, _ = correlate_elevation_to_plan([elev], [inst])
    m = enriched[0]
    assert m is inst
    rejected = [r for r in m.decision_reasons if r.get("outcome") == "rejected"]
    assert len(rejected) == 1
    assert "mark_conflict" in rejected[0]["rejection_reason"]


# ---------------------------------------------------------------------------
# 3. Ambiguous (tied-score) correlations stay unmatched, record an ambiguity
#    reason, deduction_status stays review
# ---------------------------------------------------------------------------
def test_ambiguous_tied_score_plan_side_stays_unmatched_review():
    plan = _door_plan()
    e1 = _door_elev(elevation_page_no=86)
    e2 = _door_elev(elevation_page_no=87)

    enriched, unmatched = correlate_elevation_to_plan([e1, e2], [plan])
    assert enriched[0] is plan
    assert plan.height_m is None  # not enriched
    assert len(unmatched) == 2

    ambiguous = [r for r in plan.decision_reasons if r.get("ambiguity_reason") == "ambiguous_tie"]
    assert len(ambiguous) == 1
    rec = ambiguous[0]
    assert rec["outcome"] == "unmatched"
    assert rec["correlation_score"] >= _MIN_STRONG_SIGNAL
    assert "ambiguous_tie" in rec["rejection_reason"]
    assert rec["deduction_authority"] is False
    assert rec["instance_creation_authority"] is False
    assert plan.deduction_status == DEDUCTION_REVIEW
    assert plan.deduct is False


def test_ambiguous_tied_score_elevation_side_records_ambiguity_and_no_deduction():
    p1 = _door_plan(opening_instance_id="plan-D01-a")
    p2 = _door_plan(opening_instance_id="plan-D01-b")
    elev = _door_elev()

    enriched, unmatched = correlate_elevation_to_plan([elev], [p1, p2])
    # both plans stay unmatched; the elevation candidate gets the ambiguity ledger
    assert len(enriched) == 2
    assert enriched[0] is p1 and enriched[1] is p2
    assert unmatched == [elev]

    assert elev.correlation_diagnostics
    diag = elev.correlation_diagnostics[-1]
    assert diag["match_decided"] is False
    assert diag["ambiguity_reason"] == "ambiguous_tie"
    assert "ambiguous_tie" in diag["rejection_reason"]
    assert p1.deduction_status == DEDUCTION_REVIEW
    assert p2.deduction_status == DEDUCTION_REVIEW
    assert all(p.deduct is False for p in (p1, p2))


# ---------------------------------------------------------------------------
# 4. Review-only decisions carry a structured review_required_reason; no
#    authority is inferred from diagnostics
# ---------------------------------------------------------------------------
def test_ambiguous_plan_side_record_is_a_review_only_decision():
    plan = _door_plan()
    e1 = _door_elev(elevation_page_no=86)
    e2 = _door_elev(elevation_page_no=87)
    correlate_elevation_to_plan([e1, e2], [plan])

    rec = [r for r in plan.decision_reasons if r.get("ambiguity_reason") == "ambiguous_tie"][0]
    # the review-only decision carries an explicit structured reason
    assert rec.get("review_required_reason")
    assert "review" in rec["review_required_reason"]
    assert rec["deduction_authority"] is False
    assert rec["instance_creation_authority"] is False
    assert plan.deduction_status == DEDUCTION_REVIEW
    assert plan.deduct is False


def test_review_only_helper_record_and_render_no_authority():
    inst = _door_plan()
    rec = record_decision_reason(
        inst,
        stage="B3",
        outcome="review",
        reason="tied marks across elevations",
        review_required_reason="manual confirmation required before any deduction",
        deduction_authority=False,
        instance_creation_authority=False,
    )
    assert inst.decision_reasons[-1] == rec
    assert rec["review_required_reason"] == (
        "manual confirmation required before any deduction"
    )
    assert rec["deduction_authority"] is False
    assert rec["instance_creation_authority"] is False
    # read-only render exposes it without mutating
    lines = render_decision_reasons(inst)
    assert any("[B3] review" in line and "why: " in line for line in lines)
    assert inst.deduct is False
    assert inst.deduction_status == DEDUCTION_REVIEW


def test_no_decision_record_grants_authority_or_changes_decisions():
    """Every emitted record must keep authority flags False and never touch
    deduct/deduction_status/dimension_basis."""
    inst = _window_plan()
    elev = _elev()
    enriched, _ = correlate_elevation_to_plan([elev], [inst])
    m = enriched[0]
    for rec in m.decision_reasons:
        assert rec["deduction_authority"] is False
        assert rec["instance_creation_authority"] is False
        assert "deduct" not in rec
        assert "deduction_status" not in rec
    assert m.deduct is False
    assert m.deduction_status == DEDUCTION_REVIEW
    assert m.dimension_basis == DIMENSION_BASIS_UNKNOWN


# ---------------------------------------------------------------------------
# 5. Elevation-only evidence never yields deduct=True and never creates an
#    instance; diagnostics explain the basis=unknown / no authority reason
# ---------------------------------------------------------------------------
def test_elevation_only_never_creates_instances():
    elev = _elev()
    enriched, unmatched = correlate_elevation_to_plan([elev], [])
    assert enriched == []                      # no instance created
    assert unmatched == [elev]
    assert elev.correlation_diagnostics[-1]["match_decided"] is False
    assert "no_plan_instance" in elev.correlation_diagnostics[-1]["rejection_reason"]
    assert elev.correlation_diagnostics[-1]["instance_creation_authority"] is False


def test_elevation_only_enrichment_stays_unknown_basis_review_and_never_deducts():
    # direct enrichment path
    inst = _window_plan()
    merged = _enrich_from_elevation(
        inst, _elev(), correlation_score=0.81, assessment=_assessment_details()
    )
    assert merged.deduct is False
    assert merged.dimension_source == "elevation_rect"
    assert merged.dimension_basis == DIMENSION_BASIS_UNKNOWN
    assert merged.deduction_status == DEDUCTION_REVIEW
    obs = merged.source_observations[-1]
    assert obs["dimension_basis"] == DIMENSION_BASIS_UNKNOWN
    assert obs["accepted"] is True

    # correlate path: the accepted record explains WHY there is no authority
    enriched, _ = correlate_elevation_to_plan([_elev()], [_window_plan()])
    m = enriched[0]
    rec = [r for r in m.decision_reasons if r.get("outcome") == "accepted"][0]
    assert rec["dimension_basis"] == DIMENSION_BASIS_UNKNOWN
    assert "rough_opening" in rec["dimension_basis_authority"]  # "none (...)"
    assert rec["deduction_authority"] is False
    assert m.deduct is False
    assert m.deduction_status == DEDUCTION_REVIEW


def test_diagnostic_authority_flags_do_not_change_review_for_rough_basis_instance():
    """A genuine rough_opening-backed instance still relies on B5 proof gates;
    diagnostics never flip it either way."""
    inst = _window_plan(
        width_m=0.81, height_m=2.05,
        dimension_basis=DIMENSION_BASIS_ROUGH_OPENING,
        dimension_source="schedule_parse",
        extraction_method="schedule_parse",
        dimension_confidence=0.9,
        association_confidence=0.75,
        deduction_status=DEDUCTION_DERIVED_ELIGIBLE,
    )
    # enrichment with an elevation bundle that LOSES the atomic bundle
    merged = _enrich_from_elevation(
        inst, _elev(), correlation_score=0.81, assessment=_assessment_details()
    )
    obs = merged.source_observations[-1]
    assert obs["accepted"] is False
    assert "dimension_bundle_not_won" in obs["rejection_reason"]
    assert merged.dimension_source == "schedule_parse"      # elevation did not win
    assert merged.dimension_basis == DIMENSION_BASIS_ROUGH_OPENING
    assert merged.deduction_status == DEDUCTION_DERIVED_ELIGIBLE  # untouched
    assert merged.deduct is False
    # correlation decided this pair; the elevation BUNDLE was not accepted
    # (decision flags and acceptance remain distinct)
    assert obs["match_decided"] is True


# ---------------------------------------------------------------------------
# 6. Serialization / equality regressions
# ---------------------------------------------------------------------------
def test_legacy_constructor_and_legacy_dict_input_default_empty_ledger():
    """Legacy constructors (key=value) work and decision_reasons == []."""
    legacy_ctor = OpeningEvidence(type_mark="X", width_m=1.0, height_m=2.0, wall_ref="W01")
    assert legacy_ctor.decision_reasons == []

    # W3.1: construction from a legacy dict WITHOUT decision_reasons
    legacy_dict = {
        "type_mark": "D01",
        "width_m": 0.82,
        "wall_ref": "N01",
        "dimension_basis": DIMENSION_BASIS_UNKNOWN,
    }
    from_dict = OpeningEvidence(**legacy_dict)
    assert from_dict.decision_reasons == []
    assert from_dict.width_m == 0.82

    # W3.2: rehydrating a persisted legacy instance (asdict without the key)
    legacy_a = OpeningEvidence(type_mark="ED04", width_m=0.9)
    rebuilt = OpeningEvidence(**asdict(legacy_a))
    assert rebuilt.decision_reasons == []
    assert rebuilt.width_m == 0.9


def test_asdict_emits_decision_reasons_key_consumer_tolerant_and_authorisation_unchanged():
    inst = OpeningEvidence(type_mark="D01", width_m=0.82)
    ser = asdict(inst)
    assert "decision_reasons" in ser              # shape change is known
    assert ser["decision_reasons"] == []

    # A fully-authorised persisted B5 proof row
    base = {
        "wall_ref": "W01",
        "width_m": 1.0,
        "height_m": 2.0,
        "reconciliation_complete": True,
        "deduction_status": DEDUCTION_AUTO_ELIGIBLE,
        "deduction_decision": "deducted",
        "dimension_basis": DIMENSION_BASIS_ROUGH_OPENING,
        "geometry_confidence": 0.8,
        "dimension_confidence": 0.8,
        "association_confidence": 0.8,
    }
    assert _is_authorised_b5_automatic(base) is True
    # presence/absence of the diagnostic key must not change authorisation
    assert _is_authorised_b5_automatic(dict(base, decision_reasons=[])) is True
    assert _is_authorised_b5_automatic(
        dict(base, decision_reasons=[{"stage": "B3", "outcome": "accepted"}])
    ) is True
    big_ledger = [{"stage": "B3", "outcome": "rejected", "rejection_reason": "x"}] * 20
    assert _is_authorised_b5_automatic(dict(base, decision_reasons=big_ledger)) is True

    # A non-authorised row stays non-authorised with/without the key
    bad = dict(base, deduction_status=DEDUCTION_REVIEW)
    assert _is_authorised_b5_automatic(bad) is False
    assert _is_authorised_b5_automatic(dict(bad, decision_reasons=[])) is False
    assert _is_authorised_b5_automatic(
        dict(bad, decision_reasons=[{"stage": "B3", "outcome": "accepted",
                                    "instance_creation_authority": False}])
    ) is False

    # derived_eligible also proves identically across key presence
    derived = dict(base, deduction_status=DEDUCTION_DERIVED_ELIGIBLE)
    assert _is_authorised_b5_automatic(dict(derived, decision_reasons=[])) == (
        _is_authorised_b5_automatic(derived)
    )


def test_legacy_equality_preserved_and_extended_domain_documented():
    # (a) two legacy-created instances identical in every legacy field
    #     (same identity) remain equal — both default to empty decision_reasons
    a = OpeningEvidence(opening_instance_id="id-1", type_mark="D01", width_m=0.82)
    b = OpeningEvidence(opening_instance_id="id-1", type_mark="D01", width_m=0.82)
    assert a == b
    # an explicit empty list equals the default-empty list
    c = OpeningEvidence(
        opening_instance_id="id-1", type_mark="D01", width_m=0.82,
        decision_reasons=[],
    )
    assert c == a

    # (b) the equality DOMAIN is extended: identical legacy fields but
    #     different decision_reasons no longer compare equal
    d = OpeningEvidence(opening_instance_id="id-1", type_mark="D01", width_m=0.82)
    record_decision_reason(d, stage="B3", outcome="accepted", correlation_score=0.81)
    assert d != a
    assert d.decision_reasons != []

    # asdict round-trip equality holds for the extended domain
    clone = OpeningEvidence(**asdict(d))
    assert clone == d


# ---------------------------------------------------------------------------
# 7. Existing source_observations byte-identical on a known fixture case;
#    deduction_status strings and .notes untouched
# ---------------------------------------------------------------------------
def test_source_observations_preserved_byte_identical_and_notes_untouched():
    obs = [{
        "source": "plan_vector",
        "width_m": 0.80,
        "height_m": None,
        "dimension_basis": DIMENSION_BASIS_UNKNOWN,
        "dimension_confidence": 0.55,
        "type_mark": "EW01",
        "page_no": 12,
        "accepted": True,
    }]
    inst = _window_plan(source_observations=list(obs))
    merged = _enrich_from_elevation(
        inst, _elev(), correlation_score=0.81, assessment=_assessment_details()
    )
    # original observation survives byte-identical
    assert json.dumps(merged.source_observations[0], sort_keys=True) == (
        json.dumps(obs[0], sort_keys=True)
    )
    # elevation observation carries every pre-existing key + additive why
    elev_obs = merged.source_observations[-1]
    for key in (
        "source", "width_m", "height_m", "dimension_basis",
        "dimension_confidence", "type_mark", "page_no", "level", "wall_ref",
        "drawing_ref", "coord_space", "accepted",
    ):
        assert key in elev_obs
    # deduction_status string and notes untouched
    assert merged.deduction_status == DEDUCTION_REVIEW
    assert merged.notes == "keep-me"


def test_known_elevation_fixture_carries_provenance_and_no_new_authority():
    """Reuses the committed lago_cd3001 East-elevation fixture (correlation
    inputs come from its INDEPENDENT annotation, not the detector."""
    fx = _load_elev_fixture()
    ann = fx["positive_benchmark"]["independent_annotation"]
    elev = ElevationOpening(
        elevation_page_no=fx["source"]["page_1_based"],        # 86
        elevation_side=fx["source"]["elevation_side"],          # "East"
        bbox_px=(100, 100, 300, 500),
        width_m=ann["light_width_m"],                           # 0.773 (annotation)
        height_m=ann["opening_height_m"],                       # 1.489 (annotation)
        label="EW01",
        coord_space=fx["calibration"]["coordinate_space"],      # "pdf_point"
        drawing_ref=fx["source"]["drawing_no"],                 # "CD3001"
        level="Ground",
    )
    inst = _window_plan(width_m=ann["light_width_m"], height_m=None)
    enriched, unmatched = correlate_elevation_to_plan([elev], [inst])
    assert unmatched == []
    m = enriched[0]
    assert m.height_m == ann["opening_height_m"]
    rec = [r for r in m.decision_reasons if r.get("outcome") == "accepted"][0]
    assert rec["drawing_ref"] == "CD3001"
    assert rec["coord_space"] == "pdf_point"
    assert rec["correlation_score"] >= _MIN_STRONG_SIGNAL
    assert rec["mark_evidence"] == "exact_plan_mark_match"
    assert rec["dimension_basis"] == DIMENSION_BASIS_UNKNOWN
    assert rec["deduction_authority"] is False
    assert m.deduct is False
    assert m.deduction_status == DEDUCTION_REVIEW


def test_known_door_fixture_context_runs_diagnostics_without_truth_inference():
    """Reuses the committed lago_b1_ga08_ed04 door-plan fixture for context;
    diagnostics record a structured why and never force truth/deduction."""
    fx = _load_door_fixture()
    leaf = fx["expected"]["verified_door_leaf_segments"][0]
    leaf_width_pt = abs(leaf[2] - leaf[0])
    width_m = round(leaf_width_pt / fx["source"]["scale_pt_per_m"], 4)

    plan = _door_plan(type_mark="ED04", width_m=width_m)
    elev = ElevationOpening(
        elevation_page_no=fx["source"]["pdf_page_1based"],       # 23
        elevation_side="East",
        bbox_px=(100, 100, 300, 500),
        width_m=width_m,
        height_m=2.1,
        label="ED04",
        coord_space="pdf_point",
        drawing_ref=fx["source"]["drawing_ref"],                 # "CD1161/06"
        level="Ground",
    )
    enriched, unmatched = correlate_elevation_to_plan([elev], [plan])
    assert unmatched == []
    m = enriched[0]
    recs = [r for r in m.decision_reasons if r.get("outcome") == "accepted"]
    assert len(recs) == 1
    rec = recs[0]
    assert rec["mark_evidence"] == "exact_plan_mark_match"
    assert rec["deduction_authority"] is False
    assert rec["instance_creation_authority"] is False
    # fixture safety: never deducts, never manufactures rough_opening authority
    assert m.deduct is False
    assert m.dimension_basis == DIMENSION_BASIS_UNKNOWN
    assert m.deduction_status == DEDUCTION_REVIEW


# ---------------------------------------------------------------------------
# 8. asdict round-trip safety with the new field + merge dedup additive
# ---------------------------------------------------------------------------
def test_asdict_round_trip_reconstructs_identical_core_fields():
    inst = _window_plan()
    enriched, _ = correlate_elevation_to_plan([_elev()], [inst])
    m = enriched[0]
    assert m.decision_reasons  # non-empty after accepted match

    payload = asdict(m)
    clone = OpeningEvidence(**payload)
    assert clone == m
    assert clone.opening_instance_id == m.opening_instance_id
    assert clone.decision_reasons == m.decision_reasons
    assert clone.width_m == m.width_m
    assert clone.height_m == m.height_m
    assert clone.dimension_basis == m.dimension_basis
    assert clone.dimension_source == m.dimension_source
    assert clone.source_observations == m.source_observations


def test_merge_opening_evidence_dedups_and_merges_decision_reasons_additively():
    a = _window_plan()
    b = _window_plan(opening_instance_id="plan-EW01-b")
    record_decision_reason(a, stage="B3", outcome="rejected", rejection_reason="r1")
    record_decision_reason(a, stage="B3", outcome="rejected", rejection_reason="r1")  # dup
    record_decision_reason(b, stage="B3", outcome="rejected", rejection_reason="r1")  # same
    record_decision_reason(b, stage="B3", outcome="accepted", correlation_score=0.81)

    merged = merge_opening_evidence(a, b)
    # exact duplicates removed, distinct records preserved additively
    assert len(merged.decision_reasons) == 2
    assert merged.decision_reasons[0]["rejection_reason"] == "r1"
    assert merged.decision_reasons[1]["outcome"] == "accepted"
    # merge did not touch measurement / decision fields
    assert merged.deduct is False
    assert merged.deduction_status == DEDUCTION_REVIEW


def test_read_only_diagnostic_helpers_do_not_mutate():
    inst = _window_plan()
    record_decision_reason(
        inst, stage="B3", outcome="accepted", reason="why-x",
        correlation_score=0.81, deduction_authority=False,
    )
    lines = render_decision_reasons(inst)
    assert any("[B3] accepted" in line and "why: why-x" in line for line in lines)
    summary = decision_reasons_summary(inst)
    assert summary["decision_reasons"] == inst.decision_reasons
    assert summary["opening_instance_id"] == inst.opening_instance_id
    assert summary["deduction_status"] == inst.deduction_status
    assert summary["deduct"] is False
    # read-only — helpers must not mutate the ledger
    assert len(inst.decision_reasons) == 1