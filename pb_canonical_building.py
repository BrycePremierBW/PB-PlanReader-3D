"""
PlanReader Canonical Building Model Schema & Provenance Engine.

Defines the single-source-of-truth object graph representing physical building geometry:
Project -> Building -> Level -> Space/Room -> Wall -> Opening (Door/Window) ->
Floor -> Ceiling -> Roof -> Soffit -> Balcony -> Parapet -> Column -> Balustrade -> Screen -> FinishSurface.

Fail-Closed Security & Provenance Guarantees:
- Default review_state = REVIEW_REQUIRED
- Default confidence = 0.0 (unproven)
- Default takeoff_eligible = False
- Default deduction_authority = False
- Strict 0.0..1.0 confidence clamping; malformed values fail-closed to 0.0
"""

from dataclasses import dataclass, field
from enum import Enum
import math
import uuid
import json
from typing import List, Dict, Any, Optional, Tuple, Union


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
    x: float = 0.0
    y: float = 0.0

    def to_dict(self) -> Dict[str, float]:
        return {"x": float(self.x), "y": float(self.y)}

    @classmethod
    def from_dict(cls, data: Union[Dict[str, Any], List[float], Tuple[float, float]]) -> "Vector2D":
        try:
            if isinstance(data, dict):
                vx = float(data.get("x", 0.0))
                vy = float(data.get("y", 0.0))
                if math.isnan(vx) or math.isinf(vx): vx = 0.0
                if math.isnan(vy) or math.isinf(vy): vy = 0.0
                return cls(x=vx, y=vy)
            elif isinstance(data, (list, tuple)) and len(data) >= 2:
                vx = float(data[0])
                vy = float(data[1])
                if math.isnan(vx) or math.isinf(vx): vx = 0.0
                if math.isnan(vy) or math.isinf(vy): vy = 0.0
                return cls(x=vx, y=vy)
        except (ValueError, TypeError):
            pass
        return cls(x=0.0, y=0.0)


@dataclass
class Vector3D:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def to_dict(self) -> Dict[str, float]:
        return {"x": float(self.x), "y": float(self.y), "z": float(self.z)}

    @classmethod
    def from_dict(cls, data: Union[Dict[str, Any], List[float], Tuple[float, float, float]]) -> "Vector3D":
        try:
            if isinstance(data, dict):
                vx = float(data.get("x", 0.0))
                vy = float(data.get("y", 0.0))
                vz = float(data.get("z", 0.0))
                if math.isnan(vx) or math.isinf(vx): vx = 0.0
                if math.isnan(vy) or math.isinf(vy): vy = 0.0
                if math.isnan(vz) or math.isinf(vz): vz = 0.0
                return cls(x=vx, y=vy, z=vz)
            elif isinstance(data, (list, tuple)) and len(data) >= 3:
                vx, vy, vz = float(data[0]), float(data[1]), float(data[2])
                if math.isnan(vx) or math.isinf(vx): vx = 0.0
                if math.isnan(vy) or math.isinf(vy): vy = 0.0
                if math.isnan(vz) or math.isinf(vz): vz = 0.0
                return cls(x=vx, y=vy, z=vz)
        except (ValueError, TypeError):
            pass
        return cls(x=0.0, y=0.0, z=0.0)


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
            return cls(min_point=Vector3D(0, 0, 0), max_point=Vector3D(0, 0, 0))
        return cls(
            min_point=Vector3D.from_dict(data.get("min_point")),
            max_point=Vector3D.from_dict(data.get("max_point")),
        )


