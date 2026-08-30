"""pb_multi_page_scale_v170.py — Multi-Page Scale Authority & Scale Gate Delegation Engine.

Provides unified, multi-page scale calibration authority across selected drawing set pages,
vector/text scale bar parsing, dynamic scale propagation/inheritance, geometric re-scaling,
and strict scale-gate issue delegation for PB PlanReader.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple, Sequence


@dataclass
class PageScaleRecord:
    page_id: int
    page_label: str
    page_type: str
    scale_status: str  # 'CALIBRATED', 'PROVISIONAL_AUTO', 'UNCALIBRATED', 'NOT_REQUIRED'
    px_per_m: float
    m_per_pt: float
    scale_ratio: Optional[int]
    calibration_method: str  # 'MANUAL', 'VECTOR_DIMENSION', 'TITLE_BLOCK_TEXT', 'INHERITED', 'NONE'
    confidence: float
    has_measurement_lines: bool
    has_takeoff_rows: bool
    source_note: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page_id": self.page_id,
            "page_label": self.page_label,
            "page_type": self.page_type,
            "scale_status": self.scale_status,
            "px_per_m": round(self.px_per_m, 4),
            "m_per_pt": round(self.m_per_pt, 6) if self.m_per_pt > 0 else 0.0,
            "scale_ratio": self.scale_ratio,
            "calibration_method": self.calibration_method,
            "confidence": round(self.confidence, 2),
            "has_measurement_lines": self.has_measurement_lines,
            "has_takeoff_rows": self.has_takeoff_rows,
            "source_note": self.source_note,
        }


def extract_scale_ratio_from_text(text: str) -> Optional[int]:
    """Parse common Australian/international drawing scale strings e.g., '1:100', '1:50', '1/100 @ A1'."""
    if not text:
        return None
    # Look for patterns like 1:100, 1:50, 1/200, SCALE 1:100
    m = re.search(r'(?:scale\s*)?1\s*[:/]\s*(\d{2,5})', text, re.IGNORECASE)
    if m:
        try:
            val = int(m.group(1))
            if 5 <= val <= 10000:
                return val
        except ValueError:
            pass
    return None


def calculate_m_per_pt_from_px_per_m(px_per_m: float, render_zoom: float = 2.0) -> float:
    """Convert screen render pixels per metre (at render_zoom) to PDF native points per metre."""
    if px_per_m <= 0:
        return 0.0
    # PDF native resolution is 72 pt/inch. Standard render zoom 2.0 is 144 DPI (72 * 2).
    # px_per_pt = 72 * render_zoom / 72 = render_zoom.
    # Therefore, m_per_pt = 1.0 / (px_per_m / render_zoom) = render_zoom / px_per_m.
    return render_zoom / px_per_m


class MultiPageScaleRegistry:
    """Manages multi-page scale authority across all pages in a workspace."""

    def __init__(self, records: List[PageScaleRecord]):
        self.records = records
        self._by_id = {r.page_id: r for r in records}

    @classmethod
    def derive_for_workspace(cls, conn: sqlite3.Connection, workspace_id: int) -> "MultiPageScaleRegistry":
        """Build scale authority for all selected pages in a workspace."""
        cur = conn.cursor()

        # Query all selected pages for workspace
        cur.execute(
            """
            SELECT p.id, p.page_label, p.page_type, p.px_per_m, p.scale_text, p.selected, p.page_number
            FROM pages p
            WHERE p.workspace_id=? AND COALESCE(p.selected, 1)=1
            ORDER BY p.page_number ASC, p.id ASC
            """,
            (workspace_id,)
        )
        pages_data = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]

        # Query page IDs that have measurement lines
        cur.execute(
            "SELECT DISTINCT page_id FROM measurement_lines WHERE workspace_id=? AND page_id IS NOT NULL",
            (workspace_id,)
        )
        measured_page_ids = {r[0] for r in cur.fetchall() if r[0] is not None}

        # Query page IDs associated with takeoff rows
        cur.execute(
            "SELECT DISTINCT source_page FROM takeoff_rows WHERE workspace_id=? AND source_page IS NOT NULL",
            (workspace_id,)
        )
        takeoff_page_refs = {str(r[0]).strip() for r in cur.fetchall() if r[0] is not None}

        records = []
        primary_calibrated_ratio: Optional[int] = None
        primary_px_per_m: float = 0.0

        # First pass: evaluate direct calibration per page
        for p in pages_data:
            pid = int(p["id"])
            plabel = str(p.get("page_label") or f"Page {p.get('page_number', pid)}")
            ptype = str(p.get("page_type") or "Plan").lower()
            px_m = float(p.get("px_per_m") or 0.0)
            stext = str(p.get("scale_text") or "")

            has_m = pid in measured_page_ids
            has_t = plabel in takeoff_page_refs or str(pid) in takeoff_page_refs

            # Detect cover/legend/specification pages that do not require scale
            is_non_drawing = any(k in ptype for k in ["cover", "legend", "specification", "index", "notes"]) or \
                             any(k in plabel.lower() for k in ["cover", "legend", "index", "specification"])

            ratio = extract_scale_ratio_from_text(stext)

            if px_m > 0:
                status = "CALIBRATED"
                method = "MANUAL"
                conf = 1.0
                note = f"Explicit calibration ({px_m:.1f} px/m)"
                if not primary_calibrated_ratio and ratio:
                    primary_calibrated_ratio = ratio
                    primary_px_per_m = px_m
            elif ratio:
                # Text scale ratio parsed, but explicit pixel calibration pending
                px_m = (72.0 * 2.0) / (ratio / 1000.0 * 72.0 / 0.0254) if ratio > 0 else 0.0
                status = "PROVISIONAL_AUTO"
                method = "TITLE_BLOCK_TEXT"
                conf = 0.75
                note = f"Derived title block scale 1:{ratio}"
            elif is_non_drawing and not has_m and not has_t:
                status = "NOT_REQUIRED"
                method = "NONE"
                conf = 1.0
                note = "Cover/legend page — scale not required"
            else:
                status = "UNCALIBRATED"
                method = "NONE"
                conf = 0.0
                note = "Uncalibrated drawing page"

            m_pt = calculate_m_per_pt_from_px_per_m(px_m)

            records.append(PageScaleRecord(
                page_id=pid,
                page_label=plabel,
                page_type=ptype,
                scale_status=status,
                px_per_m=px_m,
                m_per_pt=m_pt,
                scale_ratio=ratio,
                calibration_method=method,
                confidence=conf,
                has_measurement_lines=has_m,
                has_takeoff_rows=has_t,
                source_note=note,
            ))

        # Second pass: cross-page scale inheritance for uncalibrated detail/elevation pages
        if primary_px_per_m > 0:
            for rec in records:
                if rec.scale_status == "UNCALIBRATED" and (rec.has_measurement_lines or rec.has_takeoff_rows):
                    # Inherit workspace primary scale context provisionally
                    rec.px_per_m = primary_px_per_m
                    rec.m_per_pt = calculate_m_per_pt_from_px_per_m(primary_px_per_m)
                    rec.scale_ratio = primary_calibrated_ratio
                    rec.scale_status = "PROVISIONAL_AUTO"
                    rec.calibration_method = "INHERITED"
                    rec.confidence = 0.60
                    rec.source_note = f"Inherited workspace primary scale context (1:{primary_calibrated_ratio or '100'})"

        return cls(records)

    def get_issues(self) -> List[Dict[str, Any]]:
        """Return scale-gate issue descriptors for review and preflight blocking."""
        issues = []
        for r in self.records:
            # Block if page has measurements or takeoff rows but is UNCALIBRATED (px_per_m <= 0)
            if (r.has_measurement_lines or r.has_takeoff_rows or r.scale_status == "UNCALIBRATED") and r.px_per_m <= 0:
                issues.append({
                    "page_id": r.page_id,
                    "page_label": r.page_label,
                    "page_type": r.page_type,
                    "px_per_m": r.px_per_m,
                    "scale_status": r.scale_status,
                    "issue_type": "UNCALIBRATED_SCALE",
                    "severity": "Critical",
                    "title": f"Uncalibrated Scale on {r.page_label}",
                    "description": f"Page '{r.page_label}' has referenced measurements or work items but lacks authoritative scale calibration.",
                })
        return issues

    def is_blocked(self) -> bool:
        """Returns True if any referenced drawing page has uncalibrated scale (px_per_m <= 0)."""
        return len(self.get_issues()) > 0


def derive_workspace_scale_authority(conn: sqlite3.Connection, workspace_id: int) -> MultiPageScaleRegistry:
    return MultiPageScaleRegistry.derive_for_workspace(conn, workspace_id)


def recompute_page_scale_geometry(conn: sqlite3.Connection, workspace_id: int, page_id: int, old_px_per_m: float, new_px_per_m: float) -> int:
    """Server-side recomputation of saved measurement_lines geometry when page scale is updated."""
    if new_px_per_m <= 0 or old_px_per_m <= 0 or math.isclose(old_px_per_m, new_px_per_m):
        return 0

    scale_ratio = old_px_per_m / new_px_per_m
    area_ratio = scale_ratio * scale_ratio

    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, line_type, length_m, area_m2
        FROM measurement_lines
        WHERE workspace_id=? AND page_id=?
        """,
        (workspace_id, page_id)
    )
    rows = cur.fetchall()
    updated_count = 0
    for rid, ltype, length_m, area_m2 in rows:
        new_length = round(float(length_m or 0.0) * scale_ratio, 4) if length_m else None
        new_area = round(float(area_m2 or 0.0) * area_ratio, 4) if area_m2 else None
        cur.execute(
            "UPDATE measurement_lines SET length_m=?, area_m2=? WHERE id=?",
            (new_length, new_area, rid)
        )
        updated_count += 1

    cur.execute("UPDATE pages SET px_per_m=? WHERE id=? AND workspace_id=?", (new_px_per_m, page_id, workspace_id))
    conn.commit()
    return updated_count
