"""PlanReader Phase 6A commercial estimator workspace shell.

This module is deliberately read-only. It derives workflow/status information from
existing workspace data and wraps the existing ``hero`` renderer. It does not
change quantity maths, geometry, calibration, opening authority, producer output,
or estimator-confirmed data.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional


VERSION = "1.6.0"
WORKFLOW_STEPS = ("Upload", "Scope & Read", "Review", "3D", "Export")


@dataclass(frozen=True)
class CommercialWorkspaceStatus:
    workspace_id: int
    documents_total: int = 0
    pages_total: int = 0
    pages_selected: int = 0
    pages_calibrated: int = 0
    takeoff_total: int = 0
    takeoff_ready: int = 0
    takeoff_review: int = 0
    register_review: int = 0
    scale_review: int = 0
    review_total: int = 0
    canonical_model_saved: bool = False
    canonical_model_fingerprint: Optional[str] = None
    current_step: str = "Upload"
    overall_state: str = "New"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _rows(frame: Any) -> List[Dict[str, Any]]:
    """Normalize pandas/list query results without making pandas a hard dependency."""
    if frame is None:
        return []
    if isinstance(frame, list):
        return [dict(row) for row in frame if isinstance(row, dict)]
    try:
        return [dict(row) for row in frame.to_dict("records")]
    except Exception:
        return []


def _query(app: Any, sql: str, params: tuple) -> List[Dict[str, Any]]:
    try:
        return _rows(app.ldf(sql, params))
    except Exception:
        return []


def _positive(value: Any) -> bool:
    try:
        result = float(value)
        return math.isfinite(result) and result > 0
    except Exception:
        return False


def _selected(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _quantity_present(value: Any) -> bool:
    """Distinguish a legitimate numeric zero from missing/malformed quantity data."""
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, str) and not value.strip():
        return False
    try:
        return math.isfinite(float(value))
    except Exception:
        return False


def _takeoff_needs_review(row: Dict[str, Any]) -> bool:
    status = str(row.get("quantity_status") or "").strip().lower()
    confidence = str(row.get("confidence") or "").strip().lower()
    inclusion = str(row.get("inclusion_status") or "").strip().lower()

    if status in {"to measure", "provisional measured", "provisional", ""}:
        return True
    if status in {"measured", "allowance"} and not _quantity_present(row.get("quantity")):
        return True
    if any(token in confidence for token in ("review", "check", "provisional", "derived", "low")):
        return True
    if inclusion in {"clarification", "provisional"}:
        return True
    return False


def _takeoff_ready(row: Dict[str, Any]) -> bool:
    status = str(row.get("quantity_status") or "").strip().lower()
    if _takeoff_needs_review(row):
        return False
    if status in {"excluded", "not applicable"}:
        return True
    # A measured zero can be a legitimate confirmed zero, but measured/allowance
    # rows still require an explicitly present finite numeric quantity.
    return status in {"measured", "allowance"} and _quantity_present(row.get("quantity"))


def _model_setting(app: Any, workspace_id: int) -> tuple[bool, Optional[str]]:
    """Recognise only structurally valid persisted model snapshots.

    This deliberately says only "saved", never "fresh": checking source staleness
    requires the heavier canonical evidence snapshot and is left to the 3D page.
    """
    if not hasattr(app, "workspace_setting"):
        return False, None
    try:
        raw = app.workspace_setting(workspace_id, "canonical_3d_model_v1", None)
    except Exception:
        return False, None
    if raw is None:
        return False, None
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except Exception:
            return False, None
    elif isinstance(raw, dict):
        payload = raw
    else:
        return False, None
    if not isinstance(payload.get("model_data"), dict):
        return False, None
    fingerprint = payload.get("source_revision_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        return False, None
    return True, fingerprint.strip()


def _workflow_state(
    *, documents_total: int, pages_total: int, takeoff_total: int,
    review_total: int, canonical_model_saved: bool,
) -> tuple[str, str]:
    if documents_total <= 0 or pages_total <= 0:
        return "Upload", "New"
    if takeoff_total <= 0:
        return "Scope & Read", "In progress"
    if review_total > 0:
        return "Review", "Review required"
    if not canonical_model_saved:
        return "3D", "Take-off reviewed"
    # "Available" is intentionally weaker than "ready": this lightweight shell
    # does not recompute canonical source staleness on every Streamlit rerun.
    return "Export", "Export available"


def derive_workspace_status(app: Any, workspace: Dict[str, Any]) -> CommercialWorkspaceStatus:
    """Derive commercial workflow status strictly from existing read-only evidence."""
    workspace_id = int(workspace.get("id"))

    documents = _query(
        app,
        "SELECT id FROM documents WHERE workspace_id=? ORDER BY id",
        (workspace_id,),
    )
    pages = _query(
        app,
        "SELECT id,selected,px_per_m FROM pages WHERE workspace_id=? ORDER BY id",
        (workspace_id,),
    )
    takeoff = _query(
        app,
        "SELECT id,quantity,quantity_status,confidence,inclusion_status FROM takeoff_rows WHERE workspace_id=? ORDER BY id",
        (workspace_id,),
    )
    registers = _query(
        app,
        "SELECT id,status FROM register_items WHERE workspace_id=? ORDER BY id",
        (workspace_id,),
    )

    pages_selected = sum(1 for row in pages if _selected(row.get("selected")))
    pages_calibrated = sum(
        1 for row in pages if _selected(row.get("selected")) and _positive(row.get("px_per_m"))
    )
    takeoff_review = sum(1 for row in takeoff if _takeoff_needs_review(row))
    takeoff_ready = sum(1 for row in takeoff if _takeoff_ready(row))
    register_review = sum(
        1 for row in registers
        if str(row.get("status") or "").strip().lower() in {"open", "to review"}
    )

    scale_review = 0
    if hasattr(app, "scale_gate_issues"):
        try:
            scale_review = len(app.scale_gate_issues(workspace_id) or [])
        except Exception:
            scale_review = 0

    # This is a count of independent review signals from existing sources, not a
    # deduplicated issue ledger. The UI labels it accordingly.
    review_total = takeoff_review + register_review + scale_review
    canonical_model_saved, fingerprint = _model_setting(app, workspace_id)
    current_step, overall_state = _workflow_state(
        documents_total=len(documents),
        pages_total=len(pages),
        takeoff_total=len(takeoff),
        review_total=review_total,
        canonical_model_saved=canonical_model_saved,
    )

    return CommercialWorkspaceStatus(
        workspace_id=workspace_id,
        documents_total=len(documents),
        pages_total=len(pages),
        pages_selected=pages_selected,
        pages_calibrated=pages_calibrated,
        takeoff_total=len(takeoff),
        takeoff_ready=takeoff_ready,
        takeoff_review=takeoff_review,
        register_review=register_review,
        scale_review=scale_review,
        review_total=review_total,
        canonical_model_saved=canonical_model_saved,
        canonical_model_fingerprint=fingerprint,
        current_step=current_step,
        overall_state=overall_state,
    )


def workflow_step_states(status: CommercialWorkspaceStatus) -> List[Dict[str, str]]:
    """Return compact presentation states without claiming unsupported accuracy."""
    current_index = WORKFLOW_STEPS.index(status.current_step)
    result: List[Dict[str, str]] = []
    for index, label in enumerate(WORKFLOW_STEPS):
        state = "upcoming"
        detail = ""
        if index < current_index:
            state = "complete"
        elif index == current_index:
            state = "current"

        if label == "Review" and status.review_total > 0:
            state = "review" if current_index >= 2 else state
            detail = f"{status.review_total} signal{'s' if status.review_total != 1 else ''}"
        elif label == "3D":
            detail = "saved snapshot" if status.canonical_model_saved else "not saved"
        elif label == "Upload":
            detail = f"{status.pages_total} sheet{'s' if status.pages_total != 1 else ''}"
        elif label == "Scope & Read":
            detail = f"{status.takeoff_total} row{'s' if status.takeoff_total != 1 else ''}"
        elif label == "Export" and status.overall_state == "Export available":
            detail = "available"
        result.append({"label": label, "state": state, "detail": detail})
    return result


def render_commercial_workspace_shell(app: Any, workspace: Dict[str, Any]) -> CommercialWorkspaceStatus:
    """Render a compact estimator-facing header; all values are read-only."""
    status = derive_workspace_status(app, workspace)
    st = app.st

    st.markdown(
        """
        <style>
        .pb-commercial-strip {background:#fff;border:1px solid #ddd4c7;border-radius:12px;
          padding:.75rem .9rem;margin:-.25rem 0 .8rem 0;box-shadow:0 2px 10px rgba(0,0,0,.035)}
        .pb-commercial-steps {display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:.4rem;margin-top:.55rem}
        .pb-commercial-step {border-radius:8px;padding:.42rem .55rem;background:#f4f1eb;border:1px solid #e5dfd4;
          font-size:.78rem;line-height:1.05rem}
        .pb-commercial-step.complete {border-left:4px solid #2E8B57}
        .pb-commercial-step.current {border-left:4px solid #276FBF;background:#eef5fd}
        .pb-commercial-step.review {border-left:4px solid #D7A21B;background:#fff8df}
        .pb-commercial-step.upcoming {color:#746e65}
        .pb-commercial-step b {display:block;color:#171717}
        @media (max-width:900px){.pb-commercial-steps{grid-template-columns:1fr 1fr}.pb-commercial-step:last-child{grid-column:1/-1}}
        </style>
        """,
        unsafe_allow_html=True,
    )

    job_no = str(workspace.get("job_no") or "").strip()
    job_name = str(workspace.get("job_name") or "Untitled project").strip()
    drawing_issue = str(workspace.get("drawing_issue") or "").strip()
    issue_text = f" · Issue {drawing_issue}" if drawing_issue else ""

    steps_html = "".join(
        f"<div class='pb-commercial-step {step['state']}'><b>{step['label']}</b>{step['detail']}</div>"
        for step in workflow_step_states(status)
    )
    st.markdown(
        f"<div class='pb-commercial-strip'><strong>{job_no + ' · ' if job_no else ''}{job_name}</strong>"
        f"<span style='float:right;font-size:.82rem'>{status.overall_state}{issue_text}</span>"
        f"<div class='pb-commercial-steps'>{steps_html}</div></div>",
        unsafe_allow_html=True,
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Drawings", status.pages_total, f"{status.pages_selected} selected")
    c2.metric("Take-off", status.takeoff_total, f"{status.takeoff_ready} reviewed")
    c3.metric(
        "Review signals",
        status.review_total,
        "needs attention" if status.review_total else "none detected",
    )
    c4.metric("3D Model", "Saved snapshot" if status.canonical_model_saved else "Not saved")
    return status


def apply(app: Any) -> None:
    """Install the read-only shell through the existing shared ``hero`` hook."""
    if getattr(app, "_commercial_workspace_v160_installed", False):
        return
    if not hasattr(app, "hero"):
        return

    original_hero = app.hero

    def commercial_hero(workspace=None, *args, **kwargs):
        result = original_hero(workspace, *args, **kwargs)
        if isinstance(workspace, dict) and workspace.get("id") is not None:
            render_commercial_workspace_shell(app, workspace)
        return result

    app.hero = commercial_hero
    app._commercial_workspace_v160_installed = True
