"""pb_australian_takeoff_standards_v178.py — Deductions, Height Rules, & Australian Standards Takeoff Authority Engine.

Applies Master Painters Australia & ASMM commercial measurement rules:
- Height work surcharges (<3.0m standard, 3.0m-4.5m +15%, >=4.5m +30%)
- Minimum item area allowances (0.5m² min for small structural items)
- Scaffold and access equipment surface area allowances for PB PlanReader.
"""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class AustralianTakeoffRuleResult:
    row_id: int
    element_name: str
    height_m: float
    base_quantity: float
    unit: str
    height_surcharge_factor: float
    adjusted_quantity: float
    requires_scaffold: bool
    rule_applied_note: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "row_id": self.row_id,
            "element_name": self.element_name,
            "height_m": round(self.height_m, 2),
            "base_quantity": round(self.base_quantity, 2),
            "unit": self.unit,
            "height_surcharge_factor": round(self.height_surcharge_factor, 2),
            "adjusted_quantity": round(self.adjusted_quantity, 2),
            "requires_scaffold": self.requires_scaffold,
            "rule_applied_note": self.rule_applied_note,
        }


def calculate_height_surcharge_factor(height_m: float) -> Tuple[float, bool, str]:
    """Calculate Australian Standard commercial height surcharge multiplier based on wall/ceiling working height."""
    if height_m < 3.0:
        return (1.00, False, "Standard working height (< 3.0m)")
    elif 3.0 <= height_m < 4.5:
        return (1.15, True, "High work surcharge (+15% for 3.0m-4.5m)")
    else:
        return (1.30, True, "Scaffold / EWP high work surcharge (+30% for >= 4.5m)")


def apply_minimum_area_rule(quantity: float, unit: str, is_small_item: bool = False) -> float:
    """Enforce minimum 0.5m² rule for small structural items/columns per Australian standards."""
    if is_small_item and unit.lower() in ["m²", "sqm", "m2"]:
        return max(quantity, 0.5)
    return quantity


class AustralianTakeoffRegistry:
    """Applies Australian takeoff standards across all takeoff rows in a workspace."""

    def __init__(self, rule_results: List[AustralianTakeoffRuleResult]):
        self.rule_results = rule_results
        self._by_id = {r.row_id: r for r in rule_results}

    @classmethod
    def derive_for_workspace(cls, conn: sqlite3.Connection, workspace_id: int, default_height: float = 2.7) -> "AustralianTakeoffRegistry":
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, element, quantity, unit, location, notes
            FROM takeoff_rows
            WHERE workspace_id=?
            ORDER BY id ASC
            """,
            (workspace_id,)
        )
        rows = cur.fetchall()

        results = []
        for rid, elem, qty, unit, loc, notes in rows:
            elem_str = str(elem or "")
            unit_str = str(unit or "m²")
            qty_val = float(qty or 0.0)

            # Determine working height from notes or location
            h_val = default_height
            if "high" in str(notes or "").lower() or "void" in str(notes or "").lower():
                h_val = 4.8
            elif "stair" in str(loc or "").lower() or "external" in str(loc or "").lower():
                h_val = 3.6

            surcharge_factor, req_scaff, note_str = calculate_height_surcharge_factor(h_val)
            adj_qty = apply_minimum_area_rule(qty_val, unit_str, is_small_item=("column" in elem_str.lower()))
            final_qty = adj_qty * surcharge_factor

            results.append(AustralianTakeoffRuleResult(
                row_id=int(rid),
                element_name=elem_str,
                height_m=h_val,
                base_quantity=qty_val,
                unit=unit_str,
                height_surcharge_factor=surcharge_factor,
                adjusted_quantity=final_qty,
                requires_scaffold=req_scaff,
                rule_applied_note=note_str,
            ))

        return cls(results)

    def total_scaffold_surface_area_m2(self) -> float:
        return sum(r.adjusted_quantity for r in self.rule_results if r.requires_scaffold)


def derive_australian_takeoff_authority(conn: sqlite3.Connection, workspace_id: int, default_height: float = 2.7) -> AustralianTakeoffRegistry:
    return AustralianTakeoffRegistry.derive_for_workspace(conn, workspace_id, default_height)
