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
from pb_geometry_services import validate_opening_geometry


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


def _clean_str(val: Any) -> Optional[str]:
    if val is None or isinstance(val, bool):
        return None
    s = str(val).strip()
    if not s or s.lower() in {"nan", "none", "null", "undefined"}:
        return None
    return s


def _clean_workspace_id(val: Any) -> Optional[str]:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return str(val) if val > 0 else None
    if isinstance(val, float):
        if math.isfinite(val) and val.is_integer() and int(val) > 0:
            return str(int(val))
        return None
    s = str(val).strip()
    if not s or s.lower() in {"nan", "none", "null", "undefined"}:
        return None
    if s.isdigit():
        return s if int(s) > 0 else None
    return s


def _valid_page_number(val: Any) -> bool:
    """Page numbers must be positive finite integers (booleans and sentinels excluded)."""
    if val is None:
        return True
    if isinstance(val, bool):
        return False
    if isinstance(val, (int, float)):
        if not math.isfinite(float(val)):
            return False
        return float(val) > 0.0 and float(val).is_integer()
    if isinstance(val, str):
        s = val.strip()
        if s.isdigit():
            return int(s) > 0
        return False
    return False


def _is_superseded_or_stale(
    provenance: Dict[str, Any],
    obj: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Optional[str]]:
    if parse_strict_bool(provenance.get("is_superseded", False)):
        return True, "Canonical drawing revision is superseded or unapproved"
    if str(provenance.get("revision_status", "")).strip().upper() == "SUPERSEDED":
        return True, "Canonical drawing revision is superseded or unapproved"
    if str(provenance.get("status", "")).strip().upper() == "SUPERSEDED":
        return True, "Canonical drawing revision is superseded or unapproved"
    if any("SUPERSEDED" in str(ev).upper() for ev in provenance.get("contributing_evidence", [])):
        return True, "Canonical drawing revision is superseded or unapproved"
    if parse_strict_bool(provenance.get("is_stale", False)):
        return True, "Canonical drawing revision is stale"
    if obj:
        if str(obj.get("review_state", "")).strip().upper() in {"SUPERSEDED", "STALE"}:
            return True, "Canonical drawing revision is superseded or unapproved"
        if parse_strict_bool(obj.get("is_superseded", False)):
            return True, "Canonical drawing revision is superseded or unapproved"
        if parse_strict_bool(obj.get("is_stale", False)):
            return True, "Canonical drawing revision is stale"
    return False, None


def _point_2d(value: Any) -> Optional[Tuple[float, float]]:
    if isinstance(value, dict):
        x, y = value.get("x"), value.get("y")
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        x, y = value[0], value[1]
    else:
        return None
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
        rejected_elements: Optional[List[Dict[str, Any]]] = None,
    ):
        self.nodes = list(nodes)
        self.project_id = project_id
        self.workspace_id = workspace_id
        self.source_status = source_status
        self.duplicate_id_conflicts = duplicate_id_conflicts or []
        self.rejected_elements = deepcopy(rejected_elements) if rejected_elements else []
        self._by_element_id = {node.element_id: node for node in self.nodes}

    @classmethod
    def unavailable(cls, status: str) -> "SceneProvenanceGraph":
        return cls(
            [],
            project_id=None,
            workspace_id=None,
            source_status=status,
            duplicate_id_conflicts=[],
            rejected_elements=[],
        )

    @classmethod
    def derive_from_canonical_project(
        cls,
        project: Any,
        expected_workspace_id: Any = None,
    ) -> "SceneProvenanceGraph":
        if not isinstance(project, CanonicalProject):
            return cls.unavailable("CANONICAL_PROJECT_REQUIRED")
        if parse_strict_bool(getattr(project, "is_synthetic_demo", False)):
            return cls.unavailable("SYNTHETIC_CANONICAL_PROJECT_REJECTED")

        # Stale scene detection (P6)
        if parse_strict_bool(getattr(project, "is_stale", False)):
            return cls.unavailable("STALE_CANONICAL_SCENE_REJECTED")
        if getattr(project, "source_status", None) in {"STALE", "STALE_SCENE"}:
            return cls.unavailable("STALE_CANONICAL_SCENE_REJECTED")
        if str(getattr(project, "review_state", "")).strip().upper() in {"STALE", "SUPERSEDED"}:
            return cls.unavailable("STALE_CANONICAL_SCENE_REJECTED")
        project_provenance = getattr(project, "provenance", None)
        if project_provenance and parse_strict_bool(getattr(project_provenance, "is_stale", False)):
            return cls.unavailable("STALE_CANONICAL_SCENE_REJECTED")

        project_id = project.id if isinstance(project.id, str) and project.id else None
        canonical_workspace_id = _clean_workspace_id(getattr(project_provenance, "workspace_id", None))
        workspace_id = canonical_workspace_id

        # Workspace binding validation against expected workspace (P6)
        if expected_workspace_id is not None:
            clean_expected = _clean_workspace_id(expected_workspace_id)
            if clean_expected is None:
                return cls.unavailable("WORKSPACE_MISMATCH")
            norm_expected = clean_expected.lstrip("workspace-")
            norm_canonical = (canonical_workspace_id or "").lstrip("workspace-")
            if canonical_workspace_id is None or (clean_expected != canonical_workspace_id and norm_expected != norm_canonical):
                return cls.unavailable("WORKSPACE_MISMATCH")

        try:
            scene = project_to_viewer_payload(project)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return cls(
                [],
                project_id=project_id,
                workspace_id=workspace_id,
                source_status="CANONICAL_SCENE_UNAVAILABLE",
                rejected_elements=[],
            )

        rejected_elements = _canonical_rejected_openings(project)
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

        # Track duplicate ID conflicts for observability.
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
                canonical_workspace_id=canonical_workspace_id,
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

        # Detect duplicate/copied spatial geometry on the same level (P6)
        spatial_signatures: Dict[Tuple, List[str]] = {}
        for node in nodes:
            if node.geometry_valid and node.position_3d is not None and node.dimensions_3d is not None:
                sig = (
                    node.level_id,
                    node.element_type,
                    tuple(round(c, 3) for c in node.position_3d),
                    tuple(round(d, 3) for d in node.dimensions_3d),
                )
                spatial_signatures.setdefault(sig, []).append(node.element_id)

        duplicate_spatial_ids = {
            eid
            for elem_ids in spatial_signatures.values()
            if len(elem_ids) > 1
            for eid in elem_ids
        }

        if duplicate_spatial_ids:
            hardened_nodes: List[SpatialProvenanceNode] = []
            for node in nodes:
                if node.element_id in duplicate_spatial_ids:
                    hardened_nodes.append(
                        SpatialProvenanceNode(
                            element_id=node.element_id,
                            element_type=node.element_type,
                            level_id=node.level_id,
                            parent_element_id=node.parent_element_id,
                            geometry_valid=False,
                            position_3d=None,
                            dimensions_3d=None,
                            provenance=node.provenance,
                            geometry_error="Duplicate/copied spatial geometry detected at identical coordinates",
                        )
                    )
                else:
                    hardened_nodes.append(node)
            nodes = hardened_nodes

        return cls(
            nodes,
            project_id=project_id,
            workspace_id=workspace_id,
            source_status="CANONICAL_SCENE",
            duplicate_id_conflicts=duplicate_id_conflicts if duplicate_id_conflicts else [],
            rejected_elements=rejected_elements,
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
            "rejected_elements": deepcopy(self.rejected_elements),
        }


