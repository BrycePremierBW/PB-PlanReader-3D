"""PlanReader Commercial Review & QA Workspace (Phase 6B).

Version: 1.6.1
Provides normalized, read-only review signal derivation from authoritative
source data (take-off rows, register items, scale gate issues, 3D model settings).

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

# -----------------------------------------------------------------------------
# Data Models
# -----------------------------------------------------------------------------

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
    def is_complete(self) -> bool:
        return all(status == "AVAILABLE" for status in self.source_coverage.values())


# -----------------------------------------------------------------------------
# Helper Utilities
# -----------------------------------------------------------------------------

def _safe_float(val: Any) -> Optional[float]:
    """Parse numeric quantity safely without throwing or treating bools as float."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        fval = float(val)
        return fval if math.isfinite(fval) else None
    s = str(val).strip()
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
    """Derive deduplicated review signals from takeoff_rows table."""
    signals: List[CommercialReviewSignal] = []
    rows = _query(
        app,
        "SELECT * FROM takeoff_rows WHERE workspace_id=? ORDER BY id",
        (int(workspace_id),),
    )

    for row in rows:
        row_id = str(row.get("id"))
        qty_status = str(row.get("quantity_status") or "").strip()
        qty_raw = row.get("quantity")
        parsed_qty = _safe_float(qty_raw)
        conf = str(row.get("confidence") or "").strip().lower()
        incl = str(row.get("inclusion_status") or "").strip().lower()
        
        # Check non-finite or invalid string numbers when quantity was supplied
        qty_str = str(qty_raw).strip() if qty_raw is not None else ""
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
            # Excluded rows only produce signals if they carry explicit clarification or confidence flags
            if not any("Confidence" in r or "Scope inclusion" in r for r in reasons):
                continue

        if not reasons:
            continue

        # Sort reasons deterministically
        reasons_sorted = tuple(sorted(reasons))

        signal_id = f"review:{workspace_id}:takeoff:{row_id}:{primary_category.replace(' ', '_').replace('/', '_')}"
        element_name = str(row.get("element") or row.get("description") or row.get("item_name") or f"Take-off Row #{row_id}").strip()
        location_name = str(row.get("location") or row.get("zone") or row.get("level") or "").strip() or None
        dwg_ref = str(row.get("drawing_reference") or row.get("drawing_ref") or "").strip() or None

        signals.append(
            CommercialReviewSignal(
                signal_id=signal_id,
                workspace_id=int(workspace_id),
                source_family="takeoff",
                source_type="takeoff_row",
                source_id=row_id,
                category=primary_category,
                severity=primary_severity,
                title=f"{element_name}",
                summary=reasons_sorted[0],
                reasons=reasons_sorted,
                status=qty_status or "Unrecorded",
                page_id=int(row["page_id"]) if row.get("page_id") else None,
                document_id=int(row["document_id"]) if row.get("document_id") else None,
                takeoff_row_id=int(row_id),
                drawing_reference=dwg_ref,
                location=location_name,
                element=element_name,
                unit=str(row.get("unit") or "").strip() or None,
                quantity=parsed_qty,
                quantity_status=qty_status or None,
                confidence=conf or None,
                inclusion_status=incl or None,
                recommended_action="Open take-off row to confirm quantity, status, and measurement evidence.",
                navigation_target="takeoff",
                navigation_payload={"workspace_id": int(workspace_id), "takeoff_row_id": int(row_id)},
                metadata={"row_id": int(row_id), "raw_quantity": qty_raw},
            )
        )

    return signals


