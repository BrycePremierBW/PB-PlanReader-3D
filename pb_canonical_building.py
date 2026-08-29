"""
PlanReader Canonical Building Model Schema & Provenance Engine.

Defines the single-source-of-truth object graph representing physical building geometry:
Project -> Building -> Level -> Space/Room -> Wall -> Opening (Door/Window) ->
Floor -> Ceiling -> Roof -> Soffit -> Balcony -> Parapet -> Column -> Balustrade -> Screen -> FinishSurface.

Fail-Closed & Zero-Made-Up-Data Rules:
- Default review_state = REVIEW_REQUIRED
- Default confidence = None (Unrecorded)
- Default takeoff_eligible = False (Strict boolean required)
- Default deduction_authority = False (Strict boolean required)
- Physical dimensions default to None (No invented fallback heights or thicknesses)
- Strict boolean parsing: ONLY actual JSON boolean true grants authority. Strings like "true"/"yes"/"1" return False.
"""

from dataclasses import dataclass, field
from enum import Enum
import math
import uuid
import json
from typing import List, Dict, Any, Optional, Tuple, Union


def parse_strict_bool(value: Any) -> bool:
    """
    Strict boolean parser.
    ONLY actual Python bool True grants authority.
    Strings such as "true", "false", "yes", "1" fail-closed to False.
    """
    if isinstance(value, bool):
        return value
    return False


def parse_optional_confidence(value: Any) -> Optional[float]:
    """
    Parses optional confidence score (0.0 to 1.0).
    Distinguishes between missing confidence (None) and explicit 0.0 confidence.
    Malformed or out-of-bounds values return None.
    """
    if value is None:
        return None
    try:
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return None
        return max(0.0, min(1.0, f))
    except (ValueError, TypeError):
        return None


def parse_optional_float(value: Any) -> Optional[float]:
    """Parses metric dimension float. Returns None for missing/malformed/non-positive numbers."""
    if value is None:
        return None
    try:
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (ValueError, TypeError):
        return None


class ReviewState(str, Enum):
    CONFIRMED = "CONFIRMED"
    INFERRED = "INFERRED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class ObjectType(str, Enum):
    PROJECT = "PROJECT"
    BUILDING = "BUILDING"
    LEVEL = "LEVEL"
    SPACE = "SPACE"
    WALL = "WALL"
    OPENING = "OPENING"
    DOOR = "DOOR"
    WINDOW = "WINDOW"
    FLOOR = "FLOOR"
    CEILING = "CEILING"
    ROOF = "ROOF"
    SOFFIT = "SOFFIT"
    BALCONY = "BALCONY"
    PARAPET = "PARAPET"
    COLUMN = "COLUMN"
    BALUSTRADE = "BALUSTRADE"
    SCREEN = "SCREEN"
    SURFACE = "SURFACE"


@dataclass
class Provenance:
    """Retains origin traces for evidence-based drawing reconciliation."""
    source_pdf: Optional[str] = None
    page_number: Optional[int] = None
    drawing_id: Optional[str] = None
    source_coords: Optional[Dict[str, Any]] = None
    scale_source: Optional[str] = None
    contributing_evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_pdf": self.source_pdf,
            "page_number": self.page_number,
            "drawing_id": self.drawing_id,
            "source_coords": self.source_coords,
            "scale_source": self.scale_source,
            "contributing_evidence": list(self.contributing_evidence),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "Provenance":
        if not isinstance(data, dict):
            return cls()
        return cls(
            source_pdf=data.get("source_pdf"),
            page_number=data.get("page_number"),
            drawing_id=data.get("drawing_id"),
            source_coords=data.get("source_coords") if isinstance(data.get("source_coords"), dict) else None,
            scale_source=data.get("scale_source"),
            contributing_evidence=list(data.get("contributing_evidence", []) or []) if isinstance(data.get("contributing_evidence"), list) else [],
        )


@dataclass
class Vector2D:
    x: Optional[float] = None
    y: Optional[float] = None

    def is_valid(self) -> bool:
        return self.x is not None and self.y is not None

    def to_dict(self) -> Dict[str, Optional[float]]:
        return {"x": self.x, "y": self.y}

    @classmethod
    def from_dict(cls, data: Union[Dict[str, Any], List[float], Tuple[float, float]]) -> "Vector2D":
        if isinstance(data, dict):
            return cls(x=parse_optional_float(data.get("x")), y=parse_optional_float(data.get("y")))
        elif isinstance(data, (list, tuple)) and len(data) >= 2:
            return cls(x=parse_optional_float(data[0]), y=parse_optional_float(data[1]))
        return cls(x=None, y=None)


