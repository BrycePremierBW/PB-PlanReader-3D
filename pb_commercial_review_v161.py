"""PlanReader Commercial Review & QA Workspace (Phase 6B Correction Pass).

Version: 1.6.1
Provides normalized, read-only review signal derivation from authoritative
source data (take-off rows, register items, scale gate issues).

Review signals are DERIVED FROM UNDERLYING SOURCE DATA ONLY.
Zero parallel truth database or arbitrary bulk silencing allowed.
"""

from __future__ import annotations

import html
import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

MODULE_VERSION = "1.6.1"

REQUIRED_FAMILIES = ("takeoff", "register", "scale")
OPTIONAL_FAMILIES = ("model",)


def _safe_str(val: Any) -> str:
    """Safely convert any value (dict, list, None, int, float, str) to string without throwing."""
    if val is None:
        return ""
    if isinstance(val, (dict, list, tuple)):
        try:
            return json.dumps(val)
        except Exception:
            return str(val)
    return str(val)


def _safe_int(val: Any) -> Optional[int]:
    """Parse integer safely without treating bools or floats as invalid int."""
    if val is None or isinstance(val, bool):
        return None
    try:
        ival = int(val)
        return ival
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class CommercialReviewSignal:
    """Normalized, immutable review signal derived from authoritative source data."""
    signal_id: str
    workspace_id: int
    source_family: str          # "takeoff", "register", "scale", "model"
    source_type: str            # "takeoff_row", "register_item", "page_scale", "model_setting"
    source_id: str              # Authoritative source row/item/page ID
    category: str               # "Measurement", "Scale & calibration", "Scope / inclusion", "Clarification", "Drawing evidence", "3D model"
    severity: str               # "BLOCKER", "REVIEW", "INFORMATION"
    title: str
    summary: str
    reasons: Tuple[str, ...]
    status: str                 # Original source status string
    page_id: Optional[int] = None
    document_id: Optional[int] = None
    takeoff_row_id: Optional[int] = None
    register_item_id: Optional[int] = None
    wall_ref: Optional[str] = None
    opening_ref: Optional[str] = None
    drawing_reference: Optional[str] = None
    drawing_title: Optional[str] = None
    location: Optional[str] = None
    element: Optional[str] = None
    unit: Optional[str] = None
    quantity: Optional[float] = None
    quantity_status: Optional[str] = None
    confidence: Optional[str] = None
    inclusion_status: Optional[str] = None
    recommended_action: Optional[str] = None
    navigation_target: Optional[str] = None
    navigation_payload: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return clean JSON-serializable dictionary representation."""
        res = asdict(self)
        res["reasons"] = list(self.reasons)
        return res


@dataclass
class CommercialReviewResult:
    """Container for derived commercial review signals and source availability."""
    workspace_id: int
    signals: List[CommercialReviewSignal] = field(default_factory=list)
    source_coverage: Dict[str, str] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    @property
    def signal_count(self) -> int:
        return len(self.signals)

    @property
    def blocker_count(self) -> int:
        return sum(1 for s in self.signals if s.severity == "BLOCKER")

    @property
    def review_count(self) -> int:
        return sum(1 for s in self.signals if s.severity == "REVIEW")

    @property
    def info_count(self) -> int:
        return sum(1 for s in self.signals if s.severity == "INFORMATION")

    @property
    def required_coverage_complete(self) -> bool:
        """Required coverage is complete if workspace_id > 0 and all REQUIRED_FAMILIES are AVAILABLE."""
        if not self.workspace_id or self.workspace_id <= 0:
            return False
        for fam in REQUIRED_FAMILIES:
            status = self.source_coverage.get(fam, "UNAVAILABLE")
            if status != "AVAILABLE":
                return False
        return True

    @property
    def is_complete(self) -> bool:
        """Alias for required_coverage_complete."""
        return self.required_coverage_complete


def _safe_float(val: Any) -> Optional[float]:
    """Parse numeric quantity safely without throwing or treating bools as float."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        fval = float(val)
        return fval if math.isfinite(fval) else None
    s = _safe_str(val).strip()
    if not s:
        return None
    try:
        fval = float(s)
        return fval if math.isfinite(fval) else None
    except (ValueError, TypeError):
        return None


