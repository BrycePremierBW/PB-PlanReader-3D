"""PlanReader v1.3.4 selectable door/window deductions.

Every detected opening is a first-class record. The estimator can decide whether
that opening is deducted from gross wall area. This keeps geometry evidence and
commercial measurement policy separate: PlanReader can detect an opening
without silently deciding that it must reduce paint m2.
"""
from __future__ import annotations

import json
import math
import uuid
from typing import Any, Dict, Iterable, List

VERSION = "1.3.4"
SETTING_KEY = "opening_register_v134"
KINDS = ["Window", "Door", "Garage door", "Glazed opening", "Other opening"]


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def normalise_opening(raw: Dict[str, Any]) -> Dict[str, Any]:
    raw = dict(raw or {})
    kind = str(raw.get("kind") or "Window")
    if kind not in KINDS:
        kind = "Other opening"
    width = max(0.0, _num(raw.get("width_m")))
    height = max(0.0, _num(raw.get("height_m")))
    quantity = max(1, int(_num(raw.get("quantity"), 1)))
    return {
        "id": str(raw.get("id") or uuid.uuid4().hex[:12]),
        "kind": kind,
        "label": str(raw.get("label") or kind).strip(),
        "wall_ref": str(raw.get("wall_ref") or "Unassigned wall").strip(),
        "width_m": round(width, 4),
        "height_m": round(height, 4),
        "quantity": quantity,
        "deduct": bool(raw.get("deduct", True)),
        "source_reference": str(raw.get("source_reference") or "").strip(),
        "confidence": str(raw.get("confidence") or "To review").strip(),
    }


def opening_area_m2(opening: Dict[str, Any]) -> float:
    item = normalise_opening(opening)
    return round(item["width_m"] * item["height_m"] * item["quantity"], 4)


def deducted_area_m2(openings: Iterable[Dict[str, Any]]) -> float:
    return round(sum(opening_area_m2(item) for item in openings or [] if normalise_opening(item)["deduct"]), 4)


def net_wall_area_m2(gross_wall_m2: float, openings: Iterable[Dict[str, Any]]) -> float:
    return round(max(0.0, _num(gross_wall_m2) - deducted_area_m2(openings)), 4)


