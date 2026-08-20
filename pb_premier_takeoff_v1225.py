"""PlanReader v1.2.25 Premier Brushworks commercial take-off builder.

This is deliberately painting-estimator centric rather than a generic geometry dump.
It reconciles the measured/reference data already in PlanReader into the same scope
structure used in Premier Brushworks commercial take-offs: floor-area basis, ceilings,
internal walls, doors/frames/trim, external substrates, soffits/eaves and specialist
finishes, with quantity basis, coating/preparation, drawing basis and confidence.

The builder never invents rates.  Unsupported quantities remain Pending / To measure.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pb_auto_geometry_v1219 as auto
import pb_material_schedule_v1222 as material
import pb_no_ai_takeoff_v1216 as noai

VERSION = "1.2.25"
SETTING_KEY = "premier_brushworks_takeoff_v1225"
SOURCE_PREFIX = f"PB Commercial Takeoff v{VERSION}"

PB_COLUMNS = [
    "item", "level", "area", "element", "finish_code", "colour_finish", "unit",
    "base_qty", "factor", "gross_qty", "deduction", "adjustment_pct", "net_qty",
    "qty_basis", "coating_preparation", "drawing_basis", "confidence", "notes",
]

_SPECIALIST_WORDS = (
    "texture", "acratex", "epoxy", "2 pac", "2-pack", "two pack", "non-slip", "non slip",
    "intumescent", "clear finish", "stain", "membrane", "elastomeric", "specialist",
)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else default
    except (TypeError, ValueError):
        return default


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _setting_get(app: Any, workspace_id: int) -> Dict[str, Any]:
    rows = app.lquery("SELECT value FROM workspace_settings WHERE workspace_id=? AND key=?", (int(workspace_id), SETTING_KEY))
    try:
        return json.loads(str(rows[0].get("value") or "{}")) if rows else {}
    except Exception:
        return {}


def _setting_set(app: Any, workspace_id: int, state: Dict[str, Any]) -> None:
    app.lexecute(
        """INSERT INTO workspace_settings(workspace_id,key,value,updated_at) VALUES(?,?,?,?)
           ON CONFLICT(workspace_id,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
        (int(workspace_id), SETTING_KEY, json.dumps(state, separators=(",", ":"), default=str), app.now_stamp()),
    )


def _level(value: Any) -> str:
    text = str(value or "")
    low = text.lower()
    if "ground" in low or re.search(r"\b(?:gf|gnd)\b", low):
        return "Ground"
    basement = re.search(r"\b(?:basement|b)\s*0*([1-9]\d*)\b", low)
    if basement:
        return f"Basement {int(basement.group(1))}"
    match = re.search(r"\blevel\s*0*([0-9]{1,2})\b", low)
    if match:
        return f"Level {int(match.group(1))}"
    # Unit 501 / Apartment 501 normally means level 5 in multi-unit sets.  Keep
    # this only as a grouping hint, never a measured geometry assumption.
    unit = re.search(r"\b(?:unit|apt|apartment)\s*[-#:]*\s*([1-9])(\d{2})\b", low)
    if unit:
        return f"Level {int(unit.group(1))}"
    if "roof" in low:
        return "Roof"
    return "Unassigned"


def _selected_pages(app: Any, workspace_id: int) -> List[Dict[str, Any]]:
    return [dict(row) for row in app.lquery(
        """SELECT p.*,d.file_name FROM pages p JOIN documents d ON d.id=p.document_id
           WHERE p.workspace_id=? AND COALESCE(p.selected,0)=1 ORDER BY p.document_id,p.page_no,p.id""",
        (int(workspace_id),),
    )]


def _material_state(app: Any, workspace_id: int) -> Dict[str, Any]:
    state = material._setting_get(app, int(workspace_id))
    if state.get("dictionary"):
        return state
    try:
        return material.build_material_state(app, int(workspace_id))
    except Exception:
        return {"dictionary": {}, "occurrences": [], "issues": []}


