"""PlanReader v1.5.3 take-off colour overrides.

Colours are estimator display metadata only.  This module never changes scale,
geometry, quantity, confidence, or deduction logic.

Precedence for new mapper shapes:
    take-off row colour > substrate colour > existing automatic colour
An explicitly coloured saved box/shape is then preserved independently.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict

SETTING_KEY = "takeoff_colour_overrides_v153"
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def normalise_colour(value: Any, fallback: str = "#1F6FB2") -> str:
    text = str(value or "").strip()
    if _HEX.match(text):
        return text.upper()
    return str(fallback or "#1F6FB2").upper()


def _empty() -> Dict[str, Dict[str, str]]:
    return {"substrates": {}, "rows": {}, "boxes": {}}


def load_overrides(app: Any, workspace_id: int) -> Dict[str, Dict[str, str]]:
    try:
        raw = app.workspace_setting(int(workspace_id), SETTING_KEY, "{}")
        parsed = json.loads(str(raw or "{}"))
    except Exception:
        parsed = {}
    result = _empty()
    if isinstance(parsed, dict):
        for bucket in result:
            values = parsed.get(bucket)
            if isinstance(values, dict):
                result[bucket] = {
                    str(k): normalise_colour(v)
                    for k, v in values.items()
                    if _HEX.match(str(v or "").strip())
                }
    return result


def save_overrides(app: Any, workspace_id: int, overrides: Dict[str, Any]) -> None:
    app.set_workspace_setting(
        int(workspace_id), SETTING_KEY,
        json.dumps(overrides, separators=(",", ":"), sort_keys=True),
    )


def resolve_colour(default: str, substrate: str, row_id: Any, overrides: Dict[str, Any]) -> str:
    default = normalise_colour(default)
    substrate_colour = (overrides.get("substrates") or {}).get(str(substrate or ""))
    row_colour = (overrides.get("rows") or {}).get(str(row_id or ""))
    return normalise_colour(row_colour or substrate_colour or default, default)


def _reapply_box_colours(app: Any, workspace_id: int, overrides: Dict[str, Any]) -> None:
    for box_id, colour in (overrides.get("boxes") or {}).items():
        try:
            app.lexecute(
                "UPDATE measurement_lines SET colour=? WHERE workspace_id=? AND id=?",
                (normalise_colour(colour), int(workspace_id), int(box_id)),
            )
        except Exception:
            continue


def _apply_row_colour_to_saved(app: Any, workspace_id: int, row_id: int, colour: str, overrides: Dict[str, Any]) -> None:
    app.lexecute(
        "UPDATE measurement_lines SET colour=? WHERE workspace_id=? AND takeoff_row_id=?",
        (normalise_colour(colour), int(workspace_id), int(row_id)),
    )
    _reapply_box_colours(app, workspace_id, overrides)


def _apply_substrate_colour_to_saved(app: Any, workspace_id: int, substrate: str, colour: str, overrides: Dict[str, Any]) -> None:
    rows = app.lquery(
        "SELECT id FROM takeoff_rows WHERE workspace_id=? AND COALESCE(substrate,'')=?",
        (int(workspace_id), str(substrate or "")),
    )
    for row in rows:
        row_id = int(row["id"])
        # Explicit row colours outrank substrate colours.
        row_colour = (overrides.get("rows") or {}).get(str(row_id))
        _apply_row_colour_to_saved(app, workspace_id, row_id, row_colour or colour, overrides)
    _reapply_box_colours(app, workspace_id, overrides)


def apply(app: Any) -> None:
    if getattr(app, "_pb_takeoff_colours_v153_applied", False):
        return

    base_targets = app.takeoff_rows_for_mapper
    base_auto_map = app.auto_map_measurements
    base_mapper_page = app.plan_mapper_page

    def coloured_targets(workspace_id: int):
        rows = base_targets(workspace_id)
        overrides = load_overrides(app, int(workspace_id))
        for row in rows:
            row["colour"] = resolve_colour(
                str(row.get("colour") or "#1F6FB2"),
                str(row.get("substrate") or ""),
                row.get("id"),
                overrides,
            )
        return rows

    def coloured_auto_map(workspace_id: int, page_id: int, px_per_m: float):
        shapes = base_auto_map(workspace_id, page_id, px_per_m)
        overrides = load_overrides(app, int(workspace_id))
        rows = {
            int(r["id"]): dict(r)
            for r in app.lquery("SELECT id,substrate FROM takeoff_rows WHERE workspace_id=?", (int(workspace_id),))
        }
        for shape in shapes:
            row_id = shape.get("takeoff_row_id")
            try:
                row = rows.get(int(row_id), {})
            except (TypeError, ValueError):
                row = {}
            shape["colour"] = resolve_colour(
                str(shape.get("colour") or "#1F6FB2"),
                str(row.get("substrate") or ""),
                row_id,
                overrides,
            )
        return shapes

    def mapper_with_colours(workspace: Dict[str, Any]) -> None:
        base_mapper_page(workspace)
        wid = int(workspace["id"])
        overrides = load_overrides(app, wid)

        app.st.markdown("### 🎨 Take-off colours")
        app.st.caption("Colours are visual only. They do not alter any measurement or quantity.")
        tab_sub, tab_row, tab_box = app.st.tabs(["Substrate", "Take-off row", "Individual box"])

        with tab_sub:
            substrate_rows = app.lquery(
                "SELECT DISTINCT COALESCE(substrate,'') AS substrate FROM takeoff_rows WHERE workspace_id=? ORDER BY substrate",
                (wid,),
            )
            substrates = [str(r["substrate"] or "") for r in substrate_rows if str(r["substrate"] or "").strip()]
            if not substrates:
                app.st.caption("No substrates are available yet.")
            else:
                substrate = app.st.selectbox("Substrate", substrates, key=f"pb_colour_sub_{wid}")
                existing = (overrides.get("substrates") or {}).get(substrate, "#1F6FB2")
                colour = app.st.color_picker("Substrate colour", value=normalise_colour(existing), key=f"pb_colour_sub_pick_{wid}_{substrate}")
                c1, c2 = app.st.columns(2)
                if c1.button("Save substrate colour", type="primary", use_container_width=True, key=f"pb_colour_sub_save_{wid}"):
                    overrides.setdefault("substrates", {})[substrate] = normalise_colour(colour)
                    save_overrides(app, wid, overrides)
                    _apply_substrate_colour_to_saved(app, wid, substrate, colour, overrides)
                    app.st.success("Substrate colour saved and applied to its mapped boxes.")
                    app.st.rerun()
                if c2.button("Reset substrate", use_container_width=True, key=f"pb_colour_sub_reset_{wid}"):
                    overrides.setdefault("substrates", {}).pop(substrate, None)
                    save_overrides(app, wid, overrides)
                    app.st.rerun()

        with tab_row:
            takeoff_rows = [dict(r) for r in app.lquery(
                "SELECT id,section,element,location,substrate FROM takeoff_rows WHERE workspace_id=? ORDER BY section,element,location,id",
                (wid,),
            )]
            if not takeoff_rows:
                app.st.caption("No take-off rows are available yet.")
            else:
                ids = [int(r["id"]) for r in takeoff_rows]
                labels = {
                    int(r["id"]): " · ".join(
                        part for part in [str(r.get("section") or ""), str(r.get("element") or ""), str(r.get("location") or "")]
                        if part
                    ) or f"Row {r['id']}"
                    for r in takeoff_rows
                }
                row_id = app.st.selectbox("Take-off row", ids, format_func=lambda value: labels.get(value, str(value)), key=f"pb_colour_row_{wid}")
                row = next(r for r in takeoff_rows if int(r["id"]) == int(row_id))
                default = app.line_colour_for(row.get("section"), row.get("element"))
                current = resolve_colour(default, str(row.get("substrate") or ""), row_id, overrides)
                colour = app.st.color_picker("Row colour", value=current, key=f"pb_colour_row_pick_{wid}_{row_id}")
                c1, c2 = app.st.columns(2)
                if c1.button("Save row colour", type="primary", use_container_width=True, key=f"pb_colour_row_save_{wid}"):
                    overrides.setdefault("rows", {})[str(row_id)] = normalise_colour(colour)
                    save_overrides(app, wid, overrides)
                    _apply_row_colour_to_saved(app, wid, int(row_id), colour, overrides)
                    app.st.success("Row colour saved. New and existing mapped boxes use it.")
                    app.st.rerun()
                if c2.button("Reset row", use_container_width=True, key=f"pb_colour_row_reset_{wid}"):
                    overrides.setdefault("rows", {}).pop(str(row_id), None)
                    save_overrides(app, wid, overrides)
                    app.st.rerun()

        with tab_box:
            boxes = [dict(r) for r in app.lquery(
                """SELECT ml.id,ml.label,ml.colour,ml.page_id,ml.takeoff_row_id,
                          COALESCE(p.page_label,'') AS page_label,
                          COALESCE(t.element,'') AS element
                   FROM measurement_lines ml
                   LEFT JOIN pages p ON p.id=ml.page_id
                   LEFT JOIN takeoff_rows t ON t.id=ml.takeoff_row_id
                   WHERE ml.workspace_id=? ORDER BY ml.page_id,ml.id""",
                (wid,),
            )]
            if not boxes:
                app.st.caption("Draw/save a measurement box or shape first, then its individual colour can be changed here.")
            else:
                box_ids = [int(r["id"]) for r in boxes]
                labels = {
                    int(r["id"]): f"{r.get('page_label') or 'Page'} · {r.get('label') or r.get('element') or 'Shape'} · #{r['id']}"
                    for r in boxes
                }
                box_id = app.st.selectbox("Mapped box / shape", box_ids, format_func=lambda value: labels.get(value, str(value)), key=f"pb_colour_box_{wid}")
                box = next(r for r in boxes if int(r["id"]) == int(box_id))
                current = (overrides.get("boxes") or {}).get(str(box_id), str(box.get("colour") or "#1F6FB2"))
                colour = app.st.color_picker("Box colour", value=normalise_colour(current), key=f"pb_colour_box_pick_{wid}_{box_id}")
                c1, c2 = app.st.columns(2)
                if c1.button("Save box colour", type="primary", use_container_width=True, key=f"pb_colour_box_save_{wid}"):
                    overrides.setdefault("boxes", {})[str(box_id)] = normalise_colour(colour)
                    save_overrides(app, wid, overrides)
                    app.lexecute("UPDATE measurement_lines SET colour=? WHERE workspace_id=? AND id=?", (normalise_colour(colour), wid, int(box_id)))
                    app.st.success("Individual box colour saved.")
                    app.st.rerun()
                if c2.button("Reset box", use_container_width=True, key=f"pb_colour_box_reset_{wid}"):
                    overrides.setdefault("boxes", {}).pop(str(box_id), None)
                    save_overrides(app, wid, overrides)
                    # Restore inherited row/substrate/default colour.
                    row_id = box.get("takeoff_row_id")
                    row_data = app.lquery("SELECT id,section,element,substrate FROM takeoff_rows WHERE workspace_id=? AND id=?", (wid, row_id)) if row_id else []
                    if row_data:
                        r = dict(row_data[0])
                        inherited = resolve_colour(app.line_colour_for(r.get("section"), r.get("element")), str(r.get("substrate") or ""), r.get("id"), overrides)
                        app.lexecute("UPDATE measurement_lines SET colour=? WHERE workspace_id=? AND id=?", (inherited, wid, int(box_id)))
                    app.st.rerun()

    app.takeoff_rows_for_mapper = coloured_targets
    app.auto_map_measurements = coloured_auto_map
    app.plan_mapper_page = mapper_with_colours
    app._pb_takeoff_colours_v153_applied = True
