"""
pb_opening_deduction_v174.py — B5: Controlled deduction integration (v1.7.4)

Converts reconciled, eligible evidence into actual deduct=True decisions.

Design principles:
  - ONLY a reconciled physical opening with proven rough-opening dimensions,
    sufficient confidence, no unresolved B4 conflicts/ambiguity, and an
    explicit eligible status may ever become deduct=True.
  - B5 never creates instances; B1 controls physical-candidate count.
  - B5 never overrides B4-forced review status.
  - B5 is the ONLY code path that sets deduct=True on OpeningEvidence.
  - deduction_status is PRESERVED (auto_eligible/derived_eligible);
    deduction_decision records the commercial decision separately.

Pipeline: B1 (detection) → physical dedup → B2 (schedule) → B3 (elevation)
          → B4 (reconcile) → B5 gate → deduct decision
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from pb_opening_evidence_v170 import (
    DEDUCTION_AUTO_ELIGIBLE,
    DEDUCTION_DERIVED_ELIGIBLE,
    DEDUCTION_DEDUCTED,
    DEDUCTION_NOT_DEDUCTED,
    DEDUCTION_REVIEW,
    DIMENSION_BASIS_ROUGH_OPENING,
    CONFIDENCE_AUTO_DEDUCT,
    CONFIDENCE_DERIVED_DEDUCT,
    OpeningEvidence,
    deducted_area_m2,
    deduplicate_openings,
    merge_opening_evidence,
    net_wall_area_m2,
    same_physical_opening,
)

VERSION = "1.7.4"

# ---------------------------------------------------------------------------
# Mark conflict check — prevents silent merge of contradictory identities
# ---------------------------------------------------------------------------
def _marks_conflict(a: OpeningEvidence, b: OpeningEvidence) -> bool:
    """True if both instances have nonblank, different type marks.

    D01 + D02 at the same location should NOT silently merge — they
    represent different opening types and merging would lose identity.
    One blank + one nonblank is acceptable (the blank inherits).
    """
    if not a.type_mark or not b.type_mark:
        return False
    return a.type_mark.upper() != b.type_mark.upper()


# ---------------------------------------------------------------------------
# Safety dedup — cross-detector duplicate removal (runs BEFORE B4)
# ---------------------------------------------------------------------------
def resolve_physical_duplicates(
    instances: List[OpeningEvidence],
) -> List[OpeningEvidence]:
    """Resolve cross-detector physical duplicates before B4 reconciliation.

    Catches any remaining duplicates from B1 (e.g. door + gap detectors
    flagging the same physical opening) BEFORE enrichment and reconciliation.

    Uses same_physical_opening() which requires position anchor from
    both records.  Rejects merges when both have nonblank, conflicting
    marks (D01 + D02 → not merged).  Merged dimensions come from the
    higher-confidence source via merge_opening_evidence().
    """
    result: List[OpeningEvidence] = []
    for new in instances:
        matched = False
        for i, existing in enumerate(result):
            if same_physical_opening(existing, new):
                # Reject merge if both have conflicting marks
                if _marks_conflict(existing, new):
                    new.notes += (
                        f" [B5: not merged with {existing.opening_instance_id} "
                        f"due to conflicting marks '{existing.type_mark}' vs "
                        f"'{new.type_mark}']"
                    )
                    continue
                result[i] = merge_opening_evidence(existing, new)
                matched = True
                break
        if not matched:
            result.append(new)
    return result


# ---------------------------------------------------------------------------
# Eligibility gate — all criteria must pass
# ---------------------------------------------------------------------------
def passes_eligibility_gate(inst: OpeningEvidence) -> bool:
    """Check all eligibility criteria before a deduction is applied.

    Returns True ONLY when ALL of the following hold:
      1. reconciliation_complete is True
         (B4 must have run; no bypassing reconciliation)
      2. deduction_status is auto_eligible or derived_eligible
         (B4 may force review if conflicts exist)
      3. width_m and height_m both present and > 0
         (rough-opening dims required for wall-void area)
      4. dimension_basis == "rough_opening"
         (other bases are not eligible for wall deduction)
      5. minimum(geometry, dimension, association) >= 0.70
         (derived_eligible or better; review/none gate)
      6. wall_ref is non-empty
         (physical location must be anchored to a wall)
      7. area_m2 is computable (width * height > 0)
    """
    # 1. Reconciliation gate — B4 must have run
    if not inst.reconciliation_complete:
        return False

    # 2. Status check
    if inst.deduction_status not in (
        DEDUCTION_AUTO_ELIGIBLE, DEDUCTION_DERIVED_ELIGIBLE
    ):
        return False

    # 3. Dimension presence
    if inst.width_m is None or inst.height_m is None:
        return False
    if inst.width_m <= 0 or inst.height_m <= 0:
        return False

    # 4. Basis check
    if inst.dimension_basis != DIMENSION_BASIS_ROUGH_OPENING:
        return False

    # 5. Confidence check
    min_conf = min(
        inst.geometry_confidence,
        inst.dimension_confidence,
        inst.association_confidence,
    )
    if min_conf < CONFIDENCE_DERIVED_DEDUCT:
        return False

    # 6. Wall reference
    if not inst.wall_ref:
        return False

    # 7. Area
    if inst.area_m2 is None or inst.area_m2 <= 0:
        return False

    return True


# ---------------------------------------------------------------------------
# Deduction assignment
# ---------------------------------------------------------------------------
def apply_deductions(
    instances: List[OpeningEvidence],
) -> List[OpeningEvidence]:
    """Apply the eligibility gate and set deduct=True on eligible instances.

    For each instance:
      - If passes_eligibility_gate(): deduct=True, deduction_decision="deducted"
      - Otherwise: deduct=False, deduction_decision="not_deducted"
        (unless deduction_status is already review/none)

    deduction_status (evidence eligibility) is NEVER modified by B5.
    Only deduct (bool) and deduction_decision (str) are set.

    This is the ONLY function that sets deduct=True on OpeningEvidence.

    Returns the same list (mutated in place).  Idempotent: running twice
    produces the same result.
    """
    for inst in instances:
        if passes_eligibility_gate(inst):
            inst.deduct = True
            inst.deduction_decision = DEDUCTION_DEDUCTED
        else:
            inst.deduct = False
            if inst.deduction_decision != DEDUCTION_NOT_DEDUCTED:
                # Only set not_deducted if not already processed
                inst.deduction_decision = DEDUCTION_NOT_DEDUCTED
    return instances


# ---------------------------------------------------------------------------
# Area computation
# ---------------------------------------------------------------------------
def deducted_total_area_m2(
    instances: Sequence[OpeningEvidence],
) -> float:
    """Total deducted area from instances where deduct=True."""
    return deducted_area_m2(instances)


def net_wall_area_after_deductions(
    gross_wall_m2: float,
    instances: Sequence[OpeningEvidence],
    tolerance: float = 0.001,
) -> Dict[str, Any]:
    """Net wall area after B5 deductions.

    Returns dict with keys:
      - net_area_m2: float — net wall area (clamped to 0.0 minimum)
      - valid: bool — False if deducted_area exceeds gross (measurement error)
      - error: str — error description if not valid, "" otherwise

    If deducted_area > gross_wall_area + tolerance, this is an invalid
    measurement state (over-deduction from duplicate, wall-association,
    or dimension error).  valid=False signals this to the caller.
    """
    d_area = deducted_area_m2(instances)
    excess = d_area - _num(gross_wall_m2) - tolerance
    if excess > 0:
        return {
            "net_area_m2": 0.0,
            "valid": False,
            "error": (
                f"Deducted area ({d_area:.4f} m²) exceeds gross wall area "
                f"({gross_wall_m2:.4f} m²) — possible duplicate detection, "
                f"wall-association, or dimension error"
            ),
        }
    return {
        "net_area_m2": round(max(0.0, _num(gross_wall_m2) - d_area), 4),
        "valid": True,
        "error": "",
    }


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Pipeline orchestration (B1 → B5)
# ---------------------------------------------------------------------------
def run_opening_pipeline(
    segments: Sequence[Any],
    words: Sequence[Any],
    wall_lines: Optional[Sequence[Any]] = None,
    schedule_entries: Optional[Sequence[Any]] = None,
    elevation_openings: Optional[Sequence[Any]] = None,
    scale_info: Optional[Dict[str, Any]] = None,
    scale_px_per_m: float = 0.0,
    page_no: int = 0,
    gross_wall_m2: Optional[float] = None,
) -> Dict[str, Any]:
    """Full pipeline: B1 → dedup → B2 → B3 → B4 → B5 gate → deduct.

    Pipeline order:
      B1 (detection) → physical dedup → B2 (schedule) → B3 (elevation)
      → B4 (reconcile) → B5 gate → deduct decision

    Dedup runs BEFORE enrichment (B2/B3) so that merge_opening_evidence()
    never erases B4-forced review status.  B4 is always the last
    recomputation of eligibility before B5 reads it.

    Returns dict with keys:
      - instances: List[OpeningEvidence] — after deductions applied
      - conflicts: List[ConflictRecord] — from B4
      - deducted_area_m2: float
      - net_wall: dict with net_area_m2, valid, error (if gross provided)
      - pipeline_notes: List[str]
    """
    from pb_plan_opening_detection_v171 import plan_opening_candidates

    notes: List[str] = []

    # --- B1: plan detection ---
    b1_result = plan_opening_candidates(
        segments=segments,
        words=words,
        wall_lines=wall_lines,
        scale_info=scale_info,
        scale_px_per_m=scale_px_per_m,
        page_no=page_no,
    )
    instances = list(b1_result.candidates)
    notes.append(
        f"B1: {b1_result.door_count} doors, "
        f"{b1_result.window_count} windows, "
        f"{b1_result.gap_count} gaps"
    )

    if not instances:
        result: Dict[str, Any] = {
            "instances": [],
            "conflicts": [],
            "deducted_area_m2": 0.0,
            "pipeline_notes": notes,
        }
        if gross_wall_m2 is not None:
            result["net_wall"] = {
                "net_area_m2": gross_wall_m2,
                "valid": True,
                "error": "",
            }
        return result

    # --- Physical duplicate resolution (BEFORE enrichment) ---
    before_dedup = len(instances)
    instances = resolve_physical_duplicates(instances)
    dedup_removed = before_dedup - len(instances)
    if dedup_removed > 0:
        notes.append(
            f"B5-dedup: removed {dedup_removed} duplicate(s) before enrichment"
        )

    # --- B2: schedule enrichment ---
    if schedule_entries:
        from pb_opening_schedule_v171 import enrich_opening_evidence
        instances = enrich_opening_evidence(instances, schedule_entries)
        notes.append(f"B2: enriched {len(instances)} instances from schedule")

    # --- B3: elevation correlation ---
    if elevation_openings:
        from pb_elevation_evidence_v172 import correlate_elevation_to_plan
        instances, unmatched = correlate_elevation_to_plan(
            elevation_openings, instances
        )
        notes.append(
            f"B3: correlated elevation openings "
            f"({len(instances)} matched, {len(unmatched)} unmatched)"
        )

    # --- B4: cross-source reconciliation (LAST recomputation of eligibility) ---
    from pb_opening_reconciliation_v173 import reconcile_opening_evidence
    instances, conflicts = reconcile_opening_evidence(instances)
    conflict_count = len(conflicts)
    notes.append(f"B4: {conflict_count} conflict(s) detected")

    # --- B5 gate → deduct decision ---
    instances = apply_deductions(instances)
    deducted_count = sum(1 for i in instances if i.deduct)
    notes.append(f"B5: {deducted_count}/{len(instances)} instances deducted")

    # --- Area computation ---
    d_area = deducted_total_area_m2(instances)
    result = {
        "instances": instances,
        "conflicts": conflicts,
        "deducted_area_m2": d_area,
        "pipeline_notes": notes,
    }
    if gross_wall_m2 is not None:
        result["net_wall"] = net_wall_area_after_deductions(
            gross_wall_m2, instances
        )

    return result
