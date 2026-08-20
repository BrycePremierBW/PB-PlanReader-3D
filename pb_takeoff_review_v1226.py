"""PlanReader v1.2.26 take-off review tools.

Adds three estimator-facing controls to the no-AI/PB take-off workflow:
1. merge compatible take-off rows while retaining all source evidence;
2. show source page(s) and highlighted box/polygon for every take-off row;
3. edit/draw m² polygons directly over the source drawing and recalculate the
   quantity from the page calibration. A saved manual polygon becomes authoritative
   and is protected from later automatic regeneration.
"""
from __future__ import annotations

import io
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw

import pb_3d_surface_editor_v1212 as surface
import pb_memory_stability_v1220 as memory
import pb_no_ai_takeoff_v1216 as noai
import pb_premier_takeoff_v1225 as premier
import pb_selected_evidence_floor_v1226 as selected
import pb_takeoff_studio_v1211 as studio

try:
    from planreader_takeoff_studio import takeoff_studio_editor
except Exception:  # pragma: no cover
    takeoff_studio_editor = None

VERSION = "1.2.26"
MANUAL_PREFIX = f"PB Manual Polygon v{VERSION}"
MERGE_PREFIX = f"PB Merge v{VERSION}"
MERGED_INPUTS_KEY = "merged_takeoff_source_refs_v1226"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _json(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except Exception:
        return fallback


def _unique(values: Iterable[Any]) -> List[str]:
    out = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _load_provenance(app: Any, workspace_id: int) -> Dict[str, Any]:
    return selected._load_provenance(app, int(workspace_id))


def _save_provenance(app: Any, workspace_id: int, state: Dict[str, Any]) -> None:
    selected._save_provenance(app, int(workspace_id), state)


def _page(app: Any, workspace_id: int, *, page_id: int = 0, page_label: str = "") -> Optional[Dict[str, Any]]:
    if page_id:
        rows = app.lquery(
            "SELECT id,page_label,page_type,image_path,width_px,height_px,px_per_m,selected FROM pages WHERE workspace_id=? AND id=?",
            (int(workspace_id), int(page_id)),
        )
    else:
        rows = app.lquery(
            """SELECT id,page_label,page_type,image_path,width_px,height_px,px_per_m,selected
               FROM pages WHERE workspace_id=? AND page_label=? AND COALESCE(selected,0)=1 ORDER BY id LIMIT 1""",
            (int(workspace_id), str(page_label or "")),
        )
    return dict(rows[0]) if rows else None


def _points_from_pixels(points: Sequence[Any], page: Dict[str, Any]) -> List[Dict[str, float]]:
    width = _num(page.get("width_px")); height = _num(page.get("height_px"))
    if width <= 0 or height <= 0:
        return []
    out = []
    for raw in points or []:
        if isinstance(raw, dict):
            x, y = _num(raw.get("x")), _num(raw.get("y"))
        elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
            x, y = _num(raw[0]), _num(raw[1])
        else:
            continue
        out.append({"x": max(0.0, min(100.0, x / width * 100.0)), "y": max(0.0, min(100.0, y / height * 100.0))})
    return out


def _bbox_percent(bbox: Sequence[Any], page: Dict[str, Any], mode: str = "xyxy") -> List[Dict[str, float]]:
    if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
        return []
    x0, y0, a, b = map(_num, bbox[:4])
    x1, y1 = (x0 + a, y0 + b) if mode == "xywh" else (a, b)
    return _points_from_pixels([[x0,y0],[x1,y0],[x1,y1],[x0,y1]], page)


def _studio_source(app: Any, workspace_id: int, source_reference: str) -> List[Dict[str, Any]]:
    match = re.search(r"PB Takeoff Studio v1\.2\.11\s*·\s*page:(\d+)\s*·\s*area:([^·]+)", source_reference)
    if not match:
        return []
    page_id, area_id = int(match.group(1)), match.group(2).strip()
    page = _page(app, workspace_id, page_id=page_id)
    if not page:
        return []
    state = _json(app.workspace_setting(workspace_id, f"takeoff_studio_v1211_page_{page_id}", "{}"), {})
    area = next((dict(item) for item in state.get("areas") or [] if str(item.get("id")) == area_id), None)
    if not area:
        return [{"page_id": page_id, "page_label": page.get("page_label"), "source_kind": "Takeoff Studio", "points": [], "evidence_text": source_reference}]
    return [{
        "page_id": page_id, "page_label": page.get("page_label"), "source_kind": "Takeoff Studio",
        "points": list(area.get("points") or []), "evidence_text": str(area.get("notes") or source_reference),
    }]


def _zone_source(app: Any, workspace_id: int, source_reference: str) -> List[Dict[str, Any]]:
    match = re.search(r"\bzone:(\d+)\b", source_reference)
    if not match:
        return []
    rows = app.lquery(
        """SELECT z.*,p.page_label,p.width_px,p.height_px,p.px_per_m,p.selected FROM mapped_zones z
           JOIN pages p ON p.id=z.page_id WHERE z.workspace_id=? AND z.id=?""",
        (int(workspace_id), int(match.group(1))),
    )
    if not rows:
        return []
    row = dict(rows[0]); page = row
    raw_points = _json(row.get("polygon_json"), [])
    points = _points_from_pixels(raw_points, page)
    if not points and _num(row.get("w_px")) > 0 and _num(row.get("h_px")) > 0:
        x, y, w, h = map(_num, [row.get("x_px"),row.get("y_px"),row.get("w_px"),row.get("h_px")])
        points = _points_from_pixels([[x,y],[x+w,y],[x+w,y+h],[x,y+h]], page)
    return [{
        "page_id": int(row.get("page_id") or 0), "page_label": row.get("page_label"), "source_kind": "Mapped zone",
        "points": points, "evidence_text": str(row.get("source_reference") or row.get("name") or "Mapped zone"),
    }]


def _measurement_source(app: Any, workspace_id: int, source_reference: str) -> List[Dict[str, Any]]:
    match = re.search(r"\bmeasurement:(\d+)\b", source_reference)
    if not match:
        return []
    rows = app.lquery(
        """SELECT m.*,p.page_label,p.width_px,p.height_px,p.px_per_m,p.selected FROM measurement_lines m
           JOIN pages p ON p.id=m.page_id WHERE m.workspace_id=? AND m.id=?""",
        (int(workspace_id), int(match.group(1))),
    )
    if not rows:
        return []
    row = dict(rows[0]); raw = _json(row.get("points"), [])
    points: List[Dict[str, float]] = []
    if isinstance(raw, list) and len(raw) >= 3:
        # Existing mapper data can be pixel coordinates or percentage coordinates.
        values = []
        for item in raw:
            if isinstance(item, dict): values.append((_num(item.get("x")), _num(item.get("y"))))
            elif isinstance(item, (list, tuple)) and len(item) >= 2: values.append((_num(item[0]), _num(item[1])))
        if values and max(max(abs(x), abs(y)) for x,y in values) <= 100.0001:
            points = [{"x": x, "y": y} for x,y in values]
        else:
            points = _points_from_pixels(values, row)
    return [{
        "page_id": int(row.get("page_id") or 0), "page_label": row.get("page_label"), "source_kind": "Plan Mapper measurement",
        "points": points, "evidence_text": str(row.get("notes") or row.get("label") or "Plan Mapper measurement"),
    }]


def provenance_for_row(app: Any, workspace_id: int, row: Dict[str, Any]) -> Dict[str, Any]:
    ref = str(row.get("source_reference") or "")
    saved = _load_provenance(app, int(workspace_id)).get(ref)
    if isinstance(saved, dict):
        return dict(saved)
    sources = _studio_source(app, workspace_id, ref) or _zone_source(app, workspace_id, ref) or _measurement_source(app, workspace_id, ref)
    if not sources:
        page = _page(app, workspace_id, page_label=str(row.get("source_page") or ""))
        if page:
            sources = [{
                "page_id": int(page["id"]), "page_label": str(page.get("page_label") or ""),
                "source_kind": "Drawing reference", "points": [], "evidence_text": ref,
            }]
    return {
        "kind": "area" if str(row.get("unit") or "") == "m²" else ("line" if str(row.get("unit") or "") == "lm" else "reference"),
        "unit": str(row.get("unit") or ""), "location": str(row.get("location") or row.get("element") or ""),
        "quantity": _num(row.get("quantity")), "authoritative_source": str(row.get("confidence") or ""),
        "sources": sources,
    }


def _source_points(source: Dict[str, Any], page: Dict[str, Any]) -> List[Dict[str, float]]:
    points = source.get("points")
    if isinstance(points, list) and len(points) >= 3:
        return [{"x": _num(p.get("x") if isinstance(p,dict) else p[0]), "y": _num(p.get("y") if isinstance(p,dict) else p[1])} for p in points]
    polygon = source.get("polygon")
    if isinstance(polygon, list) and len(polygon) >= 3:
        return _points_from_pixels(polygon, page)
    for key, mode in (("boundary_bbox","xywh"),("bbox","xywh"),("text_bbox","xyxy"),("unit_bbox","xyxy")):
        bbox = source.get(key)
        if bbox:
            # Bboxes are evidence highlights, not automatically accepted measurement
            # polygons. They are used only for drawing preview below.
            return []
    return []


def _preview_bytes(page: Dict[str, Any], source: Dict[str, Any], max_edge: int = 1200) -> bytes:
    path = memory.regular_file(page.get("image_path"))
    if path is None:
        return b""
    with Image.open(path) as raw:
        image = raw.convert("RGB")
        ow, oh = image.size
        ratio = min(1.0, max_edge / float(max(ow, oh)))
        if ratio < 1.0:
            image = image.resize((max(1, round(ow * ratio)), max(1, round(oh * ratio))), Image.Resampling.LANCZOS)
        draw = ImageDraw.Draw(image)
        points = _source_points(source, page)
        if points:
            xy = [(round(p["x"] / 100.0 * image.width), round(p["y"] / 100.0 * image.height)) for p in points]
            if len(xy) >= 3:
                draw.polygon(xy, outline=(220,35,35), fill=(220,35,35,35) if image.mode == "RGBA" else None)
                draw.line(xy + [xy[0]], fill=(220,35,35), width=4)
        else:
            bbox = source.get("text_bbox") or source.get("unit_bbox") or source.get("bbox") or source.get("boundary_bbox")
            if isinstance(bbox, (list,tuple)) and len(bbox) >= 4:
                mode = "xywh" if source.get("boundary_bbox") or source.get("bbox") else "xyxy"
                pcts = _bbox_percent(bbox, page, mode)
                if pcts:
                    xy = [(round(p["x"] / 100.0 * image.width), round(p["y"] / 100.0 * image.height)) for p in pcts]
                    draw.line(xy + [xy[0]], fill=(220,35,35), width=4)
        buffer = io.BytesIO(); image.save(buffer, format="JPEG", quality=82); return buffer.getvalue()


def _row_label(row: Dict[str, Any]) -> str:
    return f"#{int(row.get('id') or 0)} · {row.get('section') or ''} · {row.get('location') or row.get('element') or ''} · {_num(row.get('quantity')):.2f} {row.get('unit') or ''}"


def source_summary(app: Any, workspace_id: int, row: Dict[str, Any]) -> Tuple[str, str]:
    provenance = provenance_for_row(app, int(workspace_id), row)
    sources = provenance.get("sources") or []
    pages = _unique(source.get("page_label") for source in sources)
    geometry = []
    for source in sources:
        if _source_points(source, _page(app, workspace_id, page_id=int(source.get("page_id") or 0)) or {}): geometry.append("polygon")
        elif source.get("text_bbox") or source.get("bbox") or source.get("boundary_bbox") or source.get("unit_bbox"): geometry.append("box")
        else: geometry.append("page")
    return " | ".join(pages) or str(row.get("source_page") or ""), " + ".join(_unique(geometry)) or "reference"


def _merged_inputs(app: Any, workspace_id: int) -> List[str]:
    raw = _json(app.workspace_setting(int(workspace_id), MERGED_INPUTS_KEY, "[]"), [])
    return [str(value) for value in raw if str(value).strip()] if isinstance(raw, list) else []


def _set_merged_inputs(app: Any, workspace_id: int, values: Sequence[str]) -> None:
    app.set_workspace_setting(int(workspace_id), MERGED_INPUTS_KEY, json.dumps(_unique(values), separators=(",", ":")))


def cleanup_merged_inputs(app: Any, workspace_id: int) -> int:
    refs = _merged_inputs(app, int(workspace_id))
    if not refs:
        return 0
    conn = app.local_connect(); deleted = 0
    try:
        for ref in refs:
            cur = conn.execute("DELETE FROM takeoff_rows WHERE workspace_id=? AND source_reference=?", (int(workspace_id), ref))
            deleted += int(cur.rowcount or 0)
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()
    return deleted


def merge_rows(app: Any, workspace_id: int, row_ids: Sequence[int], final: Dict[str, Any]) -> int:
    ids = sorted({int(value) for value in row_ids if int(value) > 0})
    if len(ids) < 2:
        raise ValueError("Select at least two take-off rows to merge.")
    placeholders = ",".join("?" for _ in ids)
    rows = [dict(row) for row in app.lquery(f"SELECT * FROM takeoff_rows WHERE workspace_id=? AND id IN ({placeholders}) ORDER BY id", (int(workspace_id), *ids))]
    if len(rows) != len(ids):
        raise ValueError("One or more selected rows no longer exist.")
    units = {str(row.get("unit") or "") for row in rows}
    if len(units) != 1:
        raise ValueError("Only rows with the same unit can be merged. Convert/split incompatible units first.")
    unit = next(iter(units))
    quantity = sum(_num(row.get("quantity")) for row in rows)
    refs = _unique(row.get("source_reference") for row in rows)
    pages = _unique(row.get("source_page") for row in rows)
    source_ref = f"{MERGE_PREFIX} · rows:{','.join(str(i) for i in ids)}"
    all_measured = all("measur" in str(row.get("quantity_status") or "").lower() and "provisional" not in str(row.get("quantity_status") or "").lower() for row in rows)
    status = "Measured" if all_measured else "Provisional measured"
    confidence = "Measured" if all_measured else "Derived"
    notes = " | ".join(_unique(row.get("notes") for row in rows))
    notes = (notes + " | " if notes else "") + f"Merged from take-off rows {', '.join(str(i) for i in ids)}; all source pages/geometry retained in provenance."

    conn = app.local_connect()
    try:
        cur = conn.execute(
            """INSERT INTO takeoff_rows(workspace_id,section,element,location,substrate,finish_system,quantity,unit,quantity_status,
               source_page,source_reference,inclusion_status,coats,coverage_m2_per_litre,productivity_m2_per_hour,rate_per_unit,
               confidence,notes,row_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(workspace_id), str(final.get("section") or rows[0].get("section") or ""),
                str(final.get("element") or rows[0].get("element") or ""), str(final.get("location") or rows[0].get("location") or ""),
                str(final.get("substrate") or rows[0].get("substrate") or "Other"), str(final.get("finish_system") or rows[0].get("finish_system") or "To be confirmed"),
                quantity, unit, status, pages[0] if pages else "", source_ref,
                str(final.get("inclusion_status") or rows[0].get("inclusion_status") or "INCLUSION"),
                _num(final.get("coats"), _num(rows[0].get("coats"))), _num(final.get("coverage_m2_per_litre"), _num(rows[0].get("coverage_m2_per_litre"))),
                _num(final.get("productivity_m2_per_hour"), _num(rows[0].get("productivity_m2_per_hour"))),
                _num(final.get("rate_per_unit"), _num(rows[0].get("rate_per_unit"))), confidence, notes,
                str(final.get("row_role") or rows[0].get("row_role") or ""), app.now_stamp(), app.now_stamp(),
            ),
        )
        new_id = int(cur.lastrowid)
        conn.execute(f"UPDATE measurement_lines SET takeoff_row_id=? WHERE workspace_id=? AND takeoff_row_id IN ({placeholders})", (new_id, int(workspace_id), *ids))
        conn.execute(f"DELETE FROM takeoff_rows WHERE workspace_id=? AND id IN ({placeholders})", (int(workspace_id), *ids))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()

    provenance = _load_provenance(app, int(workspace_id)); sources = []
    for row in rows:
        item = provenance_for_row(app, int(workspace_id), row)
        sources.extend(dict(source) for source in item.get("sources") or [])
    provenance[source_ref] = {"kind": "area" if unit == "m²" else "merged", "unit": unit, "location": str(final.get("location") or rows[0].get("location") or ""), "quantity": quantity, "authoritative_source": "merged estimator row", "sources": sources, "merged_refs": refs}
    _save_provenance(app, int(workspace_id), provenance)
    _set_merged_inputs(app, int(workspace_id), _merged_inputs(app, int(workspace_id)) + refs)
    return new_id


def save_polygon_override(app: Any, workspace_id: int, row: Dict[str, Any], page: Dict[str, Any], areas: Sequence[Dict[str, Any]], original_provenance: Dict[str, Any]) -> float:
    if str(row.get("unit") or "") != "m²":
        raise ValueError("Editable polygons recalculate m² rows only.")
    pxpm = _num(page.get("px_per_m")); width = _num(page.get("width_px")); height = _num(page.get("height_px"))
    if pxpm <= 0 or width <= 0 or height <= 0:
        raise ValueError("This source page must be calibrated before its polygon can recalculate m².")
    normalised = studio.normalise_studio_areas(areas or [], width_px=width, height_px=height, px_per_m=pxpm, view_label=str(page.get("page_label") or ""))
    active = [area for area in normalised if str(area.get("status") or "") != "Excluded" and _num(area.get("area_m2")) > 0]
    if not active:
        raise ValueError("Draw or retain at least one valid polygon before saving.")
    quantity = round(sum(_num(area.get("area_m2")) for area in active), 2)
    old_ref = str(row.get("source_reference") or "")
    new_ref = f"{MANUAL_PREFIX} · row:{int(row['id'])} · from:{old_ref[:180]}"
    note = str(row.get("notes") or "").strip()
    if note: note += " | "
    note += f"Estimator-adjusted polygon on {page.get('page_label')}; m² recalculated from calibrated page geometry."
    app.lexecute(
        "UPDATE takeoff_rows SET quantity=?,quantity_status='Measured',confidence='Measured',source_page=?,source_reference=?,notes=?,updated_at=? WHERE id=? AND workspace_id=?",
        (quantity, str(page.get("page_label") or ""), new_ref, note, app.now_stamp(), int(row["id"]), int(workspace_id)),
    )
    provenance = _load_provenance(app, int(workspace_id))
    provenance[new_ref] = {
        "kind": "area", "unit": "m²", "location": str(row.get("location") or row.get("element") or ""),
        "quantity": quantity, "authoritative_source": "manual polygon",
        "sources": [{
            "page_id": int(page["id"]), "page_label": str(page.get("page_label") or ""), "source_kind": "Estimator-adjusted polygon",
            "points": list(area.get("points") or []), "area_m2": _num(area.get("area_m2")), "evidence_text": "Manual polygon override",
        } for area in active],
        "derived_from": original_provenance,
    }
    _save_provenance(app, int(workspace_id), provenance)
    if old_ref:
        _set_merged_inputs(app, int(workspace_id), _merged_inputs(app, int(workspace_id)) + [old_ref])
    return quantity


def review_panel(app: Any, workspace: Dict[str, Any]) -> None:
    workspace_id = int(workspace["id"])
    rows = [dict(row) for row in app.lquery("SELECT * FROM takeoff_rows WHERE workspace_id=? ORDER BY id", (workspace_id,))]
    app.st.markdown("### Take-off source & polygon review")
    app.st.caption("Every line retains its drawing basis. Select a line to see its source page/box/polygon. For m² lines, adjust or draw the polygon and save it to recalculate the quantity from the page calibration.")
    if not rows:
        app.st.info("No take-off rows yet."); return
    summary = []
    for row in rows:
        pages, geometry = source_summary(app, workspace_id, row)
        summary.append({"ID": int(row["id"]), "Element": row.get("element"), "Location": row.get("location"), "Qty": row.get("quantity"), "Unit": row.get("unit"), "Source page(s)": pages, "Source geometry": geometry})
    app.st.dataframe(app.pd.DataFrame(summary), hide_index=True, use_container_width=True, height=min(420, 70 + len(summary) * 30))

    labels = [_row_label(row) for row in rows]
    chosen_label = app.st.selectbox("Show measurement on drawing", labels, key=f"takeoff_source_row_v1226_{workspace_id}")
    row = rows[labels.index(chosen_label)]
    provenance = provenance_for_row(app, workspace_id, row)
    sources = [dict(source) for source in provenance.get("sources") or []]
    selected_ids, _selected_labels = selected.selected_page_identity(app, workspace_id)
    sources = [source for source in sources if not int(source.get("page_id") or 0) or int(source.get("page_id") or 0) in selected_ids]
    if not sources:
        app.st.warning("This row has no active selected drawing source geometry recorded yet. Its quantity should remain under review until it is mapped to a selected page.")
    else:
        source_labels = [f"{source.get('page_label') or 'Page'} · {source.get('source_kind') or 'Source'} · {source.get('evidence_text') or ''}"[:180] for source in sources]
        selected_source = app.st.selectbox("Source page / evidence", source_labels, key=f"takeoff_source_evidence_v1226_{workspace_id}_{int(row['id'])}")
        source = sources[source_labels.index(selected_source)]
        page = _page(app, workspace_id, page_id=int(source.get("page_id") or 0), page_label=str(source.get("page_label") or ""))
        if page:
            payload = _preview_bytes(page, source)
            if payload:
                app.st.image(payload, caption=f"{page.get('page_label')} · source evidence", use_container_width=True)
            app.st.caption(f"Basis: {source.get('source_kind') or provenance.get('authoritative_source') or 'Drawing'} · {source.get('evidence_text') or ''}")

            if str(row.get("unit") or "") == "m²":
                app.st.markdown("#### Editable measurement polygon")
                if takeoff_studio_editor is None:
                    app.st.warning("Polygon editor component is unavailable in this environment.")
                elif _num(page.get("px_per_m")) <= 0:
                    app.st.warning("Calibrate this page first. The source can be displayed, but m² cannot be recalculated safely without a scale.")
                else:
                    points = _source_points(source, page)
                    initial = []
                    if points:
                        initial = [{
                            "id": f"ROW-{int(row['id'])}-1", "label": str(row.get("location") or row.get("element") or f"Row {row['id']}"),
                            "substrate": "OTHER", "elevation": str(page.get("page_label") or ""), "status": "Paint Included",
                            "progress_pct": 0, "notes": "Take-off source polygon", "manual_m2": 0, "points": points,
                        }]
                    else:
                        app.st.info("PlanReader has a source page/box but no reliable area boundary for this line yet. Use **Polygon** in the editor to draw the actual extent; do not use the red text box itself as the measured area.")
                    result = takeoff_studio_editor(
                        memory.regular_file(page.get("image_path")).read_bytes() if memory.regular_file(page.get("image_path")) else b"",
                        areas=initial, substrates=studio.SUBSTRATE_PRESETS, px_per_m=_num(page.get("px_per_m")),
                        page_type=str(page.get("page_type") or ""), view_label=str(page.get("page_label") or ""),
                        revision=int(app.st.session_state.get(f"takeoff_poly_rev_v1226_{int(row['id'])}", 0)),
                        key=f"takeoff_polygon_v1226_{workspace_id}_{int(row['id'])}_{int(page['id'])}", height=820,
                    )
                    areas = list((result or {}).get("areas") or initial)
                    if areas:
                        normalised = studio.normalise_studio_areas(areas, width_px=_num(page.get("width_px")), height_px=_num(page.get("height_px")), px_per_m=_num(page.get("px_per_m")), view_label=str(page.get("page_label") or ""))
                        total = sum(_num(area.get("area_m2")) for area in normalised if str(area.get("status") or "") != "Excluded")
                        app.st.metric("Polygon quantity", f"{total:.2f} m²", delta=f"{total - _num(row.get('quantity')):+.2f} m² vs current row")
                    if app.st.button("Save polygon & recalculate this take-off row", type="primary", use_container_width=True, key=f"save_takeoff_polygon_v1226_{int(row['id'])}"):
                        try:
                            qty = save_polygon_override(app, workspace_id, row, page, areas, provenance)
                        except ValueError as exc:
                            app.st.error(str(exc))
                        else:
                            app.st.success(f"Saved estimator polygon. Take-off row updated to {qty:.2f} m² and protected from automatic overwrite.")
                            app.st.rerun()

    app.st.divider()
    app.st.markdown("### Merge take-off rows")
    merge_options = {_row_label(row): int(row["id"]) for row in rows}
    selected_labels = app.st.multiselect("Rows to merge", list(merge_options.keys()), key=f"merge_rows_v1226_{workspace_id}")
    selected_rows = [row for row in rows if int(row["id"]) in {merge_options[label] for label in selected_labels}]
    if len(selected_rows) >= 2:
        units = {str(row.get("unit") or "") for row in selected_rows}
        if len(units) != 1:
            app.st.error("Selected rows have different units. Only compatible units can be merged.")
        else:
            first = selected_rows[0]; total = sum(_num(row.get("quantity")) for row in selected_rows)
            app.st.metric("Merged quantity", f"{total:.2f} {next(iter(units))}")
            c1, c2 = app.st.columns(2)
            final_section = c1.text_input("Final section", value=str(first.get("section") or ""), key=f"merge_section_v1226_{workspace_id}")
            final_element = c2.text_input("Final element", value=str(first.get("element") or ""), key=f"merge_element_v1226_{workspace_id}")
            final_location = c1.text_input("Final location", value=str(first.get("location") or ""), key=f"merge_location_v1226_{workspace_id}")
            final_substrate = c2.text_input("Final substrate", value=str(first.get("substrate") or "Other"), key=f"merge_substrate_v1226_{workspace_id}")
            final_finish = app.st.text_input("Final finish / coating", value=str(first.get("finish_system") or "To be confirmed"), key=f"merge_finish_v1226_{workspace_id}")
            if app.st.button("Merge selected rows", type="primary", use_container_width=True, key=f"merge_commit_v1226_{workspace_id}"):
                try:
                    new_id = merge_rows(app, workspace_id, [int(row["id"]) for row in selected_rows], {
                        "section": final_section, "element": final_element, "location": final_location,
                        "substrate": final_substrate, "finish_system": final_finish,
                        "inclusion_status": first.get("inclusion_status"), "coats": first.get("coats"),
                        "coverage_m2_per_litre": first.get("coverage_m2_per_litre"), "productivity_m2_per_hour": first.get("productivity_m2_per_hour"),
                        "rate_per_unit": first.get("rate_per_unit"), "row_role": first.get("row_role"),
                    })
                except ValueError as exc:
                    app.st.error(str(exc))
                else:
                    app.st.success(f"Merged into take-off row #{new_id}. All original source pages and geometry were retained.")
                    app.st.rerun()


def apply(app: Any) -> None:
    if getattr(app, "_pb_takeoff_review_v1226_applied", False):
        return
    app._pb_takeoff_review_v1226_applied = True

    base_panel = noai.no_ai_takeoff_panel
    def _panel_with_review(app_obj: Any, workspace: Dict[str, Any]):
        result = base_panel(app_obj, workspace)
        app_obj.st.divider()
        review_panel(app_obj, workspace)
        return result
    noai.no_ai_takeoff_panel = _panel_with_review

    # If an automatic generator is refreshed after rows were manually merged or
    # replaced by an estimator polygon, remove the superseded source rows again.
    base_noai_replace = noai.replace_no_ai_rows
    def _noai_replace(app_obj: Any, workspace_id: int, rows):
        result = base_noai_replace(app_obj, int(workspace_id), rows)
        cleanup_merged_inputs(app_obj, int(workspace_id))
        return result
    noai.replace_no_ai_rows = _noai_replace

    base_pb_sync = premier.sync_pb_generated_rows
    def _pb_sync(app_obj: Any, workspace_id: int, rows):
        result = base_pb_sync(app_obj, int(workspace_id), rows)
        cleanup_merged_inputs(app_obj, int(workspace_id))
        return result
    premier.sync_pb_generated_rows = _pb_sync

    base_studio_replace = studio._replace_studio_rows
    def _studio_replace(app_obj: Any, workspace_id: int, page_id: int, rows):
        result = base_studio_replace(app_obj, int(workspace_id), int(page_id), rows)
        cleanup_merged_inputs(app_obj, int(workspace_id))
        return result
    studio._replace_studio_rows = _studio_replace

    base_surface_replace = surface._replace_rows
    def _surface_replace(app_obj: Any, workspace_id: int, rows):
        result = base_surface_replace(app_obj, int(workspace_id), rows)
        cleanup_merged_inputs(app_obj, int(workspace_id))
        return result
    surface._replace_rows = _surface_replace

    app.merge_takeoff_rows_v1226 = lambda workspace_id, row_ids, final: merge_rows(app, int(workspace_id), row_ids, final)
    app.takeoff_row_provenance_v1226 = lambda workspace_id, row: provenance_for_row(app, int(workspace_id), dict(row))
