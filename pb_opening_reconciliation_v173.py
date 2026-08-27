"""PlanReader v1.7.3 cross-source reconciliation — Phase B4.

Reconciles plan (B1) + schedule (B2) + elevation (B3) evidence into
best-supported physical instances.  Detects conflicts between source
observations and surfaces them for human review.

Safety contract:
  - B4 does NOT create or delete OpeningEvidence instances.
  - B4 does NOT set deduct=True (that is B5's decision).
  - B4 does NOT overwrite geometry_confidence, dimension_confidence,
    or association_confidence.  Those fields reflect per-source quality
    and are only upgraded when that particular property is directly
    corroborated.
  - B4 writes reconciliation_confidence (separate field) based on
    source diversity and agreement.
  - Unresolved dimension/identity conflicts force deduction_status
    to review, regardless of confidence values.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pb_opening_evidence_v170 import (
    OpeningEvidence,
    DIMENSION_BASIS_UNKNOWN,
    DIMENSION_BASIS_ROUGH_OPENING,
    DEDUCTION_REVIEW,
    DEDUCTION_AUTO_ELIGIBLE,
    DEDUCTION_DERIVED_ELIGIBLE,
    TOLERANCE_WIDTH_M,
)

VERSION = "1.7.3"

# ---------------------------------------------------------------------------
# Conflict thresholds
# ---------------------------------------------------------------------------
DIMENSION_CONFLICT_THRESHOLD_M = 0.05  # 50 mm — plan vs schedule disagreement

# Source IDs (not free-text — explicit constants)
SOURCE_PLAN = "plan_vector"
SOURCE_SCHEDULE = "schedule_parse"
SOURCE_ELEVATION = "elevation_rect"
SOURCE_PHYSICAL_CONFLICT = "physical_instance_conflict"

# ---------------------------------------------------------------------------
# Source diversity confidence table
# ---------------------------------------------------------------------------
# Maps (has_plan, has_schedule, has_elevation) → base confidence
_SOURCE_CONFIDENCE: Dict[Tuple[bool, bool, bool], float] = {
    (True,  False, False): 0.55,   # plan only
    (True,  True,  False): 0.75,   # plan + schedule
    (True,  False, True):  0.65,   # plan + elevation
    (True,  True,  True):  0.90,   # plan + schedule + elevation
    (False, True,  False): 0.60,   # schedule only
    (False, False, True):  0.50,   # elevation only
    (False, True,  True):  0.55,   # schedule + elevation
    (False, False, False): 0.30,   # no sources (should not exist)
}


# ---------------------------------------------------------------------------
# Conflict record
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ConflictRecord:
    """A detected conflict between source observations for one instance.

    Frozen for immutability.  Conflicts do NOT block B4 output —
    the instance stays in the register — but they block automatic
    deduction eligibility.
    """
    opening_instance_id: str
    conflict_type: str          # "dimension_mismatch", "mark_mismatch",
                                # "basis_ambiguous", "count_mismatch"
    source_a: str               # e.g. "plan_vector"
    source_b: str               # e.g. "schedule_parse"
    field_name: str             # e.g. "width_m", "height_m", "type_mark"
    value_a: Optional[str]      # stringified value from source A
    value_b: Optional[str]      # stringified value from source B
    severity: str               # "warning" or "error"
    description: str            # human-readable explanation


# ---------------------------------------------------------------------------
# Source detection from structured observations
# ---------------------------------------------------------------------------
def _obs_sources(inst: OpeningEvidence) -> Dict[str, bool]:
    """Determine which source types contributed VALID observations.

    Uses explicit source IDs from source_observations, NOT free-text
    substring matching on evidence strings.  Ambiguous observations
    (status="ambiguous") do NOT count as corroborating evidence.
    """
    sources: Dict[str, bool] = {
        SOURCE_PLAN: False,
        SOURCE_SCHEDULE: False,
        SOURCE_ELEVATION: False,
    }
    for obs in inst.source_observations:
        src = obs.get("source", "")
        if src in sources and obs.get("status") != "ambiguous":
            sources[src] = True
    return sources


def _has_plan_evidence(inst: OpeningEvidence) -> bool:
    return _obs_sources(inst)[SOURCE_PLAN]


def _has_schedule_evidence(inst: OpeningEvidence) -> bool:
    return _obs_sources(inst)[SOURCE_SCHEDULE]


def _has_elevation_evidence(inst: OpeningEvidence) -> bool:
    return _obs_sources(inst)[SOURCE_ELEVATION]


def _has_rough_opening_basis(inst: OpeningEvidence) -> bool:
    return inst.dimension_basis == DIMENSION_BASIS_ROUGH_OPENING


# ---------------------------------------------------------------------------
# Conflict detection from structured observations
# ---------------------------------------------------------------------------
def _detect_conflicts(inst: OpeningEvidence) -> List[ConflictRecord]:
    """Detect cross-source conflicts by comparing structured observations.

    Compares every applicable source pair for width, height, mark, and
    basis disagreements.  Distinguishes agreement, disagreement, and
    not-comparable (one source lacks the field).

    Returns list of ConflictRecord (empty if no conflicts).
    """
    conflicts: List[ConflictRecord] = []
    iid = inst.opening_instance_id
    obs = inst.source_observations

    if len(obs) < 2:
        return conflicts  # need at least 2 sources for conflict

    # Group observations by source
    by_source: Dict[str, List[Dict[str, Any]]] = {}
    for o in obs:
        src = o.get("source", "")
        by_source.setdefault(src, []).append(o)

    # Compare each pair of sources
    source_pairs = [
        (SOURCE_PLAN, SOURCE_SCHEDULE),
        (SOURCE_PLAN, SOURCE_ELEVATION),
        (SOURCE_SCHEDULE, SOURCE_ELEVATION),
    ]

    for src_a, src_b in source_pairs:
        obs_a = by_source.get(src_a, [])
        obs_b = by_source.get(src_b, [])
        if not obs_a or not obs_b:
            continue  # not comparable — one source absent

        # Use the accepted observation from each source (if any)
        # or the last observation (most recent)
        best_a = _best_obs(obs_a)
        best_b = _best_obs(obs_b)

        if best_a is None or best_b is None:
            continue

        # --- Width conflict ---
        w_a = best_a.get("width_m")
        w_b = best_b.get("width_m")
        if w_a is not None and w_b is not None:
            diff = abs(w_a - w_b)
            if diff > DIMENSION_CONFLICT_THRESHOLD_M:
                conflicts.append(ConflictRecord(
                    opening_instance_id=iid,
                    conflict_type="dimension_mismatch",
                    source_a=src_a,
                    source_b=src_b,
                    field_name="width_m",
                    value_a=f"{w_a:.4f}",
                    value_b=f"{w_b:.4f}",
                    severity="warning",
                    description=(
                        f"{src_a} width {w_a:.3f}m differs from "
                        f"{src_b} width {w_b:.3f}m by {diff:.3f}m"
                    ),
                ))

        # --- Height conflict ---
        h_a = best_a.get("height_m")
        h_b = best_b.get("height_m")
        if h_a is not None and h_b is not None:
            diff = abs(h_a - h_b)
            if diff > DIMENSION_CONFLICT_THRESHOLD_M:
                conflicts.append(ConflictRecord(
                    opening_instance_id=iid,
                    conflict_type="dimension_mismatch",
                    source_a=src_a,
                    source_b=src_b,
                    field_name="height_m",
                    value_a=f"{h_a:.4f}",
                    value_b=f"{h_b:.4f}",
                    severity="warning",
                    description=(
                        f"{src_a} height {h_a:.3f}m differs from "
                        f"{src_b} height {h_b:.3f}m by {diff:.3f}m"
                    ),
                ))

        # --- Mark conflict ---
        m_a = best_a.get("type_mark", "")
        m_b = best_b.get("type_mark", "")
        if m_a and m_b and m_a.upper() != m_b.upper():
            conflicts.append(ConflictRecord(
                opening_instance_id=iid,
                conflict_type="mark_mismatch",
                source_a=src_a,
                source_b=src_b,
                field_name="type_mark",
                value_a=m_a,
                value_b=m_b,
                severity="warning",
                description=(
                    f"{src_a} mark '{m_a}' differs from "
                    f"{src_b} mark '{m_b}'"
                ),
            ))

        # --- Basis conflict ---
        b_a = best_a.get("dimension_basis", "")
        b_b = best_b.get("dimension_basis", "")
        if (b_a and b_b
                and b_a != b_b
                and b_a != DIMENSION_BASIS_UNKNOWN
                and b_b != DIMENSION_BASIS_UNKNOWN):
            conflicts.append(ConflictRecord(
                opening_instance_id=iid,
                conflict_type="basis_disagreement",
                source_a=src_a,
                source_b=src_b,
                field_name="dimension_basis",
                value_a=b_a,
                value_b=b_b,
                severity="warning",
                description=(
                    f"{src_a} basis '{b_a}' differs from "
                    f"{src_b} basis '{b_b}'"
                ),
            ))

    # --- Basis ambiguity: multiple sources but unknown basis ---
    if inst.dimension_basis == DIMENSION_BASIS_UNKNOWN:
        source_count = sum([
            _has_plan_evidence(inst),
            _has_schedule_evidence(inst),
            _has_elevation_evidence(inst),
        ])
        if source_count >= 2:
            conflicts.append(ConflictRecord(
                opening_instance_id=iid,
                conflict_type="basis_ambiguous",
                source_a="multiple",
                source_b="",
                field_name="dimension_basis",
                value_a=inst.dimension_basis,
                value_b="rough_opening expected",
                severity="warning",
                description=(
                    f"Multiple sources agree on dimensions but basis "
                    f"remains unknown — cannot confirm rough_opening "
                    f"for wall deduction"
                ),
            ))

    # --- Source ambiguity: internally contradictory observations ---
    for obs in inst.source_observations:
        if obs.get("status") == "ambiguous":
            alternatives = obs.get("alternatives", [])
            if alternatives:
                alt_strs = [
                    f"{a.get('width_mm', '?')}×{a.get('height_mm', '?')}mm"
                    for a in alternatives
                ]
                desc = (
                    f"{obs['source']} has conflicting alternatives for "
                    f"mark '{obs.get('type_mark', '?')}': "
                    f"{', '.join(alt_strs)}"
                )
            else:
                desc = (
                    f"{obs['source']} has internally conflicting "
                    f"observations for mark '{obs.get('type_mark', '?')}'"
                )
            conflicts.append(ConflictRecord(
                opening_instance_id=iid,
                conflict_type="source_ambiguous",
                source_a=obs["source"],
                source_b="",
                field_name="source_observations",
                value_a=obs.get("status"),
                value_b=str(len(alternatives)) if alternatives else "",
                severity="error",
                description=desc,
            ))

    # --- Physical instance identity conflict ---
    # B5 marks candidates when geometric identity says they are the same
    # physical opening but their explicit marks conflict (D01 + D02).
    for obs in inst.source_observations:
        if obs.get("source") == SOURCE_PHYSICAL_CONFLICT:
            conflicts.append(ConflictRecord(
                opening_instance_id=iid,
                conflict_type="physical_instance_conflict",
                source_a=obs.get("conflicting_id", ""),
                source_b=obs.get("type_mark", ""),
                field_name="type_mark",
                value_a=obs.get("conflicting_mark", ""),
                value_b=obs.get("type_mark", ""),
                severity="error",
                description=obs.get("description", (
                    "Geometrically overlapping candidates have "
                    "conflicting identity marks"
                )),
            ))

    return conflicts


def _best_obs(observations: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Select the best observation from a list for the same source.

    Prefers the accepted observation; falls back to the last one.
    """
    accepted = [o for o in observations if o.get("accepted")]
    if accepted:
        return accepted[-1]
    return observations[-1] if observations else None