def _normalize_rows(raw: Any) -> List[Dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [dict(r) for r in raw if isinstance(r, dict)]
    if hasattr(raw, "to_dict"):
        return [dict(r) for r in raw.to_dict("records")]
    return []


def _query(app: Any, sql: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
    """Execute workspace SQL query using available DB helper or FakeApp attributes."""
    if hasattr(app, "lquery"):
        return _normalize_rows(app.lquery(sql, params))
    if hasattr(app, "ldf"):
        return _normalize_rows(app.ldf(sql, params))
    if hasattr(app, "execute") or hasattr(app, "cursor"):
        # Handle raw sqlite3 Connection — allow query exceptions to propagate so collector marks family UNAVAILABLE
        conn = app
        cur = conn.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]
    if "documents" in sql:
        return _normalize_rows(getattr(app, "documents", []))
    if "pages" in sql:
        return _normalize_rows(getattr(app, "pages", []))
    if "takeoff_rows" in sql:
        return _normalize_rows(getattr(app, "takeoff", []))
    if "register_items" in sql:
        return _normalize_rows(getattr(app, "registers", []))
    if "workspace_settings" in sql:
        return _normalize_rows(getattr(app, "workspace_settings", []))
    return []



# -----------------------------------------------------------------------------
# Source Collectors
# -----------------------------------------------------------------------------

def collect_takeoff_review_signals(app: Any, workspace_id: int) -> List[CommercialReviewSignal]:
    """Derive deduplicated review signals from takeoff_rows table with per-row quarantine."""
    signals: List[CommercialReviewSignal] = []
    rows = _query(
        app,
        "SELECT * FROM takeoff_rows WHERE workspace_id=? ORDER BY id",
        (int(workspace_id),),
    )

    for row in rows:
        raw_id = row.get("id")
        parsed_id = _safe_int(raw_id)
        if parsed_id is None or parsed_id <= 0:
            # Per-row quarantine for missing/non-numeric ID: skip item safely without killing entire family
            continue

        row_id_str = str(parsed_id)
        qty_status = _safe_str(row.get("quantity_status")).strip()
        qty_raw = row.get("quantity")
        parsed_qty = _safe_float(qty_raw)
        conf = _safe_str(row.get("confidence")).strip().lower()
        incl = _safe_str(row.get("inclusion_status")).strip().lower()
        
        qty_str = _safe_str(qty_raw).strip() if qty_raw is not None else ""
        is_nonfinite = False
        if qty_raw is not None and not isinstance(qty_raw, bool):
            if isinstance(qty_raw, float) and not math.isfinite(qty_raw):
                is_nonfinite = True
            elif qty_str.lower() in ("nan", "inf", "-inf", "+inf"):
                is_nonfinite = True

        reasons: List[str] = []
        primary_severity = "REVIEW"
        primary_category = "Measurement"

        # Rule 1: Quantity status signals
        if not qty_status or qty_status.lower() in ("to measure", "provisional measured", "provisional"):
            if qty_status.lower() == "to measure":
                reasons.append("Quantity status is marked 'To measure'")
                primary_severity = "BLOCKER"
            elif "provisional" in qty_status.lower():
                reasons.append(f"Quantity status is provisional ('{qty_status}')")
            else:
                reasons.append("Quantity status is unrecorded or missing")

        # Rule 2: Numeric quantity check for Measured or Allowance rows
        if qty_status.lower() in ("measured", "allowance"):
            if parsed_qty is None:
                if is_nonfinite:
                    reasons.append(f"Measured quantity is non-finite or invalid ('{qty_str}')")
                    primary_severity = "BLOCKER"
                else:
                    reasons.append("Measured item lacks a valid numeric quantity")
                    primary_severity = "BLOCKER"
            elif parsed_qty == 0.0:
                # Valid confirmed zero -> NO measurement error unless provisional/confidence flag exists
                pass

        # Rule 3: Confidence review semantics
        for kw in ("review", "check", "provisional", "derived", "low"):
            if kw in conf:
                reasons.append(f"Confidence requires review ('{conf}')")
                break

        # Rule 4: Inclusion status semantics
        if incl in ("clarification", "provisional"):
            reasons.append(f"Scope inclusion status requires review ('{incl}')")
            if primary_category == "Measurement" and not any("Quantity" in r or "Measured" in r for r in reasons):
                primary_category = "Scope / inclusion"

        # Excluded / Not applicable filter
        if qty_status.lower() in ("excluded", "not applicable", "n/a"):
            if not any("Confidence" in r or "Scope inclusion" in r for r in reasons):
                continue

        if not reasons:
            continue

        reasons_sorted = tuple(sorted(reasons))
        signal_id = f"review:{workspace_id}:takeoff:{row_id_str}:{primary_category.replace(' ', '_').replace('/', '_')}"
        element_name = _safe_str(row.get("element") or row.get("description") or row.get("item_name") or row.get("item") or f"Take-off Row #{row_id_str}").strip()
        location_name = _safe_str(row.get("location") or row.get("zone") or row.get("level")).strip() or None
        dwg_ref = _safe_str(row.get("source_reference") or row.get("drawing_reference") or row.get("drawing_ref")).strip() or None

        signals.append(
            CommercialReviewSignal(
                signal_id=signal_id,
                workspace_id=int(workspace_id),
                source_family="takeoff",
                source_type="takeoff_row",
                source_id=row_id_str,
                category=primary_category,
                severity=primary_severity,
                title=f"{element_name}",
                summary=reasons_sorted[0],
                reasons=reasons_sorted,
                status=qty_status or "Unrecorded",
                page_id=_safe_int(row.get("page_id")),
                document_id=_safe_int(row.get("document_id")),
                takeoff_row_id=parsed_id,
                drawing_reference=dwg_ref,
                location=location_name,
                element=element_name,
                unit=_safe_str(row.get("unit")).strip() or None,
                quantity=parsed_qty,
                quantity_status=qty_status or None,
                confidence=conf or None,
                inclusion_status=incl or None,
                recommended_action="Open take-off row to confirm quantity, status, and measurement evidence.",
                navigation_target="takeoff",
                navigation_payload={"workspace_id": int(workspace_id), "takeoff_row_id": parsed_id},
                metadata={"row_id": parsed_id},
            )
        )

    return signals


def collect_register_review_signals(app: Any, workspace_id: int) -> List[CommercialReviewSignal]:
    """Derive review signals from register_items table with explicit status allowlist."""
    signals: List[CommercialReviewSignal] = []
    items = _query(
        app,
        "SELECT * FROM register_items WHERE workspace_id=? ORDER BY id",
        (int(workspace_id),),
    )

    RESOLVED_STATUSES = {"accepted", "closed", "resolved", "approved"}
    UNRESOLVED_ALLOWLIST = {"open", "to review", "review required", "pending", "unresolved"}

    for item in items:
        raw_id = item.get("id")
        parsed_id = _safe_int(raw_id)
        if parsed_id is None or parsed_id <= 0:
            # Per-row quarantine for missing/non-numeric ID: skip item safely
            continue

        item_id_str = str(parsed_id)
        status_val = _safe_str(item.get("status") or item.get("review_state")).strip()
        status_lower = status_val.lower()

        if status_lower in RESOLVED_STATUSES:
            continue

        # All unresolved register items have REVIEW severity (Priority does NOT create BLOCKER authority)
        severity = "REVIEW"
        reasons: List[str] = []

        if status_lower in UNRESOLVED_ALLOWLIST:
            reasons.append(f"Register item remains in '{status_val or 'Open'}' state")
        elif not status_val:
            reasons.append("Register item status is blank/unrecorded")
        else:
            # Unknown/malformed status is explicitly surfaced with REVIEW severity
            reasons.append(f"Register item has unrecognised status ('{status_val}')")

        reg_name = _safe_str(item.get("register_name")).strip()
        reg_title = _safe_str(item.get("title")).strip()
        if reg_name and reg_title and reg_name.lower() != reg_title.lower():
            title_text = f"{reg_name} — {reg_title}"
        else:
            title_text = reg_title or reg_name or f"Clarification Item #{item_id_str}"

        detail_text = _safe_str(item.get("detail") or item.get("notes")).strip()
        dwg_ref = _safe_str(item.get("source_reference") or item.get("drawing_ref") or item.get("drawing_reference")).strip() or None
        priority = _safe_str(item.get("priority")).strip().upper()

        signals.append(
            CommercialReviewSignal(
                signal_id=f"review:{workspace_id}:register:{item_id_str}:Clarification",
                workspace_id=int(workspace_id),
                source_family="register",
                source_type="register_item",
                source_id=item_id_str,
                category="Clarification",
                severity=severity,
                title=title_text,
                summary=detail_text[:120] if detail_text else reasons[0],
                reasons=tuple(reasons),
                status=status_val or "Unrecorded",
                register_item_id=parsed_id,
                drawing_reference=dwg_ref,
                recommended_action="Open clarification register to review and resolve project query.",
                navigation_target="register",
                navigation_payload={"workspace_id": int(workspace_id), "register_item_id": parsed_id},
                metadata={"register_id": parsed_id, "priority": priority or "NORMAL"},
            )
        )

    return signals


def collect_scale_review_signals(app: Any, workspace_id: int) -> Tuple[List[CommercialReviewSignal], str]:
    """Derive review signals strictly from scale gate issues."""
    signals: List[CommercialReviewSignal] = []

    if hasattr(app, "scale_gate_issues") and callable(getattr(app, "scale_gate_issues")):
        try:
            issues = app.scale_gate_issues(int(workspace_id))
            if not isinstance(issues, list):
                return signals, "UNAVAILABLE"
        except Exception:
            return signals, "UNAVAILABLE"
    elif hasattr(app, "execute") or hasattr(app, "cursor") or app is None:
        try:
            from pb_planreader_3d_app import scale_gate_issues as _authoritative_scale_gate
            issues = _authoritative_scale_gate(int(workspace_id))
        except Exception:
            return signals, "NOT_SUPPORTED"
    else:
        return signals, "NOT_SUPPORTED"

    for issue in issues:



        if not isinstance(issue, dict):
            continue
        page_id_val = issue.get("page_id")
        page_id_str = str(page_id_val or issue.get("page_label") or "unknown")
        reason_text = _safe_str(issue.get("reason") or "Selected drawing page requires scale calibration").strip()
        label_text = _safe_str(issue.get("page_label") or f"Page #{page_id_str}").strip()

        signals.append(
            CommercialReviewSignal(
                signal_id=f"review:{workspace_id}:scale:page:{page_id_str}:Scale_calibration",
                workspace_id=int(workspace_id),
                source_family="scale",
                source_type="page_scale",
                source_id=page_id_str,
                category="Scale & calibration",
                severity="BLOCKER",
                title=f"Uncalibrated Scale — {label_text}",
                summary=reason_text,
                reasons=(reason_text,),
                status="Uncalibrated",
                page_id=_safe_int(page_id_val),
                drawing_reference=label_text,
                recommended_action="Open drawing in Plan Mapper to calibrate scale (px/m) before trusting quantities.",
                navigation_target="drawing",
                navigation_payload={"workspace_id": int(workspace_id), "page_id": _safe_int(page_id_val)},
                metadata={"page_label": label_text, "source": "scale_gate_issues"},
            )
        )

    return signals, "AVAILABLE"


def collect_model_review_signals(app: Any, workspace_id: int) -> Tuple[List[CommercialReviewSignal], str]:
    """3D Model review signals are NOT_SUPPORTED (no parallel staleness engine created)."""
    return [], "NOT_SUPPORTED"


# -----------------------------------------------------------------------------
# Main Signal Collector
# -----------------------------------------------------------------------------

def collect_commercial_review_signals(app: Any, workspace: Dict[str, Any]) -> CommercialReviewResult:
    """Collect and deterministically sort all normalized review signals for a workspace."""
    if not isinstance(workspace, dict):
        return CommercialReviewResult(workspace_id=0, source_coverage={fam: "UNAVAILABLE" for fam in REQUIRED_FAMILIES})

    workspace_id = _safe_int(workspace.get("id"))
    if not workspace_id or workspace_id <= 0:
        res = CommercialReviewResult(workspace_id=0)
        for fam in REQUIRED_FAMILIES:
            res.source_coverage[fam] = "UNAVAILABLE"
        for fam in OPTIONAL_FAMILIES:
            res.source_coverage[fam] = "NOT_SUPPORTED"
        return res

    result = CommercialReviewResult(workspace_id=workspace_id)

    # 1. Collect Take-off Signals (Required)
    try:
        to_signals = collect_takeoff_review_signals(app, workspace_id)
        result.signals.extend(to_signals)
        result.source_coverage["takeoff"] = "AVAILABLE"
    except Exception as exc:
        result.source_coverage["takeoff"] = "UNAVAILABLE"
        result.errors.append(f"Take-off collector error: {exc}")

    # 2. Collect Register Signals (Required)
    try:
        reg_signals = collect_register_review_signals(app, workspace_id)
        result.signals.extend(reg_signals)
        result.source_coverage["register"] = "AVAILABLE"
    except Exception as exc:
        result.source_coverage["register"] = "UNAVAILABLE"
        result.errors.append(f"Register collector error: {exc}")

    # 3. Collect Scale Signals (Required)
    try:
        scale_signals, scale_status = collect_scale_review_signals(app, workspace_id)
        result.signals.extend(scale_signals)
        result.source_coverage["scale"] = scale_status
    except Exception as exc:
        result.source_coverage["scale"] = "UNAVAILABLE"
        result.errors.append(f"Scale collector error: {exc}")

    # 4. Collect 3D Model Signals (Optional)
    try:
        model_signals, model_status = collect_model_review_signals(app, workspace_id)
        result.signals.extend(model_signals)
        result.source_coverage["model"] = model_status
    except Exception as exc:
        result.source_coverage["model"] = "NOT_SUPPORTED"

    # Deterministic Sort: Severity (BLOCKER -> REVIEW -> INFORMATION) -> Category -> Drawing -> Source ID -> Signal ID
    sev_rank = {"BLOCKER": 0, "REVIEW": 1, "INFORMATION": 2}
    result.signals.sort(
        key=lambda s: (
            sev_rank.get(s.severity, 99),
            s.category,
            s.drawing_reference or "",
            s.source_id,
            s.signal_id,
        )
    )

    return result


# -----------------------------------------------------------------------------
# Streamlit UI Rendering Component
# -----------------------------------------------------------------------------

def render_commercial_review_workspace(app: Any, workspace: Dict[str, Any]) -> None:
    """Render the central Phase 6B Commercial Review & QA workspace."""
    st = getattr(app, "st", None)
    if not st:
        return

    workspace_id = _safe_int(workspace.get("id")) if isinstance(workspace, dict) else None
    if not workspace_id or workspace_id <= 0:
        st.error("Invalid or unselected workspace. Select a valid workspace to view commercial review signals.")
        return

    job_no_raw = _safe_str(workspace.get("job_no")).strip()
    job_name_raw = _safe_str(workspace.get("job_name")).strip()
    drawing_issue_raw = _safe_str(workspace.get("drawing_issue")).strip()

    job_no = html.escape(job_no_raw) if job_no_raw else "Not recorded"
    job_name = html.escape(job_name_raw) if job_name_raw else "Not recorded"
    drawing_issue = html.escape(drawing_issue_raw) if drawing_issue_raw else "Not recorded"

    # Collect signals safely
    try:
        review_res = collect_commercial_review_signals(app, workspace)
    except Exception as exc:
        st.caption("Review status unavailable. Estimator tools remain available.")
        st.warning(f"Unable to derive commercial review signals: {html.escape(str(exc))}")
        return

    # Partial source failure alert
    if not review_res.required_coverage_complete:
        unavail = [src for src, status in review_res.source_coverage.items() if status == "UNAVAILABLE"]
        unavail_str = html.escape(", ".join(unavail)) if unavail else "Required sources"
        st.warning(f"⚠️ Review coverage incomplete — Sources unavailable: {unavail_str}. Estimator tools remain fully functional.")

    # Header Card
    st.markdown(
        f"""
        <div class="pb-card" style="padding:1rem 1.25rem; margin-bottom:1rem; border-left:4px solid #1E3A8A;">
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.5rem;">
                <div>
                    <h2 style="margin:0; font-size:1.4rem; color:#0F172A; font-weight:700;">
                        📋 Review &amp; QA Workspace
                    </h2>
                    <p style="margin:0.25rem 0 0 0; color:#475569; font-size:0.9rem;">
                        <strong>{job_no} — {job_name}</strong> &nbsp;·&nbsp; Issue: <span>{drawing_issue}</span>
                    </p>
                </div>
                <div style="display:flex; gap:0.5rem; flex-wrap:wrap;">
                    <span style="background:#EF4444; color:white; padding:0.25rem 0.6rem; border-radius:4px; font-weight:600; font-size:0.85rem;">
                        {review_res.blocker_count} BLOCKERS
                    </span>
                    <span style="background:#F59E0B; color:white; padding:0.25rem 0.6rem; border-radius:4px; font-weight:600; font-size:0.85rem;">
                        {review_res.review_count} REVIEWS
                    </span>
                    <span style="background:#3B82F6; color:white; padding:0.25rem 0.6rem; border-radius:4px; font-weight:600; font-size:0.85rem;">
                        {review_res.info_count} INFO
                    </span>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption("Review items are derived directly from authoritative source data. Fix items at source to clear signals.")

    # Summary Metrics Row
    m1, m2, m3, m4, m5 = st.columns(5)
    meas_count = sum(1 for s in review_res.signals if s.category == "Measurement")
    scale_count = sum(1 for s in review_res.signals if s.category == "Scale & calibration")
    scope_count = sum(1 for s in review_res.signals if s.category in ("Scope / inclusion", "Clarification"))
    model_count = sum(1 for s in review_res.signals if s.category == "3D model")

    m1.metric("Outstanding Signals", str(review_res.signal_count))
    m2.metric("Measurement", str(meas_count))
    m3.metric("Scale Gate", str(scale_count))
    m4.metric("Scope & Clarify", str(scope_count))
    m5.metric("3D Model", str(model_count))

    if review_res.signal_count == 0:
        if review_res.required_coverage_complete:
            st.success("✅ No current review signals detected from the available sources.")
        else:
            st.warning("⚠️ Review coverage is incomplete. No review signals were detected from the sources currently available.")
        return

    # Filter Controls (Severity, Category, Source Family, Drawing / Page, Search text)
    st.markdown("### Search & Filters")
    f1, f2, f3, f4, f5 = st.columns([1.2, 1.2, 1.2, 1.4, 2])
    
    severities = ["All", "BLOCKER", "REVIEW", "INFORMATION"]
    categories = ["All"] + sorted(list({s.category for s in review_res.signals}))
    families = ["All"] + sorted(list({s.source_family for s in review_res.signals}))
    drawings = ["All"] + sorted(list({s.drawing_reference for s in review_res.signals if s.drawing_reference}))

    selected_sev = f1.selectbox("Severity", severities, key="_pb_rev_sev")
    selected_cat = f2.selectbox("Category", categories, key="_pb_rev_cat")
    selected_fam = f3.selectbox("Source Family", families, key="_pb_rev_fam")
    selected_dwg = f4.selectbox("Drawing / Page", drawings, key="_pb_rev_dwg")
    search_query = f5.text_input("Search text", placeholder="Filter by element, location, drawing...", key="_pb_rev_search").strip().lower()

    # Apply Filters
    filtered_signals = review_res.signals
    if selected_sev != "All":
        filtered_signals = [s for s in filtered_signals if s.severity == selected_sev]
    if selected_cat != "All":
        filtered_signals = [s for s in filtered_signals if s.category == selected_cat]
    if selected_fam != "All":
        filtered_signals = [s for s in filtered_signals if s.source_family == selected_fam]
    if selected_dwg != "All":
        filtered_signals = [s for s in filtered_signals if s.drawing_reference == selected_dwg]
    if search_query:
        filtered_signals = [
            s for s in filtered_signals
            if search_query in _safe_str(s.title).lower()
            or search_query in _safe_str(s.summary).lower()
            or search_query in _safe_str(s.element).lower()
            or search_query in _safe_str(s.location).lower()
            or search_query in _safe_str(s.drawing_reference).lower()
            or any(search_query in _safe_str(r).lower() for r in s.reasons)
        ]

    st.caption(f"Showing {len(filtered_signals)} of {review_res.signal_count} review signals")

    if not filtered_signals:
        st.info("No review signals match these filters.")
        return

    # Signal Cards Listing
    st.markdown("---")
    for idx, sig in enumerate(filtered_signals):
        badge_bg = "#EF4444" if sig.severity == "BLOCKER" else ("#F59E0B" if sig.severity == "REVIEW" else "#3B82F6")
        title_esc = html.escape(_safe_str(sig.title))
        summary_esc = html.escape(_safe_str(sig.summary))
        cat_esc = html.escape(_safe_str(sig.category))
        elem_esc = html.escape(_safe_str(sig.element) or "—")
        loc_esc = html.escape(_safe_str(sig.location) or "—")
        dwg_esc = html.escape(_safe_str(sig.drawing_reference) or "—")
        status_esc = html.escape(_safe_str(sig.status) or "Unspecified")

        st.markdown(
            f"""
            <div class="pb-card" style="margin-bottom:0.75rem; padding:1rem; border-left:4px solid {badge_bg};">
                <div style="display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:0.5rem;">
                    <div>
                        <span style="background:{badge_bg}; color:white; padding:0.15rem 0.45rem; border-radius:3px; font-weight:700; font-size:0.75rem;">
                            {sig.severity}
                        </span>
                        <span style="color:#64748B; font-weight:600; font-size:0.85rem; margin-left:0.4rem;">
                            {cat_esc}
                        </span>
                        <h4 style="margin:0.25rem 0 0.15rem 0; color:#0F172A; font-size:1.05rem;">{title_esc}</h4>
                        <p style="margin:0; color:#334155; font-size:0.9rem;">{summary_esc}</p>
                    </div>
                    <div style="font-size:0.82rem; color:#475569; text-align:right;">
                        <div><strong>Status:</strong> {status_esc}</div>
                        <div><strong>Drawing:</strong> {dwg_esc}</div>
                    </div>
                </div>
                <div style="margin-top:0.5rem; font-size:0.83rem; color:#475569; display:flex; gap:1.25rem; flex-wrap:wrap;">
                    <div><strong>Element:</strong> {elem_esc}</div>
                    <div><strong>Location:</strong> {loc_esc}</div>
                    <div><strong>Source:</strong> {html.escape(_safe_str(sig.source_family)).upper()} #{html.escape(_safe_str(sig.source_id))}</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        col1, col2 = st.columns([4, 1])
        with col1:
            with st.expander(f"Why am I seeing this? ({len(sig.reasons)} reasons)", expanded=False):
                for reason in sig.reasons:
                    st.markdown(f"- {html.escape(_safe_str(reason))}")
                st.caption(f"Deterministic Signal ID: `{html.escape(_safe_str(sig.signal_id))}`")
                if sig.metadata and isinstance(sig.metadata, dict):
                    bounded_meta = {html.escape(_safe_str(k)): html.escape(_safe_str(v)) for k, v in sig.metadata.items()}
                    st.json(bounded_meta)

        with col2:
            if sig.navigation_target:
                btn_label = "Open source"
                if sig.navigation_target == "takeoff":
                    btn_label = "Open take-off"
                elif sig.navigation_target == "drawing":
                    btn_label = "Open drawing"
                elif sig.navigation_target == "register":
                    btn_label = "Open register"
                elif sig.navigation_target == "model":
                    btn_label = "Open 3D model"

                if st.button(btn_label, key=f"nav_{sig.signal_id}_{idx}", use_container_width=True):
                    st.session_state["_pb_nav_target"] = sig.navigation_target
                    st.session_state["_pb_nav_payload"] = sig.navigation_payload
                    st.rerun()


# Alias for programmatic consumers
derive_commercial_review = collect_commercial_review_signals
SEVERITY_BLOCKER = "BLOCKER"
SEVERITY_REVIEW = "REVIEW"
SEVERITY_INFO = "INFORMATION"