def _find_code(dictionary: Dict[str, Dict[str, Any]], *values: Any) -> str:
    target = _norm(" ".join(str(v or "") for v in values))
    if not target:
        return ""
    # First honour an explicit code written in the source text.
    for code in dictionary:
        if re.search(rf"\b{re.escape(code.lower())}\b", target):
            return code
    scored: List[Tuple[int, str]] = []
    for code, item in dictionary.items():
        hay = _norm(" ".join(str(item.get(k) or "") for k in ("description", "substrate", "finish", "element")))
        if not hay:
            continue
        score = 0
        for token in set(target.split()):
            if len(token) >= 4 and token in hay:
                score += 1
        if score:
            scored.append((score, code))
    return max(scored, default=(0, ""))[1]


def _coating_preparation(element: str, finish: str, section: str = "") -> str:
    low = _norm(f"{element} {finish} {section}")
    if any(token in low for token in ("door", "frame", "architrave", "skirting", "timber", "enamel")):
        return "Prepare / undercoat as required + scheduled finish coats"
    if any(token in low for token in ("steel", "metal", "galvan", "zinc")):
        return "Prepare metal substrate + specified primer / finish system"
    if any(token in low for token in ("external", "cladding", "render", "soffit", "eave", "facade")):
        return "Prepare substrate + scheduled exterior coating system"
    if any(token in low for token in _SPECIALIST_WORDS):
        return "Prepare substrate + specialist system as scheduled"
    return "Prepare / seal as required + scheduled finish coats"


def _pb_row(
    *, item: int = 0, level: str = "", area: str = "", element: str = "", finish_code: str = "",
    colour_finish: str = "", unit: str = "m²", base_qty: float = 0.0, factor: float = 1.0,
    deduction: float = 0.0, adjustment_pct: float = 0.0, qty_basis: str = "", coating_preparation: str = "",
    drawing_basis: str = "", confidence: str = "Pending", notes: str = "", source_page: str = "",
    sync: bool = False, section: str = "", substrate: str = "", row_role: str = "",
) -> Dict[str, Any]:
    base = max(0.0, _num(base_qty)); factor = max(0.0, _num(factor, 1.0))
    gross = base * factor
    net = max(0.0, (gross - max(0.0, _num(deduction))) * (1.0 + _num(adjustment_pct) / 100.0))
    return {
        "item": int(item), "level": level or "Unassigned", "area": area or "General", "element": element,
        "finish_code": finish_code, "colour_finish": colour_finish, "unit": unit,
        "base_qty": round(base, 3), "factor": round(factor, 4), "gross_qty": round(gross, 3),
        "deduction": round(max(0.0, _num(deduction)), 3), "adjustment_pct": round(_num(adjustment_pct), 3),
        "net_qty": round(net, 3), "qty_basis": qty_basis, "coating_preparation": coating_preparation,
        "drawing_basis": drawing_basis, "confidence": confidence, "notes": notes,
        "source_page": source_page, "sync": bool(sync), "section": section,
        "substrate": substrate, "row_role": row_role,
    }


def _existing_takeoff(app: Any, workspace_id: int) -> List[Dict[str, Any]]:
    return [dict(row) for row in app.lquery("SELECT * FROM takeoff_rows WHERE workspace_id=? ORDER BY id", (int(workspace_id),))]


def _is_pb_generated(row: Dict[str, Any]) -> bool:
    return str(row.get("source_reference") or "").startswith(SOURCE_PREFIX)


def _floor_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [row for row in rows if str(row.get("row_role") or "") == "floor_area" and _num(row.get("quantity")) > 0]


def _wall_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        if _is_pb_generated(row) or str(row.get("row_role") or "") == "floor_area":
            continue
        low = _norm(f"{row.get('section')} {row.get('element')} {row.get('location')}")
        if "internal" in low and "wall" in low and _num(row.get("quantity")) > 0:
            out.append(row)
    return out


def _ceiling_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [row for row in rows if not _is_pb_generated(row) and "ceiling" in _norm(f"{row.get('element')} {row.get('location')}") and _num(row.get("quantity")) > 0]


def _door_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [row for row in rows if not _is_pb_generated(row) and "door" in _norm(f"{row.get('element')} {row.get('location')}")]


