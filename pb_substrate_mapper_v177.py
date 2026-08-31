"""pb_substrate_mapper_v177.py — Finish Schedule & Specification Substrate/System Mapper Engine.

Maps finish codes (P1, P2, FC01) and substrates to canonical commercial paint systems,
coat counts, spreading rates, labor productivity, and paint volumes for PB PlanReader.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import sqlite3
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class SubstratePaintSystem:
    finish_code: str
    substrate: str
    system_name: str
    coat_count: int
    primer_sealer: str
    topcoat: str
    coverage_m2_per_litre: float
    productivity_m2_per_hour: float

    def calculate_litres(self, area_m2: float) -> float:
        if area_m2 <= 0 or self.coverage_m2_per_litre <= 0:
            return 0.0
        return (area_m2 * self.coat_count) / self.coverage_m2_per_litre

    def calculate_labour_hours(self, area_m2: float) -> float:
        if area_m2 <= 0 or self.productivity_m2_per_hour <= 0:
            return 0.0
        return (area_m2 * self.coat_count) / self.productivity_m2_per_hour

    def to_dict(self) -> Dict[str, Any]:
        return {
            "finish_code": self.finish_code,
            "substrate": self.substrate,
            "system_name": self.system_name,
            "coat_count": self.coat_count,
            "primer_sealer": self.primer_sealer,
            "topcoat": self.topcoat,
            "coverage_m2_per_litre": round(self.coverage_m2_per_litre, 2),
            "productivity_m2_per_hour": round(self.productivity_m2_per_hour, 2),
        }


# Canonical Commercial Paint System Database
STANDARD_SYSTEM_MAP: Dict[str, SubstratePaintSystem] = {
    "P1": SubstratePaintSystem(
        finish_code="P1",
        substrate="Plasterboard",
        system_name="Dulux Wash&Wear Low Sheen System",
        coat_count=3,  # 1 Sealer + 2 Topcoats
        primer_sealer="Dulux Acrylic Sealer Undercoat",
        topcoat="Dulux Wash&Wear Low Sheen Acrylic",
        coverage_m2_per_litre=16.0,
        productivity_m2_per_hour=15.0,
    ),
    "P2": SubstratePaintSystem(
        finish_code="P2",
        substrate="Timber / Doors",
        system_name="Dulux Aquanamel Semi-Gloss System",
        coat_count=3,
        primer_sealer="Dulux Prepcoat Acrylic Primer Undercoat",
        topcoat="Dulux Aquanamel Semi-Gloss Enamel",
        coverage_m2_per_litre=14.0,
        productivity_m2_per_hour=12.0,
    ),
    "FC01": SubstratePaintSystem(
        finish_code="FC01",
        substrate="Fibre Cement Board",
        system_name="Dulux Weathershield Exterior System",
        coat_count=3,
        primer_sealer="Dulux AcraTex PrimeBind",
        topcoat="Dulux Weathershield Low Sheen",
        coverage_m2_per_litre=12.0,
        productivity_m2_per_hour=14.0,
    ),
    "EP01": SubstratePaintSystem(
        finish_code="EP01",
        substrate="Concrete Floor",
        system_name="Dulux Luxepoxy 2-Pack Epoxy Floor System",
        coat_count=2,
        primer_sealer="Luxepoxy Sealer",
        topcoat="Luxepoxy 4 HR Express Finish",
        coverage_m2_per_litre=10.0,
        productivity_m2_per_hour=18.0,
    ),
}


def map_finish_code_and_substrate(finish_code: str, substrate: str = "") -> SubstratePaintSystem:
    """Resolve canonical paint system for a given finish code and substrate."""
    code_upper = str(finish_code or "").strip().upper()
    if code_upper in STANDARD_SYSTEM_MAP:
        return STANDARD_SYSTEM_MAP[code_upper]

    sub_upper = str(substrate or "").strip().lower()
    if "timber" in sub_upper or "wood" in sub_upper or "door" in sub_upper:
        return STANDARD_SYSTEM_MAP["P2"]
    if "cement" in sub_upper or "fc" in sub_upper or "external" in sub_upper:
        return STANDARD_SYSTEM_MAP["FC01"]
    if "floor" in sub_upper or "concrete" in sub_upper or "epoxy" in sub_upper:
        return STANDARD_SYSTEM_MAP["EP01"]

    # General Plasterboard Wall Default (P1)
    return SubstratePaintSystem(
        finish_code=code_upper or "PT01",
        substrate=substrate or "Plasterboard",
        system_name="Standard Commercial 3-Coat Acrylic System",
        coat_count=3,
        primer_sealer="Acrylic Sealer Undercoat",
        topcoat="Premium Commercial Low Sheen",
        coverage_m2_per_litre=16.0,
        productivity_m2_per_hour=15.0,
    )


class SubstrateMapperRegistry:
    """Manages substrate and paint system mapping across a workspace."""

    def __init__(self, mapped_systems: Dict[int, SubstratePaintSystem]):
        self.mapped_systems = mapped_systems

    @classmethod
    def derive_for_workspace(cls, conn: sqlite3.Connection, workspace_id: int) -> "SubstrateMapperRegistry":
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, finish_system, substrate
            FROM takeoff_rows
            WHERE workspace_id=?
            ORDER BY id ASC
            """,
            (workspace_id,)
        )
        rows = cur.fetchall()

        mapped = {}
        for rid, fcode, sub in rows:
            system = map_finish_code_and_substrate(str(fcode or ""), str(sub or ""))
            mapped[int(rid)] = system

        return cls(mapped)


def derive_substrate_mapping(conn: sqlite3.Connection, workspace_id: int) -> SubstrateMapperRegistry:
    return SubstrateMapperRegistry.derive_for_workspace(conn, workspace_id)
