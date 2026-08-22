"""PlanReader v1.3.0 Accuracy Lab UI.

Adds native vector analysis, benchmark scoring, and a one-click Fix workflow that
opens the exact source drawing for an error and records the estimator correction.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

VERSION = "1.3.0"
_CATEGORIES = [
    "sheet_classification", "scale", "floor_area", "ceiling_area", "room_perimeter",
    "door_count", "window_count", "external_area", "substrate_allocation",
    "finish_association", "missed_scope", "false_inclusion",
]


def _truth_row(app: Any, workspace_id: int, detail: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    rows = app.lquery(
        "SELECT * FROM accuracy_ground_truth WHERE workspace_id=? AND category=? AND item_key=? LIMIT 1",
        (int(workspace_id), str(detail.get("category") or ""), str(detail.get("item_key") or "")),
    )
    return dict(rows[0]) if rows else None


def _page_row(app: Any, page_id: Any) -> Optional[Dict[str, Any]]:
    if not page_id:
        return None
    rows = app.lquery(
        """SELECT p.*,d.file_name,d.path AS document_path FROM pages p
           JOIN documents d ON d.id=p.document_id WHERE p.id=? LIMIT 1""",
        (int(page_id),),
    )
    return dict(rows[0]) if rows else None


def _is_error(detail: Dict[str, Any]) -> bool:
    if not detail.get("matched"):
        return True
    if "correct" in detail:
        return not bool(detail.get("correct"))
    return float(detail.get("percent_error") or 0.0) > 0.001


def _render_fix_panel(app: Any, workspace_id: int, detail: Dict[str, Any]) -> None:
    truth = _truth_row(app, workspace_id, detail) or {}
    page = _page_row(app, truth.get("page_id"))
    app.st.markdown("### Verify & fix")
    app.st.caption(f"{detail.get('category')} · {detail.get('item_key')}")

    c1, c2, c3 = app.st.columns(3)
    c1.metric("PlanReader", str(detail.get("predicted", "No result")))
    c2.metric("Verified / expected", str(detail.get("expected", "")))
    if detail.get("percent_error") is not None:
        c3.metric("Error", f"{float(detail.get('percent_error') or 0):.2f}%")
    elif "correct" in detail:
        c3.metric("Match", "Yes" if detail.get("correct") else "No")
    else:
        c3.metric("Status", "Missing")

    if page:
        app.st.info(
            f"Source page: {page.get('page_label') or 'Unlabelled'} · page {int(page.get('page_no') or 0)} · "
            f"{page.get('page_type') or 'Other'} · {page.get('file_name') or ''}"
        )
        image_path = Path(str(page.get("image_path") or ""))
        if image_path.is_file():
            app.st.image(str(image_path), caption=f"Verify on {page.get('page_label') or 'source drawing'}", use_container_width=True)
        else:
            app.st.warning("The source page is identified, but its rendered image is not currently available. Re-process/render this page to verify visually.")
    else:
        app.st.warning("This benchmark item is not yet linked to a drawing page. Link it to the correct page before accepting the correction.")

    numeric_expected = truth.get("expected_numeric") is not None
    form_key = f"accuracy_fix_form_{workspace_id}_{detail.get('category')}_{detail.get('item_key')}"
    with app.st.form(form_key):
        if numeric_expected:
            default_value = detail.get("expected")
            corrected_raw = app.st.text_input("Correct value", value=str(default_value if default_value is not None else ""))
            corrected_text = ""
        else:
            corrected_raw = ""
            corrected_text = app.st.text_input("Correct value", value=str(detail.get("expected") or ""))
        reason = app.st.text_area(
            "Why was PlanReader wrong?",
            placeholder="e.g. missed breezeway wall; wrong scale; opening deducted twice; finish code linked to wrong legend row",
        )
        save = app.st.form_submit_button("Save fix as verified", type="primary", use_container_width=True)
    if save:
        corrected_numeric = None
        if numeric_expected:
            try:
                corrected_numeric = float(str(corrected_raw).replace(",", ""))
            except ValueError:
                app.st.error("Correct value must be numeric.")
                return
        user = str((app.st.session_state.get("planreader_user") or {}).get("username") or "")
        app.accuracy_record_correction_v130(
            int(workspace_id), str(detail.get("category") or ""), str(detail.get("item_key") or ""),
            predicted_numeric=float(detail.get("predicted")) if numeric_expected and detail.get("predicted") is not None else None,
            corrected_numeric=corrected_numeric,
            predicted_text="" if numeric_expected else str(detail.get("predicted") or ""),
            corrected_text=corrected_text,
            unit=str(truth.get("unit") or ""),
            page_id=int(truth.get("page_id")) if truth.get("page_id") else None,
            reason=str(reason or "").strip(),
            source_reference=str(truth.get("source_reference") or ""),
            corrected_by=user,
            engine_version=VERSION,
        )
        app.st.session_state.pop(f"accuracy_fix_{workspace_id}", None)
        report = app.accuracy_evaluate_workspace_v130(int(workspace_id), VERSION)
        app.st.session_state[f"accuracy_benchmark_{workspace_id}"] = report
        app.st.success("Fix saved. The correction is now retained as verified learning/benchmark evidence.")
        app.st.rerun()

    if app.st.button("Close verification", use_container_width=True, key=f"accuracy_fix_close_{workspace_id}"):
        app.st.session_state.pop(f"accuracy_fix_{workspace_id}", None)
        app.st.rerun()


def apply(app: Any) -> None:
    if getattr(app, "_pb_accuracy_ui_v130_applied", False):
        return
    app._pb_accuracy_ui_v130_applied = True
    base_selector = app.sidebar_workspace_selector

    def selector_with_accuracy_lab(bridge):
        workspace_id = base_selector(bridge)
        if not workspace_id:
            return workspace_id
        with app.st.sidebar.expander("Accuracy Lab v1.3.0", expanded=False):
            app.st.caption("Native PDF geometry + evidence-based scale + benchmark scoring. Existing take-off quantities are not silently overwritten.")
            pages = app.lquery(
                "SELECT id,page_no,page_label,page_type,px_per_m,scale_text FROM pages WHERE workspace_id=? AND selected=1 ORDER BY page_no,id",
                (int(workspace_id),),
            )
            page = None
            if pages:
                labels = [f"p{int(p.get('page_no') or 0)} · {p.get('page_label') or 'Unlabelled'} · {p.get('page_type') or 'Other'}" for p in pages]
                selected = app.st.selectbox("Analyse drawing page", range(len(pages)), format_func=lambda i: labels[i], key=f"accuracy_page_{workspace_id}")
                page = pages[int(selected)]
                if app.st.button("Run native vector analysis", use_container_width=True, key=f"accuracy_run_{workspace_id}"):
                    try:
                        result = app.analyse_stored_page_v130(int(page["id"]))
                        app.accuracy_record_vector_analysis_v130(int(workspace_id), int(page["id"]), result)
                        app.st.session_state[f"accuracy_result_{workspace_id}"] = result
                        app.st.success("Analysis complete")
                    except Exception as exc:
                        app.st.error(f"Could not analyse this page: {exc}")
                result = app.st.session_state.get(f"accuracy_result_{workspace_id}")
                if result and int(result.get("page_id") or 0) == int(page["id"]):
                    scale = result.get("scale") or {}
                    c1, c2 = app.st.columns(2)
                    c1.metric("Vector lines", int((result.get("native") or {}).get("segment_count") or 0))
                    c2.metric("Wall pairs", int(result.get("wall_pair_count") or 0))
                    app.st.caption(
                        f"Scale: {float(scale.get('px_per_m') or 0):,.2f} px/m · confidence {int(scale.get('confidence') or 0)}% · "
                        f"{'verified' if scale.get('verified') else 'provisional'}"
                    )
                    graph = result.get("graph") or {}
                    app.st.caption(f"Graph: {int(graph.get('node_count') or 0):,} nodes · {int(graph.get('edge_count') or 0):,} edges · {int(graph.get('junction_count') or 0):,} junctions")
            else:
                app.st.caption("No selected drawing pages in this workspace.")

            app.st.markdown("**Verified benchmark data**")
            with app.st.form(f"accuracy_truth_form_{workspace_id}"):
                category = app.st.selectbox("Category", _CATEGORIES, key=f"truth_category_{workspace_id}")
                item_key = app.st.text_input("Item key", placeholder="e.g. Unit 1 floor area", key=f"truth_key_{workspace_id}")
                expected_text = app.st.text_input("Verified text (for codes/classes)", key=f"truth_text_{workspace_id}")
                expected_numeric = app.st.text_input("Verified number (for quantities)", placeholder="e.g. 152.4", key=f"truth_number_{workspace_id}")
                unit = app.st.text_input("Unit", placeholder="m² / lm / No. / px/m", key=f"truth_unit_{workspace_id}")
                source = app.st.text_input("Evidence/source", placeholder="Latest PB take-off / checked drawing", key=f"truth_source_{workspace_id}")
                submitted_truth = app.st.form_submit_button("Save verified item", use_container_width=True)
            if submitted_truth:
                if not str(item_key or "").strip():
                    app.st.error("Enter an item key.")
                else:
                    numeric = None
                    if str(expected_numeric or "").strip():
                        try:
                            numeric = float(str(expected_numeric).replace(",", ""))
                        except ValueError:
                            app.st.error("Verified number must be numeric.")
                    if numeric is not None or str(expected_text or "").strip():
                        app.accuracy_upsert_truth_v130(
                            int(workspace_id), str(category), str(item_key).strip(),
                            expected_numeric=numeric, expected_text=str(expected_text or "").strip(), unit=str(unit or "").strip(),
                            page_id=int(page["id"]) if page else None, source_reference=str(source or "").strip(),
                            verified_by=str((app.st.session_state.get("planreader_user") or {}).get("username") or ""),
                        )
                        app.st.success("Verified benchmark item saved")

            uploaded_truth = app.st.file_uploader("Import ground-truth JSON", type=["json"], key=f"accuracy_truth_upload_{workspace_id}")
            if uploaded_truth is not None and app.st.button("Import benchmark", use_container_width=True, key=f"accuracy_truth_import_{workspace_id}"):
                try:
                    payload = json.loads(uploaded_truth.getvalue().decode("utf-8"))
                    count = app.accuracy_import_truth_v130(int(workspace_id), payload)
                    app.st.success(f"Imported {count} verified items")
                except Exception as exc:
                    app.st.error(f"Could not import benchmark: {exc}")

            if app.st.button("Score benchmark", use_container_width=True, key=f"accuracy_score_{workspace_id}"):
                report = app.accuracy_evaluate_workspace_v130(int(workspace_id), VERSION)
                app.st.session_state[f"accuracy_benchmark_{workspace_id}"] = report
            report = app.st.session_state.get(f"accuracy_benchmark_{workspace_id}")
            if report:
                app.st.caption(
                    f"Verified items: {int(report.get('ground_truth_count') or 0)} · "
                    f"saved corrections: {int(report.get('correction_count') or 0)}"
                )
                for category_name, metrics in (report.get("categories") or {}).items():
                    value = metrics.get("mape")
                    if value is not None:
                        text = f"{category_name}: error {100*float(value):.2f}%"
                    elif metrics.get("accuracy") is not None:
                        text = f"{category_name}: accuracy {100*float(metrics['accuracy']):.1f}%"
                    else:
                        text = f"{category_name}: {100*float(metrics.get('matched_rate') or 0):.1f}% matched"
                    passes = metrics.get("passes_target")
                    if passes is True:
                        app.st.success(text)
                    elif passes is False:
                        app.st.warning(text)
                    else:
                        app.st.caption(text)

                errors = [d for d in (report.get("details") or []) if _is_error(d)]
                if errors:
                    app.st.markdown("**Errors to verify**")
                    for idx, detail in enumerate(errors[:30]):
                        cols = app.st.columns([4, 1])
                        if detail.get("percent_error") is not None:
                            summary = f"{detail.get('item_key')} · {float(detail.get('percent_error') or 0):.2f}% error"
                        elif not detail.get("matched"):
                            summary = f"{detail.get('item_key')} · no prediction"
                        else:
                            summary = f"{detail.get('item_key')} · wrong classification"
                        cols[0].caption(f"{detail.get('category')} · {summary}")
                        if cols[1].button("Fix", key=f"accuracy_fix_btn_{workspace_id}_{idx}", use_container_width=True):
                            app.st.session_state[f"accuracy_fix_{workspace_id}"] = dict(detail)
                            truth = _truth_row(app, int(workspace_id), detail) or {}
                            if truth.get("page_id") and pages:
                                for page_idx, candidate in enumerate(pages):
                                    if int(candidate.get("id") or 0) == int(truth.get("page_id") or 0):
                                        app.st.session_state[f"accuracy_page_{workspace_id}"] = page_idx
                                        break
                            app.st.rerun()
                else:
                    app.st.success("No benchmark errors currently require verification.")

            fix_detail = app.st.session_state.get(f"accuracy_fix_{workspace_id}")
            if fix_detail:
                _render_fix_panel(app, int(workspace_id), dict(fix_detail))

            truth = app.accuracy_export_truth_v130(int(workspace_id))
            app.st.download_button(
                "Download benchmark ground truth",
                data=json.dumps(truth, indent=2),
                file_name=f"planreader_ground_truth_{workspace_id}.json",
                mime="application/json",
                use_container_width=True,
                key=f"accuracy_truth_download_{workspace_id}",
            )
        return workspace_id

    app.sidebar_workspace_selector = selector_with_accuracy_lab
