"""pb_paintable_surface_v176.py — Floor, Ceiling, & Roof Paintable Surface Engine.

Calculates surface areas across internal walls, flat ceilings, pitched ceilings (trigonometric pitch factor),
floors/epoxy, soffits, and linear skirting/cornice items for PB PlanReader.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import sqlite3
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class SurfaceRecord:
    surface_id: int
    surface_type: str  # 'INTERNAL_WALL', 'CEILING_FLAT', 'CEILING_PITCHED', 'FLOOR_EPOXY', 'SOFFIT', 'SKIRTING_LINEAR'
    location_name: str
    flat_area_m2: float
    pitch_degrees: float
    net_paintable_area_m2: float
    linear_metres: float
    substrate: str
    finish_system: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "surface_id": self.surface_id,
            "surface_type": self.surface_type,
            "location_name": self.location_name,
            "flat_area_m2": round(self.flat_area_m2, 2),
            "pitch_degrees": round(self.pitch_degrees, 1),
            "net_paintable_area_m2": round(self.net_paintable_area_m2, 2),
            "linear_metres": round(self.linear_metres, 2),
            "substrate": self.substrate,
            "finish_system": self.finish_system,
        }


def calculate_pitched_surface_area(flat_area_m2: float, pitch_degrees: float) -> float:
    """Calculate actual sloped surface area given flat projected area and pitch angle in degrees."""
    if flat_area_m2 <= 0:
        return 0.0
    if pitch_degrees <= 0 or pitch_degrees >= 85.0:
        return flat_area_m2

    rad = math.radians(pitch_degrees)
    cos_val = math.cos(rad)
    if cos_val <= 0:
        return flat_area_m2
    return flat_area_m2 / cos_val


class SurfaceEngineRegistry:
    """Manages paintable surface calculations across a workspace."""

    def __init__(self, surfaces: List[SurfaceRecord]):
        self.surfaces = surfaces
        self._by_id = {s.surface_id: s for s in surfaces}

    @classmethod
    def derive_for_workspace(cls, conn: sqlite3.Connection, workspace_id: int) -> "SurfaceEngineRegistry":
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, section, element, location, substrate, unit, quantity, finish_system
            FROM takeoff_rows
            WHERE workspace_id=?
            ORDER BY id ASC
            """,
            (workspace_id,)
        )
        rows = cur.fetchall()

        surfaces = []
        for rid, sec, elem, loc, sub, unit, qty, fsys in rows:
            elem_str = str(elem or "").lower()
            unit_str = str(unit or "m²").lower()
            qty_val = float(qty or 0.0)

            if "skirting" in elem_str or "cornice" in elem_str or "m" == unit_str:
                stype = "SKIRTING_LINEAR"
                flat_a = 0.0
                net_a = 0.0
                lin_m = qty_val
            elif "ceiling" in elem_str:
                if "pitched" in elem_str or "raked" in elem_str:
                    stype = "CEILING_PITCHED"
                    flat_a = qty_val
                    net_a = calculate_pitched_surface_area(flat_a, 25.0)  # 25 deg default pitch
                    lin_m = 0.0
                else:
                    stype = "CEILING_FLAT"
                    flat_a = qty_val
                    net_a = qty_val
                    lin_m = 0.0
            elif "floor" in elem_str or "epoxy" in elem_str:
                stype = "FLOOR_EPOXY"
                flat_a = qty_val
                net_a = qty_val
                lin_m = 0.0
            elif "soffit" in elem_str or "eave" in elem_str:
                stype = "SOFFIT"
                flat_a = qty_val
                net_a = qty_val
                lin_m = 0.0
            else:
                stype = "INTERNAL_WALL"
                flat_a = qty_val
                net_a = qty_val
                lin_m = 0.0

            surfaces.append(SurfaceRecord(
                surface_id=int(rid),
                surface_type=stype,
                location_name=str(loc or "General"),
                flat_area_m2=flat_a,
                pitch_degrees=25.0 if stype == "CEILING_PITCHED" else 0.0,
                net_paintable_area_m2=net_a,
                linear_metres=lin_m,
                substrate=str(sub or "Plasterboard"),
                finish_system=str(fsys or "PT01"),
            ))

        return cls(surfaces)

    def total_paintable_area_m2(self) -> float:
        return sum(s.net_paintable_area_m2 for s in self.surfaces)

    def total_skirting_linear_m(self) -> float:
        return sum(s.linear_metres for s in self.surfaces if s.surface_type == "SKIRTING_LINEAR")


def derive_surface_registry(conn: sqlite3.Connection, workspace_id: int) -> SurfaceEngineRegistry:
    return SurfaceEngineRegistry.derive_for_workspace(conn, workspace_id)
