"""pb_wall_topology_v174.py — Wall Network Topology & Room Space Reconstruction Engine.

Reconstructs closed 2D room polygons, wall networks, room perimeters, and wall surface areas
from vector geometry and measurement polygons for PB PlanReader.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import sqlite3
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class RoomSpaceRecord:
    room_id: int
    page_id: int
    room_name: str
    storey_level: str
    area_m2: float
    perimeter_m: float
    default_wall_height_m: float
    wall_surface_area_m2: float  # perimeter_m * default_wall_height_m
    raw_points: List[Tuple[float, float]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "room_id": self.room_id,
            "page_id": self.page_id,
            "room_name": self.room_name,
            "storey_level": self.storey_level,
            "area_m2": round(self.area_m2, 2),
            "perimeter_m": round(self.perimeter_m, 2),
            "default_wall_height_m": round(self.default_wall_height_m, 2),
            "wall_surface_area_m2": round(self.wall_surface_area_m2, 2),
            "raw_points_count": len(self.raw_points),
        }


def compute_polygon_area_and_perimeter(pts: Sequence[Tuple[float, float]], px_per_m: float = 100.0) -> Tuple[float, float]:
    """Calculate real-world area (m²) and perimeter (m) for a 2D polygon in pixel coordinates using Shoelace formula."""
    if not pts or len(pts) < 3 or px_per_m <= 0:
        return (0.0, 0.0)

    n = len(pts)
    shoelace_area = 0.0
    perimeter_px = 0.0

    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        shoelace_area += (x1 * y2) - (x2 * y1)
        dx = x2 - x1
        dy = y2 - y1
        perimeter_px += math.sqrt(dx * dx + dy * dy)

    area_px2 = abs(shoelace_area) / 2.0
    area_m2 = area_px2 / (px_per_m * px_per_m)
    perimeter_m = perimeter_px / px_per_m

    return (area_m2, perimeter_m)


class WallTopologyRegistry:
    """Manages reconstructed room spaces and wall topology for a workspace."""

    def __init__(self, rooms: List[RoomSpaceRecord]):
        self.rooms = rooms
        self._by_id = {r.room_id: r for r in rooms}

    @classmethod
    def derive_for_workspace(cls, conn: sqlite3.Connection, workspace_id: int, default_wall_height: float = 2.7) -> "WallTopologyRegistry":
        cur = conn.cursor()
        cur.execute(
            """
            SELECT ml.id, ml.page_id, ml.line_type, ml.length_m, ml.area_m2, ml.raw_points, p.px_per_m, p.page_label
            FROM measurement_lines ml
            JOIN pages p ON p.id = ml.page_id
            WHERE ml.workspace_id=? AND ml.line_type IN ('polygon', 'room', 'rectangle')
            ORDER BY ml.id ASC
            """,
            (workspace_id,)
        )
        rows = cur.fetchall()

        rooms = []
        for rid, pid, ltype, len_m, area_m2, raw_pts_json, px_m, plabel in rows:
            pts: List[Tuple[float, float]] = []
            if raw_pts_json:
                try:
                    import json
                    parsed = json.loads(raw_pts_json)
                    if isinstance(parsed, list):
                        pts = [(float(p[0]), float(p[1])) for p in parsed if len(p) >= 2]
                except Exception:
                    pass

            calc_area, calc_perim = compute_polygon_area_and_perimeter(pts, px_per_m=float(px_m or 100.0))
            area_val = float(area_m2) if area_m2 else calc_area
            perim_val = float(len_m) if len_m else calc_perim
            wall_m2 = perim_val * default_wall_height

            rooms.append(RoomSpaceRecord(
                room_id=int(rid),
                page_id=int(pid),
                room_name=f"Room Space #{rid} ({plabel})",
                storey_level="Level 1",
                area_m2=area_val,
                perimeter_m=perim_val,
                default_wall_height_m=default_wall_height,
                wall_surface_area_m2=wall_m2,
                raw_points=pts,
            ))

        return cls(rooms)

    def total_floor_area_m2(self) -> float:
        return sum(r.area_m2 for r in self.rooms)

    def total_wall_surface_m2(self) -> float:
        return sum(r.wall_surface_area_m2 for r in self.rooms)


def derive_wall_topology(conn: sqlite3.Connection, workspace_id: int, default_wall_height: float = 2.7) -> WallTopologyRegistry:
    return WallTopologyRegistry.derive_for_workspace(conn, workspace_id, default_wall_height)
