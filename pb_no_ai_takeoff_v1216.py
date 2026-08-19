"""PlanReader v1.2.16 deterministic take-off workflow that does not require AI.

AI remains available as an optional assistant, but the default Subscription Take-off
workflow can now build a draft directly from PlanReader's calibrated mapped zones
and unlinked measurement geometry.  The rules engine never invents dimensions,
coating systems, rates, coats or productivity.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence, Tuple

SOURCE_PREFIX = "PB No-AI v1.2.16"

_TAKEOFF_INSERT_SQL = """INSERT INTO takeoff_rows(
    workspace_id,section,element,location,substrate,finish_system,quantity,unit,
    quantity_status,source_page,source_reference,inclusion_status,coats,
    coverage_m2_per_litre,productivity_m2_per_hour,rate_per_unit,confidence,
    notes,row_role,created_at,updated_at
) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        if result != result or result in (float("inf"), float("-inf")):
            return default
        return result
    except (TypeError, ValueError):
        return default


def _text(*values: Any) -> str:
    return " ".join(str(value or "").strip() for value in values).strip().lower()


def _measured_status(raw_status: Any, calibrated: bool, quantity: float) -> Tuple[str, str]:
    if quantity <= 0:
        return "To measure", "To review"
    low = str(raw_status or "").strip().lower()
    if calibrated and (not low or ("measur" in low and "provisional" not in low)):
        return "Measured", "Measured"
    if calibrated:
        return "Provisional measured", "Derived"
    return "Provisional measured", "Derived"


def classify_context(*, page_type: Any = "", view_type: Any = "", label: Any = "", substrate: Any = "") -> Dict[str, str]:
    """Return conservative painting scope labels from explicit drawing metadata only."""
    text = _text(page_type, view_type, label, substrate)
    if any(token in text for token in ("soffit", "eave", "canopy underside", "balcony underside")):
        return {"section": "External", "element": "Soffits / eaves", "row_role": ""}
    if any(token in text for token in ("ceiling", "reflected ceiling", "rcp")):
        return {"section": "Internal", "element": "Ceilings", "row_role": ""}
    if any(token in text for token in ("elevation", "external", "exterior", "facade", "façade", "cladding", "render")):
        return {"section": "External", "element": "External walls / cladding", "row_role": ""}
    if "floor" in text and any(token in text for token in ("area", "plan", "footprint", "internal")):
        return {"section": "Internal", "element": "Floor area", "row_role": "floor_area"}
    return {"section": "General", "element": "Measured area", "row_role": ""}


def zone_to_takeoff_row(zone: Dict[str, Any]) -> Dict[str, Any] | None:
    """Convert one mapped zone to a safe, deterministic take-off draft row."""
    px_per_m = _num(zone.get("px_per_m"))
    area = max(0.0, _num(zone.get("area_m2")))
    if area <= 0 and px_per_m > 0:
        width_px = max(0.0, _num(zone.get("w_px")))
        height_px = max(0.0, _num(zone.get("h_px")))
        if width_px > 0 and height_px > 0:
            area = width_px * height_px / (px_per_m * px_per_m)
    if area <= 0:
        return None

    context = classify_context(
        page_type=zone.get("page_type"),
        view_type=zone.get("view_type"),
        label=zone.get("name"),
        substrate=zone.get("substrate"),
    )
    calibrated = px_per_m > 0 or _num(zone.get("page_px_per_m")) > 0
    quantity_status, confidence = _measured_status(zone.get("quantity_status"), calibrated, area)
    substrate = str(zone.get("substrate") or "Other").strip() or "Other"
    finish = str(zone.get("finish_system") or "To be confirmed").strip() or "To be confirmed"
    zone_id = int(_num(zone.get("id"), 0))
    original_ref = str(zone.get("source_reference") or "").strip()
    source_ref = f"{SOURCE_PREFIX} · zone:{zone_id}"
    if original_ref:
        source_ref += f" · {original_ref}"
    is_floor_area = context["row_role"] == "floor_area"

    return {
        "section": context["section"],
        "element": context["element"],
        "location": str(zone.get("name") or zone.get("page_label") or f"Mapped zone {zone_id}"),
        "substrate": "Other" if is_floor_area else substrate,
        "finish_system": "To be confirmed" if is_floor_area else finish,
        "quantity": round(area, 2),
        "unit": "m²",
        "quantity_status": quantity_status,
        "source_page": str(zone.get("page_label") or ""),
        "source_reference": source_ref,
        "inclusion_status": "INCLUSION" if is_floor_area else "PROVISIONAL",
        "coats": 0,
        "coverage_m2_per_litre": 0,
        "productivity_m2_per_hour": 0,
        "rate_per_unit": 0,
        "confidence": confidence,
        "notes": "Rules-based no-AI draft from mapped geometry. Review painting scope, substrate and finish before pricing.",
        "row_role": context["row_role"],
    }