def openings_by_wall(openings: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    result: Dict[str, List[Dict[str, Any]]] = {}
    for raw in openings or []:
        item = normalise_opening(raw)
        result.setdefault(item["wall_ref"], []).append(item)
    return result


def _load(app: Any, workspace_id: int) -> List[Dict[str, Any]]:
    raw = app.workspace_setting(workspace_id, SETTING_KEY, "[]")
    try:
        parsed = json.loads(str(raw or "[]"))
    except Exception:
        parsed = []
    return [normalise_opening(item) for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def _save(app: Any, workspace_id: int, openings: Iterable[Dict[str, Any]],
          confirm_all: bool = False, confirm_ids: Iterable[Any] = ()) -> None:
    # `confirm_all` / `confirm_ids` carry the estimator's explicit action intent
    # for the production safety fence: which openings are confirmed manual
    # decisions (Save choices / Deduct all / Deduct none / the Added opening).
    # The raw persister below only stores normalised rows; the fence wrapper
    # (safe_save) uses the intent to stamp manual_override_confirmed precisely.
    payload = [normalise_opening(item) for item in openings or []]
    app.set_workspace_setting(workspace_id, SETTING_KEY, json.dumps(payload, separators=(",", ":")))


def opening_panel(app: Any, workspace: Dict[str, Any]) -> None:
    wid = int(workspace["id"])
    openings = _load(app, wid)
    app.st.markdown("### Door & window deductions")
    app.st.caption(
        "Every opening is independently selectable. Tick Deduct to subtract it from wall m²; untick it to leave the gross wall area unchanged. "
        "Detected opening geometry can be added automatically by the elevation-registration engine and remains estimator-editable here."
    )

    c1, c2, c3 = app.st.columns(3)
    c1.metric("Openings", len(openings))
    c2.metric("Selected deductions", sum(1 for item in openings if item["deduct"]))
    c3.metric("Deducted area", f"{deducted_area_m2(openings):,.2f} m²")

    if openings:
        rows = []
        for item in openings:
            row = dict(item)
            row["area_m2"] = opening_area_m2(item)
            rows.append(row)
        df = app.pd.DataFrame(rows)
        edited = app.st.data_editor(
            df[["id", "kind", "label", "wall_ref", "width_m", "height_m", "quantity", "area_m2", "deduct", "confidence", "source_reference"]],
            use_container_width=True,
            hide_index=True,
            disabled=["id", "area_m2"],
            column_config={
                "deduct": app.st.column_config.CheckboxColumn("Deduct from wall m²", help="Untick to keep this opening visible but not subtract its area."),
                "kind": app.st.column_config.SelectboxColumn("Type", options=KINDS),
                "width_m": app.st.column_config.NumberColumn("Width (m)", min_value=0.0, step=0.01, format="%.3f"),
                "height_m": app.st.column_config.NumberColumn("Height (m)", min_value=0.0, step=0.01, format="%.3f"),
                "quantity": app.st.column_config.NumberColumn("Qty", min_value=1, step=1),
                "area_m2": app.st.column_config.NumberColumn("Opening m²", format="%.3f"),
            },
            key=f"opening_editor_{wid}",
        )
        actions = app.st.columns(4)
        if actions[0].button("Save opening choices", type="primary", key=f"save_openings_{wid}"):
            records = edited.to_dict("records")
            _save(app, wid, records, confirm_all=True)
            app.st.success("Door/window deduction choices saved.")
            app.st.rerun()
        if actions[1].button("Deduct all", key=f"deduct_all_{wid}"):
            for item in openings:
                item["deduct"] = True
            _save(app, wid, openings, confirm_all=True)
            app.st.rerun()
        if actions[2].button("Deduct none", key=f"deduct_none_{wid}"):
            for item in openings:
                item["deduct"] = False
            _save(app, wid, openings, confirm_all=True)
            app.st.rerun()
        if actions[3].button("Remove all", key=f"remove_openings_{wid}"):
            _save(app, wid, [])
            app.st.rerun()

        grouped = openings_by_wall(openings)
        summary = [
            {
                "Wall": wall,
                "Openings": len(items),
                "Deducted openings": sum(1 for item in items if item["deduct"]),
                "Deduction m²": deducted_area_m2(items),
            }
            for wall, items in grouped.items()
        ]
        app.st.dataframe(app.pd.DataFrame(summary), use_container_width=True, hide_index=True)
    else:
        app.st.info("No door/window openings are registered yet. Add one manually now, or let the elevation reader populate this register when openings are detected.")

    with app.st.expander("Add opening manually", expanded=not openings):
        cols = app.st.columns(4)
        kind = cols[0].selectbox("Type", KINDS, key=f"new_opening_kind_{wid}")
        label = cols[1].text_input("Label", value="", placeholder="W01 / D03", key=f"new_opening_label_{wid}")
        wall_ref = cols[2].text_input("Wall / elevation", value="", placeholder="North · wall N04", key=f"new_opening_wall_{wid}")
        deduct = cols[3].toggle("Deduct from m²", value=True, key=f"new_opening_deduct_{wid}")
        dims = app.st.columns(3)
        width = dims[0].number_input("Width (m)", min_value=0.0, value=0.0, step=0.01, key=f"new_opening_width_{wid}")
        height = dims[1].number_input("Height (m)", min_value=0.0, value=0.0, step=0.01, key=f"new_opening_height_{wid}")
        quantity = dims[2].number_input("Quantity", min_value=1, value=1, step=1, key=f"new_opening_qty_{wid}")
        if app.st.button("Add opening", key=f"add_opening_{wid}"):
            new_item = normalise_opening({
                "kind": kind,
                "label": label or kind,
                "wall_ref": wall_ref or "Unassigned wall",
                "width_m": width,
                "height_m": height,
                "quantity": quantity,
                "deduct": deduct,
                "confidence": "Manual estimator entry",
            })
            openings.append(new_item)
            # Only the newly added opening is a confirmed manual decision;
            # existing openings keep their own (possibly no) override state.
            _save(app, wid, openings, confirm_ids={new_item["id"]})
            app.st.rerun()


def apply(app: Any) -> None:
    if getattr(app, "_pb_opening_deductions_v134_applied", False):
        return
    app._pb_opening_deductions_v134_applied = True
    base_model_page = app.model_3d_page

    def _model_page_with_openings(workspace, session_api_key="", ai_provider="OpenAI"):
        with app.st.expander("Openings · choose what deducts from m²", expanded=True):
            opening_panel(app, workspace)
        return base_model_page(workspace, session_api_key, ai_provider)

    app.opening_area_m2 = opening_area_m2
    app.deducted_opening_area_m2 = deducted_area_m2
    app.net_wall_area_m2 = net_wall_area_m2
    app.openings_by_wall = openings_by_wall
    app.model_3d_page = _model_page_with_openings