# ---------------------------------------------------------------------------
# Reconciliation confidence (separate from per-source confidence)
# ---------------------------------------------------------------------------
def _compute_reconciliation_confidence(inst: OpeningEvidence) -> float:
    """Compute reconciliation confidence from source diversity.

    This is written to inst.reconciliation_confidence — it does NOT
    overwrite geometry_confidence, dimension_confidence, or
    association_confidence.

    Base confidence comes from how many distinct source types contributed
    structured observations.  Bonus for rough_opening basis.
    """
    has_plan = _has_plan_evidence(inst)
    has_schedule = _has_schedule_evidence(inst)
    has_elevation = _has_elevation_evidence(inst)

    key = (has_plan, has_schedule, has_elevation)
    base = _SOURCE_CONFIDENCE.get(key, 0.30)

    # Bonus: rough_opening basis with dimensions
    if (_has_rough_opening_basis(inst)
            and inst.width_m is not None
            and inst.height_m is not None):
        base = min(base + 0.05, 0.97)

    return round(min(base, 0.97), 3)


# ---------------------------------------------------------------------------
# Main reconciliation entry point
# ---------------------------------------------------------------------------
def reconcile_opening_evidence(
    instances: Sequence[OpeningEvidence],
) -> Tuple[List[OpeningEvidence], List[ConflictRecord]]:
    """Reconcile enriched opening instances: detect conflicts, compute
    reconciliation confidence, update deduction status.

    This is a POST-ENRICHMENT pass.  B2 (schedule) and B3 (elevation)
    have already enriched the instances and appended source observations.
    B4:

      1. Detects conflicts between structured source observations.
      2. Computes reconciliation_confidence (separate field).
      3. If conflicts exist, forces deduction_status = review.
      4. Does NOT overwrite per-source confidence fields.
      5. Does NOT create or delete instances.
      6. Does NOT set deduct=True.

    Args:
        instances: Enriched B1 instances (after B2 + B3).

    Returns:
        (reconciled_instances, all_conflicts)
        reconciled_instances: Same instances with updated
                             reconciliation_confidence and
                             deduction_status.
        all_conflicts: All detected ConflictRecords across all instances.
    """
    all_conflicts: List[ConflictRecord] = []
    reconciled: List[OpeningEvidence] = []

    for inst in instances:
        # 1. Detect conflicts
        conflicts = _detect_conflicts(inst)
        all_conflicts.extend(conflicts)

        # 2. Compute reconciliation confidence (separate field)
        inst.reconciliation_confidence = _compute_reconciliation_confidence(inst)

        # 3. If conflicts exist, force review (do NOT call
        #    compute_deduction_status which might set eligible)
        if conflicts:
            inst.deduction_status = DEDUCTION_REVIEW
        else:
            # No conflicts: recompute eligibility from existing confidence
            inst.compute_deduction_status()

        # 4. Flag conflicts in notes
        if conflicts:
            conflict_notes = "; ".join(c.description for c in conflicts)
            suffix = f" [B4: {conflict_notes}]"
            if inst.notes:
                inst.notes += suffix
            else:
                inst.notes = suffix

        # 5. Mark reconciliation complete (B5 requires this)
        inst.reconciliation_complete = True

        reconciled.append(inst)

    return reconciled, all_conflicts
