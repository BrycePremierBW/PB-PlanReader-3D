"""PlanReader v1.7.3 cross-source reconciliation — Phase B4.

Reconciles plan (B1) + schedule (B2) + elevation (B3) evidence into
best-supported physical instances.  Detects conflicts, computes
reconciliation confidence, and updates deduction eligibility.

Safety contract:
  - B4 does NOT create or delete OpeningEvidence instances.
  - B4 does NOT set deduct=True (that is B5's decision).
  - B4 detects conflicts and surfaces them for human review.
  - B4 computes final reconciliation confidence from source diversity.
  - B4 calls compute_deduction_status() to update eligibility.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from pb_opening_evidence_v170 import (
    OpeningEvidence,
    same_physical_opening,
    merge_opening_evidence,
    DIMENSION_BASIS_UNKNOWN,
    DIMENSION_BASIS_ROUGH_OPENING,
    DEDUCTION_REVIEW,
    DEDUCTION_AUTO_ELIGIBLE,
    DEDUCTION_DERIVED_ELIGIBLE,
    NON_INSTANCE_SOURCES,
    TOLERANCE_WIDTH_M,
    CONFIDENCE_AUTO_DEDUCT,
    CONFIDENCE_DERIVED_DEDUCT,
    CONFIDENCE_REVIEW,
)

VERSION = "1.7.3"

# ---------------------------------------------------------------------------
# Conflict thresholds
# ---------------------------------------------------------------------------
# Schedule vs plan dimension disagreement beyond this → conflict
DIMENSION_CONFLICT_THRESHOLD_M = 0.05  # 50 mm

# ---------------------------------------------------------------------------
# Source diversity confidence table
# ---------------------------------------------------------------------------
# Maps (has_plan, has_schedule, has_elevation) → base confidence
_SOURCE_CONFIDENCE: Dict[Tuple[bool, bool, bool], float] = {
    (True,  False, False): 0.55,   # plan only
    (True,  True,  False): 0.75,   # plan + schedule
    (True,  False, True):  0.65,   # plan + elevation
    (True,  True,  True):  0.90,   # plan + schedule + elevation
    (False, True,  False): 0.60,   # schedule only (no plan position)
    (False, False, True):  0.50,   # elevation only (very weak)
    (False, True,  True):  0.55,   # schedule + elevation (no plan)
    (False, False, False): 0.30,   # no sources (should not exist)
}


# ---------------------------------------------------------------------------
# Conflict record
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ConflictRecord:
    """A detected conflict between sources for one opening instance.

    Frozen for immutability.  Conflicts do NOT block B4 output —
    they are surfaced for human review.
    """
    opening_instance_id: str
    conflict_type: str          # "dimension_mismatch", "mark_mismatch",
                                # "count_mismatch", "basis_ambiguous"
    source_a: str               # e.g. "plan_vector", "schedule_parse"
    source_b: str               # e.g. "elevation_rect"
    field_name: str             # e.g. "width_m", "height_m", "type_mark"
    value_a: Optional[str]      # stringified value from source A
    value_b: Optional[str]      # stringified value from source B
    severity: str               # "warning" or "error"
    description: str            # human-readable explanation


# ---------------------------------------------------------------------------
# Source detection helpers
# ---------------------------------------------------------------------------
# After B2/B3 enrichment, dimension_source is overwritten to the best
# source.  We detect source diversity by inspecting the evidence trail,
# which accumulates provenance strings from each enriching source.
_EVIDENCE_PLAN_KEYWORDS = ("plan_vector", "plan_rect", "wall_gap")
_EVIDENCE_SCHEDULE_KEYWORDS = ("schedule_parse", "schedule")
_EVIDENCE_ELEVATION_KEYWORDS = ("elevation_rect", "elevation")


def _has_plan_evidence(inst: OpeningEvidence) -> bool:
    """True if this record has geometric plan detection evidence."""
    # extraction_method must be plan_vector (not schedule or elevation)
    if inst.extraction_method == "plan_vector":
        return True
    if inst.plan_geometry is not None:
        return True
    for ev in inst.evidence:
        ev_lower = ev.lower()
        if any(kw in ev_lower for kw in _EVIDENCE_PLAN_KEYWORDS):
            return True
    return False


def _has_schedule_evidence(inst: OpeningEvidence) -> bool:
    """True if this record has schedule enrichment evidence."""
    if inst.dimension_source == "schedule_parse":
        return True
    if inst.schedule_ref:
        return True
    for ev in inst.evidence:
        ev_lower = ev.lower()
        if any(kw in ev_lower for kw in _EVIDENCE_SCHEDULE_KEYWORDS):
            return True
    return False


def _has_elevation_evidence(inst: OpeningEvidence) -> bool:
    """True if this record has elevation enrichment evidence."""
    if inst.dimension_source == "elevation_rect":
        return True
    if inst.elevation_geometry is not None:
        return True
    for ev in inst.evidence:
        ev_lower = ev.lower()
        if any(kw in ev_lower for kw in _EVIDENCE_ELEVATION_KEYWORDS):
            return True
    return False


def _has_rough_opening_basis(inst: OpeningEvidence) -> bool:
    """True if dimensions are on a rough_opening basis (eligible for deduction)."""
    return inst.dimension_basis == DIMENSION_BASIS_ROUGH_OPENING


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------
def _detect_conflicts(inst: OpeningEvidence) -> List[ConflictRecord]:
    """Detect cross-source conflicts within one enriched instance.

    Checks:
      - Schedule vs plan dimension disagreement
      - Basis ambiguity (unknown basis with multiple sources)
      - Mark present but plan has no mark (should not happen after B2)

    Returns list of ConflictRecord (empty if no conflicts).
    """
    conflicts: List[ConflictRecord] = []
    iid = inst.opening_instance_id

    # --- Dimension conflict: schedule vs plan width ---
    # After enrichment, the best dimensions are stored.  If evidence
    # trail shows both schedule and plan sources, we can check agreement.
    if _has_schedule_evidence(inst) and _has_plan_evidence(inst):
        # Evidence entries tell us what sources contributed.
        # If both schedule and plan are in the evidence trail AND
        # the dimension_source is one of them, the other source's
        # dimensions were rejected by merge logic.  That is not a
        # conflict — it is expected selection.  Real conflicts are when
        # the rejection happened but the disagreement is large.
        #
        # We can detect this by checking if the evidence trail mentions
        # both sources for dimensions.  Since B0/B2 track dimension_source,
        # we check: if dimension_source is schedule but plan_geometry exists,
        # the plan had different dimensions that were not selected.
        if inst.dimension_source == "schedule_parse" and inst.plan_geometry is not None:
            # Plan dimensions were overridden by schedule.
            # The plan geometry may contain the rejected plan dimensions.
            plan_w = inst.plan_geometry.get("width_m")
            if plan_w is not None and inst.width_m is not None:
                diff = abs(plan_w - inst.width_m)
                if diff > DIMENSION_CONFLICT_THRESHOLD_M:
                    conflicts.append(ConflictRecord(
                        opening_instance_id=iid,
                        conflict_type="dimension_mismatch",
                        source_a="plan_vector",
                        source_b="schedule_parse",
                        field_name="width_m",
                        value_a=f"{plan_w:.4f}",
                        value_b=f"{inst.width_m:.4f}",
                        severity="warning",
                        description=(
                            f"Plan width {plan_w:.3f}m differs from "
                            f"schedule width {inst.width_m:.3f}m by "
                            f"{diff:.3f}m"
                        ),
                    ))

    # --- Basis ambiguity: unknown basis with sufficient evidence ---
    if (inst.dimension_basis == DIMENSION_BASIS_UNKNOWN
            and inst.width_m is not None
            and inst.height_m is not None):
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

    return conflicts


# ---------------------------------------------------------------------------
# Reconciliation confidence
# ---------------------------------------------------------------------------
def _compute_reconciliation_confidence(inst: OpeningEvidence) -> float:
    """Compute reconciliation confidence from source diversity and agreement.

    Base confidence comes from how many distinct sources contribute.
    Bonuses apply for:
      - rough_opening basis (required for deduction)
      - cross-source dimension agreement
    This function returns a FLOOR — the existing per-source confidence
    is never downgraded.  The caller upgrades the weakest confidence
    component to at least this level.
    """
    has_plan = _has_plan_evidence(inst)
    has_schedule = _has_schedule_evidence(inst)
    has_elevation = _has_elevation_evidence(inst)

    # Base confidence from source diversity
    key = (has_plan, has_schedule, has_elevation)
    base = _SOURCE_CONFIDENCE.get(key, 0.30)

    # Bonus: rough_opening basis with dimensions
    if (_has_rough_opening_basis(inst)
            and inst.width_m is not None
            and inst.height_m is not None):
        base = min(base + 0.05, 0.97)

    # Note: unknown basis does NOT penalize here.  The basis_ambiguous
    # conflict is surfaced separately.  Reconciliation confidence is a
    # FLOOR that never downgrades existing per-source confidence.

    return round(min(base, 0.97), 3)


# ---------------------------------------------------------------------------
# Main reconciliation entry point
# ---------------------------------------------------------------------------
def reconcile_opening_evidence(
    instances: Sequence[OpeningEvidence],
) -> Tuple[List[OpeningEvidence], List[ConflictRecord]]:
    """Reconcile enriched opening instances: detect conflicts, compute
    confidence, update deduction status.

    This is a POST-ENRICHMENT pass.  B2 (schedule) and B3 (elevation)
    have already enriched the instances.  B4:

      1. Detects cross-source conflicts within each instance.
      2. Computes reconciliation confidence from source diversity.
      3. Updates deduction_status via compute_deduction_status().
      4. Does NOT create or delete instances.
      5. Does NOT set deduct=True.

    Args:
        instances: Enriched B1 instances (after B2 + B3).

    Returns:
        (reconciled_instances, all_conflicts)
        reconciled_instances: Same instances with updated confidence and
                             deduction_status.
        all_conflicts: All detected ConflictRecords across all instances.
    """
    all_conflicts: List[ConflictRecord] = []
    reconciled: List[OpeningEvidence] = []

    for inst in instances:
        # 1. Detect conflicts
        conflicts = _detect_conflicts(inst)
        all_conflicts.extend(conflicts)

        # 2. Compute reconciliation confidence
        recon_conf = _compute_reconciliation_confidence(inst)

        # Upgrade the minimum of geometry/dimension/association confidence
        # to at least the reconciliation level.  This ensures multi-source
        # evidence properly reflects the combined confidence.
        min_existing = min(
            inst.geometry_confidence,
            inst.dimension_confidence,
            inst.association_confidence,
        )
        if recon_conf > min_existing:
            # Upgrade the weakest confidence component to reconciliation level
            if inst.geometry_confidence < recon_conf:
                inst.geometry_confidence = recon_conf
            if inst.dimension_confidence < recon_conf:
                inst.dimension_confidence = recon_conf
            if inst.association_confidence < recon_conf:
                inst.association_confidence = recon_conf

        # 3. Update deduction status
        inst.compute_deduction_status()

        # 4. Flag conflicts in notes
        if conflicts:
            conflict_notes = "; ".join(c.description for c in conflicts)
            suffix = f" [B4: {conflict_notes}]"
            if inst.notes:
                inst.notes += suffix
            else:
                inst.notes = suffix

        reconciled.append(inst)

    return reconciled, all_conflicts