def measurement_to_takeoff_row(item: Dict[str, Any]) -> Dict[str, Any] | None:
    """Convert one unlinked Plan Mapper measurement into a conservative take-off row."""
    if item.get("takeoff_row_id") not in (None, "", 0, "0"):
        return None

    kind = str(item.get("kind") or "").strip().lower()
    requested_unit = str(item.get("unit") or "").strip()
    area = max(0.0, _num(item.get("area_m2")))
    length = max(0.0, _num(item.get("length_m")))
    perimeter = max(0.0, _num(item.get("perimeter_m")))

    if requested_unit == "m²" or (area > 0 and any(token in kind for token in ("area", "polygon", "rect", "box"))):
        quantity, unit = area, "m²"
    elif requested_unit == "lm" or length > 0:
        quantity, unit = (length if length > 0 else perimeter), "lm"
    elif area > 0:
        quantity, unit = area, "m²"
    elif perimeter > 0:
        quantity, unit = perimeter, "lm"
    else:
        return None
    if quantity <= 0:
        return None

    label = str(item.get("label") or "").strip()
    context = classify_context(page_type=item.get("page_type"), label=label)
    # A generic polygon on a floor plan is not automatically a floor-area pricing row.
    # Only explicit floor-area labels get that role.
    if context["row_role"] == "floor_area" and "floor" not in label.lower():
        context = {"section": "General", "element": "Measured area", "row_role": ""}

    calibrated = _num(item.get("page_px_per_m")) > 0
    quantity_status, confidence = _measured_status(item.get("quantity_status"), calibrated, quantity)
    measurement_id = int(_num(item.get("id"), 0))
    is_floor_area = context["row_role"] == "floor_area"
    return {
        "section": context["section"],
        "element": context["element"] if unit == "m²" else ("Measured lineal item" if context["element"] == "Measured area" else context["element"]),
        "location": label or str(item.get("page_label") or f"Measurement {measurement_id}"),
        "substrate": "Other",
        "finish_system": "To be confirmed",
        "quantity": round(quantity, 2),
        "unit": unit,
        "quantity_status": quantity_status,
        "source_page": str(item.get("page_label") or ""),
        "source_reference": f"{SOURCE_PREFIX} · measurement:{measurement_id}",
        "inclusion_status": "INCLUSION" if is_floor_area else "PROVISIONAL",
        "coats": 0,
        "coverage_m2_per_litre": 0,
        "productivity_m2_per_hour": 0,
        "rate_per_unit": 0,
        "confidence": confidence,
        "notes": "Rules-based no-AI draft from Plan Mapper geometry. Assign the painting substrate/finish and review scope before pricing.",
        "row_role": context["row_role"],
    }


def build_no_ai_rows(app: Any, workspace_id: int) -> List[Dict[str, Any]]:
    zones = app.lquery(
        """SELECT z.*,p.page_label,p.page_type,p.px_per_m AS page_px_per_m
           FROM mapped_zones z LEFT JOIN pages p ON p.id=z.page_id
           WHERE z.workspace_id=? ORDER BY z.id""",
        (workspace_id,),
    )
    measurements = app.lquery(
        """SELECT m.*,p.page_label,p.page_type,p.px_per_m AS page_px_per_m
           FROM measurement_lines m LEFT JOIN pages p ON p.id=m.page_id
           WHERE m.workspace_id=? AND (m.takeoff_row_id IS NULL OR m.takeoff_row_id=0)
           ORDER BY m.id""",
        (workspace_id,),
    )
    rows: List[Dict[str, Any]] = []
    for zone in zones:
        row = zone_to_takeoff_row(dict(zone))
        if row:
            rows.append(row)
    for measurement in measurements:
        row = measurement_to_takeoff_row(dict(measurement))
        if row:
            rows.append(row)
    return rows