@dataclass
class CanonicalElement:
    """Base class for all canonical building elements. Fails closed by default."""
    id: str = field(default_factory=lambda: f"elem_{uuid.uuid4().hex[:8]}")
    name: str = "Unnamed Element"
    object_type: ObjectType = ObjectType.SURFACE
    level_id: Optional[str] = None
    parent_id: Optional[str] = None
    children_ids: List[str] = field(default_factory=list)
    confidence: float = 0.0  # Fail-closed default: 0.0 (unproven)
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
            "confidence": float(self.confidence),
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

        # Fail-closed confidence deserialization (clamped to 0.0..1.0)
        raw_conf = data.get("confidence")
        try:
            if raw_conf is not None:
                conf_val = float(raw_conf)
                if math.isnan(conf_val) or math.isinf(conf_val):
                    conf_val = 0.0
                else:
                    conf_val = max(0.0, min(1.0, conf_val))
            else:
                conf_val = 0.0
        except (ValueError, TypeError):
            conf_val = 0.0

        return {
            "id": str(data.get("id", f"elem_{uuid.uuid4().hex[:8]}")),
            "name": str(data.get("name", "Unnamed Element")),
            "object_type": obj_type,
            "level_id": data.get("level_id"),
            "parent_id": data.get("parent_id"),
            "children_ids": [str(c) for c in (data.get("children_ids", []) or []) if c],
            "confidence": conf_val,
            "review_state": rev_state,
            "provenance": Provenance.from_dict(data.get("provenance") if isinstance(data.get("provenance"), dict) else {}),
            "substrate": data.get("substrate"),
            "finish": data.get("finish"),
            "metadata": dict(data.get("metadata", {}) or {}) if isinstance(data.get("metadata"), dict) else {},
            "takeoff_eligible": bool(data.get("takeoff_eligible", False)),
            "deduction_authority": bool(data.get("deduction_authority", False)),
        }


@dataclass
class CanonicalOpening(CanonicalElement):
    wall_id: Optional[str] = None
    opening_type: str = "GENERIC"
    offset_along_wall_m: float = 0.0
    sill_height_m: float = 0.0
    width_m: float = 0.0
    height_m: float = 0.0
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
            "offset_along_wall_m": float(self.offset_along_wall_m),
            "sill_height_m": float(self.sill_height_m),
            "width_m": float(self.width_m),
            "height_m": float(self.height_m),
            "mark": self.mark,
        })
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CanonicalOpening":
        base_args = cls.base_from_dict_args(data)
        
        def safe_float(val, default=0.0):
            try:
                f = float(val)
                return default if math.isnan(f) or math.isinf(f) else f
            except (ValueError, TypeError):
                return default

        return cls(
            **base_args,
            wall_id=data.get("wall_id"),
            opening_type=str(data.get("opening_type", "GENERIC")),
            offset_along_wall_m=safe_float(data.get("offset_along_wall_m")),
            sill_height_m=safe_float(data.get("sill_height_m")),
            width_m=safe_float(data.get("width_m")),
            height_m=safe_float(data.get("height_m")),
            mark=data.get("mark"),
        )


@dataclass
class CanonicalWall(CanonicalElement):
    start_point: Vector2D = field(default_factory=lambda: Vector2D(0.0, 0.0))
    end_point: Vector2D = field(default_factory=lambda: Vector2D(0.0, 0.0))
    thickness_m: float = 0.15
    height_m: float = 2.7
    is_external: bool = False
    openings: List[CanonicalOpening] = field(default_factory=list)

    def __post_init__(self):
        self.object_type = ObjectType.WALL

    def to_dict(self) -> Dict[str, Any]:
        res = self.base_to_dict()
        res.update({
            "start_point": self.start_point.to_dict(),
            "end_point": self.end_point.to_dict(),
            "thickness_m": float(self.thickness_m),
            "height_m": float(self.height_m),
            "is_external": bool(self.is_external),
            "openings": [op.to_dict() for op in self.openings],
        })
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CanonicalWall":
        base_args = cls.base_from_dict_args(data)
        openings_raw = data.get("openings", []) or []
        openings = [CanonicalOpening.from_dict(op) for op in openings_raw if isinstance(op, dict)]
        
        def safe_float(val, default=0.0):
            try:
                f = float(val)
                return default if math.isnan(f) or math.isinf(f) else f
            except (ValueError, TypeError):
                return default

        return cls(
            **base_args,
            start_point=Vector2D.from_dict(data.get("start_point")),
            end_point=Vector2D.from_dict(data.get("end_point")),
            thickness_m=safe_float(data.get("thickness_m"), 0.15),
            height_m=safe_float(data.get("height_m"), 2.7),
            is_external=bool(data.get("is_external", False)),
            openings=openings,
        )


