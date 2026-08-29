"""Final Phase 5M diagnostics import surface."""
from __future__ import annotations

import pb_3d_diagnostics_phase5m as _phase5m
from pb_3d_diagnostics_phase5m import *  # noqa: F401,F403


def generate_production_diagnostics_report(project, workspace_data=None, skipped_items=None):
    report = _phase5m.generate_production_diagnostics_report(
        project,
        workspace_data=workspace_data,
        skipped_items=skipped_items,
    )
    # Retain the explicit historical QA wording that documents why duplicate
    # strong candidates are never collapsed by last-write-wins.
    for rec in report.get("per_wall_quantity_reconciliation") or []:
        if rec.get("reconciliation_status") == "ambiguous":
            text = str(rec.get("explanation") or "")
            if "Rejection of last-write-wins" not in text:
                rec["explanation"] = (text.rstrip(".") + ". Rejection of last-write-wins.").strip()
    return report


def __getattr__(name: str):
    return getattr(_phase5m, name)