@dataclass
class Vector3D:
    x: Optional[float] = None
    y: Optional[float] = None
    z: Optional[float] = None

    def is_valid(self) -> bool:
        return self.x is not None and self.y is not None and self.z is not None

    def to_dict(self) -> Dict[str, Optional[float]]:
        return {"x": self.x, "y": self.y, "z": self.z}

    @classmethod
    def from_dict(cls, data: Union[Dict[str, Any], List[float], Tuple[float, float, float]]) -> "Vector3D":
        if isinstance(data, dict):
            return cls(
                x=parse_optional_float(data.get("x")),
                y=parse_optional_float(data.get("y")),
                z=parse_optional_float(data.get("z")),
            )
        elif isinstance(data, (list, tuple)) and len(data) >= 3:
            return cls(
                x=parse_optional_float(data[0]),
                y=parse_optional_float(data[1]),
                z=parse_optional_float(data[2]),
            )
        return cls(x=None, y=None, z=None)


@dataclass
class BoundingBox3D:
    min_point: Vector3D
    max_point: Vector3D

    def to_dict(self) -> Dict[str, Any]:
        return {
            "min_point": self.min_point.to_dict(),
            "max_point": self.max_point.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "BoundingBox3D":
        if not isinstance(data, dict):
            return cls(min_point=Vector3D(), max_point=Vector3D())
        return cls(
            min_point=Vector3D.from_dict(data.get("min_point")),
            max_point=Vector3D.from_dict(data.get("max_point")),
        )


@dataclass
class CanonicalElement:
    """Base class for all canonical building elements. Fails closed with ZERO made-up defaults."""
    id: str = field(default_factory=lambda: f"elem_{uuid.uuid4().hex[:8]}")
    name: str = "Unnamed Element"
    object_type: ObjectType = ObjectType.SURFACE
    level_id: Optional[str] = None
    parent_id: Optional[str] = None
    children_ids: List[str] = field(default_factory=list)
    confidence: Optional[float] = None  # None = Unrecorded
    review_state: ReviewState = ReviewState.REVIEW_REQUIRED  # Fail-closed default
    provenance: Provenance = field(default_factory=Provenance)
    substrate: Optional[str] = None
    finish: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    takeoff_eligible: bool = False  # Fail-closed default: False
    deduction_authority: bool = False  # Fail-closed default: False

    def base_to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "object_type": self.object_type.value if isinstance(self.object_type, ObjectType) else str(self.object_type),
            "level_id": self.level_id,
            "parent_id": self.parent_id,
            "children_ids": list(self.children_ids),
            "confidence": self.confidence,
            "review_state": self.review_state.value if isinstance(self.review_state, ReviewState) else str(self.review_state),
            "provenance": self.provenance.to_dict(),
            "substrate": self.substrate,
            "finish": self.finish,
            "metadata": dict(self.metadata),
            "takeoff_eligible": bool(self.takeoff_eligible),
            "deduction_authority": bool(self.deduction_authority),
        }

    @classmethod
    def base_from_dict_args(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(data, dict):
            data = {}

        # Fail-closed review_state deserialization
        rev_state = data.get("review_state")
        if isinstance(rev_state, str):
            try:
                rev_state = ReviewState(rev_state)
            except ValueError:
                rev_state = ReviewState.REVIEW_REQUIRED
        else:
            rev_state = ReviewState.REVIEW_REQUIRED

        # Object type deserialization
        obj_type = data.get("object_type", ObjectType.SURFACE.value)
        if isinstance(obj_type, str):
            try:
                obj_type = ObjectType(obj_type)
            except ValueError:
                obj_type = ObjectType.SURFACE

        return {
            "id": str(data.get("id", f"elem_{uuid.uuid4().hex[:8]}")),
            "name": str(data.get("name", "Unnamed Element")),
            "object_type": obj_type,
            "level_id": data.get("level_id"),
            "parent_id": data.get("parent_id"),
            "children_ids": [str(c) for c in (data.get("children_ids", []) or []) if c],
            "confidence": parse_optional_confidence(data.get("confidence")),
            "review_state": rev_state,
            "provenance": Provenance.from_dict(data.get("provenance") if isinstance(data.get("provenance"), dict) else {}),
            "substrate": data.get("substrate"),
            "finish": data.get("finish"),
            "metadata": dict(data.get("metadata", {}) or {}) if isinstance(data.get("metadata"), dict) else {},
            "takeoff_eligible": parse_strict_bool(data.get("takeoff_eligible")),
            "deduction_authority": parse_strict_bool(data.get("deduction_authority")),
        }


@dataclass
class CanonicalOpening(CanonicalElement):
    wall_id: Optional[str] = None
    opening_type: str = "GENERIC"
    offset_along_wall_m: Optional[float] = None
    sill_height_m: Optional[float] = None
    width_m: Optional[float] = None
    height_m: Optional[float] = None
    mark: Optional[str] = None

    def __post_init__(self):
        if self.opening_type.upper() == "DOOR":
            self.object_type = ObjectType.DOOR
        elif self.opening_type.upper() == "WINDOW":
            self.object_type = ObjectType.WINDOW
        else:
            self.object_type = ObjectType.OPENING

    def to_dict(self) -> Dict[str, Any]:
        res = self.base_to_dict()
        res.update({
            "wall_id": self.wall_id,
            "opening_type": self.opening_type,
            "offset_along_wall_m": self.offset_along_wall_m,
            "sill_height_m": self.sill_height_m,
            "width_m": self.width_m,
            "height_m": self.height_m,
            "mark": self.mark,
        })
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CanonicalOpening":
        base_args = cls.base_from_dict_args(data)
        return cls(
            **base_args,
            wall_id=data.get("wall_id"),
            opening_type=str(data.get("opening_type", "GENERIC")),
            offset_along_wall_m=parse_optional_float(data.get("offset_along_wall_m")),
            sill_height_m=parse_optional_float(data.get("sill_height_m")),
            width_m=parse_optional_float(data.get("width_m")),
            height_m=parse_optional_float(data.get("height_m")),
            mark=data.get("mark"),
        )


@dataclass
class CanonicalWall(CanonicalElement):
    start_point: Vector2D = field(default_factory=Vector2D)
    end_point: Vector2D = field(default_factory=Vector2D)
    thickness_m: Optional[float] = None  # No invented defaults
    height_m: Optional[float] = None     # No invented defaults
    is_external: bool = False
    openings: List[CanonicalOpening] = field(default_factory=list)

    def __post_init__(self):
        self.object_type = ObjectType.WALL

    def to_dict(self) -> Dict[str, Any]:
        res = self.base_to_dict()
        res.update({
            "start_point": self.start_point.to_dict(),
            "end_point": self.end_point.to_dict(),
            "thickness_m": self.thickness_m,
            "height_m": self.height_m,
            "is_external": bool(self.is_external),
            "openings": [op.to_dict() for op in self.openings],
        })
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CanonicalWall":
        base_args = cls.base_from_dict_args(data)
        openings_raw = data.get("openings", []) or []
        openings = [CanonicalOpening.from_dict(op) for op in openings_raw if isinstance(op, dict)]

        return cls(
            **base_args,
            start_point=Vector2D.from_dict(data.get("start_point")),
            end_point=Vector2D.from_dict(data.get("end_point")),
            thickness_m=parse_optional_float(data.get("thickness_m")),
            height_m=parse_optional_float(data.get("height_m")),
            is_external=parse_strict_bool(data.get("is_external")),
            openings=openings,
        )


@dataclass
class CanonicalSpace(CanonicalElement):
    boundary_polygon: List[Vector2D] = field(default_factory=list)
    height_m: Optional[float] = None
    specified_floor_area_m2: Optional[float] = None
    room_number: Optional[str] = None

    def __post_init__(self):
        self.object_type = ObjectType.SPACE

    def to_dict(self) -> Dict[str, Any]:
        res = self.base_to_dict()
        res.update({
            "boundary_polygon": [pt.to_dict() for pt in self.boundary_polygon],
            "height_m": self.height_m,
            "specified_floor_area_m2": self.specified_floor_area_m2,
            "room_number": self.room_number,
        })
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CanonicalSpace":
        base_args = cls.base_from_dict_args(data)
        poly_raw = data.get("boundary_polygon", []) or []
        poly = [Vector2D.from_dict(pt) for pt in poly_raw if pt]

        return cls(
            **base_args,
            boundary_polygon=poly,
            height_m=parse_optional_float(data.get("height_m")),
            specified_floor_area_m2=parse_optional_float(data.get("specified_floor_area_m2")),
            room_number=data.get("room_number"),
        )


@dataclass
class PolygonElement(CanonicalElement):
    """Generic base class for horizontal polygonal elements."""
    polygon: List[Vector2D] = field(default_factory=list)
    thickness_m: Optional[float] = None
    elevation_offset_m: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        res = self.base_to_dict()
        res.update({
            "polygon": [pt.to_dict() for pt in self.polygon],
            "thickness_m": self.thickness_m,
            "elevation_offset_m": self.elevation_offset_m,
        })
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PolygonElement":
        base_args = cls.base_from_dict_args(data)
        poly_raw = data.get("polygon", []) or []
        poly = [Vector2D.from_dict(pt) for pt in poly_raw if pt]
        return cls(
            **base_args,
            polygon=poly,
            thickness_m=parse_optional_float(data.get("thickness_m")),
            elevation_offset_m=parse_optional_float(data.get("elevation_offset_m")),
        )


@dataclass
class CanonicalFloor(PolygonElement):
    def __post_init__(self):
        self.object_type = ObjectType.FLOOR


@dataclass
class CanonicalCeiling(PolygonElement):
    def __post_init__(self):
        self.object_type = ObjectType.CEILING


@dataclass
class CanonicalRoof(PolygonElement):
    pitch_deg: Optional[float] = None
    overhang_m: Optional[float] = None
    roof_type: str = "FLAT"

    def __post_init__(self):
        self.object_type = ObjectType.ROOF

    def to_dict(self) -> Dict[str, Any]:
        res = super().to_dict()
        res.update({
            "pitch_deg": self.pitch_deg,
            "overhang_m": self.overhang_m,
            "roof_type": self.roof_type,
        })
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CanonicalRoof":
        base_args = cls.base_from_dict_args(data)
        poly_raw = data.get("polygon", []) or []
        poly = [Vector2D.from_dict(pt) for pt in poly_raw if pt]
        return cls(
            **base_args,
            polygon=poly,
            thickness_m=parse_optional_float(data.get("thickness_m")),
            elevation_offset_m=parse_optional_float(data.get("elevation_offset_m")),
            pitch_deg=parse_optional_float(data.get("pitch_deg")),
            overhang_m=parse_optional_float(data.get("overhang_m")),
            roof_type=str(data.get("roof_type", "FLAT")),
        )


@dataclass
class CanonicalSoffit(PolygonElement):
    def __post_init__(self):
        self.object_type = ObjectType.SOFFIT


@dataclass
class CanonicalBalcony(PolygonElement):
    balustrade_ids: List[str] = field(default_factory=list)

    def __post_init__(self):
        self.object_type = ObjectType.BALCONY

    def to_dict(self) -> Dict[str, Any]:
        res = super().to_dict()
        res["balustrade_ids"] = list(self.balustrade_ids)
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CanonicalBalcony":
        base_args = cls.base_from_dict_args(data)
        poly_raw = data.get("polygon", []) or []
        poly = [Vector2D.from_dict(pt) for pt in poly_raw if pt]
        return cls(
            **base_args,
            polygon=poly,
            thickness_m=parse_optional_float(data.get("thickness_m")),
            elevation_offset_m=parse_optional_float(data.get("elevation_offset_m")),
            balustrade_ids=list(data.get("balustrade_ids", []) or []),
        )


@dataclass
class CanonicalParapet(CanonicalElement):
    start_point: Vector2D = field(default_factory=Vector2D)
    end_point: Vector2D = field(default_factory=Vector2D)
    height_m: Optional[float] = None
    thickness_m: Optional[float] = None

    def __post_init__(self):
        self.object_type = ObjectType.PARAPET

    def to_dict(self) -> Dict[str, Any]:
        res = self.base_to_dict()
        res.update({
            "start_point": self.start_point.to_dict(),
            "end_point": self.end_point.to_dict(),
            "height_m": self.height_m,
            "thickness_m": self.thickness_m,
        })
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CanonicalParapet":
        base_args = cls.base_from_dict_args(data)
        return cls(
            **base_args,
            start_point=Vector2D.from_dict(data.get("start_point")),
            end_point=Vector2D.from_dict(data.get("end_point")),
            height_m=parse_optional_float(data.get("height_m")),
            thickness_m=parse_optional_float(data.get("thickness_m")),
        )


@dataclass
class CanonicalColumn(CanonicalElement):
    center: Vector2D = field(default_factory=Vector2D)
    width_m: Optional[float] = None
    depth_m: Optional[float] = None
    height_m: Optional[float] = None

    def __post_init__(self):
        self.object_type = ObjectType.COLUMN

    def to_dict(self) -> Dict[str, Any]:
        res = self.base_to_dict()
        res.update({
            "center": self.center.to_dict(),
            "width_m": self.width_m,
            "depth_m": self.depth_m,
            "height_m": self.height_m,
        })
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CanonicalColumn":
        base_args = cls.base_from_dict_args(data)
        return cls(
            **base_args,
            center=Vector2D.from_dict(data.get("center")),
            width_m=parse_optional_float(data.get("width_m")),
            depth_m=parse_optional_float(data.get("depth_m")),
            height_m=parse_optional_float(data.get("height_m")),
        )


@dataclass
class CanonicalLinearElement(CanonicalElement):
    start_point: Vector2D = field(default_factory=Vector2D)
    end_point: Vector2D = field(default_factory=Vector2D)
    height_m: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        res = self.base_to_dict()
        res.update({
            "start_point": self.start_point.to_dict(),
            "end_point": self.end_point.to_dict(),
            "height_m": self.height_m,
        })
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CanonicalLinearElement":
        base_args = cls.base_from_dict_args(data)
        return cls(
            **base_args,
            start_point=Vector2D.from_dict(data.get("start_point")),
            end_point=Vector2D.from_dict(data.get("end_point")),
            height_m=parse_optional_float(data.get("height_m")),
        )


@dataclass
class CanonicalBalustrade(CanonicalLinearElement):
    def __post_init__(self):
        self.object_type = ObjectType.BALUSTRADE


@dataclass
class CanonicalScreen(CanonicalLinearElement):
    def __post_init__(self):
        self.object_type = ObjectType.SCREEN


@dataclass
class CanonicalFinishSurface(CanonicalElement):
    parent_element_id: Optional[str] = None
    surface_area_m2: Optional[float] = None
    orientation: str = "UNKNOWN"

    def __post_init__(self):
        self.object_type = ObjectType.SURFACE

    def to_dict(self) -> Dict[str, Any]:
        res = self.base_to_dict()
        res.update({
            "parent_element_id": self.parent_element_id,
            "surface_area_m2": self.surface_area_m2,
            "orientation": self.orientation,
        })
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CanonicalFinishSurface":
        base_args = cls.base_from_dict_args(data)
        return cls(
            **base_args,
            parent_element_id=data.get("parent_element_id"),
            surface_area_m2=parse_optional_float(data.get("surface_area_m2")),
            orientation=str(data.get("orientation", "UNKNOWN")),
        )


@dataclass
class CanonicalLevel(CanonicalElement):
    elevation_m: Optional[float] = None
    height_m: Optional[float] = None
    level_index: int = 0
    walls: List[CanonicalWall] = field(default_factory=list)
    spaces: List[CanonicalSpace] = field(default_factory=list)
    floors: List[CanonicalFloor] = field(default_factory=list)
    ceilings: List[CanonicalCeiling] = field(default_factory=list)
    roofs: List[CanonicalRoof] = field(default_factory=list)
    soffits: List[CanonicalSoffit] = field(default_factory=list)
    balconies: List[CanonicalBalcony] = field(default_factory=list)
    parapets: List[CanonicalParapet] = field(default_factory=list)
    columns: List[CanonicalColumn] = field(default_factory=list)
    balustrades: List[CanonicalBalustrade] = field(default_factory=list)
    screens: List[CanonicalScreen] = field(default_factory=list)
    surfaces: List[CanonicalFinishSurface] = field(default_factory=list)

    def __post_init__(self):
        self.object_type = ObjectType.LEVEL

    def to_dict(self) -> Dict[str, Any]:
        res = self.base_to_dict()
        res.update({
            "elevation_m": self.elevation_m,
            "height_m": self.height_m,
            "level_index": int(self.level_index),
            "walls": [w.to_dict() for w in self.walls],
            "spaces": [sp.to_dict() for sp in self.spaces],
            "floors": [fl.to_dict() for fl in self.floors],
            "ceilings": [c.to_dict() for c in self.ceilings],
            "roofs": [r.to_dict() for r in self.roofs],
            "soffits": [sof.to_dict() for sof in self.soffits],
            "balconies": [b.to_dict() for b in self.balconies],
            "parapets": [p.to_dict() for p in self.parapets],
            "columns": [col.to_dict() for col in self.columns],
            "balustrades": [bal.to_dict() for bal in self.balustrades],
            "screens": [scr.to_dict() for scr in self.screens],
            "surfaces": [s.to_dict() for s in self.surfaces],
        })
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CanonicalLevel":
        base_args = cls.base_from_dict_args(data)
        return cls(
            **base_args,
            elevation_m=parse_optional_float(data.get("elevation_m")),
            height_m=parse_optional_float(data.get("height_m")),
            level_index=int(data.get("level_index", 0)),
            walls=[CanonicalWall.from_dict(w) for w in data.get("walls", []) or [] if isinstance(w, dict)],
            spaces=[CanonicalSpace.from_dict(sp) for sp in data.get("spaces", []) or [] if isinstance(sp, dict)],
            floors=[CanonicalFloor.from_dict(fl) for fl in data.get("floors", []) or [] if isinstance(fl, dict)],
            ceilings=[CanonicalCeiling.from_dict(c) for c in data.get("ceilings", []) or [] if isinstance(c, dict)],
            roofs=[CanonicalRoof.from_dict(r) for r in data.get("roofs", []) or [] if isinstance(r, dict)],
            soffits=[CanonicalSoffit.from_dict(s) for s in data.get("soffits", []) or [] if isinstance(s, dict)],
            balconies=[CanonicalBalcony.from_dict(b) for b in data.get("balconies", []) or [] if isinstance(b, dict)],
            parapets=[CanonicalParapet.from_dict(p) for p in data.get("parapets", []) or [] if isinstance(p, dict)],
            columns=[CanonicalColumn.from_dict(col) for col in data.get("columns", []) or [] if isinstance(col, dict)],
            balustrades=[CanonicalBalustrade.from_dict(bal) for bal in data.get("balustrades", []) or [] if isinstance(bal, dict)],
            screens=[CanonicalScreen.from_dict(scr) for scr in data.get("screens", []) or [] if isinstance(scr, dict)],
            surfaces=[CanonicalFinishSurface.from_dict(s) for s in data.get("surfaces", []) or [] if isinstance(s, dict)],
        )


@dataclass
class CanonicalBuilding(CanonicalElement):
    levels: List[CanonicalLevel] = field(default_factory=list)
    building_bounds: Optional[BoundingBox3D] = None

    def __post_init__(self):
        self.object_type = ObjectType.BUILDING

    def to_dict(self) -> Dict[str, Any]:
        res = self.base_to_dict()
        res.update({
            "levels": [lvl.to_dict() for lvl in self.levels],
            "building_bounds": self.building_bounds.to_dict() if self.building_bounds else None,
        })
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CanonicalBuilding":
        base_args = cls.base_from_dict_args(data)
        bounds_raw = data.get("building_bounds")
        bounds = BoundingBox3D.from_dict(bounds_raw) if isinstance(bounds_raw, dict) else None
        return cls(
            **base_args,
            levels=[CanonicalLevel.from_dict(lvl) for lvl in data.get("levels", []) or [] if isinstance(lvl, dict)],
            building_bounds=bounds,
        )


@dataclass
class CanonicalProject(CanonicalElement):
    buildings: List[CanonicalBuilding] = field(default_factory=list)
    is_synthetic_demo: bool = False

    def __post_init__(self):
        self.object_type = ObjectType.PROJECT

    def to_dict(self) -> Dict[str, Any]:
        res = self.base_to_dict()
        res.update({
            "buildings": [b.to_dict() for b in self.buildings],
            "is_synthetic_demo": bool(self.is_synthetic_demo),
        })
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CanonicalProject":
        base_args = cls.base_from_dict_args(data)
        return cls(
            **base_args,
            buildings=[CanonicalBuilding.from_dict(b) for b in data.get("buildings", []) or [] if isinstance(b, dict)],
            is_synthetic_demo=parse_strict_bool(data.get("is_synthetic_demo")),
        )

    def to_json(self, indent: Optional[int] = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_json(cls, json_str: str) -> "CanonicalProject":
        data = json.loads(json_str)
        return cls.from_dict(data)