def collect_register_review_signals(app: Any, workspace_id: int) -> List[CommercialReviewSignal]:
    """Derive review signals from register_items table."""
    signals: List[CommercialReviewSignal] = []
    items = _query(
        app,
        "SELECT * FROM register_items WHERE workspace_id=? ORDER BY id",
        (int(workspace_id),),
    )

    for item in items:
        item_id = str(item.get("id"))
        status_val = str(item.get("status") or item.get("review_state") or "").strip()
        status_lower = status_val.lower()

        # Unresolved states: "open", "to review", "review required", "pending", "unresolved"
        if status_lower in ("accepted", "closed", "resolved", "approved"):
            continue

        reasons = [f"Register item remains in '{status_val or 'Open'}' state"]
        priority = str(item.get("priority") or "").strip().upper()
        severity = "BLOCKER" if priority in ("HIGH", "URGENT", "BLOCKER") else "REVIEW"

        reg_name = str(item.get("register_name") or "").strip()
        reg_title = str(item.get("title") or "").strip()
        if reg_name and reg_title and reg_name.lower() != reg_title.lower():
            title_text = f"{reg_name} — {reg_title}"
        else:
            title_text = reg_title or reg_name or f"Clarification Item #{item_id}"

        detail_text = str(item.get("detail") or item.get("notes") or "").strip()
        dwg_ref = str(item.get("drawing_ref") or item.get("drawing_reference") or "").strip() or None

        signals.append(
            CommercialReviewSignal(
                signal_id=f"review:{workspace_id}:register:{item_id}:Clarification",
                workspace_id=int(workspace_id),
                source_family="register",
                source_type="register_item",
                source_id=item_id,
                category="Clarification",
                severity=severity,
                title=title_text,
                summary=detail_text[:120] if detail_text else reasons[0],
                reasons=tuple(reasons),
                status=status_val or "Open",
                register_item_id=int(item_id),
                drawing_reference=dwg_ref,
                recommended_action="Open clarification register to review and resolve project query.",
                navigation_target="register",
                navigation_payload={"workspace_id": int(workspace_id), "register_item_id": int(item_id)},
                metadata={"register_id": int(item_id), "priority": priority},
            )
        )

    return signals


def collect_scale_review_signals(app: Any, workspace_id: int) -> List[CommercialReviewSignal]:
    """Derive review signals from scale_gate_issues(workspace_id)."""
    signals: List[CommercialReviewSignal] = []
    
    # Safely call scale_gate_issues helper
    issues: List[Dict[str, Any]] = []
    if hasattr(app, "scale_gate_issues"):
        try:
            issues = app.scale_gate_issues(int(workspace_id))
        except Exception:
            raise
    else:
        # Fallback query if app scale_gate_issues is not mounted
        pages = _query(
            app,
            "SELECT p.id, p.page_no, p.page_label, p.document_id, d.file_name FROM pages p "
            "JOIN documents d ON d.id=p.document_id "
            "WHERE p.workspace_id=? AND (p.px_per_m IS NULL OR p.px_per_m <= 0) "
            "AND p.id IN (SELECT DISTINCT page_id FROM mapped_zones WHERE workspace_id=?)",
            (int(workspace_id), int(workspace_id)),
        )
        for pg in pages:
            issues.append({
                "page_id": int(pg["id"]),
                "page_label": str(pg.get("page_label") or pg.get("page_no") or ""),
                "reason": "Drawing page feeds measurement but scale calibration is not confirmed",
            })

    for issue in issues:
        page_id_val = issue.get("page_id")
        page_id_str = str(page_id_val or issue.get("page_label") or "unknown")
        reason_text = str(issue.get("reason") or "Selected drawing page requires scale calibration").strip()
        label_text = str(issue.get("page_label") or f"Page #{page_id_str}").strip()

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
                page_id=int(page_id_val) if page_id_val and str(page_id_val).isdigit() else None,
                drawing_reference=label_text,
                recommended_action="Open drawing in Plan Mapper to calibrate scale (px/m) before trusting quantities.",
                navigation_target="drawing",
                navigation_payload={"workspace_id": int(workspace_id), "page_id": page_id_val},
                metadata={"issue_raw": issue},
            )
        )

    return signals