def _external_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        if _is_pb_generated(row):
            continue
        low = _norm(f"{row.get('section')} {row.get('element')} {row.get('location')}")
        if any(token in low for token in ("external", "exterior", "facade", "cladding", "soffit", "eave", "screen", "balustrade", "sunhood", "downpipe", "render")):
            out.append(row)
    return out


def _page_evidence(pages: Sequence[Dict[str, Any]], *tokens: str) -> List[Dict[str, Any]]:
    terms = [str(token).lower() for token in tokens]
    result = []
    for page in pages:
        hay = _low_page(page)
        if any(term in hay for term in terms):
            result.append(page)
    return result


def _low_page(page: Dict[str, Any]) -> str:
    return f"{page.get('page_type') or ''} {page.get('page_label') or ''} {page.get('extracted_text') or ''}".lower()


def _group_floor_basis(floors: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}
    for row in floors:
        level = _level(f"{row.get('location')} {row.get('source_page')}")
        item = groups.setdefault(level, {"qty": 0.0, "rows": [], "units": set(), "sources": set()})
        item["qty"] += _num(row.get("quantity")); item["rows"].append(row)
        location = str(row.get("location") or "")
        if re.search(r"\b(?:unit|apt|apartment|villa|townhouse)\b", location, re.I):
            item["units"].add(location)
        if row.get("source_page"):
            item["sources"].add(str(row["source_page"]))
    return groups


def _row_to_pb(row: Dict[str, Any], dictionary: Dict[str, Dict[str, Any]], item_no: int) -> Dict[str, Any]:
    quantity = _num(row.get("quantity")); unit = str(row.get("unit") or "m²")
    code = _find_code(dictionary, row.get("source_reference"), row.get("substrate"), row.get("finish_system"), row.get("location"))
    entry = dictionary.get(code, {}) if code else {}
    finish = str(row.get("finish_system") or entry.get("finish") or entry.get("description") or "To confirm")
    element = str(row.get("element") or "Measured painting item")
    section = str(row.get("section") or "")
    return _pb_row(
        item=item_no, level=_level(f"{row.get('location')} {row.get('source_page')}"), area=str(row.get("location") or "General"),
        element=element, finish_code=code, colour_finish=finish, unit=unit, base_qty=quantity,
        factor=1, deduction=0, adjustment_pct=0,
        qty_basis=str(row.get("quantity_status") or "Measured drawing quantity"),
        coating_preparation=_coating_preparation(element, finish, section),
        drawing_basis=" · ".join(x for x in [str(row.get("source_page") or ""), str(row.get("source_reference") or "")] if x),
        confidence=str(row.get("confidence") or row.get("quantity_status") or "To review"),
        notes=str(row.get("notes") or ""), source_page=str(row.get("source_page") or ""), sync=False,
        section=section, substrate=str(row.get("substrate") or ""), row_role=str(row.get("row_role") or ""),
    )


