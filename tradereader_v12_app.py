"""TradeReader 3D v1.2 all-trade production entry point.

One processed drawing/specification set can be detected and read trade by trade.
The Premier Brushworks painting PlanReader remains a separate entry point.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Sequence

import tradereader_v11_app as v11
from tradereader_profiles import TRADE_OPTIONS
from tradereader_trade_detection import detect_trades

base = v11.base
APP_VERSION = "1.2.0"
base.APP_VERSION = APP_VERSION
base.app.APP_VERSION = APP_VERSION

_original_trade_takeoff_page = base.trade_takeoff_page


def _page_records(workspace_id: int) -> List[Dict[str, Any]]:
    return base.app.lquery(
        """SELECT p.id,p.page_no,p.page_label,p.page_type,p.extracted_text,p.selected,d.file_name
           FROM pages p JOIN documents d ON d.id=p.document_id
           WHERE p.workspace_id=? ORDER BY d.id,p.page_no,p.id""",
        (workspace_id,),
    )


def _tag_trade_result(trade_name: str, data: Dict[str, Any]) -> Dict[str, Any]:
    tagged = dict(data or {})
    rows = []
    for source in tagged.get("takeoff_rows", []) or []:
        row = dict(source)
        section = str(row.get("section") or "Scope").strip()
        if not section.lower().startswith(trade_name.lower()):
            row["section"] = f"{trade_name} · {section}"
        marker = f"[Trade: {trade_name}]"
        note = str(row.get("notes") or "").strip()
        row["notes"] = note if marker in note else f"{marker} {note}".strip()
        row["rate_per_unit"] = 0 if row.get("rate_per_unit") in (None, "") else row.get("rate_per_unit")
        rows.append(row)
    tagged["takeoff_rows"] = rows

    registers = []
    for source in tagged.get("register_items", []) or []:
        item = dict(source)
        title = str(item.get("title") or "").strip()
        if title and not title.lower().startswith(trade_name.lower()):
            item["title"] = f"{trade_name} · {title}"
        registers.append(item)
    tagged["register_items"] = registers
    tagged["unknowns"] = [
        f"{trade_name}: {str(value).strip()}"
        for value in tagged.get("unknowns", []) or []
        if str(value).strip()
    ]
    summary = str(tagged.get("executive_summary") or "").strip()
    tagged["executive_summary"] = f"{trade_name}\n{summary}" if summary else f"{trade_name} trade scan completed."
    return tagged


def _merge(results: Sequence[tuple[str, Dict[str, Any]]]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {
        "executive_summary": "", "drawing_issue": "", "takeoff_rows": [],
        "register_items": [], "model_masses": [], "model_openings": [], "unknowns": [],
    }
    summaries: List[str] = []
    issues: List[str] = []
    seen = {key: set() for key in ("takeoff_rows", "register_items", "model_masses", "model_openings", "unknowns")}
    for trade_name, raw in results:
        data = _tag_trade_result(trade_name, raw)
        if data.get("executive_summary"):
            summaries.append(str(data["executive_summary"]))
        issue = str(data.get("drawing_issue") or "").strip()
        if issue and issue not in issues:
            issues.append(issue)
        for key in ("takeoff_rows", "register_items", "model_masses", "model_openings"):
            for row in data.get(key, []) or []:
                marker = json.dumps(row, sort_keys=True, default=str)
                if marker not in seen[key]:
                    seen[key].add(marker)
                    merged[key].append(row)
        for value in data.get("unknowns", []) or []:
            marker = str(value).strip()
            if marker and marker not in seen["unknowns"]:
                seen["unknowns"].add(marker)
                merged["unknowns"].append(marker)
    merged["executive_summary"] = "\n\n".join(summaries)
    merged["drawing_issue"] = " / ".join(issues)
    return merged


def run_all_trade_ai_plan_read(
    workspace_id: int,
    page_ids: Sequence[int],
    trades: Sequence[str],
    api_key: str,
    model: str,
    provider: str = "OpenAI",
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    selected = [str(t) for t in trades if str(t) in TRADE_OPTIONS and str(t) != "Custom trade"]
    if not selected:
        raise RuntimeError("Select at least one trade for the all-trade scan.")
    previous_trade = base.app.st.session_state.get("tradereader_trade")
    previous_custom = base.app.st.session_state.get("tradereader_custom_trade")
    results: List[tuple[str, Dict[str, Any]]] = []
    report: List[Dict[str, Any]] = []
    try:
        for trade_name in selected:
            base.app.st.session_state["tradereader_trade"] = trade_name
            raw = base.run_trade_ai_plan_read(workspace_id, page_ids, api_key, model, provider)
            tagged = _tag_trade_result(trade_name, raw)
            results.append((trade_name, raw))
            report.append({
                "Trade": trade_name,
                "Take-off lines": len(tagged.get("takeoff_rows", []) or []),
                "Register items": len(tagged.get("register_items", []) or []),
                "Unknowns / RFIs": len(tagged.get("unknowns", []) or []),
            })
    finally:
        if previous_trade is None:
            base.app.st.session_state.pop("tradereader_trade", None)
        else:
            base.app.st.session_state["tradereader_trade"] = previous_trade
        if previous_custom is None:
            base.app.st.session_state.pop("tradereader_custom_trade", None)
        else:
            base.app.st.session_state["tradereader_custom_trade"] = previous_custom
    return _merge(results), report


def _render_all_trade_scan(workspace: Dict[str, Any], session_api_key: str, ai_provider: str) -> None:
    app = base.app
    workspace_id = int(workspace["id"])
    app.hero(workspace)
    app.st.subheader("All-trade project scan")
    app.st.caption(
        "TradeReader detects likely trades from drawing titles, page types, filenames and extracted text. "
        "You review the trade list before AI runs."
    )
    pages = _page_records(workspace_id)
    if not pages:
        app.st.info("Process the project documents first.")
        return

    options = {
        f"#{int(row['id'])} · {row.get('page_label') or 'Page'} · {row.get('page_type') or 'Other'}": int(row["id"])
        for row in pages
    }
    defaults = [
        label for label, page_id in options.items()
        if any(int(row["id"]) == page_id and bool(row.get("selected")) for row in pages)
    ]
    selected_labels = app.st.multiselect(
        "Pages to scan", list(options), default=defaults or list(options), key="tradereader_v12_pages"
    )
    selected_ids = [options[label] for label in selected_labels]
    selected_set = set(selected_ids)
    selected_pages = [row for row in pages if int(row["id"]) in selected_set]
    detections = detect_trades(selected_pages)

    if detections:
        app.st.markdown("### Detected trade evidence")
        app.st.dataframe([
            {
                "Trade": row["trade"], "Evidence score": row["score"],
                "Matched pages": row["matched_pages"], "Matched terms": ", ".join(row["evidence"]),
            }
            for row in detections
        ], use_container_width=True, hide_index=True)
    else:
        app.st.warning("No trade passed the conservative detection threshold; select trades manually below.")

    scan_trades = [trade for trade in TRADE_OPTIONS if trade != "Custom trade"]
    suggested = [row["trade"] for row in detections if row["trade"] in scan_trades]
    selected_trades = app.st.multiselect(
        "Trades to read", scan_trades, default=suggested, key="tradereader_v12_trades",
        help="Detected trades are preselected. Add any trade that is in scope even when its drawing text is sparse.",
    )
    if app.st.button("Select every built-in trade", key="tradereader_v12_every_trade"):
        app.st.session_state["tradereader_v12_trades"] = scan_trades
        app.st.rerun()

    model = app.st.text_input(
        "All-trade AI model", value=app.default_ai_model(ai_provider), key="tradereader_v12_model"
    )
    key = app.resolve_ai_key(ai_provider, session_api_key)
    app.st.info(
        "Each selected trade is analysed independently against the same source pages. Unsupported dimensions, "
        "routes, materials, systems or counts must remain To measure / RFI. AI rates remain zero."
    )
    if app.st.button(
        f"Run all-trade scan ({len(selected_trades)} trade{'s' if len(selected_trades) != 1 else ''})",
        type="primary",
        disabled=not bool(key) or not bool(selected_ids) or not bool(selected_trades),
        key="tradereader_v12_run",
    ):
        with app.st.spinner("Reading the project set trade by trade..."):
            try:
                combined, report = run_all_trade_ai_plan_read(
                    workspace_id, selected_ids, selected_trades, key,
                    model.strip() or app.default_ai_model(ai_provider), ai_provider,
                )
                app.st.session_state["latest_all_trade_ai_result"] = combined
                app.st.session_state["latest_all_trade_ai_report"] = report
                app.st.success(f"All-trade scan completed for {len(selected_trades)} trade(s). Review before importing.")
            except Exception as exc:
                app.st.error(app._ai_error_hint(exc))

    result = app.st.session_state.get("latest_all_trade_ai_result")
    report = app.st.session_state.get("latest_all_trade_ai_report")
    if result:
        if report:
            app.st.dataframe(report, use_container_width=True, hide_index=True)
        app.st.json(result, expanded=False)
        if app.st.button("Import reviewed all-trade draft into the project", type="primary", key="tradereader_v12_import"):
            counts = app.import_ai_result(workspace_id, result)
            app.st.success(f"Imported {counts['takeoff']} take-off lines and {counts['registers']} scope/register items.")
            app.st.session_state.pop("latest_all_trade_ai_result", None)
            app.st.session_state.pop("latest_all_trade_ai_report", None)
            app.st.rerun()
    if not key:
        app.st.warning("Configure an AI key to enable the all-trade read.")


def _v12_trade_takeoff_page(workspace, session_api_key, ai_provider="OpenAI"):
    all_tab, single_tab = base.app.st.tabs(["All-trade project scan", "Single-trade take-off & specialist tools"])
    with all_tab:
        _render_all_trade_scan(workspace, session_api_key, ai_provider)
    with single_tab:
        _original_trade_takeoff_page(workspace, session_api_key, ai_provider)


base.trade_takeoff_page = _v12_trade_takeoff_page

if __name__ == "__main__":
    base.main()
