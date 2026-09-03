"""Canonical-only 3D spatial provenance for PlanReader Workstream A10.

The graph is a read-only projection of CanonicalProject through the same
canonical scene payload used by the BIM viewer. It never queries workspace,
page, measurement, or takeoff tables and it never fills missing geometry with
rendering defaults.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Any, Dict, List, Optional, Tuple

from pb_bim_viewer import project_to_viewer_payload
from pb_canonical_building import CanonicalProject, parse_strict_bool


Vector3 = Tuple[float, float, float]


def _finite_number(value: Any) -> bool:
    """Return True only for real, finite numeric values (booleans excluded)."""
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _positive_number(value: Any) -> bool:
    return _finite_number(value) and float(value) > 0.0


def _point_2d(value: Any) -> Optional[Tuple[float, float]]:
    if not isinstance(value, dict):
        return None
    x, y = value.get("x"), value.get("y")
    if not (_finite_number(x) and _finite_number(y)):
        return None
    return float(x), float(y)


def _line_geometry(
    start_value: Any,
    end_value: Any,
) -> Optional[Tuple[float, float, float, float, float]]:
    start = _point_2d(start_value)
    end = _point_2d(end_value)
    if start is None or end is None:
        return None
    start_x, start_y = start
    end_x, end_y = end
    delta_x, delta_y = end_x - start_x, end_y - start_y
    length = math.hypot(delta_x, delta_y)
    if not math.isfinite(length) or length <= 1e-4:
        return None
    return start_x, start_y, delta_x, delta_y, length


@dataclass(frozen=True)
class SpatialProvenanceNode:
    """One canonical scene entity and its explicitly available 3D bounds.

    element_id is the original CanonicalElement.id. Complete position and
    dimensions are exposed only when every required physical input exists.
    Partial or invalid geometry is represented by None and
    geometry_valid=False.
    """

    element_id: str
    element_type: str
    level_id: Optional[str]
    parent_element_id: Optional[str]
    geometry_valid: bool
    position_3d: Optional[Vector3]
    dimensions_3d: Optional[Vector3]
    provenance: Dict[str, Any]
    geometry_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "element_id": self.element_id,
            "element_type": self.element_type,
            "level_id": self.level_id,
            "parent_element_id": self.parent_element_id,
            "geometry_valid": self.geometry_valid,
            "position_3d": list(self.position_3d) if self.position_3d is not None else None,
            "dimensions_3d": list(self.dimensions_3d) if self.dimensions_3d is not None else None,
            "provenance": deepcopy(self.provenance),
            "geometry_error": self.geometry_error,
        }


class SceneProvenanceGraph:
    """Deterministic, read-only spatial index of canonical scene entities."""

    def __init__(
        self,
        nodes: List[SpatialProvenanceNode],
        *,
        project_id: Optional[str],
        workspace_id: Optional[str],
        source_status: str,
        duplicate_id_conflicts: Optional[List[str]] = None,
    ):
        self.nodes = list(nodes)
        self.project_id = project_id
        self.workspace_id = workspace_id
        self.source_status = source_status
        self.duplicate_id_conflicts = duplicate_id_conflicts or []
        self._by_element_id = {node.element_id: node for node in self.nodes}

    @classmethod
    def unavailable(cls, status: str) -> "SceneProvenanceGraph":
        return cls(
            [],
            project_id=None,
            workspace_id=None,
            source_status=status,
            duplicate_id_conflicts=[],
        )

    @classmethod
    def derive_from_canonical_project(cls, project: Any) -> "SceneProvenanceGraph":
        if not isinstance(project, CanonicalProject):
            return cls.unavailable("CANONICAL_PROJECT_REQUIRED")
        if parse_strict_bool(getattr(project, "is_synthetic_demo", False)):
            return cls.unavailable("SYNTHETIC_CANONICAL_PROJECT_REJECTED")

        project_id = project.id if isinstance(project.id, str) and project.id else None
        project_provenance = getattr(project, "provenance", None)
        canonical_workspace_id = getattr(project_provenance, "workspace_id", None)
        workspace_id = (
            canonical_workspace_id
            if isinstance(canonical_workspace_id, str) and canonical_workspace_id
            else None
        )

        try:
            scene = project_to_viewer_payload(project)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return cls(
                [],
                project_id=project_id,
                workspace_id=workspace_id,
                source_status="CANONICAL_SCENE_UNAVAILABLE",
            )

        raw_objects = [obj for obj in scene.get("objects", []) if isinstance(obj, dict)]
        candidate_ids = [
            obj.get("id")
            for obj in raw_objects
            if isinstance(obj.get("id"), str) and obj.get("id")
        ]
        id_counts = Counter(candidate_ids)
        objects_by_id = {
            obj["id"]: obj
            for obj in raw_objects
            if isinstance(obj.get("id"), str)
            and obj.get("id")
            and id_counts[obj["id"]] == 1
        }
        level_elevations = {
            level.get("id"): level.get("elevation_m")
            for level in scene.get("levels", [])
            if isinstance(level, dict) and isinstance(level.get("id"), str)
        }

        # Track duplicate ID conflicts for observability
        duplicate_id_conflicts = [
            element_id
            for element_id, count in id_counts.items()
            if count > 1
        ]

        nodes: List[SpatialProvenanceNode] = []
        for obj in raw_objects:
            element_id = obj.get("id")
            if (
                not isinstance(element_id, str)
                or not element_id
                or id_counts[element_id] != 1
            ):
                continue

            element_type = obj.get("type")
            if not isinstance(element_type, str) or not element_type:
                continue
            level_id = obj.get("level_id") if isinstance(obj.get("level_id"), str) else None
            parent_element_id = _parent_element_id(obj)
            geometry_valid, position, dimensions, geometry_error = _spatial_geometry(
                obj,
                level_elevations.get(level_id),
                objects_by_id,
            )
            raw_provenance = obj.get("provenance")
            provenance = deepcopy(raw_provenance) if isinstance(raw_provenance, dict) else {}
            nodes.append(
                SpatialProvenanceNode(
                    element_id=element_id,
                    element_type=element_type,
                    level_id=level_id,
                    parent_element_id=parent_element_id,
                    geometry_valid=geometry_valid,
                    position_3d=position,
                    dimensions_3d=dimensions,
                    provenance=provenance,
                    geometry_error=geometry_error,
                )
            )

        return cls(
            nodes,
            project_id=project_id,
            workspace_id=workspace_id,
            source_status="CANONICAL_SCENE",
            duplicate_id_conflicts=duplicate_id_conflicts if duplicate_id_conflicts else [],
        )

    def lookup_by_element_id(self, element_id: str) -> Optional[SpatialProvenanceNode]:
        return self._by_element_id.get(element_id)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "workspace_id": self.workspace_id,
            "source_status": self.source_status,
            "nodes": [node.to_dict() for node in self.nodes],
            "duplicate_id_conflicts": list(self.duplicate_id_conflicts) if self.duplicate_id_conflicts else [],
        }


def _parent_element_id(obj: Dict[str, Any]) -> Optional[str]:
    for key in ("wall_id", "parent_id", "parent_element_id"):
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _spatial_geometry(
    obj: Dict[str, Any],
    level_elevation: Any,
    objects_by_id: Dict[str, Dict[str, Any]],
) -> Tuple[bool, Optional[Vector3], Optional[Vector3], Optional[str]]:
    element_type = obj.get("type")
    if not _finite_number(level_elevation):
        return False, None, None, "Canonical level elevation is unavailable or non-finite"
    elevation = float(level_elevation)

    if element_type in {"WALL", "PARAPET"}:
        return _wall_like_geometry(obj, elevation)
    if element_type in {"DOOR", "WINDOW", "OPENING"}:
        return _opening_geometry(obj, elevation, objects_by_id)
    if element_type in {"FLOOR", "CEILING", "ROOF", "SOFFIT", "BALCONY"}:
        return _polygon_geometry(obj, elevation)
    if element_type == "COLUMN":
        return _column_geometry(obj, elevation)
    return False, None, None, "Complete canonical 3D bounds are unavailable for this element type"


def _wall_like_geometry(
    obj: Dict[str, Any],
    elevation: float,
) -> Tuple[bool, Optional[Vector3], Optional[Vector3], Optional[str]]:
    line = _line_geometry(obj.get("start_point"), obj.get("end_point"))
    if line is None:
        return False, None, None, "Canonical baseline is missing, non-finite, or zero-length"
    height, thickness = obj.get("height_m"), obj.get("thickness_m")
    if not _positive_number(height):
        return False, None, None, "Canonical height is missing, non-finite, or non-positive"
    if not _positive_number(thickness):
        return False, None, None, "Canonical thickness is missing, non-finite, or non-positive"

    start_x, start_y, delta_x, delta_y, length = line
    height_value, thickness_value = float(height), float(thickness)
    position = (
        start_x + delta_x / 2.0,
        start_y + delta_y / 2.0,
        elevation + height_value / 2.0,
    )
    dimensions = (length, thickness_value, height_value)
    return True, position, dimensions, None


def _opening_geometry(
    obj: Dict[str, Any],
    elevation: float,
    objects_by_id: Dict[str, Dict[str, Any]],
) -> Tuple[bool, Optional[Vector3], Optional[Vector3], Optional[str]]:
    wall_id = obj.get("wall_id")
    if obj.get("is_host_attached") is not True or not isinstance(wall_id, str):
        return False, None, None, "Canonical host-wall attachment is unavailable"
    wall = objects_by_id.get(wall_id)
    if wall is None or wall.get("type") != "WALL":
        return False, None, None, "Canonical host wall is unavailable or ambiguous"

    line = _line_geometry(wall.get("start_point"), wall.get("end_point"))
    if line is None:
        return False, None, None, "Canonical host-wall baseline is unavailable"
    width, height = obj.get("width_m"), obj.get("height_m")
    offset, sill = obj.get("offset_along_wall_m"), obj.get("sill_height_m")
    wall_thickness = wall.get("thickness_m")
    if not (_positive_number(width) and _positive_number(height)):
        return False, None, None, "Canonical opening dimensions are unavailable or invalid"
    if not (_finite_number(offset) and float(offset) >= 0.0):
        return False, None, None, "Canonical opening offset is unavailable or invalid"
    if not (_finite_number(sill) and float(sill) >= 0.0):
        return False, None, None, "Canonical opening sill height is unavailable or invalid"
    if not _positive_number(wall_thickness):
        return False, None, None, "Canonical host-wall thickness is unavailable or invalid"

    start_x, start_y, delta_x, delta_y, wall_length = line
    width_value, height_value = float(width), float(height)
    offset_value, sill_value = float(offset), float(sill)
    if offset_value + width_value > wall_length + 1e-3:
        return False, None, None, "Canonical opening extends beyond its host wall"
    wall_height = wall.get("height_m")
    opening_top = sill_value + height_value
    if not _positive_number(wall_height) or opening_top > float(wall_height) + 1e-3:
        return (
            False,
            None,
            None,
            "Canonical opening exceeds unavailable or invalid host-wall height",
        )

    unit_x, unit_y = delta_x / wall_length, delta_y / wall_length
    centre_offset = offset_value + width_value / 2.0
    position = (
        start_x + unit_x * centre_offset,
        start_y + unit_y * centre_offset,
        elevation + sill_value + height_value / 2.0,
    )
    dimensions = (width_value, float(wall_thickness), height_value)
    return True, position, dimensions, None


def _polygon_geometry(
    obj: Dict[str, Any],
    level_elevation: float,
) -> Tuple[bool, Optional[Vector3], Optional[Vector3], Optional[str]]:
    points = [_point_2d(point) for point in obj.get("polygon", [])]
    if len(points) < 3 or any(point is None for point in points):
        return False, None, None, "Canonical polygon is missing or invalid"
    valid_points = [point for point in points if point is not None]
    twice_area = abs(
        sum(
            point[0] * valid_points[(index + 1) % len(valid_points)][1]
            - valid_points[(index + 1) % len(valid_points)][0] * point[1]
            for index, point in enumerate(valid_points)
        )
    )
    if not math.isfinite(twice_area) or twice_area <= 1e-8:
        return False, None, None, "Canonical polygon has no finite physical area"

    absolute_elevation = obj.get("elevation")
    elevation_offset = obj.get("elevation_offset_m")
    if _finite_number(absolute_elevation):
        base_elevation = float(absolute_elevation)
    elif _finite_number(elevation_offset):
        base_elevation = level_elevation + float(elevation_offset)
    else:
        return False, None, None, "Canonical polygon elevation is unavailable"
    thickness = obj.get("thickness_m")
    if not _positive_number(thickness):
        return False, None, None, "Canonical polygon thickness is unavailable or invalid"

    xs = [point[0] for point in valid_points]
    ys = [point[1] for point in valid_points]
    span_x, span_y = max(xs) - min(xs), max(ys) - min(ys)
    if span_x <= 1e-4 or span_y <= 1e-4:
        return False, None, None, "Canonical polygon bounds are degenerate"
    thickness_value = float(thickness)
    position = (
        (min(xs) + max(xs)) / 2.0,
        (min(ys) + max(ys)) / 2.0,
        base_elevation + thickness_value / 2.0,
    )
    dimensions = (span_x, span_y, thickness_value)
    return True, position, dimensions, None


def _column_geometry(
    obj: Dict[str, Any],
    elevation: float,
) -> Tuple[bool, Optional[Vector3], Optional[Vector3], Optional[str]]:
    centre = _point_2d(obj.get("center"))
    width, depth, height = obj.get("width_m"), obj.get("depth_m"), obj.get("height_m")
    if centre is None:
        return False, None, None, "Canonical column centre is unavailable"
    if not all(_positive_number(value) for value in (width, depth, height)):
        return False, None, None, "Canonical column dimensions are unavailable or invalid"
    width_value, depth_value, height_value = float(width), float(depth), float(height)
    position = (centre[0], centre[1], elevation + height_value / 2.0)
    dimensions = (width_value, depth_value, height_value)
    return True, position, dimensions, None


def derive_3d_scene_provenance(
    project: Any,
    workspace_id: Any = None,
) -> SceneProvenanceGraph:
    """Derive A10 only from a real CanonicalProject.

    workspace_id remains accepted solely so legacy callers fail closed instead
    of crashing. It is never used as provenance; workspace identity is read
    only from project.provenance.workspace_id.
    """
    del workspace_id
    return SceneProvenanceGraph.derive_from_canonical_project(project)
