"""pb_sheet_classification_v172.py — Multi-Page Plan Classification & Sheet Role Detection Engine.

Classifies drawing pages into canonical construction sheet roles (Floor Plan, RCP, Elevation,
Schedules, Cover/Legend) and extracts storey/discipline context for PB PlanReader.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class SheetClassificationRecord:
    page_id: int
    page_label: str
    canonical_role: str  # FLOOR_PLAN | REFLECTED_CEILING_PLAN | ELEVATION | FINISH_SCHEDULE | DOOR_WINDOW_SCHEDULE | COVER_INDEX_LEGEND | OTHER_GENERAL
    confidence: float
    discipline: str      # ARCHITECTURAL | STRUCTURAL | INTERIOR | GENERAL
    storey_level: str    # e.g. 'Level 1', 'Ground Floor', 'All Levels', 'Unspecified'
    primary_takeoff_target: str # 'WALL_FLOOR', 'CEILING', 'ELEVATION_SURFACE', 'FINISHES', 'OPENINGS', 'NONE'
    source_note: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page_id": self.page_id,
            "page_label": self.page_label,
            "canonical_role": self.canonical_role,
            "confidence": round(self.confidence, 2),
            "discipline": self.discipline,
            "storey_level": self.storey_level,
            "primary_takeoff_target": self.primary_takeoff_target,
            "source_note": self.source_note,
        }


def classify_sheet_role(title: str, text_content: str = "") -> Tuple[str, float, str, str]:
    """Classify sheet role, confidence, discipline, and primary takeoff target from title block & page text."""
    combined = f"{title} {text_content}".lower()

    # 1. Reflected Ceiling Plan
    if any(k in combined for k in ["reflected ceiling", "rcp", "ceiling plan"]):
        return ("REFLECTED_CEILING_PLAN", 0.95, "ARCHITECTURAL", "CEILING")

    # 2. Schedules
    if any(k in combined for k in ["door schedule", "window schedule", "door & window", "opening schedule"]):
        return ("DOOR_WINDOW_SCHEDULE", 0.95, "ARCHITECTURAL", "OPENINGS")
    if any(k in combined for k in ["finish schedule", "finishes schedule", "paint schedule", "room finish"]):
        return ("FINISH_SCHEDULE", 0.95, "INTERIOR", "FINISHES")

    # 3. Cover / Legend / Index
    if any(k in combined for k in ["cover sheet", "title sheet", "locality plan", "drawing index", "legend & notes", "abbreviations"]):
        return ("COVER_INDEX_LEGEND", 0.95, "GENERAL", "NONE")

    # 4. Elevation / Section
    if any(k in combined for k in ["elevation", "facade", "external elevation", "internal elevation", "section"]):
        return ("ELEVATION", 0.90, "ARCHITECTURAL", "ELEVATION_SURFACE")

    # 5. Floor Plan
    if any(k in combined for k in ["floor plan", "ground plan", "level 1", "level 2", "level 3", "basement plan", "layout plan"]):
        return ("FLOOR_PLAN", 0.90, "ARCHITECTURAL", "WALL_FLOOR")

    # General fallback
    if "plan" in combined:
        return ("FLOOR_PLAN", 0.70, "ARCHITECTURAL", "WALL_FLOOR")

    return ("OTHER_GENERAL", 0.50, "GENERAL", "NONE")


def extract_storey_level(text: str) -> str:
    """Extract storey or level identifier from page label or title block text."""
    if not text:
        return "Unspecified"

    m = re.search(r'(ground\s*floor|level\s*\d{1,2}|basement(?:\s*\d)?|mezzanine|first\tag|roof(?:\s*plan)?)', text, re.IGNORECASE)
    if m:
        return m.group(1).title()

    m_num = re.search(r'L(\d{1,2})', text, re.IGNORECASE)
    if m_num:
        return f"Level {int(m_num.group(1))}"

    return "Unspecified"


class SheetClassificationRegistry:
    """Manages sheet role classifications across all pages in a workspace."""

    def __init__(self, records: List[SheetClassificationRecord]):
        self.records = records
        self._by_id = {r.page_id: r for r in records}

    @classmethod
    def derive_for_workspace(cls, conn: sqlite3.Connection, workspace_id: int) -> "SheetClassificationRegistry":
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, page_label, page_type
            FROM pages
            WHERE workspace_id=?
            ORDER BY id ASC
            """,
            (workspace_id,)
        )
        pages = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]

        records = []
        for p in pages:
            pid = int(p["id"])
            label = str(p.get("page_label") or "")
            ptype = str(p.get("page_type") or "")

            role, conf, disc, target = classify_sheet_role(f"{label} {ptype}")
            level = extract_storey_level(label)

            records.append(SheetClassificationRecord(
                page_id=pid,
                page_label=label,
                canonical_role=role,
                confidence=conf,
                discipline=disc,
                storey_level=level,
                primary_takeoff_target=target,
                source_note=f"Classified as {role} ({target})",
            ))

        return cls(records)

    def apply_roles_to_db(self, conn: sqlite3.Connection, workspace_id: int) -> int:
        """Update page_type column in pages table with canonical roles."""
        cur = conn.cursor()
        count = 0
        for r in self.records:
            cur.execute(
                "UPDATE pages SET page_type=? WHERE id=? AND workspace_id=?",
                (r.canonical_role, r.page_id, workspace_id)
            )
            count += 1
        conn.commit()
        return count


def derive_sheet_classifications(conn: sqlite3.Connection, workspace_id: int) -> SheetClassificationRegistry:
    return SheetClassificationRegistry.derive_for_workspace(conn, workspace_id)
