"""PlanReader v1.7.0 opening evidence contract and safety rules.

Defines the OpeningEvidence dataclass, physical-instance identity,
dimension-basis tracking, tolerance-based deduplication, and deduction
gating rules.

Safety contract:
  - Geometric evidence always creates one record per physical opening,
    quantity = 1.
  - deduct defaults to False for all auto-detected evidence.
  - Only confirmed, wall-associated, dimension-known instances may
    set deduct = True.
  - Uncertain openings never alter net wall m2.
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

# ---------------------------------------------------------------------------
# Confidence thresholds
# ---------------------------------------------------------------------------
CONFIDENCE_AUTO_DEDUCT = 0.9     # >= this: eligible for auto-deduct
CONFIDENCE_DERIVED_DEDUCT = 0.7  # >= this: deduct with "Derived" status
CONFIDENCE_REVIEW = 0.5          # >= this: flag for Review, no deduction
# < 0.5: record existence only, no deduction

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
DEDUCTION_DEDUCTED = "deducted"
DEDUCTION_NOT_DEDUCTED = "not_deducted"
DEDUCTION_REVIEW = "review"


def _num(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if x == x else default  # NaN check
    except (TypeError, ValueError):
        return default


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
      - quantity is ALWAYS 1 for geometric evidence. Grouped commercial
        allowances are a v134 estimator concept.
      - dimension_basis records what the width/height refer to.
        Unknown basis -> lower dimension_confidence.
      - deduct defaults to False. Only confirmed instances may set True.
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

    # --- Quantity (always 1 for geometric evidence) ---
    quantity: int = 1

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

    # --- Deduction status (safety gate) ---
    deduct: bool = False                # DEFAULT False for auto-detected
    deduction_status: str = DEDUCTION_REVIEW

    # --- Provenance ---
    evidence: List[str] = field(default_factory=list)
    notes: str = ""

    def compute_area(self) -> None:
        """Compute area_m2 from dimensions. Called after setting width/height."""
        if self.width_m is not None and self.height_m is not None:
            self.area_m2 = round(
                self.width_m * self.height_m * max(1, self.quantity), 4
            )
        else:
            self.area_m2 = None

    def compute_deduction_status(self) -> None:
        """Set deduction_status based on confidence thresholds.

        This is the safety gate. An opening must meet ALL criteria
        to be eligible for deduction:
          - geometry_confidence >= CONFIDENCE_DERIVED_DEDUCT
          - dimension_confidence >= CONFIDENCE_DERIVED_DEDUCT
          - association_confidence >= CONFIDENCE_DERIVED_DEDUCT
          - width_m and height_m are known
          - wall_ref is resolved
        """
        has_dims = (
            self.width_m is not None
            and self.height_m is not None
            and self.width_m > 0
            and self.height_m > 0
        )
        has_wall = bool(self.wall_ref)

        if not has_dims or not has_wall:
            self.deduction_status = DEDUCTION_REVIEW
            self.deduct = False
            return

        min_conf = min(
            self.geometry_confidence,
            self.dimension_confidence,
            self.association_confidence,
        )

        if min_conf >= CONFIDENCE_AUTO_DEDUCT:
            self.deduction_status = DEDUCTION_DEDUCTED
            self.deduct = True
        elif min_conf >= CONFIDENCE_DERIVED_DEDUCT:
            self.deduction_status = DEDUCTION_DEDUCTED
            self.deduct = True
        elif min_conf >= CONFIDENCE_REVIEW:
            self.deduction_status = DEDUCTION_REVIEW
            self.deduct = False
        else:
            self.deduction_status = DEDUCTION_REVIEW
            self.deduct = False


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
    """True if a and b are the same physical opening seen from different sources.

    Uses geometric position and dimension tolerances, NOT type marks.
    Type marks are not compared because the same mark can appear on
    different physical openings.
    """
    # Must be same wall and same level
    if a.wall_ref != b.wall_ref:
        return False
    if a.level and b.level and a.level != b.level:
        return False

    # Width within tolerance (with float epsilon)
    if a.width_m is not None and b.width_m is not None:
        if abs(a.width_m - b.width_m) > width_tol + 1e-9:
            return False

    # Height within tolerance (with float epsilon)
    if a.height_m is not None and b.height_m is not None:
        if abs(a.height_m - b.height_m) > height_tol + 1e-9:
            return False

    # Position along wall within tolerance (with float epsilon)
    if (
        a.position_along_wall_m is not None
        and b.position_along_wall_m is not None
    ):
        if abs(a.position_along_wall_m - b.position_along_wall_m) > position_tol + 1e-9:
            return False

    return True


# ---------------------------------------------------------------------------
# Deduplication: merge duplicate instances
# ---------------------------------------------------------------------------
def merge_opening_evidence(
    existing: OpeningEvidence,
    new: OpeningEvidence,
) -> OpeningEvidence:
    """Merge evidence from two records of the same physical opening.

    Keeps highest-confidence values and merges evidence provenance.
    Prefers schedule dimensions over geometric estimation when available.
    """
    merged = OpeningEvidence(**asdict(existing))

    # Merge evidence sources (deduplicated)
    merged.evidence = list(set(existing.evidence + new.evidence))

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

    # Prefer schedule dimensions over geometric estimation
    if new.extraction_method == "schedule_parse":
        if new.width_m is not None:
            merged.width_m = new.width_m
            merged.dimension_basis = new.dimension_basis
        if new.height_m is not None:
            merged.height_m = new.height_m
            merged.dimension_basis = new.dimension_basis

    # Prefer more specific dimension basis
    basis_priority = {
        DIMENSION_BASIS_ROUGH_OPENING: 5,
        DIMENSION_BASIS_CLEAR_OPENING: 4,
        DIMENSION_BASIS_FRAME: 3,
        DIMENSION_BASIS_LEAF: 2,
        DIMENSION_BASIS_UNKNOWN: 1,
    }
    if basis_priority.get(new.dimension_basis, 0) > basis_priority.get(
        merged.dimension_basis, 0
    ):
        merged.dimension_basis = new.dimension_basis

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

    Returns a new list with duplicates merged. The first occurrence
    of each physical opening is kept as the base record.
    """
    result: List[OpeningEvidence] = []
    for new in openings:
        matched = False
        for i, existing in enumerate(result):
            if same_physical_opening(existing, new):
                result[i] = merge_opening_evidence(existing, new)
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