def build_pb_schedule(app: Any, workspace_id: int) -> Dict[str, Any]:
    pages = _selected_pages(app, int(workspace_id))
    existing = _existing_takeoff(app, int(workspace_id))
    dictionary = dict(_material_state(app, int(workspace_id)).get("dictionary") or {})
    floors = _floor_rows(existing)
    floor_groups = _group_floor_basis(floors)
    existing_walls = _wall_rows(existing)
    existing_ceilings = _ceiling_rows(existing)
    existing_doors = _door_rows(existing)
    existing_external = _external_rows(existing)

    has_rcp = bool(_page_evidence(pages, "reflected ceiling", "rcp", "ceiling plan"))
    has_partition = bool(_page_evidence(pages, "partition plan", "floor plan", "unit plan", "general arrangement"))
    has_door_schedule = bool(_page_evidence(pages, "door schedule", "door elevations", "door / window schedule"))
    has_finishes = bool(_page_evidence(pages, "finish schedule", "finishes schedule", "paint schedule", "colour schedule", "material schedule"))
    has_elevations = bool(_page_evidence(pages, "elevation"))

    schedule: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    item_no = 1

    # 1) Internal floor-area basis.  Keep every measured unit/reference line auditably.
    for row in floors:
        code = _find_code(dictionary, row.get("source_reference"), row.get("location"))
        schedule.append(_pb_row(
            item=item_no, level=_level(f"{row.get('location')} {row.get('source_page')}"), area=str(row.get("location") or "Internal"),
            element="Internal floor area (pricing / quantity basis)", finish_code=code, colour_finish="Reference quantity",
            unit="m²", base_qty=_num(row.get("quantity")), qty_basis="Measured internal floor-area basis",
            coating_preparation="Reference basis only — not a painted surface quantity",
            drawing_basis=" · ".join(x for x in [str(row.get("source_page") or ""), str(row.get("source_reference") or "")] if x),
            confidence=str(row.get("confidence") or row.get("quantity_status") or "Measured basis"),
            notes="PB floor-area reference. Pricing treatment depends on the project rate basis.", source_page=str(row.get("source_page") or ""), row_role="floor_area",
        )); item_no += 1
    if not floors:
        issues.append({"severity": "High", "category": "Internal floor area", "message": "No measured internal floor-area basis is available yet.", "drawing_basis": "Floor / partition / unit plans"})

    # 2) Ceilings. Prefer actual measured ceiling rows; otherwise PB practice allows
    # a floor-area basis only where RCP/ceiling evidence exists, and labels it derived.
    if existing_ceilings:
        for row in existing_ceilings:
            schedule.append(_row_to_pb(row, dictionary, item_no)); item_no += 1
    elif has_rcp and floor_groups:
        for level, info in sorted(floor_groups.items()):
            code = _find_code(dictionary, "ceiling flat ceiling builders white")
            entry = dictionary.get(code, {}) if code else {}
            finish = str(entry.get("finish") or entry.get("description") or "As scheduled")
            schedule.append(_pb_row(
                item=item_no, level=level, area="Apartments / internal", element="General painted ceilings",
                finish_code=code, colour_finish=finish, unit="m²", base_qty=info["qty"],
                qty_basis="Internal floor-area basis; split wet/special ceiling zones to RCP",
                coating_preparation=_coating_preparation("ceilings", finish, "Internal"),
                drawing_basis="RCP + floor/unit plans + finishing schedule", confidence="Derived basis",
                notes="Automatically carried from measured internal floor area because selected RCP/ceiling evidence exists. Confirm voids, wet areas, feature ceilings and non-painted finishes.",
                sync=True, section="Internal", substrate="Plasterboard",
            )); item_no += 1

    # 3) Internal walls. Never use floor area as wall m². Existing measured wall
    # surfaces are accepted; otherwise create an explicit Pending scope row.
    if existing_walls:
        for row in existing_walls:
            schedule.append(_row_to_pb(row, dictionary, item_no)); item_no += 1
    elif has_partition or floor_groups:
        levels = sorted(floor_groups) or ["Unassigned"]
        for level in levels:
            code = _find_code(dictionary, "internal wall low sheen plasterboard")
            entry = dictionary.get(code, {}) if code else {}
            finish = str(entry.get("finish") or entry.get("description") or "As scheduled")
            schedule.append(_pb_row(
                item=item_no, level=level, area="Apartments / internal", element="Internal walls",
                finish_code=code, colour_finish=finish, unit="m²", base_qty=0,
                qty_basis="Surface measurement required from partition / unit plans",
                coating_preparation=_coating_preparation("internal walls", finish, "Internal"),
                drawing_basis="Partition / unit plans + finishing schedule", confidence="Pending",
                notes="Exclude tiles, splashbacks, joinery and prefinished surfaces. PlanReader will populate this when a calibrated wall-surface measurement is available.",
                sync=True, section="Internal", substrate="Plasterboard",
            )); item_no += 1
        issues.append({"severity": "Medium", "category": "Internal walls", "message": "Internal wall scope is identified but wall-face m² is still pending measured partition geometry.", "drawing_basis": "Partition / unit plans"})

    # 4) Entry doors and internal timber/trim. Existing schedule measurements win.
    if existing_doors:
        for row in existing_doors:
            schedule.append(_row_to_pb(row, dictionary, item_no)); item_no += 1
    else:
        for level, info in sorted(floor_groups.items()):
            unit_count = len(info.get("units") or [])
            if unit_count:
                code = _find_code(dictionary, "entry door frame enamel")
                entry = dictionary.get(code, {}) if code else {}
                finish = str(entry.get("finish") or entry.get("description") or "As scheduled")
                schedule.append(_pb_row(
                    item=item_no, level=level, area="Apartments", element="Apartment entry doors & frames",
                    finish_code=code, colour_finish=finish, unit="No.", base_qty=unit_count,
                    qty_basis="1 entry set per clearly identified unit", coating_preparation=_coating_preparation("doors frames", finish, "Internal"),
                    drawing_basis="Unit plans" + (" + door schedule" if has_door_schedule else "") + (" + finishes" if has_finishes else ""),
                    confidence="High" if has_door_schedule else "Derived", notes="Cross-check fire/acoustic door types and frame finish before tender issue.",
                    sync=True, section="Internal", substrate="Timber door",
                )); item_no += 1
        if has_door_schedule or floors:
            code = _find_code(dictionary, "internal doors architraves skirting enamel")
            entry = dictionary.get(code, {}) if code else {}
            finish = str(entry.get("finish") or entry.get("description") or "As scheduled")
            schedule.append(_pb_row(
                item=item_no, level="All applicable levels", area="Apartments / common areas", element="Internal doors / frames / architraves / skirting",
                finish_code=code, colour_finish=finish, unit="No./lm", base_qty=0,
                qty_basis="Door schedule counts + measured trim/skirting lineal metres",
                coating_preparation=_coating_preparation("doors frames architraves skirting", finish, "Internal"),
                drawing_basis="Door schedule + unit/partition plans + finishes", confidence="Pending",
                notes="Keep door counts and lineal trim/skirting auditable; do not convert to arbitrary floor-area factors.",
                sync=True, section="Internal", substrate="Timber trim / joinery",
            )); item_no += 1

    # 5) External quantities already measured by elevations / Studio / Mapper are
    # translated directly into PB scope rows rather than regenerated as one gross facade.
    seen_external = set()
    for row in existing_external:
        key = (_norm(row.get("element")), _norm(row.get("location")), _norm(row.get("substrate")), str(row.get("unit") or ""), round(_num(row.get("quantity")), 3))
        if key in seen_external:
            continue
        seen_external.add(key)
        schedule.append(_row_to_pb(row, dictionary, item_no)); item_no += 1
    if has_elevations and not existing_external:
        issues.append({"severity": "High", "category": "External", "message": "Elevations are selected but no external substrate m² has been established yet.", "drawing_basis": "Elevations + external finishes"})

    # 6) Ensure schedule-defined soffits and specialist systems are visible even
    # before geometry is measured, matching PB tender take-off practice.
    schedule_text = " ".join(_norm(f"{row.get('element')} {row.get('finish_code')} {row.get('colour_finish')}") for row in schedule)
    for code, entry in sorted(dictionary.items()):
        desc = _norm(f"{entry.get('description')} {entry.get('substrate')} {entry.get('finish')} {entry.get('element')}")
        if any(token in desc for token in ("soffit", "eave")) and code.lower() not in schedule_text:
            schedule.append(_pb_row(
                item=item_no, level="External", area="Soffits / eaves", element="External soffits / eaves",
                finish_code=code, colour_finish=str(entry.get("finish") or entry.get("description") or "As scheduled"), unit="m²", base_qty=0,
                qty_basis="RCP / elevation soffit surface measurement required",
                coating_preparation=_coating_preparation("soffit eave", entry.get("finish"), "External"),
                drawing_basis="RCP + elevations + finishing schedule", confidence="Pending",
                notes="Measure only the painted soffit/eave extents; exclude factory-finished surfaces unless paint-coded.",
                sync=True, section="External", substrate=str(entry.get("substrate") or "Soffits / Eaves"),
            )); item_no += 1
        elif any(token in desc for token in _SPECIALIST_WORDS) and code.lower() not in schedule_text:
            schedule.append(_pb_row(
                item=item_no, level="As documented", area="Specialist", element="Specialist finish",
                finish_code=code, colour_finish=str(entry.get("finish") or entry.get("description") or "As scheduled"), unit="m²", base_qty=0,
                qty_basis="Detailed/schedule surface measurement required",
                coating_preparation=_coating_preparation("specialist", entry.get("finish"), "Specialist"),
                drawing_basis="Finishing schedule + relevant details/elevations/RCP", confidence="Pending",
                notes="Specialist system remains separate from standard internal/external rates unless the project pricing basis says otherwise.",
                sync=True, section="Specialist", substrate=str(entry.get("substrate") or "Other"),
            )); item_no += 1

    # Re-number after all reconciliation and compute evidence completeness.
    for idx, row in enumerate(schedule, 1):
        row["item"] = idx
    measured = sum(1 for row in schedule if _num(row.get("net_qty")) > 0)
    pending = sum(1 for row in schedule if _num(row.get("net_qty")) <= 0 and str(row.get("confidence") or "").lower() == "pending")
    completeness = round(100.0 * measured / max(1, measured + pending), 1)
    return {
        "version": VERSION, "generated_at": app.now_stamp(), "rows": schedule, "issues": issues,
        "selected_pages": len(pages), "measured_rows": measured, "pending_rows": pending,
        "evidence_completeness_pct": completeness,
        "page_types": sorted({str(page.get("page_type") or "Other") for page in pages}),
    }