@dataclass
class CanonicalSpace(CanonicalElement):
    boundary_polygon: List[Vector2D] = field(default_factory=list)
    height_m: float = 2.7
    specified_floor_area_m2: Optional[float] = None
    room_number: Optional[str] = None

    def __post_init__(self):
        self.object_type = ObjectType.SPACE

    def to_dict(self) -> Dict[str, Any]:
        res = self.base_to_dict()
        res.update({
            "boundary_polygon": [pt.to_dict() for pt in self.boundary_polygon],
            "height_m": float(self.height_m),
            "specified_floor_area_m2": self.specified_floor_area_m2,
            "room_number": self.room_number,
        })
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CanonicalSpace":
        base_args = cls.base_from_dict_args(data)
        poly_raw = data.get("boundary_polygon", []) or []
        poly = [Vector2D.from_dict(pt) for pt in poly_raw if pt]
        
        spec_area = data.get("specified_floor_area_m2")
        try:
            if spec_area is not None:
                spec_area = float(spec_area)
                if math.isnan(spec_area) or math.isinf(spec_area): spec_area = None
        except (ValueError, TypeError):
            spec_area = None

        return cls(
            **base_args,
            boundary_polygon=poly,
            height_m=float(data.get("height_m", 2.7)),
            specified_floor_area_m2=spec_area,
            room_number=data.get("room_number"),
        )