def from_v134_record(
    record: Dict[str, Any],
    workspace_id: int = 0,
) -> OpeningEvidence:
    """Convert a v134 register record to OpeningEvidence.

    v134 records may have quantity > 1 (grouped commercial allowances).
    For geometric evidence, quantity should always be 1.
    """
    kind = str(record.get("kind", "")).lower()
    opening_type = OPENING_TYPE_OTHER
    if "door" in kind:
        opening_type = OPENING_TYPE_DOOR
    elif "window" in kind:
        opening_type = OPENING_TYPE_WINDOW
    elif "glaz" in kind:
        opening_type = OPENING_TYPE_GLAZED
    elif "garage" in kind:
        opening_type = OPENING_TYPE_GARAGE
    elif "roller" in kind:
        opening_type = OPENING_TYPE_ROLLER

    width = _num(record.get("width_m"))
    height = _num(record.get("height_m"))
    quantity = max(1, int(_num(record.get("quantity"), 1)))

    ev = OpeningEvidence(
        opening_instance_id=str(record.get("id", uuid.uuid4().hex[:12])),
        type_mark=str(record.get("label", "")),
        workspace_id=workspace_id,
        wall_ref=str(record.get("wall_ref", "")),
        opening_type=opening_type,
        quantity=quantity,
        width_m=width if width > 0 else None,
        height_m=height if height > 0 else None,
        dimension_basis=DIMENSION_BASIS_UNKNOWN,
        sill_m=0.0 if opening_type == OPENING_TYPE_DOOR else 0.9,
        deduction_status=DEDUCTION_REVIEW,
        extraction_method="manual",
        evidence=[str(record.get("source_reference", ""))],
    )
    ev.compute_area()

    # For v134 records, respect the existing deduct toggle
    ev.deduct = bool(record.get("deduct", False))
    if ev.deduct and ev.area_m2 and ev.area_m2 > 0:
        ev.deduction_status = DEDUCTION_DEDUCTED

    return ev
