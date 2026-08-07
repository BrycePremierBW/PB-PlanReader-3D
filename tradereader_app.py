from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import pandas as pd

# Keep TradeReader data physically separate from the Premier Brushworks instance.
if not os.environ.get("PLANREADER_DATA_DIR"):
    os.environ["PLANREADER_DATA_DIR"] = os.environ.get(
        "TRADEREADER_DATA_DIR", str(Path.cwd() / "tradereader_data")
    )

import pb_planreader_3d_app as app
from tradereader_profiles import TRADE_OPTIONS, TRADE_PROFILES

APP_VERSION = "1.0.0"
app.APP_NAME = "TradeReader 3D — Multi-Trade AI Take-off"
app.APP_VERSION = APP_VERSION

GENERIC_SUBSTRATES = [
    "Not applicable",
    "Concrete",
    "Masonry / blockwork",
    "Brickwork",
    "Timber",
    "Steel",
    "Aluminium / metal",
    "Plasterboard",
    "Fibre cement",
    "Roof sheeting",
    "Insulation",
    "Tile",
    "Stone",
    "Vinyl / resilient",
    "Carpet",
    "Soil / landscape",
    "Pipework",
    "Ductwork",
    "Cable / containment",
    "Equipment / fixture",
    "Other",
]
GENERIC_SYSTEMS = [
    "To be confirmed",
    "Supply & install",
    "Install only",
    "Labour only",
    "Material only",
    "Testing / commissioning",
    "Provisional",
    "By others / excluded",
    "Existing / retain",
    "Other",
]
GENERIC_UNITS = ["m²", "m³", "lm", "m", "No.", "item", "set", "point", "kg", "t", "L", "hr", "allowance"]

# The base program is painting-oriented. Override the shared option lists for
# this entry point only. Premier Brushworks launches a different process.
app.SUBSTRATES = GENERIC_SUBSTRATES
app.FINISH_SYSTEMS = GENERIC_SYSTEMS
app.UNIT_OPTIONS = GENERIC_UNITS

# Never leak Premier Brushworks paint rates into another trade. Rates stay zero
# unless the estimator explicitly enters one or a source document supplies it.
app.default_rate_for = lambda *_args, **_kwargs: 0.0
app.paint_litres = lambda *_args, **_kwargs: 0.0