def _canonical_rejected_openings(project: CanonicalProject) -> List[Dict[str, Any]]:
    """Preserve canonical opening rejection evidence without deriving geometry.

    The established canonical geometry validator remains the sole source of the
    rejection decision. A10 records only canonical identity, host relation,
    validator reason, and canonical provenance; no 3D bounds are synthesized.
    """
    rejected: List[Dict[str, Any]] = []
    for building in project.buildings:
        for level in building.levels:
            for wall in level.walls:
                for opening in wall.openings:
                    valid, reason = validate_opening_geometry(opening, wall)
                    if valid:
                        continue
                    element_id = getattr(opening, "id", None)
                    if not isinstance(element_id, str) or not element_id:
                        # Never invent a semantic identity for rejected evidence.
                        continue
                    object_type = getattr(opening, "object_type", None)
                    element_type = getattr(object_type, "value", None)
                    if not isinstance(element_type, str) or not element_type:
                        element_type = str(object_type) if object_type is not None else None
                    wall_id = getattr(opening, "wall_id", None)
                    parent_element_id = wall_id if isinstance(wall_id, str) and wall_id else None
                    raw_provenance = getattr(opening, "provenance", None)
                    provenance = (
                        deepcopy(raw_provenance.to_dict())
                        if raw_provenance is not None and hasattr(raw_provenance, "to_dict")
                        else {}
                    )
                    rejected.append(
                        {
                            "element_id": element_id,
                            "element_type": element_type,
                            "parent_element_id": parent_element_id,
                            "reason": str(reason),
                            "provenance": provenance,
                        }
                    )
    return sorted(
        rejected,
        key=lambda item: (
            item["element_id"],
            item.get("parent_element_id") or "",
            item["reason"],
        ),
    )


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
    canonical_workspace_id: Optional[str] = None,
) -> Tuple[bool, Optional[Vector3], Optional[Vector3], Optional[str]]:
    raw_provenance = obj.get("provenance")
    if isinstance(raw_provenance, dict):
        # 1. Element workspace check: if element has workspace_id, it must match project's canonical workspace
        elem_ws = _clean_workspace_id(raw_provenance.get("workspace_id"))
        if elem_ws and canonical_workspace_id:
            norm_elem = elem_ws.lstrip("workspace-")
            norm_proj = canonical_workspace_id.lstrip("workspace-")
            if elem_ws != canonical_workspace_id and norm_elem != norm_proj:
                return False, None, None, "Canonical element workspace does not match project workspace"

        # 2. Page reference validation: page_number must be positive finite integer
        raw_page = raw_provenance.get("page_number")
        if not _valid_page_number(raw_page):
            return False, None, None, "Canonical element page reference is invalid, non-finite, or non-positive"

        # 3. Sentinel checks on page_id and drawing_id
        for key in ("page_id", "drawing_id"):
            val = raw_provenance.get(key)
            if val is not None and isinstance(val, str) and val.strip().lower() in {"nan", "none", "null"}:
                return False, None, None, f"Canonical {key} contains invalid sentinel"

        # 4. Revision superseded or stale check
        is_super, super_reason = _is_superseded_or_stale(raw_provenance, obj)
        if is_super:
            return False, None, None, super_reason

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

    # Level consistency check (P6)
    opening_level = obj.get("level_id")
    wall_level = wall.get("level_id")
    if opening_level and wall_level and opening_level != wall_level:
        return False, None, None, "Canonical opening level does not match host-wall level"

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

    When workspace_id is provided, validates that project.provenance.workspace_id
    matches the requested workspace to prevent cross-workspace geometry authorization.
    """
    if not isinstance(project, CanonicalProject):
        return SceneProvenanceGraph.unavailable("CANONICAL_PROJECT_REQUIRED")
    return SceneProvenanceGraph.derive_from_canonical_project(
        project,
        expected_workspace_id=workspace_id,
    )
