"""PlanReader v1.3.0 Accuracy Lab UI.

Adds a compact non-destructive sidebar lab for running native vector analysis,
recording/importing verified ground truth, and viewing benchmark results.
"""
from __future__ import annotations

import json
from typing import Any

VERSION = "1.3.0"
_CATEGORIES = [
    "sheet_classification", "scale", "floor_area", "ceiling_area", "room_perimeter",
    "door_count", "window_count", "external_area", "substrate_allocation",
    "finish_association", "missed_scope", "false_inclusion",
]


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
            app.st.caption("Native PDF geometry + evidence-based scale + benchmark scoring. Does not overwrite take-off quantities.")
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
                source = app.st.text_input("Evidence/source", placeholder="Manual take-off / checked drawing", key=f"truth_source_{workspace_id}")
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
                app.st.caption(f"Verified ground-truth items: {int(report.get('ground_truth_count') or 0)}")
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
