"""pb_commercial_export_preflight_v163.py — Phase 6D Commercial Export Preflight & JobHub Publish Integrity.

Phase 6D Authority:
  - Consumes Phase 6B `collect_commercial_review_signals` from `pb_commercial_review_v161.py`.
  - Single Review & Export Preflight Authority: NO separate scale scanner, quantity scanner,
    or readiness database. Phase 6D consumes Phase 6B.
  - Fail-Closed Source Availability: If any required source family (takeoff, register, scale)
    is unavailable or incomplete, preflight_status is forced to BLOCKED.
  - TOCTOU Safety: Immediately before consequential final JobHub publish, re-derives
    Phase 6B review and fresh Phase 6D preflight, verifying exact fingerprint & payload match.
"""
from __future__ import annotations

import dataclasses
import hashlib
import html
import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from pb_commercial_review_v161 import (
    CommercialReviewResult,
    CommercialReviewSignal,
    REQUIRED_FAMILIES,
    SEVERITY_BLOCKER,
    SEVERITY_INFO,
    SEVERITY_REVIEW,
    collect_commercial_review_signals,
)


@dataclasses.dataclass(frozen=True)
class CommercialPreflightResult:
    workspace_id: int
    job_no: str
    job_name: str
    drawing_issue: str
    jobhub_job_id: Optional[int]
    preflight_status: str              # AVAILABLE | AVAILABLE_WITH_WARNING | BLOCKED | UNAVAILABLE
    blocker_count: int
    warning_count: int
    info_count: int
    total_review_items: int
    required_coverage_complete: bool
    unavailable_required_sources: List[str]
    internal_download_state: str        # AVAILABLE | AVAILABLE_WITH_WARNING | UNAVAILABLE
    draft_handoff_state: str           # AVAILABLE | AVAILABLE_WITH_WARNING | UNAVAILABLE
    final_publish_state: str           # AVAILABLE | AVAILABLE_WITH_WARNING | BLOCKED | UNAVAILABLE
    blocking_reasons: List[str]
    warnings: List[str]
    review_fingerprint: str
    preflight_fingerprint: str
    payload_hash: str
    total_takeoff_rows: int
    publishable_takeoff_rows: int
    excluded_takeoff_rows: int
    floor_reference_rows: int
    measured_zero_rows: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "job_no": html.escape(self.job_no or ""),
            "job_name": html.escape(self.job_name or ""),
            "drawing_issue": html.escape(self.drawing_issue or ""),
            "jobhub_job_id": self.jobhub_job_id,
            "preflight_status": self.preflight_status,
            "blocker_count": self.blocker_count,
            "warning_count": self.warning_count,
            "info_count": self.info_count,
            "total_review_items": self.total_review_items,
            "required_coverage_complete": self.required_coverage_complete,
            "unavailable_required_sources": [html.escape(s) for s in self.unavailable_required_sources],
            "internal_download_state": self.internal_download_state,
            "draft_handoff_state": self.draft_handoff_state,
            "final_publish_state": self.final_publish_state,
            "blocking_reasons": [html.escape(r) for r in self.blocking_reasons],
            "warnings": [html.escape(w) for w in self.warnings],
            "review_fingerprint": self.review_fingerprint,
            "preflight_fingerprint": self.preflight_fingerprint,
            "payload_hash": self.payload_hash,
            "total_takeoff_rows": self.total_takeoff_rows,
            "publishable_takeoff_rows": self.publishable_takeoff_rows,
            "excluded_takeoff_rows": self.excluded_takeoff_rows,
            "floor_reference_rows": self.floor_reference_rows,
            "measured_zero_rows": self.measured_zero_rows,
        }


def _db_query(conn_or_app: Any, sql: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
    if hasattr(conn_or_app, "lquery"):
        raw = conn_or_app.lquery(sql, params)
        if isinstance(raw, list):
            return [dict(r) for r in raw]
        if hasattr(raw, "to_dict"):
            return [dict(r) for r in raw.to_dict("records")]
    if hasattr(conn_or_app, "execute") or hasattr(conn_or_app, "cursor"):
        conn = conn_or_app
        cur = conn.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]
    return []


def _get_workspace_meta(conn_or_app: Any, workspace_id: int) -> Dict[str, Any]:
    rows = _db_query(conn_or_app, "SELECT id, job_no, job_name, drawing_issue, jobhub_job_id FROM workspaces WHERE id=?", (workspace_id,))
    if not rows:
        raise ValueError(f"Workspace #{workspace_id} does not exist.")
    row = rows[0]
    return {
        "id": row.get("id"),
        "job_no": row.get("job_no") or "",
        "job_name": row.get("job_name") or "",
        "drawing_issue": row.get("drawing_issue") or "",
        "jobhub_job_id": row.get("jobhub_job_id"),
    }


