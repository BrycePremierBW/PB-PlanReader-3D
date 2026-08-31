"""pb_opening_deduction_v175.py — Door, Window, & Opening Schedule Deduction Engine.

Parses opening schedules, calculates gross opening areas, applies Australian Standard
deduction threshold rules (<0.5m² threshold), and calculates door/window frame paint areas.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class OpeningRecord:
    opening_id: int
    opening_code: str
    opening_type: str  # 'DOOR', 'WINDOW', 'OPENING'
    height_mm: float
    width_mm: float
    quantity: int
    gross_area_m2_single: float  # (height_mm * width_mm) / 1,000,000
    gross_area_m2_total: float   # gross_area_m2_single * quantity
    is_deductible: bool           # True if gross_area_m2_single >= 0.5m²
    net_deduction_area_m2: float  # gross_area_m2_total if is_deductible else 0.0
    door_leaf_paint_area_m2: float # 2 * gross_area_m2_total for doors
    frame_material: str
    location_notes: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "opening_id": self.opening_id,
            "opening_code": self.opening_code,
            "opening_type": self.opening_type,
            "height_mm": self.height_mm,
            "width_mm": self.width_mm,
            "quantity": self.quantity,
            "gross_area_m2_single": round(self.gross_area_m2_single, 2),
            "gross_area_m2_total": round(self.gross_area_m2_total, 2),
            "is_deductible": self.is_deductible,
            "net_deduction_area_m2": round(self.net_deduction_area_m2, 2),
            "door_leaf_paint_area_m2": round(self.door_leaf_paint_area_m2, 2),
            "frame_material": self.frame_material,
            "location_notes": self.location_notes,
        }


def parse_opening_dimensions(text: str) -> Tuple[float, float]:
    """Parse opening height and width in mm e.g. '2040 x 820', '2100h x 1800w', '1200x900'."""
    if not text:
        return (2040.0, 820.0)

    m = re.search(r'(\d{3,4})\s*(?:h|mm)?\s*[:x\*\/]\s*(\d{3,4})', text, re.IGNORECASE)
    if m:
        h = float(m.group(1))
        w = float(m.group(2))
        return (h, w)
    return (2040.0, 820.0)


class OpeningDeductionRegistry:
    """Manages opening deductions and frame paint quantities across a workspace."""

    def __init__(self, openings: List[OpeningRecord]):
        self.openings = openings
        self._by_id = {o.opening_id: o for o in openings}

    @classmethod
    def derive_for_workspace(cls, conn: sqlite3.Connection, workspace_id: int) -> "OpeningDeductionRegistry":
        cur = conn.cursor()
        # Query register items or schedule items containing opening data
        cur.execute(
            """
            SELECT id, item_name, status
            FROM register_items
            WHERE workspace_id=? AND (item_name LIKE '%Door%' OR item_name LIKE '%Window%' OR item_name LIKE '%Opening%' OR item_name LIKE 'D%' OR item_name LIKE 'W%')
            ORDER BY id ASC
            """,
            (workspace_id,)
        )
        rows = cur.fetchall()

        openings = []
        for rid, item_name, status in rows:
            name_str = str(item_name or "")
            otype = "DOOR" if any(k in name_str.upper() for k in ["DOOR", "D0", "D1"]) else ("WINDOW" if "W" in name_str.upper() else "OPENING")
            h_mm, w_mm = parse_opening_dimensions(name_str)

            qty = 1
            m_qty = re.search(r'(?:qty|count)[\s:]*(\d{1,3})', name_str, re.IGNORECASE)
            if m_qty:
                qty = int(m_qty.group(1))

            single_m2 = (h_mm * w_mm) / 1000000.0
            total_m2 = single_m2 * qty
            # Australian Standard Takeoff Rule: openings < 0.5m² are not deducted from wall areas
            is_deduct = single_m2 >= 0.5
            net_ded = total_m2 if is_deduct else 0.0
            leaf_m2 = (2.0 * total_m2) if otype == "DOOR" else 0.0

            openings.append(OpeningRecord(
                opening_id=int(rid),
                opening_code=name_str.split()[0].upper(),
                opening_type=otype,
                height_mm=h_mm,
                width_mm=w_mm,
                quantity=qty,
                gross_area_m2_single=single_m2,
                gross_area_m2_total=total_m2,
                is_deductible=is_deduct,
                net_deduction_area_m2=net_ded,
                door_leaf_paint_area_m2=leaf_m2,
                frame_material="Timber / Metal Frame",
                location_notes=str(status or "Internal"),
            ))

        return cls(openings)

    def total_net_deduction_m2(self) -> float:
        return sum(o.net_deduction_area_m2 for o in self.openings)

    def total_door_leaf_paint_m2(self) -> float:
        return sum(o.door_leaf_paint_area_m2 for o in self.openings)


def derive_opening_deductions(conn: sqlite3.Connection, workspace_id: int) -> OpeningDeductionRegistry:
    return OpeningDeductionRegistry.derive_for_workspace(conn, workspace_id)