def _takeoff_values(app: Any, workspace_id: int, row: Dict[str, Any]) -> Tuple[Any, ...]:
    finish = str(row.get("colour_finish") or "To be confirmed")
    status = "Measured" if _num(row.get("net_qty")) > 0 and str(row.get("confidence") or "").lower() not in ("pending", "derived basis") else ("Provisional measured" if _num(row.get("net_qty")) > 0 else "To measure")
    reference = f"{SOURCE_PREFIX} · item:{int(row.get('item') or 0)}"
    notes = " | ".join(x for x in [str(row.get("qty_basis") or ""), str(row.get("drawing_basis") or ""), str(row.get("notes") or "")] if x)
    return (
        int(workspace_id), str(row.get("section") or "General"), str(row.get("element") or ""),
        " · ".join(x for x in [str(row.get("level") or ""), str(row.get("area") or "")] if x),
        str(row.get("substrate") or "Other"), finish, _num(row.get("net_qty")), str(row.get("unit") or "m²"),
        status, str(row.get("source_page") or ""), reference, "INCLUSION" if _num(row.get("net_qty")) > 0 else "PROVISIONAL",
        0, 0, 0, 0, str(row.get("confidence") or "To review"), notes, str(row.get("row_role") or ""), app.now_stamp(), app.now_stamp(),
    )