def collect_model_review_signals(app: Any, workspace_id: int) -> List[CommercialReviewSignal]:
    """Derive optional 3D review signals from persisted canonical_3d_model_v1 setting."""
    signals: List[CommercialReviewSignal] = []
    raw_val = None
    if hasattr(app, "workspace_setting"):
        try:
            raw_val = app.workspace_setting(int(workspace_id), "canonical_3d_model_v1", None)
        except Exception:
            raw_val = None
    else:
        try:
            rows = _query(
                app,
                "SELECT value FROM workspace_settings WHERE workspace_id=? AND key='canonical_3d_model_v1'",
                (int(workspace_id),),
            )
            if rows:
                raw_val = rows[0].get("value")
        except Exception:
            raw_val = None

    if raw_val is None:
        return signals

    try:
        data = json.loads(raw_val) if isinstance(raw_val, str) else (raw_val if isinstance(raw_val, dict) else {})
    except Exception:
        # Malformed model payload is handled as not-saved by Phase 6A workflow step
        return signals

    if not isinstance(data, dict):
        return signals

    is_stale = bool(data.get("is_stale") or data.get("stale"))
    diagnostics = data.get("review_diagnostics") or []

    if is_stale or diagnostics:
        reasons = tuple(diagnostics) if diagnostics else ("Saved 3D model source evidence has changed",)
        signals.append(
            CommercialReviewSignal(
                signal_id=f"review:{workspace_id}:model:setting:canonical_3d_model_v1:3D_model",
                workspace_id=int(workspace_id),
                source_family="model",
                source_type="model_setting",
                source_id="canonical_3d_model_v1",
                category="3D model",
                severity="INFORMATION",
                title="3D Model Diagnostics Available",
                summary=reasons[0],
                reasons=reasons,
                status="Stale" if is_stale else "Diagnostics",
                recommended_action="Open 3D Model viewer to refresh canonical building model.",
                navigation_target="model",
                navigation_payload={"workspace_id": int(workspace_id)},
                metadata={"key": "canonical_3d_model_v1"},
            )
        )

    return signals


# -----------------------------------------------------------------------------
# Main Signal Collector
# -----------------------------------------------------------------------------