def tradereader_css() -> None:
    """Keep Streamlit light controls readable inside the dark sidebar."""
    app.st.markdown(
        """
        <style>
        [data-testid="stSidebar"] input,
        [data-testid="stSidebar"] textarea,
        [data-testid="stSidebar"] [role="combobox"] {
            color: #171717 !important;
            -webkit-text-fill-color: #171717 !important;
            caret-color: #171717 !important;
        }
        [data-testid="stSidebar"] input::placeholder,
        [data-testid="stSidebar"] textarea::placeholder {
            color: #666666 !important;
            -webkit-text-fill-color: #666666 !important;
            opacity: 1 !important;
        }
        [data-testid="stSidebar"] [data-baseweb="input"] > div,
        [data-testid="stSidebar"] [data-baseweb="textarea"] > div,
        [data-testid="stSidebar"] [data-baseweb="select"] > div {
            background-color: #ffffff !important;
        }
        [role="listbox"],
        [role="option"] {
            color: #171717 !important;
            -webkit-text-fill-color: #171717 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def current_trade() -> Tuple[str, Dict[str, Any]]:
    selected = str(app.st.session_state.get("tradereader_trade") or TRADE_OPTIONS[0])
    profile = dict(TRADE_PROFILES.get(selected) or TRADE_PROFILES["Custom trade"])
    if selected == "Custom trade":
        custom = str(app.st.session_state.get("tradereader_custom_trade") or "").strip()
        if custom:
            profile["name"] = custom
            return custom, profile
    profile["name"] = selected
    return selected, profile


def _trade_prompt(profile: Dict[str, Any]) -> str:
    trade_name = str(profile.get("name") or "selected trade")
    sections = "\n".join(f"- {x}" for x in profile.get("sections", []))
    units = ", ".join(profile.get("units", GENERIC_UNITS))
    return f"""
You are a senior Australian construction estimator preparing a source-based {trade_name} take-off.

Your job is NOT to write a generic plan summary. Build a practical, reviewable draft take-off for the {trade_name} trade.

TRADE FOCUS
{profile.get("focus", "")}

PREFERRED SCOPE SECTIONS
{sections}

EXPECTED UNITS
Use the unit that matches the work: {units}. Do not force everything into square metres.

ESTIMATING METHOD
1. Establish the drawing/specification source and current issue where the supplied evidence supports it.
2. Cross-reference plans, elevations, sections, schedules, details, specifications, legends, notes, addenda and scope documents.
3. Separate INCLUSIONS, EXCLUSIONS, SEPARATE ITEMS, PROVISIONAL ITEMS, ASSUMPTIONS, RISKS and RFIs.
4. Measure only work that belongs to the {trade_name} scope. Typical exclusions include: {profile.get("exclude", "")}.
5. Never invent a dimension, count, route, material, system, rating, product or finish. If it cannot be supported, use quantity=0 and quantity_status='To measure' and raise a clarification.
6. Use net quantities where deductions are supported. In notes show the measurement basis in a reviewable form such as Base × Factor − Deductions + Adjustments = Net.
7. Use source_page and source_reference on every measurable line. Confidence must reflect the evidence.
8. rate_per_unit must be 0 unless a rate is explicitly visible in a supplied source. Do not invent pricing.
9. The legacy field finish_system should hold the relevant specification/system/type for this trade. If coats/coverage are irrelevant, use 0. productivity_m2_per_hour is an optional generic productivity field and may be 0.
10. Create conceptual 3D masses/openings only where they genuinely help explain the take-off. Do not pretend the model is construction-grade BIM.
11. When a schedule provides counts/types, reconcile the schedule against plan locations rather than counting twice.
12. Return structured data only. Keep wording concise and estimator-focused.
"""


def _page_rows(workspace_id: int, page_ids: Sequence[int]) -> List[Dict[str, Any]]:
    if not page_ids:
        return []
    return app.lquery(
        f"""SELECT p.*,d.file_name
            FROM pages p JOIN documents d ON d.id=p.document_id
            WHERE p.workspace_id=? AND p.id IN ({','.join('?' for _ in page_ids)})
            ORDER BY p.id""",
        (workspace_id, *tuple(page_ids)),
    )


def _merge_ai_batches(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not results:
        return {
            "executive_summary": "",
            "drawing_issue": "",
            "takeoff_rows": [],
            "register_items": [],
            "model_masses": [],
            "model_openings": [],
            "unknowns": [],
        }
    merged = {
        "executive_summary": "",
        "drawing_issue": "",
        "takeoff_rows": [],
        "register_items": [],
        "model_masses": [],
        "model_openings": [],
        "unknowns": [],
    }
    summaries: List[str] = []
    issues: List[str] = []
    seen: Dict[str, set] = {k: set() for k in ["takeoff_rows", "register_items", "model_masses", "model_openings", "unknowns"]}
    for result in results:
        summary = str(result.get("executive_summary") or "").strip()
        if summary and summary not in summaries:
            summaries.append(summary)
        issue = str(result.get("drawing_issue") or "").strip()
        if issue and issue not in issues:
            issues.append(issue)
        for key in ["takeoff_rows", "register_items", "model_masses", "model_openings"]:
            for row in result.get(key, []) or []:
                marker = json.dumps(row, sort_keys=True, default=str)
                if marker not in seen[key]:
                    seen[key].add(marker)
                    merged[key].append(row)
        for unknown in result.get("unknowns", []) or []:
            marker = str(unknown).strip()
            if marker and marker not in seen["unknowns"]:
                seen["unknowns"].add(marker)
                merged["unknowns"].append(marker)
    merged["executive_summary"] = "\n\n".join(summaries)
    merged["drawing_issue"] = " / ".join(issues)
    return merged


def run_trade_ai_plan_read(
    workspace_id: int,
    page_ids: Sequence[int],
    api_key: str,
    model: str,
    provider: str = "OpenAI",
) -> Dict[str, Any]:
    pages = _page_rows(workspace_id, page_ids)
    if not pages:
        raise RuntimeError("Select at least one processed drawing or specification page.")

    _trade_name, profile = current_trade()
    prompt = _trade_prompt(profile)
    schema = app.ai_schema()

    # Give each batch a project-wide text basis, then include high-detail images
    # for only the current batch. This keeps large drawing sets usable.
    project_basis_parts: List[str] = []
    for page in pages:
        excerpt = str(page.get("extracted_text") or "")[:5000]
        if excerpt.strip():
            project_basis_parts.append(
                f"{page.get('file_name')} · p{page.get('page_no')} · {page.get('page_label')}:\n{excerpt}"
            )
    project_basis = "\n\n".join(project_basis_parts)[:70000]

    results: List[Dict[str, Any]] = []
    for start in range(0, len(pages), 8):
        batch = pages[start : start + 8]
        blocks: List[Tuple[str, str]] = [
            ("text", "PROJECT-WIDE EXTRACTED TEXT BASIS:\n" + project_basis)
        ]
        for page in batch:
            excerpt = str(page.get("extracted_text") or "")[:12000]
            blocks.append(
                (
                    "text",
                    f"SOURCE PAGE: {page.get('file_name')} · {page.get('page_label')} · "
                    f"page {page.get('page_no')} · classified {page.get('page_type')}\n"
                    f"EXTRACTED TEXT:\n{excerpt}",
                )
            )
            image_path = Path(str(page.get("image_path") or ""))
            if image_path.exists():
                blocks.append(("image", str(image_path)))
        results.append(
            app.run_ai_structured(
                provider, api_key, model, prompt, blocks, schema, "trade_takeoff_analysis"
            )
        )

    data = _merge_ai_batches(results)
    app.lexecute(
        """INSERT INTO ai_runs(workspace_id,run_type,model,source_pages,status,response_json,error_message,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            workspace_id,
            f"{profile.get('name')} trade take-off",
            f"{provider} · {model}",
            json.dumps(list(page_ids)),
            "Completed",
            json.dumps(data),
            "",
            app.now_stamp(),
        ),
    )
    return data