def _get_takeoff_row_stats(conn_or_app: Any, workspace_id: int) -> Tuple[int, int, int, int, int, str]:
    """Retrieves row statistics and payload fingerprint for a workspace."""
    rows = _db_query(
        conn_or_app,
        "SELECT id, section, element, location, unit, quantity, inclusion_status, row_role FROM takeoff_rows WHERE workspace_id=? ORDER BY id",
        (workspace_id,)
    )
    total = len(rows)
    publishable = 0
    excluded = 0
    floor_ref = 0
    measured_zero = 0

    pub_payload_items = []

    for r in rows:
        r_id = r.get("id")
        unit_str = str(r.get("unit") or "")
        qty = r.get("quantity")
        inclusion_str = str(r.get("inclusion_status") or "").lower()
        role_str = str(r.get("row_role") or "").lower()

        is_excluded = (inclusion_str == "excluded" or "exclude" in inclusion_str)
        is_floor = (role_str == "floor_area")

        if is_excluded:
            excluded += 1
        elif is_floor:
            floor_ref += 1
        else:
            publishable += 1
            pub_payload_items.append({
                "id": r_id,
                "sec": str(r.get("section") or ""),
                "elem": str(r.get("element") or ""),
                "loc": str(r.get("location") or ""),
                "unit": unit_str,
                "qty": float(qty) if qty is not None else None,
            })

        if qty is not None and float(qty) == 0.0:
            measured_zero += 1

    payload_json = json.dumps(pub_payload_items, sort_keys=True)
    payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

    return total, publishable, excluded, floor_ref, measured_zero, payload_hash


def derive_export_preflight(conn_or_app: Any, workspace_id: int, bridge_available: bool = True) -> CommercialPreflightResult:
    """Derives a normalized read-only Phase 6D Commercial Preflight Result by consuming Phase 6B."""
    meta = _get_workspace_meta(conn_or_app, workspace_id)

    # Call Phase 6B collector (Single Review Authority)
    workspace_dict = {"id": workspace_id, "job_no": meta["job_no"], "job_name": meta["job_name"], "drawing_issue": meta["drawing_issue"]}
    review_res: CommercialReviewResult = collect_commercial_review_signals(conn_or_app, workspace_dict)

    total_rows, pub_rows, excl_rows, floor_rows, zero_rows, payload_hash = _get_takeoff_row_stats(conn_or_app, workspace_id)

    blocking_reasons: List[str] = []
    warnings: List[str] = []
    unavailable_sources: List[str] = []

    # Fail-Closed Required Sources & Coverage Check
    signals = getattr(review_res, "signals", getattr(review_res, "items", []))
    source_coverage = getattr(review_res, "source_coverage", {})
    
    required_coverage_complete = True
    for fam in REQUIRED_FAMILIES:
        st_val = source_coverage.get(fam, "UNAVAILABLE")
        if st_val != "AVAILABLE":
            required_coverage_complete = False
            msg = f"Required source '{fam}' is {st_val}"
            blocking_reasons.append(msg)
            unavailable_sources.append(fam)

    # Process Phase 6B review signals
    blocker_count = getattr(review_res, "blocker_count", 0)
    warning_count = getattr(review_res, "review_count", 0)
    info_count = getattr(review_res, "info_count", 0)

    for sig in signals:
        sev = getattr(sig, "severity", SEVERITY_INFO)
        cat = getattr(sig, "category", "Review")
        summary = getattr(sig, "summary", getattr(sig, "message", "Signal"))

        if sev == SEVERITY_BLOCKER:
            blocking_reasons.append(f"{cat}: {summary}")
        elif sev == SEVERITY_REVIEW:
            warnings.append(f"{cat}: {summary}")

    # Publishable row count gate
    if pub_rows == 0:
        blocking_reasons.append("Zero publishable take-off rows present in workspace.")

    # Determine Preflight Status
    if blocking_reasons:
        preflight_status = "BLOCKED"
    elif warnings:
        preflight_status = "AVAILABLE_WITH_WARNING"
    else:
        preflight_status = "AVAILABLE"

    # Internal Downloads (Excel/ZIP) availability
    if total_rows > 0:
        internal_download_state = "AVAILABLE_WITH_WARNING" if (blocker_count > 0 or warning_count > 0) else "AVAILABLE"
    else:
        internal_download_state = "UNAVAILABLE"

    # JobHub Draft Handoff availability
    if bridge_available and meta["jobhub_job_id"] and pub_rows > 0:
        draft_handoff_state = "AVAILABLE_WITH_WARNING" if (blocker_count > 0 or warning_count > 0) else "AVAILABLE"
    else:
        draft_handoff_state = "UNAVAILABLE"

    # Final JobHub Publish Policy: STRICT GATES
    if not bridge_available or not meta["jobhub_job_id"]:
        final_publish_state = "UNAVAILABLE"
    elif preflight_status == "BLOCKED":
        final_publish_state = "BLOCKED"
    elif preflight_status == "AVAILABLE_WITH_WARNING":
        final_publish_state = "AVAILABLE_WITH_WARNING"
    else:
        final_publish_state = "AVAILABLE"

    # Compute Deterministic Fingerprints
    sig_ids = [getattr(s, "signal_id", str(idx)) for idx, s in enumerate(signals)]
    review_fp = hashlib.sha256(json.dumps(sorted(sig_ids)).encode("utf-8")).hexdigest()

    fingerprint_data = {
        "workspace_id": workspace_id,
        "job_no": meta["job_no"],
        "drawing_issue": meta["drawing_issue"],
        "review_fingerprint": review_fp,
        "preflight_status": preflight_status,
        "blocker_count": blocker_count,
        "warning_count": warning_count,
        "blocking_reasons": blocking_reasons,
        "pub_rows": pub_rows,
        "payload_hash": payload_hash,
    }
    raw_json = json.dumps(fingerprint_data, sort_keys=True)
    preflight_fingerprint = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()

    return CommercialPreflightResult(
        workspace_id=workspace_id,
        job_no=meta["job_no"],
        job_name=meta["job_name"],
        drawing_issue=meta["drawing_issue"],
        jobhub_job_id=meta["jobhub_job_id"],
        preflight_status=preflight_status,
        blocker_count=blocker_count,
        warning_count=warning_count,
        info_count=info_count,
        total_review_items=len(signals),
        required_coverage_complete=required_coverage_complete,
        unavailable_required_sources=unavailable_sources,
        internal_download_state=internal_download_state,
        draft_handoff_state=draft_handoff_state,
        final_publish_state=final_publish_state,
        blocking_reasons=blocking_reasons,
        warnings=warnings,
        review_fingerprint=review_fp,
        preflight_fingerprint=preflight_fingerprint,
        payload_hash=payload_hash,
        total_takeoff_rows=total_rows,
        publishable_takeoff_rows=pub_rows,
        excluded_takeoff_rows=excl_rows,
        floor_reference_rows=floor_rows,
        measured_zero_rows=zero_rows,
    )