def collect_commercial_review_signals(app: Any, workspace: Dict[str, Any]) -> CommercialReviewResult:
    """Collect and deterministically sort all normalized review signals for a workspace."""
    workspace_id = int(workspace.get("id") or 0)
    result = CommercialReviewResult(workspace_id=workspace_id)
    if not workspace_id:
        return result

    # 1. Collect Take-off Signals
    try:
        to_signals = collect_takeoff_review_signals(app, workspace_id)
        result.signals.extend(to_signals)
        result.source_coverage["takeoff"] = "AVAILABLE"
    except Exception as exc:
        result.source_coverage["takeoff"] = "UNAVAILABLE"
        result.errors.append(f"Take-off collector error: {exc}")

    # 2. Collect Register Signals
    try:
        reg_signals = collect_register_review_signals(app, workspace_id)
        result.signals.extend(reg_signals)
        result.source_coverage["register"] = "AVAILABLE"
    except Exception as exc:
        result.source_coverage["register"] = "UNAVAILABLE"
        result.errors.append(f"Register collector error: {exc}")

    # 3. Collect Scale Signals
    try:
        scale_signals = collect_scale_review_signals(app, workspace_id)
        result.signals.extend(scale_signals)
        result.source_coverage["scale"] = "AVAILABLE"
    except Exception as exc:
        result.source_coverage["scale"] = "UNAVAILABLE"
        result.errors.append(f"Scale collector error: {exc}")

    # 4. Collect 3D Model Signals
    try:
        model_signals = collect_model_review_signals(app, workspace_id)
        result.signals.extend(model_signals)
        result.source_coverage["model"] = "AVAILABLE"
    except Exception as exc:
        result.source_coverage["model"] = "UNAVAILABLE"
        result.errors.append(f"3D Model collector error: {exc}")

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

    workspace_id = int(workspace.get("id") or 0)
    job_no = html.escape(str(workspace.get("job_no") or f"WS-{workspace_id}"))
    job_name = html.escape(str(workspace.get("job_name") or "Unnamed Project"))
    drawing_issue = html.escape(str(workspace.get("drawing_issue") or "Current Issue"))

    # Collect signals safely
    try:
        review_res = collect_commercial_review_signals(app, workspace)
    except Exception as exc:
        st.caption("Review status unavailable. Estimator tools remain available.")
        st.warning(f"Unable to derive commercial review signals: {exc}")
        return

    # Partial source failure alert
    if not review_res.is_complete:
        unavail = [src for src, status in review_res.source_coverage.items() if status != "AVAILABLE"]
        st.warning(f"⚠️ Review coverage incomplete — Sources unavailable: {', '.join(unavail)}. Estimator tools remain fully functional.")

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
        st.success("✅ No current review signals detected from the available sources.")
        return

    # Filter Controls
    st.markdown("### Search & Filters")
    f1, f2, f3, f4 = st.columns([1.5, 1.5, 1.5, 2])
    
    severities = ["All", "BLOCKER", "REVIEW", "INFORMATION"]
    categories = ["All"] + sorted(list({s.category for s in review_res.signals}))
    families = ["All"] + sorted(list({s.source_family for s in review_res.signals}))

    selected_sev = f1.selectbox("Severity", severities, key="_pb_rev_sev")
    selected_cat = f2.selectbox("Category", categories, key="_pb_rev_cat")
    selected_fam = f3.selectbox("Source Family", families, key="_pb_rev_fam")
    search_query = f4.text_input("Search text", placeholder="Filter by element, location, drawing...", key="_pb_rev_search").strip().lower()

    # Apply Filters
    filtered_signals = review_res.signals
    if selected_sev != "All":
        filtered_signals = [s for s in filtered_signals if s.severity == selected_sev]
    if selected_cat != "All":
        filtered_signals = [s for s in filtered_signals if s.category == selected_cat]
    if selected_fam != "All":
        filtered_signals = [s for s in filtered_signals if s.source_family == selected_fam]
    if search_query:
        filtered_signals = [
            s for s in filtered_signals
            if search_query in (s.title or "").lower()
            or search_query in (s.summary or "").lower()
            or search_query in (s.element or "").lower()
            or search_query in (s.location or "").lower()
            or search_query in (s.drawing_reference or "").lower()
            or any(search_query in r.lower() for r in s.reasons)
        ]

    st.caption(f"Showing {len(filtered_signals)} of {review_res.signal_count} review signals")

    if not filtered_signals:
        st.info("No review signals match these filters.")
        return

    # Signal Cards Listing
    st.markdown("---")
    for idx, sig in enumerate(filtered_signals):
        badge_bg = "#EF4444" if sig.severity == "BLOCKER" else ("#F59E0B" if sig.severity == "REVIEW" else "#3B82F6")
        title_esc = html.escape(sig.title)
        summary_esc = html.escape(sig.summary)
        cat_esc = html.escape(sig.category)
        elem_esc = html.escape(sig.element or "—")
        loc_esc = html.escape(sig.location or "—")
        dwg_esc = html.escape(sig.drawing_reference or "—")
        status_esc = html.escape(sig.status or "Unspecified")

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
                    <div><strong>Source:</strong> {sig.source_family.upper()} #{html.escape(sig.source_id)}</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        col1, col2 = st.columns([4, 1])
        with col1:
            with st.expander(f"Why am I seeing this? ({len(sig.reasons)} reasons)", expanded=False):
                for reason in sig.reasons:
                    st.markdown(f"- {html.escape(reason)}")
                st.caption(f"Deterministic Signal ID: `{sig.signal_id}`")
                if sig.metadata:
                    st.json(sig.metadata)

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
                    # Set navigation target in session state
                    st.session_state["_pb_nav_target"] = sig.navigation_target
                    st.session_state["_pb_nav_payload"] = sig.navigation_payload
                    st.rerun()