def sync_pb_generated_rows(app: Any, workspace_id: int, schedule: Sequence[Dict[str, Any]]) -> int:
    """Replace only PB scope rows created by this builder; manual/Studio/geometry rows survive."""
    rows = [row for row in schedule if bool(row.get("sync"))]
    conn = app.local_connect()
    try:
        conn.execute("DELETE FROM takeoff_rows WHERE workspace_id=? AND source_reference LIKE ?", (int(workspace_id), SOURCE_PREFIX + "%"))
        sql = """INSERT INTO takeoff_rows(
            workspace_id,section,element,location,substrate,finish_system,quantity,unit,quantity_status,
            source_page,source_reference,inclusion_status,coats,coverage_m2_per_litre,productivity_m2_per_hour,
            rate_per_unit,confidence,notes,row_role,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        conn.executemany(sql, (_takeoff_values(app, int(workspace_id), row) for row in rows))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()
    return len(rows)


def refresh_pb_takeoff(app: Any, workspace_id: int) -> Dict[str, Any]:
    state = build_pb_schedule(app, int(workspace_id))
    state["synced_rows"] = sync_pb_generated_rows(app, int(workspace_id), state.get("rows") or [])
    _setting_set(app, int(workspace_id), state)
    return state


def _display_frame(app: Any, rows: Sequence[Dict[str, Any]]):
    frame = app.pd.DataFrame([{col: row.get(col, "") for col in PB_COLUMNS} for row in rows])
    if frame.empty:
        return frame
    labels = {
        "item": "Item", "level": "Level", "area": "Area / location", "element": "Element / substrate",
        "finish_code": "Finish code", "colour_finish": "Colour / finish", "unit": "Unit", "base_qty": "Base qty",
        "factor": "Factor", "gross_qty": "Gross qty", "deduction": "Deduction", "adjustment_pct": "Adj. %",
        "net_qty": "Net qty", "qty_basis": "Qty basis", "coating_preparation": "Coating / preparation",
        "drawing_basis": "Drawing / measurement basis", "confidence": "Confidence", "notes": "Notes",
    }
    return frame.rename(columns=labels)


def pb_takeoff_panel(app: Any, workspace: Dict[str, Any]) -> None:
    workspace_id = int(workspace["id"])
    state = _setting_get(app, workspace_id)
    if not state:
        try:
            state = refresh_pb_takeoff(app, workspace_id)
        except Exception:
            state = {}
    app.st.markdown("### Premier Brushworks commercial take-off")
    app.st.caption(
        "PB estimator view: cross-references selected plans, partitions, RCPs, elevations, schedules and measured geometry. "
        "Unsupported quantities remain Pending rather than being invented. Rates are never generated here."
    )
    c1, c2, c3, c4 = app.st.columns(4)
    c1.metric("PB take-off rows", len(state.get("rows") or []))
    c2.metric("Measured / quantified", int(state.get("measured_rows") or 0))
    c3.metric("Pending measure", int(state.get("pending_rows") or 0))
    c4.metric("Evidence completeness", f"{_num(state.get('evidence_completeness_pct')):.0f}%")
    if app.st.button("Rebuild Premier Brushworks take-off", type="primary", use_container_width=True, key=f"pb_takeoff_rebuild_v1225_{workspace_id}"):
        with app.st.spinner("Cross-referencing PB painting scope across the selected drawing set…"):
            state = refresh_pb_takeoff(app, workspace_id)
        app.st.success(f"PB take-off rebuilt: {len(state.get('rows') or [])} rows; {state.get('synced_rows', 0)} scope rows synced to the editable take-off schedule.")
        app.st.rerun()
    rows = state.get("rows") or []
    if rows:
        app.st.dataframe(_display_frame(app, rows), hide_index=True, use_container_width=True, height=min(620, 80 + len(rows) * 34))
    else:
        app.st.info("No PB take-off evidence has been generated yet. Check the Drawing Register and process the selected sheets.")
    issues = state.get("issues") or []
    if issues:
        with app.st.expander(f"PB take-off items needing attention ({len(issues)})", expanded=True):
            app.st.dataframe(app.pd.DataFrame(issues), hide_index=True, use_container_width=True)
    app.st.caption(
        "Measurement rule: internal floor area is a pricing/reference basis where the project uses one; it is not silently converted into wall-face m². "
        "External work is measured from elevations/substrate geometry, and doors/trim remain counts/lineal quantities unless documented otherwise."
    )


def apply(app: Any) -> None:
    if getattr(app, "_pb_premier_takeoff_v1225_applied", False):
        return
    app._pb_premier_takeoff_v1225_applied = True

    base_analyse = auto.analyse_workspace
    def _analyse_with_pb(app_obj: Any, workspace_id: int):
        report = base_analyse(app_obj, int(workspace_id))
        try:
            pb_state = refresh_pb_takeoff(app_obj, int(workspace_id))
            report["pb_takeoff_rows"] = len(pb_state.get("rows") or [])
            report["pb_takeoff_pending"] = int(pb_state.get("pending_rows") or 0)
        except Exception as exc:
            report["pb_takeoff_error"] = str(exc)
        return report
    auto.analyse_workspace = _analyse_with_pb

    base_panel = noai.no_ai_takeoff_panel
    def _panel(app_obj: Any, workspace: Dict[str, Any]):
        pb_takeoff_panel(app_obj, workspace)
        app_obj.st.divider()
        return base_panel(app_obj, workspace)
    noai.no_ai_takeoff_panel = _panel

    app.build_premier_brushworks_takeoff = lambda workspace_id: build_pb_schedule(app, int(workspace_id))
    app.refresh_premier_brushworks_takeoff = lambda workspace_id: refresh_pb_takeoff(app, int(workspace_id))