def verify_toctou_and_publish_jobhub(
    conn_or_app: Any,
    workspace_id: int,
    bridge: Any,
    user_name: str,
    expected_fingerprint: str,
    acknowledgement_confirmed: bool,
    publish_fn: Any
) -> Dict[str, Any]:
    """Time-Of-Check / Time-Of-Use (TOCTOU) Protected JobHub Final Publish Gate.

    Re-derives Phase 6B review, fresh Phase 6D preflight, and payload hash immediately prior to calling downstream
    final publish. Raises RuntimeError if state changed, payload mutated, or blocked.
    """
    if not bridge:
        raise RuntimeError("JobHub bridge unavailable for final publish.")

    preflight = derive_export_preflight(conn_or_app, workspace_id, bridge_available=True)

    if preflight.final_publish_state == "BLOCKED":
        reasons_str = "; ".join(preflight.blocking_reasons)
        raise RuntimeError(f"Final publish blocked by preflight QA gate: {reasons_str}")

    if preflight.final_publish_state == "UNAVAILABLE":
        raise RuntimeError("Final publish is unavailable. Link workspace to a valid JobHub job.")

    if preflight.preflight_fingerprint != expected_fingerprint:
        raise RuntimeError("Project QA/export state changed. Review the updated preflight before publishing.")

    if preflight.preflight_status == "AVAILABLE_WITH_WARNING" and not acknowledgement_confirmed:
        raise RuntimeError("Typed acknowledgement required to publish a project with REVIEW warnings.")

    # Execute downstream publish function with return verification
    result = publish_fn(workspace_id, bridge, user_name)
    if not isinstance(result, dict) or not result.get("package_id"):
        raise RuntimeError("Downstream final publish failed to generate a valid JobHub package receipt.")

    result["preflight_fingerprint"] = preflight.preflight_fingerprint
    result["review_fingerprint"] = preflight.review_fingerprint
    result["payload_hash"] = preflight.payload_hash
    return result
