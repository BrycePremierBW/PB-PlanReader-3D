"""PlanReader v1.7.0 opening evidence contract and safety rules.

Defines the OpeningEvidence dataclass, physical-instance identity,
dimension-basis tracking, tolerance-based deduplication, and deduction
gating rules.

Safety contract:
  - Geometric evidence always creates one record per physical opening,
    quantity = 1.
  - Evidence confidence establishes deduction ELIGIBILITY, not the
    commercial deduct decision. B1-B4 produce eligible/not_eligible.
    B5 converts eligible evidence into actual deductions subject to
    estimator control.
  - dimension_basis is enforced: only rough_opening dimensions are
    eligible for wall-void deduction.
  - Physical-instance dedup requires a geometric position anchor;
    schedule/type records enrich but never collapse instances.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

VERSION = "1.7.0"
SETTING_KEY = "opening_evidence_v170"

# ---------------------------------------------------------------------------
# Dimension basis: what the width/height measurements refer to.
# For wall deduction we need the wall void (rough_opening).
# ---------------------------------------------------------------------------
DIMENSION_BASIS_ROUGH_OPENING = "rough_opening"
DIMENSION_BASIS_FRAME = "frame"
DIMENSION_BASIS_LEAF = "leaf"
DIMENSION_BASIS_CLEAR_OPENING = "clear_opening"
DIMENSION_BASIS_UNKNOWN = "unknown"

DIMENSION_BASIS_VALUES = (
    DIMENSION_BASIS_ROUGH_OPENING,
    DIMENSION_BASIS_FRAME,
    DIMENSION_BASIS_LEAF,
    DIMENSION_BASIS_CLEAR_OPENING,
    DIMENSION_BASIS_UNKNOWN,
)

# Basis priority: higher = more authoritative for wall deduction.
# Only rough_opening is eligible for automatic wall-void deduction.
BASIS_PRIORITY: Dict[str, int] = {
    DIMENSION_BASIS_ROUGH_OPENING: 5,
    DIMENSION_BASIS_CLEAR_OPENING: 4,
    DIMENSION_BASIS_FRAME: 3,
    DIMENSION_BASIS_LEAF: 2,
    DIMENSION_BASIS_UNKNOWN: 1,
}

# ---------------------------------------------------------------------------
# Confidence thresholds
# ---------------------------------------------------------------------------
CONFIDENCE_AUTO_DEDUCT = 0.9     # >= this: auto_eligible
CONFIDENCE_DERIVED_DEDUCT = 0.7  # >= this: derived_eligible
CONFIDENCE_REVIEW = 0.5          # >= this: flag for Review
# < 0.5: record existence only

# ---------------------------------------------------------------------------
# Tolerances for cross-source deduplication
# ---------------------------------------------------------------------------
TOLERANCE_WIDTH_M = 0.05    # 50 mm
TOLERANCE_HEIGHT_M = 0.05   # 50 mm
TOLERANCE_POSITION_M = 0.20 # 200 mm along wall (plan vs elevation)

# ---------------------------------------------------------------------------
# Opening types
# ---------------------------------------------------------------------------
OPENING_TYPE_DOOR = "door"
OPENING_TYPE_WINDOW = "window"
OPENING_TYPE_GLAZED = "glazed_opening"
OPENING_TYPE_GARAGE = "garage_door"
OPENING_TYPE_ROLLER = "roller_door"
OPENING_TYPE_OTHER = "opening"

OPENING_TYPES = (
    OPENING_TYPE_DOOR,
    OPENING_TYPE_WINDOW,
    OPENING_TYPE_GLAZED,
    OPENING_TYPE_GARAGE,
    OPENING_TYPE_ROLLER,
    OPENING_TYPE_OTHER,
)

# ---------------------------------------------------------------------------
# Deduction statuses
# ---------------------------------------------------------------------------
DEDUCTION_AUTO_ELIGIBLE = "auto_eligible"
DEDUCTION_DERIVED_ELIGIBLE = "derived_eligible"
DEDUCTION_REVIEW = "review"
DEDUCTION_NONE = "none"

# Commercial deduction states (set by B5 / estimator)
DEDUCTION_DEDUCTED = "deducted"
DEDUCTION_NOT_DEDUCTED = "not_deducted"

# ---------------------------------------------------------------------------
# Sources that are NOT physical instances (cannot anchor dedup)
# ---------------------------------------------------------------------------
NON_INSTANCE_SOURCES = {"schedule_parse", "manual"}


def _num(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if x == x else default  # NaN check
    except (TypeError, ValueError):
        return default


def _ordered_dedup(items: list) -> list:
    """Remove duplicates preserving order. Uses dict.fromkeys not set."""
    return list(dict.fromkeys(items))


# ---------------------------------------------------------------------------
# OpeningEvidence dataclass
# ---------------------------------------------------------------------------
@dataclass
class OpeningEvidence:
    """One physical opening instance.

    Key rules:
      - opening_instance_id is unique per physical opening (UUID-based).
      - type_mark (W01, D01) is the TYPE mark, NOT physical identity.
        A type mark can repeat many times on the same wall/level.
      - quantity is ALWAYS 1 for geometric (non-manual) evidence.
        Manual/v134 records may retain grouped quantities.
      - dimension_basis records what the width/height refer to.
        Only rough_opening is eligible for wall-void deduction.
      - deduction_status records ELIGIBILITY (auto_eligible, derived_eligible,
        review, none). It does NOT mean deduct=True.
      - deduct is the commercial decision, set only by B5 or estimator.
        B1-B4 must never set deduct=True.
    """

    # --- Identity ---
    opening_instance_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    type_mark: str = ""                 # D01, W01 (type, not instance)
    workspace_id: int = 0
    page_id: Optional[int] = None
    page_no: Optional[int] = None

    # --- Location ---
    wall_ref: str = ""                  # resolved wall reference
    level: str = ""                     # Ground, First, etc.
    room_ref: str = ""                  # adjacent room
    elevation_side: str = ""            # North/South/East/West
    position_along_wall_m: Optional[float] = None  # distance from wall start

    # --- Type ---
    opening_type: str = OPENING_TYPE_OTHER

    # --- Quantity ---
    quantity: int = 1
    _quantity_from_source: str = ""     # "geometric" or "manual/v134"

    # --- Dimensions ---
    width_m: Optional[float] = None
    height_m: Optional[float] = None
    dimension_basis: str = DIMENSION_BASIS_UNKNOWN
    sill_m: float = 0.0                 # 0.0 for doors, 0.9 for windows
    area_m2: Optional[float] = None     # computed: width x height x quantity

    # --- Geometry ---
    plan_geometry: Optional[Dict[str, Any]] = None
    elevation_geometry: Optional[Dict[str, Any]] = None
    source_bbox: Optional[Tuple[float, float, float, float]] = None

    # --- Evidence provenance ---
    schedule_ref: str = ""              # schedule page/mark reference
    extraction_method: str = ""         # plan_vector, elevation_rect,
                                        # schedule_parse, manual

    # --- Confidence ---
    geometry_confidence: float = 0.0
    dimension_confidence: float = 0.0
    association_confidence: float = 0.0

    # --- Deduction status (safety gate: ELIGIBILITY, not decision) ---
    deduction_status: str = DEDUCTION_REVIEW

    # --- Commercial decision (set ONLY by B5 / estimator) ---
    deduct: bool = False

    # --- Provenance ---
    evidence: List[str] = field(default_factory=list)
    notes: str = ""

    def __post_init__(self) -> None:
        """Enforce quantity=1 for geometric sources."""
        if self._quantity_from_source != "manual":
            if self.quantity != 1 and self.extraction_method not in ("", "manual"):
                self.quantity = 1

    def set_quantity(self, qty: int, source: str = "manual") -> None:
        """Set quantity with source tracking.

        source="geometric" forces quantity=1.
        source="manual" allows grouped quantities (v134 records).
        """
        self._quantity_from_source = source
        if source == "geometric":
            self.quantity = 1
        else:
            self.quantity = max(1, int(qty))

    def compute_area(self) -> None:
        """Compute area_m2 from dimensions."""
        if self.width_m is not None and self.height_m is not None:
            self.area_m2 = round(
                self.width_m * self.height_m * max(1, self.quantity), 4
            )
        else:
            self.area_m2 = None

    def _is_geometric_source(self) -> bool:
        """True if this record came from geometric detection (not manual/schedule)."""
        return self.extraction_method not in ("", "manual", "schedule_parse")

    def compute_deduction_status(self) -> None:
        """Set deduction_status based on confidence thresholds and dimension_basis.

        This is the ELIGIBILITY gate, not the commercial deduction decision.
        It sets deduction_status to one of:
          - auto_eligible: high confidence, rough_opening dims, all criteria met
          - derived_eligible: medium confidence, rough_opening dims, criteria met
          - review: insufficient evidence or non-rough_opening dims
          - none: very low confidence

        It does NOT set deduct=True. That is B5's job.
        """
        has_dims = (
            self.width_m is not None
            and self.height_m is not None
            and self.width_m > 0
            and self.height_m > 0
        )
        has_wall = bool(self.wall_ref)

        # dimension_basis check: only rough_opening qualifies for wall deduction
        has_valid_basis = self.dimension_basis == DIMENSION_BASIS_ROUGH_OPENING

        if not has_dims or not has_wall or not has_valid_basis:
            self.deduction_status = DEDUCTION_REVIEW
            return

        min_conf = min(
            self.geometry_confidence,
            self.dimension_confidence,
            self.association_confidence,
        )

        if min_conf >= CONFIDENCE_AUTO_DEDUCT:
            self.deduction_status = DEDUCTION_AUTO_ELIGIBLE
        elif min_conf >= CONFIDENCE_DERIVED_DEDUCT:
            self.deduction_status = DEDUCTION_DERIVED_ELIGIBLE
        elif min_conf >= CONFIDENCE_REVIEW:
            self.deduction_status = DEDUCTION_REVIEW
        else:
            self.deduction_status = DEDUCTION_NONE

    def is_eligible_for_deduction(self) -> bool:
        """True if this opening is eligible for wall-void deduction.

        This does NOT mean deduct=True. It means the evidence meets
        the minimum criteria for B5 to consider it.
        """
        return self.deduction_status in (
            DEDUCTION_AUTO_ELIGIBLE,
            DEDUCTION_DERIVED_ELIGIBLE,
        )


# ---------------------------------------------------------------------------
# Tolerance-based cross-source matching
# ---------------------------------------------------------------------------
def same_physical_opening(
    a: OpeningEvidence,
    b: OpeningEvidence,
    width_tol: float = TOLERANCE_WIDTH_M,
    height_tol: float = TOLERANCE_HEIGHT_M,
    position_tol: float = TOLERANCE_POSITION_M,
) -> bool:
    """True if a and b are provably the same physical opening.

    Requires a genuine geometric position anchor.  Two records match
    only when ALL of:
      - non-empty compatible wall_ref
      - compatible level (or one is blank)
      - BOTH have position_along_wall_m and positions agree within tolerance
      - dimensions agree within tolerance (when both present)

    Schedule/type records without position never anchor a dedup;
    they enrich existing instances by type mark or dimension.
    """
    # Must have non-empty compatible wall_ref
    if not a.wall_ref or not b.wall_ref:
        return False
    if a.wall_ref != b.wall_ref:
        return False

    # Compatible level
    if a.level and b.level and a.level != b.level:
        return False

    # Conflicting specific types: door vs window at same location → not same
    # "opening" (generic) is compatible with anything
    if (a.opening_type != OPENING_TYPE_OTHER
            and b.opening_type != OPENING_TYPE_OTHER
            and a.opening_type != b.opening_type):
        return False

    # MUST have position anchor from BOTH records
    if a.position_along_wall_m is None or b.position_along_wall_m is None:
        return False

    # Position within tolerance
    if abs(a.position_along_wall_m - b.position_along_wall_m) > position_tol + 1e-9:
        return False

    # Width within tolerance (when both present)
    if a.width_m is not None and b.width_m is not None:
        if abs(a.width_m - b.width_m) > width_tol + 1e-9:
            return False

    # Height within tolerance (when both present)
    if a.height_m is not None and b.height_m is not None:
        if abs(a.height_m - b.height_m) > height_tol + 1e-9:
            return False

    return True


def enriches_by_type(
    existing: OpeningEvidence,
    candidate: OpeningEvidence,
) -> bool:
    """True if candidate can enrich existing by type mark or dimension,
    even though they are not the same physical instance (no position match).

    A schedule/manual record enriches a detected instance when:
      - same wall_ref and compatible level
      - candidate has a type_mark (existing may or may not)
      - candidate is a schedule/manual source (not another geometric source)
    """
    if not existing.wall_ref or not candidate.wall_ref:
        return False
    if existing.wall_ref != candidate.wall_ref:
        return False
    if existing.level and candidate.level and existing.level != candidate.level:
        return False
    if not candidate.type_mark:
        return False
    # Enrichment only if marks are compatible (existing has no mark, or marks match)
    if existing.type_mark and existing.type_mark != candidate.type_mark:
        return False
    if candidate.extraction_method not in NON_INSTANCE_SOURCES:
        return False
    return True


# ---------------------------------------------------------------------------
# Dimension source selection: basis + confidence + authority
# ---------------------------------------------------------------------------
def _should_replace_dimensions(
    current_basis: str,
    current_confidence: float,
    current_source: str,
    new_basis: str,
    new_confidence: float,
    new_source: str,
) -> bool:
    """Decide whether new dimensions should replace current.

    Selection is by:
      1. Basis priority (rough_opening > clear > frame > leaf > unknown)
      2. If same basis priority, higher confidence wins
      3. Schedule source is NOT automatically preferred
    """
    cur_pri = BASIS_PRIORITY.get(current_basis, 0)
    new_pri = BASIS_PRIORITY.get(new_basis, 0)

    if new_pri > cur_pri:
        return True
    if new_pri < cur_pri:
        return False

    # Same basis priority: higher confidence wins
    if new_confidence > current_confidence + 1e-9:
        return True
    if new_confidence < current_confidence - 1e-9:
        return False

    # Same confidence: schedule_parse is slightly preferred (schedule is
    # a dimension authority for the same basis)
    if new_source == "schedule_parse" and current_source != "schedule_parse":
        return True

    return False


# ---------------------------------------------------------------------------
# Deduplication: merge duplicate instances
# ---------------------------------------------------------------------------
def merge_opening_evidence(
    existing: OpeningEvidence,
    new: OpeningEvidence,
) -> OpeningEvidence:
    """Merge evidence from two records of the same physical opening.

    Keeps highest-confidence values and merges evidence provenance.
    Dimensions are chosen by basis priority + confidence, not merely
    by source type.
    """
    merged = OpeningEvidence(**asdict(existing))
    merged._quantity_from_source = existing._quantity_from_source

    # Merge evidence sources (ordered dedup, not set)
    merged.evidence = _ordered_dedup(existing.evidence + new.evidence)

    # Upgrade confidence if new source confirms
    merged.geometry_confidence = max(
        existing.geometry_confidence, new.geometry_confidence
    )
    merged.dimension_confidence = max(
        existing.dimension_confidence, new.dimension_confidence
    )
    merged.association_confidence = max(
        existing.association_confidence, new.association_confidence
    )

    # Dimensions: choose by basis + confidence, not by source type
    if new.width_m is not None or new.height_m is not None:
        should_replace = _should_replace_dimensions(
            current_basis=existing.dimension_basis,
            current_confidence=existing.dimension_confidence,
            current_source=existing.extraction_method,
            new_basis=new.dimension_basis,
            new_confidence=new.dimension_confidence,
            new_source=new.extraction_method,
        )
        if should_replace:
            if new.width_m is not None:
                merged.width_m = new.width_m
            if new.height_m is not None:
                merged.height_m = new.height_m
            merged.dimension_basis = new.dimension_basis
        # else: keep existing dims and basis

    # Take mark if new has one and existing doesn't
    if new.type_mark and not merged.type_mark:
        merged.type_mark = new.type_mark

    # Take position if new has one and existing doesn't
    if new.position_along_wall_m is not None and merged.position_along_wall_m is None:
        merged.position_along_wall_m = new.position_along_wall_m

    # Take geometry if new has one and existing doesn't
    if new.plan_geometry and not merged.plan_geometry:
        merged.plan_geometry = new.plan_geometry
    if new.elevation_geometry and not merged.elevation_geometry:
        merged.elevation_geometry = new.elevation_geometry

    # Recompute
    merged.compute_area()
    merged.compute_deduction_status()

    return merged


def deduplicate_openings(
    openings: Sequence[OpeningEvidence],
) -> List[OpeningEvidence]:
    """Deduplicate a list of opening evidence records.

    Two records are merged ONLY when same_physical_opening() confirms
    they are the same physical instance (requires position anchor).

    Schedule/type records that don't match by position may enrich an
    existing instance by type mark via enriches_by_type(), but they
    never collapse physical instances.
    """
    result: List[OpeningEvidence] = []
    for new in openings:
        matched = False
        for i, existing in enumerate(result):
            if same_physical_opening(existing, new):
                result[i] = merge_opening_evidence(existing, new)
                matched = True
                break
            elif enriches_by_type(existing, new):
                # Enrich by type mark / dimension basis, don't merge
                if new.type_mark and not existing.type_mark:
                    result[i].type_mark = new.type_mark
                if new.dimension_basis != DIMENSION_BASIS_UNKNOWN:
                    existing_basis_pri = BASIS_PRIORITY.get(existing.dimension_basis, 0)
                    new_basis_pri = BASIS_PRIORITY.get(new.dimension_basis, 0)
                    if new_basis_pri > existing_basis_pri:
                        result[i].dimension_basis = new.dimension_basis
                if new.evidence:
                    result[i].evidence = _ordered_dedup(existing.evidence + new.evidence)
                matched = True
                break
        if not matched:
            result.append(new)
    return result


# ---------------------------------------------------------------------------
# Bulk deduction calculation
# ---------------------------------------------------------------------------
def deducted_area_m2(openings: Sequence[OpeningEvidence]) -> float:
    """Total deducted area from openings where deduct=True."""
    return round(
        sum(
            o.area_m2
            for o in openings
            if o.deduct and o.area_m2 is not None
        ),
        4,
    )


def net_wall_area_m2(
    gross_wall_m2: float,
    openings: Sequence[OpeningEvidence],
) -> float:
    """Net wall area after deductions. Never negative."""
    return round(
        max(0.0, _num(gross_wall_m2) - deducted_area_m2(openings)),
        4,
    )


# ---------------------------------------------------------------------------
# Conversion to/from v134 register format
# ---------------------------------------------------------------------------
def to_v134_record(opening: OpeningEvidence) -> Dict[str, Any]:
    """Convert OpeningEvidence to v134 register format."""
    return {
        "id": opening.opening_instance_id,
        "kind": opening.opening_type.replace("_", " ").title(),
        "label": opening.type_mark or opening.opening_type,
        "wall_ref": opening.wall_ref,
        "width_m": opening.width_m or 0.0,
        "height_m": opening.height_m or 0.0,
        "quantity": opening.quantity,
        "deduct": opening.deduct,
        "source_reference": "; ".join(opening.evidence) if opening.evidence else "",
        "confidence": opening.deduction_status,
    }


def _classify_opening_type(kind: str) -> str:
    """Classify opening type from kind string. Check specific types first."""
    kind_lower = kind.lower()
    if "garage" in kind_lower:
        return OPENING_TYPE_GARAGE
    if "roller" in kind_lower:
        return OPENING_TYPE_ROLLER
    if "glaz" in kind_lower:
        return OPENING_TYPE_GLAZED
    if "window" in kind_lower:
        return OPENING_TYPE_WINDOW
    if "door" in kind_lower:
        return OPENING_TYPE_DOOR
    return OPENING_TYPE_OTHER


def from_v134_record(
    record: Dict[str, Any],
    workspace_id: int = 0,
) -> OpeningEvidence:
    """Convert a v134 register record to OpeningEvidence.

    v134 records may have quantity > 1 (grouped commercial allowances).
    These are preserved as manual-grouped records.
    """
    kind = str(record.get("kind", ""))
    opening_type = _classify_opening_type(kind)

    width = _num(record.get("width_m"))
    height = _num(record.get("height_m"))
    quantity = max(1, int(_num(record.get("quantity"), 1)))

    ev = OpeningEvidence(
        opening_instance_id=str(record.get("id", uuid.uuid4().hex[:12])),
        type_mark=str(record.get("label", "")),
        workspace_id=workspace_id,
        wall_ref=str(record.get("wall_ref", "")),
        opening_type=opening_type,
        width_m=width if width > 0 else None,
        height_m=height if height > 0 else None,
        dimension_basis=DIMENSION_BASIS_UNKNOWN,
        sill_m=0.0 if opening_type in (OPENING_TYPE_DOOR, OPENING_TYPE_GARAGE, OPENING_TYPE_ROLLER) else 0.9,
        extraction_method="manual",
        evidence=[str(record.get("source_reference", ""))],
    )
    ev.set_quantity(quantity, source="manual")
    ev.compute_area()

    # For v134 records, respect the existing deduct toggle
    ev.deduct = bool(record.get("deduct", False))
    if ev.deduct and ev.area_m2 and ev.area_m2 > 0:
        ev.deduction_status = DEDUCTION_DEDUCTED

    return ev
