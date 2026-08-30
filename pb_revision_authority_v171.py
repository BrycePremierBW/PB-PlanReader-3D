"""pb_revision_authority_v171.py — Drawing Set Revision & Superseding Authority Engine.

Manages drawing revision parsing, superseding detection, revision lineage mapping,
and historical provenance tracking across multi-revision drawing uploads in PB PlanReader.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple, Sequence


@dataclass
class PageRevisionRecord:
    page_id: int
    sheet_number: str
    revision_code: str
    revision_rank: int
    is_current: bool
    is_superseded: bool
    superseded_by_page_id: Optional[int]
    page_label: str
    file_name: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page_id": self.page_id,
            "sheet_number": self.sheet_number,
            "revision_code": self.revision_code,
            "revision_rank": self.revision_rank,
            "is_current": self.is_current,
            "is_superseded": self.is_superseded,
            "superseded_by_page_id": self.superseded_by_page_id,
            "page_label": self.page_label,
            "file_name": self.file_name,
        }


def parse_revision_code(text: str) -> Tuple[str, int]:
    """Extract revision code and numerical rank from drawing text/filenames.

    Examples:
        'Rev A' -> ('A', 1)
        'Rev B' -> ('B', 2)
        'Rev 01' -> ('01', 1)
        'Rev C2' -> ('C2', 32)
    """
    if not text:
        return ("0", 0)

    # 1. Match explicit 'Rev A', 'Rev-02', 'Revision B', '_Rev_C'
    m = re.search(r'rev(?:ision)?[\s\-_]*([a-z0-9]{1,3})', text, re.IGNORECASE)
    if not m:
        # 2. Match trailing single letter or number before extension
        m = re.search(r'[\s\-_]([a-z]|\d{1,3})\.[a-z0-9]+$', text, re.IGNORECASE)
    if m:
        code = m.group(1).upper()
        if code.isdigit():
            rank = int(code)
        elif len(code) == 1 and 'A' <= code <= 'Z':
            rank = ord(code) - ord('A') + 1
        else:
            rank = 1
        return (code, rank)
    return ("0", 0)


def extract_sheet_number(label: str) -> str:
    """Extract standard Australian sheet number from page label e.g., 'A-101 Floor Plan' -> 'A-101'."""
    if not label:
        return "UNKNOWN"
    m = re.search(r'([a-z]{1,2}[\s\-_]?\d{3,4}[a-z]?)', label, re.IGNORECASE)
    if m:
        return m.group(1).upper().replace(" ", "")
    return label.split()[0].upper()


class RevisionAuthorityRegistry:
    """Manages revision lineage and superseding authority across drawing sets."""

    def __init__(self, records: List[PageRevisionRecord]):
        self.records = records
        self._by_sheet: Dict[str, List[PageRevisionRecord]] = {}
        for r in records:
            self._by_sheet.setdefault(r.sheet_number, []).append(r)
        # Sort each lineage by rank descending
        for sheet in self._by_sheet:
            self._by_sheet[sheet].sort(key=lambda x: x.revision_rank, reverse=True)

    @classmethod
    def derive_for_workspace(cls, conn: sqlite3.Connection, workspace_id: int) -> "RevisionAuthorityRegistry":
        cur = conn.cursor()
        cur.execute(
            """
            SELECT p.id, p.page_label, p.file_name, p.page_number
            FROM pages p
            WHERE p.workspace_id=?
            ORDER BY p.id ASC
            """,
            (workspace_id,)
        )
        pages = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]

        records_map: Dict[str, List[Dict[str, Any]]] = {}
        for p in pages:
            pid = int(p["id"])
            plabel = str(p.get("page_label") or "")
            fname = str(p.get("file_name") or "")
            sheet = extract_sheet_number(plabel)
            rev_code, rank = parse_revision_code(fname or plabel)
            records_map.setdefault(sheet, []).append({
                "page_id": pid,
                "sheet_number": sheet,
                "revision_code": rev_code,
                "revision_rank": rank,
                "page_label": plabel,
                "file_name": fname,
            })

        final_records: List[PageRevisionRecord] = []
        for sheet, group in records_map.items():
            # Sort by rank descending, then page_id descending
            group.sort(key=lambda x: (x["revision_rank"], x["page_id"]), reverse=True)
            highest_id = group[0]["page_id"]
            for idx, item in enumerate(group):
                is_curr = (idx == 0)
                is_super = not is_curr
                super_by = highest_id if is_super else None
                final_records.append(PageRevisionRecord(
                    page_id=item["page_id"],
                    sheet_number=sheet,
                    revision_code=item["revision_code"],
                    revision_rank=item["revision_rank"],
                    is_current=is_curr,
                    is_superseded=is_super,
                    superseded_by_page_id=super_by,
                    page_label=item["page_label"],
                    file_name=item["file_name"],
                ))

        return cls(final_records)

    def apply_superseding_to_db(self, conn: sqlite3.Connection, workspace_id: int) -> int:
        """Mark superseded pages as selected=0 in DB while keeping current pages selected=1."""
        cur = conn.cursor()
        updated_count = 0
        for r in self.records:
            sel_val = 0 if r.is_superseded else 1
            cur.execute(
                "UPDATE pages SET selected=? WHERE id=? AND workspace_id=?",
                (sel_val, r.page_id, workspace_id)
            )
            updated_count += 1
        conn.commit()
        return updated_count

    def get_superseded_page_issues(self, conn: sqlite3.Connection, workspace_id: int) -> List[Dict[str, Any]]:
        """Find takeoff rows or measurement lines referencing superseded drawing pages."""
        cur = conn.cursor()
        superseded_ids = {r.page_id for r in self.records if r.is_superseded}
        superseded_labels = {r.page_label for r in self.records if r.is_superseded}

        issues = []
        if not superseded_ids:
            return issues

        # Check takeoff rows
        cur.execute(
            "SELECT id, source_page, element FROM takeoff_rows WHERE workspace_id=?",
            (workspace_id,)
        )
        for rid, spage, elem in cur.fetchall():
            spage_str = str(spage or "").strip()
            if spage_str in superseded_labels or spage_str in {str(i) for i in superseded_ids}:
                issues.append({
                    "row_id": rid,
                    "source_page": spage_str,
                    "element": elem,
                    "issue_type": "SUPERSEDED_DRAWING_REFERENCE",
                    "severity": "REVIEW",
                    "title": f"Take-off Row #{rid} References Superseded Drawing",
                    "description": f"Item '{elem}' references superseded drawing page '{spage_str}'. Re-verify measurements on current revision.",
                })
        return issues


def derive_revision_authority(conn: sqlite3.Connection, workspace_id: int) -> RevisionAuthorityRegistry:
    return RevisionAuthorityRegistry.derive_for_workspace(conn, workspace_id)
