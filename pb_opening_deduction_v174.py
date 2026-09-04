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

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pb_canonical_building import parse_strict_bool

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
    TOLERANCE_POSITION_M,
    TOLERANCE_WIDTH_M,
)

VERSION = "1.7.4"

# ---------------------------------------------------------------------------
# Location and type checks for conflict detection
# ---------------------------------------------------------------------------
# Types that are geometrically incompatible at the same physical location
_INCOMPATIBLE_TYPES = {
    frozenset({"door", "window"}),
    frozenset({"door", "glazed_opening"}),
    frozenset({"window", "glazed_opening"}),
    frozenset({"door", "garage_door"}),
    frozenset({"door", "roller_door"}),
}


def _types_conflict(a: OpeningEvidence, b: OpeningEvidence) -> bool:
    """True if both have specific, incompatible opening types.

    Door vs window at the same location is a physical-identity conflict.
    Generic "opening" is compatible with anything.
    """
    if not a.opening_type or not b.opening_type:
        return False
    if a.opening_type == b.opening_type:
        return False
    # Generic "opening" is compatible with everything
    if a.opening_type == "opening" or b.opening_type == "opening":
        return False
    pair = frozenset({a.opening_type, b.opening_type})
    return pair in _INCOMPATIBLE_TYPES


def same_location(a: OpeningEvidence, b: OpeningEvidence) -> bool:
    """True if two candidates are at the same physical wall location.

    Checks wall_ref, level, position, and dimensional compatibility
    WITHOUT checking opening_type.  This is intentionally broader than
    same_physical_opening() which also checks type compatibility.

    Used to detect type conflicts (door vs window) at the same location.
    """
    # Wall reference required
    if not a.wall_ref or not b.wall_ref:
        return False
    if a.wall_ref != b.wall_ref:
        return False

    # Level compatible
    if a.level and b.level and a.level != b.level:
        return False

    # Position required from both
    if a.position_along_wall_m is None or b.position_along_wall_m is None:
        return False
    if abs(a.position_along_wall_m - b.position_along_wall_m) > (
        TOLERANCE_POSITION_M + 1e-9
    ):
        return False

    # Width compatible when both present
    if (a.width_m is not None and b.width_m is not None
            and abs(a.width_m - b.width_m) > TOLERANCE_WIDTH_M + 1e-9):
        return False

    return True


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

    Three paths for candidates at the same physical location:
      1. Compatible marks + compatible types → merge via merge_opening_evidence()
      2. Conflicting marks (D01 + D02) → keep separate, record conflict on both
      3. Conflicting types (door + window) → keep separate, record conflict on both

    Paths 2 and 3 produce physical_instance_conflict observations that B4
    will force to deduction_status=review, preventing either from deducting.
    """
    result: List[OpeningEvidence] = []
    for new in instances:
        matched = False
        for i, existing in enumerate(result):
            if same_physical_opening(existing, new):
                if _marks_conflict(existing, new):
                    # Same location, compatible types, conflicting marks
                    _record_physical_conflict(existing, new, "conflicting_marks")
                    new.notes += (
                        f" [B5: not merged with "
                        f"{existing.opening_instance_id} due to conflicting "
                        f"marks '{existing.type_mark}' vs '{new.type_mark}']"
                    )
                    continue
                result[i] = merge_opening_evidence(existing, new)
                matched = True
                break
            elif same_location(existing, new) and _types_conflict(existing, new):
                # Same location but incompatible types (door vs window)
                _record_physical_conflict(existing, new, "conflicting_types")
                new.notes += (
                    f" [B5: not merged with "
                    f"{existing.opening_instance_id} due to type conflict "
                    f"'{existing.opening_type}' vs '{new.opening_type}']"
                )
                continue
            elif _same_plan_geometry(existing, new):
                # Same plan-space geometry but different wall_ref
                # → wall-association ambiguity
                _record_physical_conflict(
                    existing, new, "wall_association_conflict"
                )
                new.notes += (
                    f" [B5: wall association conflict with "
                    f"{existing.opening_instance_id} — same geometry "
                    f"on wall '{existing.wall_ref}' vs '{new.wall_ref}']"
                )
                continue
        if not matched:
            result.append(new)
    return result


def _same_plan_geometry(a: OpeningEvidence, b: OpeningEvidence) -> bool:
    """True if two instances have the same plan-space geometry signature.

    Detects the same physical opening assigned to different wall refs.
    Signature must exist on both and match exactly.
    """
    if a.plan_geometry_signature is None or b.plan_geometry_signature is None:
        return False
    return a.plan_geometry_signature == b.plan_geometry_signature


def _record_physical_conflict(
    a: OpeningEvidence,
    b: OpeningEvidence,
    reason: str,
) -> None:
    """Record a physical_instance_conflict observation on both candidates.

    B4 _detect_conflicts() will see this and force both to review.
    """
    conflict_obs_a = {
        "source": "physical_instance_conflict",
        "width_m": None,
        "height_m": None,
        "dimension_basis": "unknown",
        "dimension_confidence": 0.0,
        "type_mark": a.type_mark,
        "page_no": None,
        "accepted": False,
        "conflicting_id": b.opening_instance_id,
        "conflicting_mark": b.type_mark,
        "description": (
            f"Conflicting identities at same physical location "
            f"({reason}): '{a.type_mark or a.opening_type}' vs "
            f"'{b.type_mark or b.opening_type}'"
        ),
    }
    conflict_obs_b = {
        "source": "physical_instance_conflict",
        "width_m": None,
        "height_m": None,
        "dimension_basis": "unknown",
        "dimension_confidence": 0.0,
        "type_mark": b.type_mark,
        "page_no": None,
        "accepted": False,
        "conflicting_id": a.opening_instance_id,
        "conflicting_mark": a.type_mark,
        "description": (
            f"Conflicting identities at same physical location "
            f"({reason}): '{b.type_mark or b.opening_type}' vs "
            f"'{a.type_mark or a.opening_type}'"
        ),
    }
    a.source_observations = list(a.source_observations) + [conflict_obs_a]
    b.source_observations = list(b.source_observations) + [conflict_obs_b]


_SENTINEL_STRINGS = frozenset({
    "", "none", "nan", "null", "undefined", "unknown", "unassigned",
    "unassigned wall", "0", "-", "- ", "n/a", "na", "false", "true",
})


def _clean_str_id(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    s = str(value).strip()
    if s.lower() in _SENTINEL_STRINGS:
        return ""
    return s


def _is_valid_positive_int(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int):
        return value > 0
    if isinstance(value, float):
        return math.isfinite(value) and value.is_integer() and int(value) > 0
    if isinstance(value, str):
        s = value.strip()
        if s.lower() in _SENTINEL_STRINGS:
            return False
        try:
            val = int(s)
            return val > 0
        except (ValueError, TypeError):
            return False
    return False


def _clean_confidence(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        val = float(value)
        if math.isfinite(val) and 0.0 <= val <= 1.0:
            return val
    except (ValueError, TypeError):
        pass
    return None


# ---------------------------------------------------------------------------
# Eligibility gate — all criteria must pass
# ---------------------------------------------------------------------------
def passes_eligibility_gate(inst: OpeningEvidence) -> bool:
    """Check all eligibility criteria before a deduction is applied.

    Returns True ONLY when ALL of the following hold:
      1. reconciliation_complete is strict bool True
         (B4 must have run; no bypassing reconciliation)
      2. deduction_status is auto_eligible or derived_eligible
         (B4 may force review if conflicts exist)
      3. width_m and height_m both present, finite, and > 0
         (rough-opening dims required for wall-void area)
      4. dimension_basis == "rough_opening"
         (other bases are not eligible for wall deduction)
      5. minimum(geometry, dimension, association) >= 0.70, all finite in [0.0, 1.0]
         (derived_eligible or better; review/none gate)
      6. wall_ref is a valid assigned host wall (non-empty, non-sentinel)
         (physical location must be anchored to a wall)
      7. area_m2 is computable (width * height > 0, finite)
      8. opening_instance_id is valid (non-empty, non-sentinel)
      9. workspace_id and page_no / page_id (if present) are positive integers
      10. dimension_source is non-empty, non-sentinel
    """
    # 1. Reconciliation gate — B4 must have run with strict bool True
    if not parse_strict_bool(inst.reconciliation_complete):
        return False

    # 2. Status check
    if inst.deduction_status not in (
        DEDUCTION_AUTO_ELIGIBLE, DEDUCTION_DERIVED_ELIGIBLE
    ):
        return False

    # 3. Dimension presence and finiteness
    if inst.width_m is None or inst.height_m is None:
        return False
    if isinstance(inst.width_m, bool) or isinstance(inst.height_m, bool):
        return False
    if not (isinstance(inst.width_m, (int, float)) and isinstance(inst.height_m, (int, float))):
        return False
    if not (math.isfinite(inst.width_m) and math.isfinite(inst.height_m)):
        return False
    if inst.width_m <= 0 or inst.height_m <= 0:
        return False

    # 4. Basis check
    if inst.dimension_basis != DIMENSION_BASIS_ROUGH_OPENING:
        return False

    # 5. Confidence check — all 3 must be finite, in [0.0, 1.0], min >= 0.70
    conf_g = _clean_confidence(inst.geometry_confidence)
    conf_d = _clean_confidence(inst.dimension_confidence)
    conf_a = _clean_confidence(inst.association_confidence)
    if conf_g is None or conf_d is None or conf_a is None:
        return False
    if min(conf_g, conf_d, conf_a) < CONFIDENCE_DERIVED_DEDUCT:
        return False

    # 6. Host wall reference
    wall = _clean_str_id(inst.wall_ref)
    if not wall:
        return False

    # 7. Area
    if inst.area_m2 is None:
        return False
    if isinstance(inst.area_m2, bool) or not isinstance(inst.area_m2, (int, float)):
        return False
    if not math.isfinite(inst.area_m2) or inst.area_m2 <= 0:
        return False

    # 8. Opening instance identity
    op_id = _clean_str_id(inst.opening_instance_id)
    if not op_id:
        return False

    # 9. Workspace & page validation (if provided)
    if inst.workspace_id is not None and inst.workspace_id != 0:
        if not _is_valid_positive_int(inst.workspace_id):
            return False
    if inst.page_no is not None:
        if not _is_valid_positive_int(inst.page_no):
            return False
    if inst.page_id is not None:
        if not _is_valid_positive_int(inst.page_id):
            return False

    # 10. Dimension source validation
    src = _clean_str_id(inst.dimension_source)
    if not src:
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
    wall_ref: str,
    tolerance: float = 0.001,
) -> Dict[str, Any]:
    """Net wall area for a specific wall after B5 deductions.

    Only subtracts instances whose wall_ref matches the target wall.
    This prevents cross-wall leakage where W02 deductions reduce W01.

    Returns dict with keys:
      - net_area_m2: float — net wall area (clamped to 0.0 minimum)
      - valid: bool — False if deducted_area exceeds gross (measurement error)
      - error: str — error description if not valid, "" otherwise

    If deducted_area > gross_wall_area + tolerance, this is an invalid
    measurement state (over-deduction from duplicate, wall-association,
    or dimension error).  valid=False signals this to the caller.
    """
    wall_instances = [
        i for i in instances
        if i.wall_ref == wall_ref
        and parse_strict_bool(i.deduct)
        and i.area_m2 is not None
        and not isinstance(i.area_m2, bool)
        and math.isfinite(i.area_m2)
        and i.area_m2 > 0
    ]
    d_area = sum(i.area_m2 for i in wall_instances)
    d_area = round(d_area, 4)
    gross_val = _num(gross_wall_m2)
    excess = d_area - gross_val - tolerance
    if excess > 0:
        return {
            "net_area_m2": 0.0,
            "valid": False,
            "error": (
                f"Deducted area ({d_area:.4f} m²) exceeds gross wall area "
                f"({gross_val:.4f} m²) for wall '{wall_ref}' — possible "
                f"duplicate detection, wall-association, or dimension error"
            ),
        }
    return {
        "net_area_m2": round(max(0.0, gross_val - d_area), 4),
        "valid": True,
        "error": "",
    }


def _num(v: Any, default: float = 0.0) -> float:
    if isinstance(v, bool):
        return default
    try:
        x = float(v)
        return x if math.isfinite(x) else default
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
    wall_ref: Optional[str] = None,
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
        if gross_wall_m2 is not None and wall_ref is not None:
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
    if gross_wall_m2 is not None and wall_ref is not None:
        result["net_wall"] = net_wall_area_after_deductions(
            gross_wall_m2, instances, wall_ref
        )

    return result
