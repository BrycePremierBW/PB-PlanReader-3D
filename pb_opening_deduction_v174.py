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

Pipeline: B1 (detection) → B2 (schedule) → B3 (elevation) → B4 (reconcile)
          → B5 safety dedup → B5 gate check → B5 deduct assignment
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
    net_wall_area_m2,
    same_physical_opening,
)

VERSION = "1.7.4"

# ---------------------------------------------------------------------------
# Safety dedup — cross-detector duplicate removal
# ---------------------------------------------------------------------------
def safety_deduplicate(
    instances: List[OpeningEvidence],
) -> List[OpeningEvidence]:
    """Final dedup pass before deduction assignment.

    Catches any remaining cross-detector duplicates that survived B1–B4
    (e.g. the same physical opening detected by door + gap detectors).

    Uses same_physical_opening() which requires position anchor from
    both records.  Merged dimensions come from the higher-confidence
    source via merge_opening_evidence().
    """
    return deduplicate_openings(instances)


# ---------------------------------------------------------------------------
# Eligibility gate — all criteria must pass
# ---------------------------------------------------------------------------
def passes_eligibility_gate(inst: OpeningEvidence) -> bool:
    """Check all eligibility criteria before a deduction is applied.

    Returns True ONLY when ALL of the following hold:
      1. deduction_status is auto_eligible or derived_eligible
         (set by B0 compute_deduction_status, may be forced to
          review by B4 if conflicts exist)
      2. width_m and height_m both present and > 0
         (rough-opening dims required for wall-void area)
      3. dimension_basis == "rough_opening"
         (other bases are not eligible for wall deduction)
      4. minimum(geometry, dimension, association) >= 0.70
         (derived_eligible or better; review/none gate)
      5. wall_ref is non-empty
         (physical location must be anchored to a wall)
      6. area_m2 is computable (width * height > 0)
    """
    # 1. Status check
    if inst.deduction_status not in (
        DEDUCTION_AUTO_ELIGIBLE, DEDUCTION_DERIVED_ELIGIBLE
    ):
        return False

    # 2. Dimension presence
    if inst.width_m is None or inst.height_m is None:
        return False
    if inst.width_m <= 0 or inst.height_m <= 0:
        return False

    # 3. Basis check
    if inst.dimension_basis != DIMENSION_BASIS_ROUGH_OPENING:
        return False

    # 4. Confidence check
    min_conf = min(
        inst.geometry_confidence,
        inst.dimension_confidence,
        inst.association_confidence,
    )
    if min_conf < CONFIDENCE_DERIVED_DEDUCT:
        return False

    # 5. Wall reference
    if not inst.wall_ref:
        return False

    # 6. Area
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
      - If passes_eligibility_gate(): deduct=True, deduction_status="deducted"
      - Otherwise: deduct=False, deduction_status="not_deducted"
        (unless already review/none, which are preserved)

    This is the ONLY function that sets deduct=True on OpeningEvidence.

    Returns the same list (mutated in place).
    """
    for inst in instances:
        if passes_eligibility_gate(inst):
            inst.deduct = True
            inst.deduction_status = DEDUCTION_DEDUCTED
        else:
            inst.deduct = False
            # Preserve review/none if already set (e.g. B4 forced review)
            if inst.deduction_status not in (
                DEDUCTION_REVIEW, "none"
            ):
                inst.deduction_status = DEDUCTION_NOT_DEDUCTED
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
) -> float:
    """Net wall area after B5 deductions.  Never negative."""
    return net_wall_area_m2(gross_wall_m2, instances)


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
    """Full pipeline: B1 → B2 → B3 → B4 → B5.

    Returns dict with keys:
      - instances: List[OpeningEvidence] — after deductions applied
      - conflicts: List[ConflictRecord] — from B4
      - deducted_area_m2: float
      - net_wall_area_m2: float (only if gross_wall_m2 provided)
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
        return {
            "instances": [],
            "conflicts": [],
            "deducted_area_m2": 0.0,
            "net_wall_area_m2": (
                gross_wall_m2 if gross_wall_m2 is not None else 0.0
            ),
            "pipeline_notes": notes,
        }

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

    # --- B4: cross-source reconciliation ---
    from pb_opening_reconciliation_v173 import reconcile_opening_evidence
    instances, conflicts = reconcile_opening_evidence(instances)
    conflict_count = len(conflicts)
    notes.append(f"B4: {conflict_count} conflict(s) detected")

    # --- B5 safety dedup ---
    before_dedup = len(instances)
    instances = safety_deduplicate(instances)
    dedup_removed = before_dedup - len(instances)
    if dedup_removed > 0:
        notes.append(f"B5: safety dedup removed {dedup_removed} duplicate(s)")

    # --- B5 deduction assignment ---
    instances = apply_deductions(instances)
    deducted_count = sum(1 for i in instances if i.deduct)
    notes.append(f"B5: {deducted_count}/{len(instances)} instances deducted")

    # --- Area computation ---
    d_area = deducted_total_area_m2(instances)
    result: Dict[str, Any] = {
        "instances": instances,
        "conflicts": conflicts,
        "deducted_area_m2": d_area,
        "pipeline_notes": notes,
    }
    if gross_wall_m2 is not None:
        result["net_wall_area_m2"] = net_wall_area_after_deductions(
            gross_wall_m2, instances
        )

    return result