def _takeoff_values(workspace_id: int, row: Dict[str, Any], stamp: str) -> Tuple[Any, ...]:
    return (
        workspace_id,
        row.get("section", ""), row.get("element", ""), row.get("location", ""),
        row.get("substrate", "Other"), row.get("finish_system", "To be confirmed"),
        _num(row.get("quantity")), row.get("unit", "m²"), row.get("quantity_status", "To measure"),
        row.get("source_page", ""), row.get("source_reference", ""), row.get("inclusion_status", "PROVISIONAL"),
        _num(row.get("coats")), _num(row.get("coverage_m2_per_litre")),
        _num(row.get("productivity_m2_per_hour")), _num(row.get("rate_per_unit")),
        row.get("confidence", "To review"), row.get("notes", ""), row.get("row_role", ""),
        stamp, stamp,
    )


def replace_no_ai_rows(app: Any, workspace_id: int, rows: Sequence[Dict[str, Any]]) -> None:
    """Atomically replace only rows generated by the no-AI rules engine."""
    conn = app.local_connect()
    try:
        conn.execute(
            "DELETE FROM takeoff_rows WHERE workspace_id=? AND source_reference LIKE ?",
            (workspace_id, SOURCE_PREFIX + "%"),
        )
        stamp = app.now_stamp()
        conn.executemany(
            _TAKEOFF_INSERT_SQL,
            (_takeoff_values(workspace_id, row, stamp) for row in rows),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_schedule_batched(app: Any, workspace_id: int, rows: Iterable[Dict[str, Any]]) -> int:
    """Save the edited take-off schedule without inventing default rates or paint systems."""
    cleaned: List[Dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        if not any(str(row.get(key) or "").strip() for key in ("section", "element", "location", "source_reference")):
            continue
        role = str(row.get("row_role") or "").strip()
        row["row_role"] = role if role in {"", "floor_area", "studio_area", "model_surface"} else ""
        cleaned.append(row)

    conn = app.local_connect()
    try:
        conn.execute("DELETE FROM takeoff_rows WHERE workspace_id=?", (workspace_id,))
        stamp = app.now_stamp()
        conn.executemany(
            _TAKEOFF_INSERT_SQL,
            (_takeoff_values(workspace_id, row, stamp) for row in cleaned),
        )
        conn.commit()
        return len(cleaned)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _draft_preview(app: Any, rows: Sequence[Dict[str, Any]]):
    if not rows:
        return app.pd.DataFrame()
    columns = [
        "section", "element", "location", "substrate", "quantity", "unit",
        "quantity_status", "source_page", "inclusion_status", "confidence", "row_role",
    ]
    return app.pd.DataFrame(rows)[columns]


def no_ai_takeoff_panel(app: Any, workspace: Dict[str, Any]) -> None:
    workspace_id = int(workspace["id"])
    app.st.markdown("### Take-off without AI")
    app.st.caption(
        "PlanReader can build the take-off directly from calibrated geometry. AI is not required. "
        "Measure areas/lines in Plan Mapper or Takeoff Studio, then refresh the draft here."
    )

    draft = build_no_ai_rows(app, workspace_id)
    existing = app.lquery("SELECT id FROM takeoff_rows WHERE workspace_id=?", (workspace_id,))
    measured = sum(1 for row in draft if str(row.get("quantity_status")) == "Measured")
    c1, c2, c3, c4 = app.st.columns(4)
    c1.metric("Measured geometry", len(draft))
    c2.metric("Measured-ready", measured)
    c3.metric("Current take-off rows", len(existing))
    c4.metric("AI required", "No")

    build_tab, schedule_tab, guidance_tab = app.st.tabs([
        "Build from measurements", "Take-off schedule", "What to measure",
    ])

    with build_tab:
        if not draft:
            app.st.info(
                "There is no unlinked measured geometry to import yet. Use Plan Mapper to calibrate the drawing and draw areas/lines, "
                "or use Takeoff Studio for elevation/substrate polygons. Existing manually entered take-off rows are not affected."
            )
        else:
            app.st.dataframe(_draft_preview(app, draft), use_container_width=True, hide_index=True, height=360)
            app.st.caption(
                "This rules-based draft uses only saved geometry and drawing metadata. Unknown substrates stay 'Other', "
                "unknown finishes stay 'To be confirmed', and rates/coats/productivity remain zero until reviewed."
            )
        if app.st.button(
            "Build / Refresh take-off from measurements",
            type="primary",
            use_container_width=True,
            disabled=not bool(draft),
            key=f"no_ai_build_{workspace_id}",
        ):
            replace_no_ai_rows(app, workspace_id, draft)
            app.st.success(f"No-AI take-off refreshed from {len(draft)} measured geometry item(s). Existing manual/AI/Studio rows were preserved.")
            app.st.rerun()

    with schedule_tab:
        takeoff = app.ldf("SELECT * FROM takeoff_rows WHERE workspace_id=? ORDER BY id", (workspace_id,))
        editor_cols = ["id"] + list(app.TAKEOFF_COLUMNS) + ["row_role"]
        if takeoff.empty:
            takeoff = app.pd.DataFrame(columns=editor_cols)
        edited = app.st.data_editor(
            takeoff.reindex(columns=editor_cols),
            use_container_width=True,
            hide_index=True,
            num_rows="dynamic",
            height=560,
            key=f"no_ai_takeoff_editor_{workspace_id}",
            column_config={
                "id": app.st.column_config.NumberColumn(disabled=True),
                "substrate": app.st.column_config.SelectboxColumn(options=app.SUBSTRATES),
                "finish_system": app.st.column_config.SelectboxColumn(options=app.FINISH_SYSTEMS),
                "unit": app.st.column_config.SelectboxColumn(options=app.UNIT_OPTIONS),
                "quantity_status": app.st.column_config.SelectboxColumn(options=app.STATUS_OPTIONS),
                "inclusion_status": app.st.column_config.SelectboxColumn(options=app.INCLUSION_OPTIONS),
                "row_role": app.st.column_config.SelectboxColumn(options=["", "floor_area", "studio_area", "model_surface"], required=False),
            },
        )
        app.st.caption("You can add/edit rows here with no API key. Saving does not auto-apply rates or coating systems.")
        if app.st.button("Save no-AI take-off schedule", type="primary", use_container_width=True, key=f"no_ai_save_{workspace_id}"):
            count = save_schedule_batched(app, workspace_id, edited.to_dict("records"))
            app.st.success(f"Saved {count} take-off row(s).")
            app.st.rerun()

    with guidance_tab:
        app.st.markdown(
            "**Fast no-AI workflow:** 1) process the PDF, 2) calibrate each drawing page, 3) draw floor/elevation/soffit areas or lineal items, "
            "4) return here and click **Build / Refresh take-off from measurements**, 5) assign any unknown substrate/finish in the schedule."
        )
        app.st.info(
            "AI can still help read schedules/specifications and suggest scope, but it is optional. Geometry remains the source of truth for measured quantities."
        )


def apply(app: Any) -> None:
    """Make the non-AI take-off the default Subscription Take-off workflow."""
    if getattr(app, "_pb_no_ai_takeoff_v1216_applied", False):
        return
    app._pb_no_ai_takeoff_v1216_applied = True

    base_page = app.subscription_takeoff_page
    app.build_no_ai_takeoff_rows = lambda workspace_id: build_no_ai_rows(app, int(workspace_id))
    app.replace_no_ai_takeoff_rows = lambda workspace_id, rows: replace_no_ai_rows(app, int(workspace_id), rows)

    def _v1216_subscription_takeoff_page(workspace, session_api_key="", ai_provider="OpenAI"):
        app.hero(workspace)
        workspace_id = int(workspace["id"])
        mode = app.st.radio(
            "Take-off method",
            ["No AI — measured geometry", "AI assistant — optional"],
            horizontal=True,
            key=f"takeoff_method_v1216_{workspace_id}",
            help="No-AI is the default. AI is optional and only assists with drawing/specification interpretation.",
        )
        if mode.startswith("No AI"):
            no_ai_takeoff_panel(app, workspace)
            return

        original_hero = app.hero
        app.hero = lambda *_args, **_kwargs: None
        try:
            base_page(workspace, session_api_key, ai_provider)
        finally:
            app.hero = original_hero

    app.subscription_takeoff_page = _v1216_subscription_takeoff_page