def run_trade_render_read(
    workspace_id: int,
    page_ids: Sequence[int],
    api_key: str,
    model: str,
    provider: str = "OpenAI",
) -> Dict[str, Any]:
    pages = _page_rows(workspace_id, page_ids)
    if not pages:
        raise RuntimeError("Select at least one render / image page.")
    trade_name, _profile = current_trade()
    prompt = f"""
You are reviewing architectural renders as secondary evidence for a conceptual 3D model used during a {trade_name} take-off.

Use renders only for visible building form and appearance. Never treat render-derived dimensions as measured.
Create simple building masses/openings only where useful for understanding the {trade_name} scope.
All dimensions inferred from an image must be confidence='Assumed' and clearly described as assumed.
Do not invent hidden services, routes, quantities or trade scope from appearance alone.
Return the required structured JSON only.
"""
    blocks: List[Tuple[str, str]] = []
    for page in pages:
        blocks.append(
            (
                "text",
                f"RENDER PAGE: {page.get('file_name')} · {page.get('page_label')} · p{page.get('page_no')}",
            )
        )
        image_path = Path(str(page.get("image_path") or ""))
        if image_path.exists():
            blocks.append(("image", str(image_path)))
    data = app.run_ai_structured(
        provider, api_key, model, prompt, blocks, app.render_ai_schema(), "trade_3d_analysis"
    )
    app.lexecute(
        """INSERT INTO ai_runs(workspace_id,run_type,model,source_pages,status,response_json,error_message,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            workspace_id,
            f"{trade_name} render / 3D review",
            f"{provider} · {model}",
            json.dumps(list(page_ids)),
            "Completed",
            json.dumps(data),
            "",
            app.now_stamp(),
        ),
    )
    return data


# Any base-page buttons that call these functions now get trade-neutral logic.
app.run_ai_plan_read = run_trade_ai_plan_read
app.run_ai_render_read = run_trade_render_read


def _trade_badge() -> None:
    trade_name, _profile = current_trade()
    app.st.sidebar.markdown(
        f"""
        <div style="margin:.35rem 0 .7rem 0;padding:.55rem .7rem;border:1px solid #D7A21B;
                    border-left:5px solid #D7A21B;border-radius:8px;background:#262626;color:white;
                    font-size:.82rem;line-height:1.25rem">
          <strong style="color:#F4C84B">TRADEREADER 3D v{APP_VERSION}</strong><br>
          Active trade: {trade_name}
        </div>
        """,
        unsafe_allow_html=True,
    )


def trade_sidebar() -> Any:
    app.st.sidebar.markdown("## TradeReader 3D")
    selected = app.st.sidebar.selectbox(
        "Trade",
        TRADE_OPTIONS,
        index=TRADE_OPTIONS.index(
            app.st.session_state.get("tradereader_trade", TRADE_OPTIONS[0])
        )
        if app.st.session_state.get("tradereader_trade", TRADE_OPTIONS[0]) in TRADE_OPTIONS
        else 0,
        key="tradereader_trade",
    )
    if selected == "Custom trade":
        app.st.sidebar.text_input(
            "Custom trade name",
            key="tradereader_custom_trade",
            placeholder="e.g. Fire protection",
        )
    _trade_badge()
    app.st.sidebar.caption("Independent app · no Premier Brushworks painting rules or JobHub writes.")

    app.st.sidebar.markdown("### Project")
    workspaces = app.lquery("SELECT * FROM workspaces ORDER BY updated_at DESC,id DESC")
    if workspaces:
        labels = [f"#{w['id']} · {w.get('job_no','')} — {w.get('job_name','')}" for w in workspaces]
        current_id = app.st.session_state.get("workspace_id")
        current_index = 0
        for i, ws in enumerate(workspaces):
            if int(ws["id"]) == int(current_id or -1):
                current_index = i
                break
        picked = app.st.sidebar.selectbox("Saved TradeReader projects", labels, index=current_index)
        selected_ws = workspaces[labels.index(picked)]
        if app.st.sidebar.button("Open saved project", use_container_width=True):
            app.st.session_state["workspace_id"] = int(selected_ws["id"])
            app.st.rerun()

    with app.st.sidebar.expander("Create project"):
        with app.st.form("tradereader_create_workspace"):
            job_no = app.st.text_input("Job / project number", value="")
            job_name = app.st.text_input("Project name")
            builder = app.st.text_input("Builder / client")
            address = app.st.text_input("Site address")
            create = app.st.form_submit_button("Create project", use_container_width=True)
        if create:
            if not job_name.strip():
                app.st.error("Project name is required.")
            else:
                app.st.session_state["workspace_id"] = app.create_standalone_workspace(
                    job_no.strip(), job_name.strip(), builder.strip(), address.strip()
                )
                app.st.rerun()
    return app.st.session_state.get("workspace_id")


def trade_dashboard(workspace: Dict[str, Any]) -> None:
    app.hero(workspace)
    trade_name, profile = current_trade()
    docs = app.ldf("SELECT id FROM documents WHERE workspace_id=?", (workspace["id"],))
    pages = app.ldf("SELECT id FROM pages WHERE workspace_id=?", (workspace["id"],))
    takeoff = app.ldf(
        "SELECT section,element,quantity,unit,rate_per_unit FROM takeoff_rows WHERE workspace_id=?",
        (workspace["id"],),
    )
    masses = app.ldf("SELECT id FROM model_masses WHERE workspace_id=?", (workspace["id"],))
    cols = app.st.columns(5)
    cols[0].metric("Documents", len(docs))
    cols[1].metric("Drawing pages", len(pages))
    cols[2].metric("Take-off lines", len(takeoff))
    cols[3].metric("3D masses", len(masses))
    value = 0.0
    if not takeoff.empty:
        value = sum(
            app.to_float(r.quantity) * app.to_float(r.rate_per_unit)
            for r in takeoff.itertuples()
        )
    cols[4].metric("Entered-rate value", f"${value:,.0f}")

    app.st.markdown(
        f"""
        <div class='pb-card'>
          <h3>{trade_name} estimating workflow</h3>
          <p>Upload the current drawing/specification set, process it, review the drawing register,
          run the trade take-off, verify quantities and references, then export the reviewed schedule.</p>
          <p><b>AI focus:</b> {profile.get('focus','')}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if not takeoff.empty:
        summary = (
            takeoff.groupby(["section", "unit"], dropna=False)
            .agg(lines=("element", "count"), quantity=("quantity", "sum"))
            .reset_index()
        )
        app.st.subheader("Quantity summary")
        app.st.dataframe(summary, use_container_width=True, hide_index=True)


def _save_takeoff_rows(workspace_id: int, frame: pd.DataFrame) -> int:
    app.lexecute("DELETE FROM takeoff_rows WHERE workspace_id=?", (workspace_id,))
    inserted = 0
    for row in frame.to_dict("records"):
        if not any(
            str(row.get(c) or "").strip()
            for c in ["section", "element", "location", "source_reference"]
        ):
            continue
        row["rate_per_unit"] = app.to_float(row.get("rate_per_unit"))
        values = [row.get(col, "") for col in app.TAKEOFF_COLUMNS]
        app.lexecute(
            """INSERT INTO takeoff_rows(
               workspace_id,section,element,location,substrate,finish_system,quantity,unit,
               quantity_status,source_page,source_reference,inclusion_status,coats,
               coverage_m2_per_litre,productivity_m2_per_hour,rate_per_unit,confidence,notes,
               created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (workspace_id, *values, app.now_stamp(), app.now_stamp()),
        )
        inserted += 1
    return inserted


def trade_takeoff_page(
    workspace: Dict[str, Any],
    session_api_key: str,
    ai_provider: str = "OpenAI",
) -> None:
    app.hero(workspace)
    trade_name, profile = current_trade()
    app.st.caption(f"Active trade: {trade_name}")
    tabs = app.st.tabs(["AI plan read", "Take-off schedule", "Scope registers", "Source & basis"])

    with tabs[0]:
        pages = app.ldf(
            "SELECT id,page_label,page_type,image_path,selected FROM pages WHERE workspace_id=? ORDER BY id",
            (workspace["id"],),
        )
        if pages.empty:
            app.st.info("Process the project documents first.")
        else:
            options = {
                f"#{int(r.id)} · {r.page_label} · {r.page_type}": int(r.id)
                for r in pages.itertuples()
            }
            default = [
                label
                for label, rid in options.items()
                if bool(pages.loc[pages["id"].eq(rid), "selected"].iloc[0])
            ]
            selected = app.st.multiselect("Pages to analyse", list(options), default=default)
            model = app.st.text_input(
                "Model",
                value=app.default_ai_model(ai_provider),
                help="The selected drawing set is processed in batches of up to eight pages.",
            )
            app.st.info(
                "TradeReader measures only the selected trade, keeps source references, and leaves unsupported quantities as To measure."
            )
            if app.st.button(
                f"Run {trade_name} AI take-off",
                type="primary",
                disabled=not bool(app.resolve_ai_key(ai_provider, session_api_key)),
            ):
                with app.st.spinner(f"Reading the drawing set as a {trade_name} estimator..."):
                    try:
                        data = run_trade_ai_plan_read(
                            int(workspace["id"]),
                            [options[x] for x in selected],
                            app.resolve_ai_key(ai_provider, session_api_key),
                            model.strip() or app.default_ai_model(ai_provider),
                            ai_provider,
                        )
                        app.st.session_state["latest_trade_ai_result"] = data
                        app.st.session_state["latest_trade_ai_name"] = trade_name
                        app.st.success("AI take-off completed. Review it before importing.")
                    except Exception as exc:
                        app.st.error(app._ai_error_hint(exc))
            data = app.st.session_state.get("latest_trade_ai_result")
            if data and app.st.session_state.get("latest_trade_ai_name") == trade_name:
                app.st.json(data, expanded=False)
                if app.st.button("Import this AI draft into the project", type="primary"):
                    counts = app.import_ai_result(int(workspace["id"]), data)
                    app.st.success(
                        f"Imported {counts['takeoff']} take-off lines and {counts['registers']} scope/register items."
                    )
                    app.st.session_state.pop("latest_trade_ai_result", None)
                    app.st.session_state.pop("latest_trade_ai_name", None)
                    app.st.rerun()
            if not app.resolve_ai_key(ai_provider, session_api_key):
                app.st.warning(
                    "Configure OPENAI_API_KEY / GEMINI_API_KEY or enter a session key in the sidebar to enable AI."
                )

    with tabs[1]:
        with app.st.expander("Import an existing take-off from Excel or CSV"):
            upload = app.st.file_uploader(
                "Take-off file (.xlsx, .xls, .csv)",
                type=["xlsx", "xls", "csv"],
                key="tradereader_takeoff_import",
            )
            if upload is not None:
                try:
                    raw_headers, body, used_row, best_score, total_rows = app.detect_takeoff_columns(upload)
                except Exception as exc:
                    app.st.error(f"Could not read that file: {exc}")
                else:
                    header_row = app.st.number_input(
                        "Header row (1-based)",
                        min_value=1,
                        max_value=max(1, total_rows),
                        value=int(used_row) + 1,
                        key="tradereader_header_row",
                    )
                    if int(header_row) != used_row + 1:
                        raw_headers, body, used_row, best_score, total_rows = app.detect_takeoff_columns(
                            upload, header_row=int(header_row) - 1
                        )
                    columns = [
                        str(h).strip() if str(h).strip() else f"Column {i+1}"
                        for i, h in enumerate(raw_headers)
                    ]
                    auto = {i: app._match_takeoff_header(h) for i, h in enumerate(raw_headers)}
                    mapping_df = pd.DataFrame(
                        {"Column": columns, "Maps to": [auto.get(i) or "" for i in range(len(columns))]}
                    )
                    mapping_df = app.st.data_editor(
                        mapping_df,
                        hide_index=True,
                        use_container_width=True,
                        key="tradereader_mapping_editor",
                        column_config={
                            "Column": app.st.column_config.TextColumn(disabled=True),
                            "Maps to": app.st.column_config.SelectboxColumn(options=[""] + app.TAKEOFF_COLUMNS),
                        },
                    )
                    mapping = {
                        i: str(value).strip()
                        for i, value in enumerate(mapping_df["Maps to"])
                        if str(value).strip()
                    }
                    try:
                        parsed, warnings = app.parse_takeoff_file(
                            upload, mapping=mapping, raw_headers=raw_headers, body=body
                        )
                    except Exception as exc:
                        app.st.error(f"Could not build take-off rows: {exc}")
                    else:
                        for warning in warnings:
                            app.st.warning(warning)
                        app.st.dataframe(parsed, use_container_width=True, hide_index=True)
                        if app.st.button(
                            f"Import {len(parsed)} rows",
                            type="primary",
                            key="tradereader_import_rows",
                        ):
                            existing = app.ldf(
                                "SELECT * FROM takeoff_rows WHERE workspace_id=? ORDER BY id",
                                (workspace["id"],),
                            )
                            combined = pd.concat(
                                [
                                    existing[app.TAKEOFF_COLUMNS] if not existing.empty else pd.DataFrame(columns=app.TAKEOFF_COLUMNS),
                                    parsed[app.TAKEOFF_COLUMNS],
                                ],
                                ignore_index=True,
                            )
                            count = _save_takeoff_rows(int(workspace["id"]), combined)
                            app.st.success(f"Take-off schedule now contains {count} rows.")
                            app.st.rerun()

        takeoff = app.ldf(
            "SELECT * FROM takeoff_rows WHERE workspace_id=? ORDER BY id",
            (workspace["id"],),
        )
        editor_cols = ["id"] + app.TAKEOFF_COLUMNS
        if takeoff.empty:
            takeoff = pd.DataFrame(columns=editor_cols)
        edited = app.st.data_editor(
            takeoff[editor_cols],
            use_container_width=True,
            hide_index=True,
            num_rows="dynamic",
            height=560,
            key="tradereader_takeoff_editor",
            column_config={
                "id": app.st.column_config.NumberColumn(disabled=True),
                "substrate": app.st.column_config.SelectboxColumn(options=GENERIC_SUBSTRATES),
                "finish_system": app.st.column_config.SelectboxColumn(options=GENERIC_SYSTEMS),
                "unit": app.st.column_config.SelectboxColumn(options=GENERIC_UNITS),
                "quantity_status": app.st.column_config.SelectboxColumn(options=app.STATUS_OPTIONS),
                "inclusion_status": app.st.column_config.SelectboxColumn(options=app.INCLUSION_OPTIONS),
            },
        )
        c1, c2 = app.st.columns(2)
        if c1.button("Save take-off schedule", type="primary", use_container_width=True):
            count = _save_takeoff_rows(int(workspace["id"]), edited)
            app.st.success(f"Saved {count} take-off rows.")
            app.st.rerun()
        if c2.button("Add empty trade scope rows", use_container_width=True):
            rows = []
            default_unit = (profile.get("units") or ["item"])[0]
            for section in profile.get("sections", []):
                row = {column: "" for column in app.TAKEOFF_COLUMNS}
                row.update(
                    {
                        "section": section,
                        "element": section,
                        "location": "Project-wide / allocate",
                        "substrate": "Not applicable",
                        "finish_system": "To be confirmed",
                        "quantity": 0.0,
                        "unit": default_unit,
                        "quantity_status": "To measure",
                        "inclusion_status": "INCLUSION",
                        "rate_per_unit": 0.0,
                        "confidence": "To review",
                        "notes": f"{trade_name} scope seed — verify against current documents.",
                    }
                )
                rows.append(row)
            existing = app.ldf(
                "SELECT * FROM takeoff_rows WHERE workspace_id=? ORDER BY id",
                (workspace["id"],),
            )
            combined = pd.concat(
                [
                    existing[app.TAKEOFF_COLUMNS] if not existing.empty else pd.DataFrame(columns=app.TAKEOFF_COLUMNS),
                    pd.DataFrame(rows, columns=app.TAKEOFF_COLUMNS),
                ],
                ignore_index=True,
            )
            _save_takeoff_rows(int(workspace["id"]), combined)
            app.st.rerun()

    with tabs[2]:
        names = ["inclusions", "exclusions", "clarifications", "assumptions", "rfis", "access_constraints", "risks"]
        selected_reg = app.st.selectbox(
            "Register",
            names,
            format_func=lambda x: x.replace("_", " ").title(),
        )
        frame = app.ldf(
            """SELECT id,item_no,title,detail,priority,source_reference,status
               FROM register_items WHERE workspace_id=? AND register_name=? ORDER BY id""",
            (workspace["id"], selected_reg),
        )
        app.st.dataframe(frame, use_container_width=True, hide_index=True)
        app.add_register_item_form(int(workspace["id"]), selected_reg, "scope")

    with tabs[3]:
        source = app.ldf(
            """SELECT id,item_no,title,detail,priority,source_reference,status
               FROM register_items WHERE workspace_id=? AND register_name='source_basis' ORDER BY id""",
            (workspace["id"],),
        )
        app.st.dataframe(source, use_container_width=True, hide_index=True)
        app.add_register_item_form(int(workspace["id"]), "source_basis", "source")
        if app.st.button("Generate source/basis rows from drawing register"):
            pages = app.lquery(
                """SELECT p.page_label,p.page_type,p.scale_text,p.page_no,d.file_name
                   FROM pages p JOIN documents d ON d.id=p.document_id
                   WHERE p.workspace_id=? ORDER BY d.id,p.page_no""",
                (workspace["id"],),
            )
            for page in pages:
                ref = f"{page['file_name']} p{page['page_no']}"
                exists = app.lquery(
                    """SELECT id FROM register_items
                       WHERE workspace_id=? AND register_name='source_basis' AND source_reference=?""",
                    (workspace["id"], ref),
                )
                if not exists:
                    app.lexecute(
                        """INSERT INTO register_items(
                           workspace_id,register_name,item_no,title,detail,priority,source_reference,status,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            workspace["id"],
                            "source_basis",
                            page.get("page_label", ""),
                            page.get("page_type", ""),
                            f"Scale: {page.get('scale_text') or 'not confirmed'}",
                            "",
                            ref,
                            "Used / review",
                            app.now_stamp(),
                        ),
                    )
            app.st.rerun()


def generic_excel_export_bytes(workspace_id: int) -> bytes:
    workspace = app.lquery("SELECT * FROM workspaces WHERE id=?", (workspace_id,))[0]
    trade_name, _profile = current_trade()
    takeoff = app.ldf(
        "SELECT * FROM takeoff_rows WHERE workspace_id=? ORDER BY id", (workspace_id,)
    )
    if not takeoff.empty:
        takeoff["value_ex_gst"] = [
            app.to_float(r.quantity) * app.to_float(r.rate_per_unit)
            for r in takeoff.itertuples()
        ]
    docs = app.ldf(
        "SELECT file_name,source_type,category,page_count,uploaded_at FROM documents WHERE workspace_id=? ORDER BY id",
        (workspace_id,),
    )
    pages = app.ldf(
        """SELECT p.page_label,p.page_type,p.scale_text,p.page_no,d.file_name,p.selected
           FROM pages p JOIN documents d ON d.id=p.document_id
           WHERE p.workspace_id=? ORDER BY d.id,p.page_no""",
        (workspace_id,),
    )
    project = pd.DataFrame(
        [
            ["Job / project number", workspace.get("job_no", "")],
            ["Project", workspace.get("job_name", "")],
            ["Builder / client", workspace.get("builder_client", "")],
            ["Site address", workspace.get("site_address", "")],
            ["Drawing issue", workspace.get("drawing_issue", "")],
            ["Trade", trade_name],
            ["Generated", app.now_stamp()],
            ["Take-off method", "TradeReader source-based multi-trade take-off"],
            ["Pricing basis", "Rates are estimator-entered; AI does not invent rates"],
        ],
        columns=["Field", "Value"],
    )
    summary = pd.DataFrame(
        {"Executive Summary": [workspace.get("executive_summary", "")]}
    )
    masses = app.ldf(
        "SELECT label,level_name,x,y,z,width,depth,height,finish,source_reference,confidence,notes FROM model_masses WHERE workspace_id=? ORDER BY id",
        (workspace_id,),
    )
    openings = app.ldf(
        "SELECT label,opening_type,face,offset_x,offset_z,width,height,count,notes,source_reference FROM model_openings WHERE workspace_id=? ORDER BY id",
        (workspace_id,),
    )
    sheets = [
        ("Project Information", project),
        ("Executive Summary", summary),
        ("Source Documents", docs),
        ("Drawing Register", pages),
        ("Take-off Schedule", takeoff),
        ("Inclusions", app.register_df(workspace_id, "inclusions")),
        ("Exclusions", app.register_df(workspace_id, "exclusions")),
        ("Clarifications", app.register_df(workspace_id, "clarifications")),
        ("Assumptions", app.register_df(workspace_id, "assumptions")),
        ("RFIs", app.register_df(workspace_id, "rfis")),
        ("Risks", app.register_df(workspace_id, "risks")),
        ("3D Masses", masses),
        ("3D Openings", openings),
    ]
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, frame in sheets:
            frame.to_excel(writer, sheet_name=name[:31], index=False)
    return buffer.getvalue()


def trade_quantity_schedule_page(workspace: Dict[str, Any]) -> None:
    app.hero(workspace)
    trade_name, _profile = current_trade()
    takeoff = app.ldf(
        "SELECT section,element,location,substrate,finish_system,quantity,unit,rate_per_unit,confidence,source_reference,notes FROM takeoff_rows WHERE workspace_id=? ORDER BY id",
        (workspace["id"],),
    )
    app.st.subheader(f"{trade_name} quantity schedule")
    if takeoff.empty:
        app.st.info("No take-off quantities yet.")
        return
    takeoff["value_ex_gst"] = [
        app.to_float(r.quantity) * app.to_float(r.rate_per_unit)
        for r in takeoff.itertuples()
    ]
    summary = (
        takeoff.groupby(["section", "unit"], dropna=False)
        .agg(lines=("element", "count"), quantity=("quantity", "sum"), value_ex_gst=("value_ex_gst", "sum"))
        .reset_index()
    )
    app.st.dataframe(summary, use_container_width=True, hide_index=True)
    app.st.subheader("Detailed schedule")
    app.st.dataframe(takeoff, use_container_width=True, hide_index=True)


def trade_export_page(workspace: Dict[str, Any]) -> None:
    app.hero(workspace)
    trade_name, _profile = current_trade()
    app.st.subheader(f"Export {trade_name} take-off")
    data = generic_excel_export_bytes(int(workspace["id"]))
    stem = app.safe_name(workspace.get("job_no") or workspace.get("job_name") or "tradereader")
    app.st.download_button(
        "Download Excel take-off pack",
        data,
        file_name=f"{stem}_{app.safe_name(trade_name)}_takeoff.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )
    app.st.info(
        "This TradeReader deployment is intentionally independent of Premier Brushworks JobHub. Export the reviewed schedule for the target trade system."
    )


def trade_settings_page(
    workspace: Dict[str, Any],
    session_api_key: str,
    ai_provider: str,
) -> None:
    app.hero(workspace)
    trade_name, _profile = current_trade()
    app.st.write(f"TradeReader version: `{APP_VERSION}`")
    app.st.write(f"Active trade: `{trade_name}`")
    app.st.write(f"Data folder: `{app.DATA_DIR}`")
    app.st.write(f"AI provider: `{ai_provider}`")
    app.st.write(
        f"AI key: `{'configured' if app.resolve_ai_key(ai_provider, session_api_key) else 'not configured'}`"
    )
    app.st.info(
        "TradeReader is a separate multi-trade app. It does not load the Premier Brushworks painting estimator overlay and does not connect to JobHub."
    )


def password_gate() -> None:
    password = os.environ.get("TRADEREADER_PASSWORD", "")
    if not password:
        return
    if app.st.session_state.get("tradereader_authenticated"):
        return
    app.hero()
    with app.st.form("tradereader_login"):
        entered = app.st.text_input("TradeReader password", type="password")
        submit = app.st.form_submit_button("Sign in", type="primary")
    if submit:
        if entered == password:
            app.st.session_state["tradereader_authenticated"] = True
            app.st.rerun()
        else:
            app.st.error("Incorrect password.")
    app.st.stop()


def main() -> None:
    app.st.set_page_config(
        page_title=app.APP_NAME,
        page_icon="📐",
        layout="wide",
    )
    app.app_css()
    tradereader_css()
    app.init_local_db()
    password_gate()

    workspace_id = trade_sidebar()
    ai_provider = app.st.sidebar.selectbox(
        "AI provider",
        app.AI_PROVIDERS,
        index=app.AI_PROVIDERS.index(app.resolve_ai_provider())
        if app.resolve_ai_provider() in app.AI_PROVIDERS
        else 0,
        key="tradereader_ai_provider",
    )
    session_api_key = app.st.sidebar.text_input(
        "AI API key (session only)",
        type="password",
        key="tradereader_ai_key",
        help="Leave blank when the provider key is configured in the deployment environment.",
    )
    if app.resolve_ai_key(ai_provider, session_api_key):
        app.st.sidebar.success("AI plan reading ready")
    else:
        app.st.sidebar.caption("Manual take-off and 3D tools remain available.")

    if not workspace_id:
        app.hero()
        app.st.info("Open or create a TradeReader project from the sidebar.")
        return
    workspace = app.current_workspace()
    if not workspace:
        app.st.session_state.pop("workspace_id", None)
        app.st.rerun()

    menu = app.st.sidebar.radio(
        "Menu",
        [
            "Dashboard",
            "Project & Documents",
            "Drawing Register",
            "Trade Take-off",
            "Plan Mapper",
            "3D Building Model",
            "Quantity Schedule",
            "Export",
            "Settings",
        ],
    )
    user = {"username": "tradereader", "role": "admin"}
    if menu == "Dashboard":
        trade_dashboard(workspace)
    elif menu == "Project & Documents":
        app.project_documents_page(workspace, None, user)
    elif menu == "Drawing Register":
        app.drawing_register_page(workspace)
    elif menu == "Trade Take-off":
        trade_takeoff_page(workspace, session_api_key, ai_provider)
    elif menu == "Plan Mapper":
        app.plan_mapper_page(workspace)
    elif menu == "3D Building Model":
        app.model_3d_page(workspace, session_api_key, ai_provider)
    elif menu == "Quantity Schedule":
        trade_quantity_schedule_page(workspace)
    elif menu == "Export":
        trade_export_page(workspace)
    else:
        trade_settings_page(workspace, session_api_key, ai_provider)


if __name__ == "__main__":
    main()