@dataclass
class PolygonElement(CanonicalElement):
    """Generic base class for horizontal polygonal elements."""
    polygon: List[Vector2D] = field(default_factory=list)
    thickness_m: float = 0.2
    elevation_offset_m: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        res = self.base_to_dict()
        res.update({
            "polygon": [pt.to_dict() for pt in self.polygon],
            "thickness_m": float(self.thickness_m),
            "elevation_offset_m": float(self.elevation_offset_m),
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
            thickness_m=float(data.get("thickness_m", 0.2)),
            elevation_offset_m=float(data.get("elevation_offset_m", 0.0)),
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
    pitch_deg: float = 0.0
    overhang_m: float = 0.6
    roof_type: str = "FLAT"

    def __post_init__(self):
        self.object_type = ObjectType.ROOF

    def to_dict(self) -> Dict[str, Any]:
        res = super().to_dict()
        res.update({
            "pitch_deg": float(self.pitch_deg),
            "overhang_m": float(self.overhang_m),
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
            thickness_m=float(data.get("thickness_m", 0.2)),
            elevation_offset_m=float(data.get("elevation_offset_m", 0.0)),
            pitch_deg=float(data.get("pitch_deg", 0.0)),
            overhang_m=float(data.get("overhang_m", 0.6)),
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
            thickness_m=float(data.get("thickness_m", 0.2)),
            elevation_offset_m=float(data.get("elevation_offset_m", 0.0)),
            balustrade_ids=list(data.get("balustrade_ids", []) or []),
        )


@dataclass
class CanonicalParapet(CanonicalElement):
    start_point: Vector2D = field(default_factory=lambda: Vector2D(0.0, 0.0))
    end_point: Vector2D = field(default_factory=lambda: Vector2D(0.0, 0.0))
    height_m: float = 1.0
    thickness_m: float = 0.2

    def __post_init__(self):
        self.object_type = ObjectType.PARAPET

    def to_dict(self) -> Dict[str, Any]:
        res = self.base_to_dict()
        res.update({
            "start_point": self.start_point.to_dict(),
            "end_point": self.end_point.to_dict(),
            "height_m": float(self.height_m),
            "thickness_m": float(self.thickness_m),
        })
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CanonicalParapet":
        base_args = cls.base_from_dict_args(data)
        return cls(
            **base_args,
            start_point=Vector2D.from_dict(data.get("start_point")),
            end_point=Vector2D.from_dict(data.get("end_point")),
            height_m=float(data.get("height_m", 1.0)),
            thickness_m=float(data.get("thickness_m", 0.2)),
        )


@dataclass
class CanonicalColumn(CanonicalElement):
    center: Vector2D = field(default_factory=lambda: Vector2D(0.0, 0.0))
    width_m: float = 0.4
    depth_m: float = 0.4
    height_m: float = 2.7

    def __post_init__(self):
        self.object_type = ObjectType.COLUMN

    def to_dict(self) -> Dict[str, Any]:
        res = self.base_to_dict()
        res.update({
            "center": self.center.to_dict(),
            "width_m": float(self.width_m),
            "depth_m": float(self.depth_m),
            "height_m": float(self.height_m),
        })
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CanonicalColumn":
        base_args = cls.base_from_dict_args(data)
        return cls(
            **base_args,
            center=Vector2D.from_dict(data.get("center")),
            width_m=float(data.get("width_m", 0.4)),
            depth_m=float(data.get("depth_m", 0.4)),
            height_m=float(data.get("height_m", 2.7)),
        )


@dataclass
class CanonicalLinearElement(CanonicalElement):
    start_point: Vector2D = field(default_factory=lambda: Vector2D(0.0, 0.0))
    end_point: Vector2D = field(default_factory=lambda: Vector2D(0.0, 0.0))
    height_m: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        res = self.base_to_dict()
        res.update({
            "start_point": self.start_point.to_dict(),
            "end_point": self.end_point.to_dict(),
            "height_m": float(self.height_m),
        })
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CanonicalLinearElement":
        base_args = cls.base_from_dict_args(data)
        return cls(
            **base_args,
            start_point=Vector2D.from_dict(data.get("start_point")),
            end_point=Vector2D.from_dict(data.get("end_point")),
            height_m=float(data.get("height_m", 1.0)),
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
    surface_area_m2: float = 0.0
    orientation: str = "UNKNOWN"

    def __post_init__(self):
        self.object_type = ObjectType.SURFACE

    def to_dict(self) -> Dict[str, Any]:
        res = self.base_to_dict()
        res.update({
            "parent_element_id": self.parent_element_id,
            "surface_area_m2": float(self.surface_area_m2),
            "orientation": self.orientation,
        })
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CanonicalFinishSurface":
        base_args = cls.base_from_dict_args(data)
        return cls(
            **base_args,
            parent_element_id=data.get("parent_element_id"),
            surface_area_m2=float(data.get("surface_area_m2", 0.0)),
            orientation=str(data.get("orientation", "UNKNOWN")),
        )


@dataclass
class CanonicalLevel(CanonicalElement):
    elevation_m: float = 0.0
    height_m: float = 2.7
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

    def __post_init__(self):
        self.object_type = ObjectType.LEVEL

    def to_dict(self) -> Dict[str, Any]:
        res = self.base_to_dict()
        res.update({
            "elevation_m": float(self.elevation_m),
            "height_m": float(self.height_m),
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
        })
        return res

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CanonicalLevel":
        base_args = cls.base_from_dict_args(data)
        return cls(
            **base_args,
            elevation_m=float(data.get("elevation_m", 0.0)),
            height_m=float(data.get("height_m", 2.7)),
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
    is_synthetic_demo: bool = False  # Explicit flag for demo vs user canonical models

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
            is_synthetic_demo=bool(data.get("is_synthetic_demo", False)),
        )

    def to_json(self, indent: Optional[int] = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_json(cls, json_str: str) -> "CanonicalProject":
        data = json.loads(json_str)
        return cls.from_dict(data)
