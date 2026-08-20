"""PlanReader v1.2.26 estimator page-selection lock.

Initial indexing still starts with pages selected, allowing automatic triage to turn
irrelevant sheets off. Once a page is selected=0, later automatic re-registration,
unit recovery and Autopilot runs may not silently switch it back on. An estimator
can explicitly reselect the page in Drawing Register when it is wanted again.
"""
from __future__ import annotations

from typing import Any, Dict, List, Set

import pb_auto_geometry_v1219 as auto
import pb_autopilot_v1223 as autopilot
import pb_unit_floor_area_v1221 as unit

VERSION = "1.2.26"


def _deselected_ids_for_document(app: Any, document_id: int) -> Set[int]:
    return {int(row["id"]) for row in app.lquery("SELECT id FROM pages WHERE document_id=? AND COALESCE(selected,0)=0", (int(document_id),))}


def _deselected_ids_for_workspace(app: Any, workspace_id: int) -> Set[int]:
    return {int(row["id"]) for row in app.lquery("SELECT id FROM pages WHERE workspace_id=? AND COALESCE(selected,0)=0", (int(workspace_id),))}


def _restore_zero(app: Any, page_ids: Set[int]) -> None:
    if not page_ids:
        return
    conn = app.local_connect()
    try:
        conn.executemany("UPDATE pages SET selected=0 WHERE id=?", [(int(page_id),) for page_id in sorted(page_ids)])
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()


def apply(app: Any) -> None:
    if getattr(app, "_pb_selection_lock_v1226_applied", False):
        return
    app._pb_selection_lock_v1226_applied = True

    base_auto_select = auto.auto_select_document_pages
    def _locked_auto_select(app_obj: Any, document_id: int):
        locked = _deselected_ids_for_document(app_obj, int(document_id))
        report = base_auto_select(app_obj, int(document_id))
        _restore_zero(app_obj, locked)
        if isinstance(report, dict):
            for item in report.get("pages") or []:
                if int(item.get("page_id") or 0) in locked:
                    item["selected"] = False
                    item["reason"] = "Estimator/previous triage deselection retained"
                    item["score"] = 0
            report["kept"] = sum(1 for item in report.get("pages") or [] if item.get("selected"))
            report["discarded"] = sum(1 for item in report.get("pages") or [] if not item.get("selected"))
        return report
    auto.auto_select_document_pages = _locked_auto_select
    app.auto_select_document_pages = lambda document_id: auto.auto_select_document_pages(app, int(document_id))

    base_triage = autopilot.triage_workspace
    def _locked_triage(app_obj: Any, workspace_id: int):
        locked = _deselected_ids_for_workspace(app_obj, int(workspace_id))
        report = base_triage(app_obj, int(workspace_id))
        _restore_zero(app_obj, locked)
        if isinstance(report, dict):
            for item in report.get("pages") or []:
                if int(item.get("page_id") or 0) in locked:
                    item["selected"] = False
                    item["reason"] = "Estimator/previous triage deselection retained"
                    item["score"] = 0
            report["kept"] = sum(1 for item in report.get("pages") or [] if item.get("selected"))
            report["discarded"] = sum(1 for item in report.get("pages") or [] if not item.get("selected"))
        return report
    autopilot.triage_workspace = _locked_triage

    # v1.2.21 used to reselect any page that looked like a unit layout. That was
    # useful during the earlier classifier bug, but now violates explicit selection.
    unit.restore_obvious_unit_plan_pages = lambda app_obj, workspace_id: 0
