from __future__ import annotations

"""Universal specialist measurement engine for TradeReader 3D.

The AI reader remains source-first. This module adds reviewable trade-specific
measurement tools for every built-in trade without inventing project geometry or
commercial rates. Plastering / Linings keeps its richer dedicated module.
"""

import json
import math
from typing import Any, Dict, List

from tradereader_profiles import TRADE_PROFILES

CONFIDENCE = ["Measured", "Derived", "Provisional measured", "To review", "To measure"]
MODE_UNITS = {
    "Count / points": "No.",
    "Linear route / length": "m",
    "Linear item": "lm",
    "Plan area": "m²",
    "Wall / vertical area": "m²",
    "Volume": "m³",
    "Duct surface area": "m²",
    "Roof slope area": "m²",
    "Allowance / item": "item",
}

TRADE_MODES = {
    "Electrical": ["Count / points", "Linear route / length", "Linear item", "Allowance / item"],
    "Plumbing": ["Count / points", "Linear route / length", "Allowance / item"],
    "HVAC / Mechanical": ["Count / points", "Duct surface area", "Linear route / length", "Plan area", "Allowance / item"],
    "Carpentry / Joinery": ["Count / points", "Linear item", "Plan area", "Wall / vertical area", "Volume", "Allowance / item"],
    "Tiling": ["Plan area", "Wall / vertical area", "Linear item", "Count / points", "Allowance / item"],
    "Flooring": ["Plan area", "Linear item", "Count / points", "Allowance / item"],
    "Roofing": ["Roof slope area", "Plan area", "Linear item", "Count / points", "Allowance / item"],
    "Concreting": ["Volume", "Plan area", "Wall / vertical area", "Linear item", "Count / points"],
    "Landscaping": ["Plan area", "Volume", "Linear item", "Count / points", "Allowance / item"],
    "Custom trade": list(MODE_UNITS),
}


def _n(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _i(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return int(default)
        return int(float(value))
    except Exception:
        return int(default)


def calculate(mode: str, values: Dict[str, Any]) -> Dict[str, Any]:
    """Return a quantity and audit basis from explicit estimator inputs only."""
    mode = str(mode or "Allowance / item")
    waste = max(0.0, _n(values.get("waste_percent"))) / 100.0
    runs = max(1, _i(values.get("runs"), 1))
    length = max(0.0, _n(values.get("length_m")))
    width = max(0.0, _n(values.get("width_m")))
    height = max(0.0, _n(values.get("height_m")))
    depth = max(0.0, _n(values.get("depth_m")))
    deductions = max(0.0, _n(values.get("deductions")))
    count = max(0, _i(values.get("count")))
    plan_area = max(0.0, _n(values.get("plan_area_m2")))
    pitch = max(0.0, min(89.0, _n(values.get("pitch_degrees"))))

    quantity = 0.0
    unit = MODE_UNITS.get(mode, "item")
    basis = ""
    if mode == "Count / points":
        quantity = float(count)
        basis = f"Count {count}"
    elif mode in {"Linear route / length", "Linear item"}:
        gross = length * runs
        quantity = gross * (1.0 + waste)
        basis = f"{length:g} m × {runs} run(s) × {1+waste:.3f} waste factor"
    elif mode == "Plan area":
        gross = plan_area if plan_area > 0 else length * width
        net = max(0.0, gross - deductions)
        quantity = net * (1.0 + waste)
        basis = f"Gross {gross:.3f} m² − deductions {deductions:.3f} m² × {1+waste:.3f}"
    elif mode == "Wall / vertical area":
        gross = length * height
        net = max(0.0, gross - deductions)
        quantity = net * (1.0 + waste)
        basis = f"{length:g} m × {height:g} m − {deductions:g} m² × {1+waste:.3f}"
    elif mode == "Volume":
        gross = length * width * depth
        quantity = max(0.0, gross - deductions) * (1.0 + waste)
        basis = f"{length:g} m × {width:g} m × {depth:g} m − {deductions:g} m³ × {1+waste:.3f}"
    elif mode == "Duct surface area":
        gross = 2.0 * (width + height) * length * runs
        quantity = max(0.0, gross - deductions) * (1.0 + waste)
        basis = f"2 × ({width:g}+{height:g}) m × {length:g} m × {runs} − {deductions:g} m² × {1+waste:.3f}"
    elif mode == "Roof slope area":
        base = plan_area if plan_area > 0 else length * width
        slope_factor = 1.0 / math.cos(math.radians(pitch)) if pitch > 0 else 1.0
        quantity = max(0.0, base * slope_factor - deductions) * (1.0 + waste)
        basis = f"Plan {base:.3f} m² × slope factor {slope_factor:.4f} − {deductions:g} m² × {1+waste:.3f}"
    else:
        quantity = float(count or 1)
        basis = f"Explicit item allowance count {int(quantity)}"

    return {
        **dict(values),
        "mode": mode,
        "quantity": round(quantity, 3),
        "unit": unit,
        "measurement_basis": basis,
    }


def prompt_addendum(trade_name: str) -> str:
    profile = TRADE_PROFILES.get(trade_name) or TRADE_PROFILES.get("Custom trade", {})
    focus = str(profile.get("focus") or "the selected trade scope")
    return f"""{trade_name.upper()} SPECIALIST RULES
- Focus on {focus}.
- Reconcile plans, schedules, legends, details, sections and specifications before creating quantities.
- Count scheduled symbols/items only once and reconcile them against plan locations.
- Never invent routes, dimensions, product types, ratings, build-ups, sizes, depths or hidden work.
- If evidence is insufficient, use quantity=0, quantity_status='To measure' and raise an RFI/clarification.
- Keep source_page and source_reference on every measurable line.
- rate_per_unit must remain 0 unless a supplied source explicitly contains a rate."""


def system_types(trade_name: str) -> List[str]:
    profile = TRADE_PROFILES.get(trade_name) or TRADE_PROFILES.get("Custom trade", {})
    values = [str(x) for x in profile.get("sections", [])]
    return values + ["To be confirmed"] if "To be confirmed" not in values else values


def _table() -> str:
    return "trade_measurements_universal"


def ensure_schema(app: Any) -> None:
    app.lexecute(
        f"""CREATE TABLE IF NOT EXISTS {_table()}(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL,
            trade_name TEXT NOT NULL,
            section TEXT,
            element TEXT,
            location TEXT,
            system_type TEXT,
            mode TEXT,
            source_reference TEXT,
            confidence TEXT,
            payload_json TEXT,
            derived_json TEXT,
            notes TEXT,
            created_at TEXT
        )"""
    )
    app.lexecute(
        f"CREATE INDEX IF NOT EXISTS idx_trade_measurements_universal_ws ON {_table()}(workspace_id,trade_name,id)"
    )


def _records(app: Any, workspace_id: int, trade_name: str) -> List[Dict[str, Any]]:
    rows = app.lquery(
        f"SELECT * FROM {_table()} WHERE workspace_id=? AND trade_name=? ORDER BY section,location,id",
        (workspace_id, trade_name),
    )
    result: List[Dict[str, Any]] = []
    for row in rows:
        merged = dict(row)
        for key in ("payload_json", "derived_json"):
            try:
                merged.update(json.loads(row.get(key) or "{}"))
            except Exception:
                pass
        result.append(merged)
    return result


def _takeoff_row(record: Dict[str, Any], trade_name: str) -> Dict[str, Any]:
    quantity = _n(record.get("quantity"))
    source = str(record.get("source_reference") or "").strip()
    confidence = str(record.get("confidence") or "To review")
    status = "Measured" if quantity > 0 and source else "To measure"
    if confidence == "Provisional measured" and status == "Measured":
        status = "Provisional measured"
    section = str(record.get("section") or record.get("system_type") or "Scope")
    return {
        "section": f"{trade_name} · {section}",
        "element": str(record.get("element") or "Trade item"),
        "location": str(record.get("location") or ""),
        "substrate": "Not applicable",
        "finish_system": str(record.get("system_type") or "To be confirmed"),
        "quantity": quantity,
        "unit": str(record.get("unit") or "item"),
        "quantity_status": status,
        "source_page": trade_name,
        "source_reference": source,
        "inclusion_status": "INCLUSION",
        "coats": 0,
        "coverage_m2_per_litre": 0,
        "productivity_m2_per_hour": 0,
        "rate_per_unit": 0,
        "confidence": confidence,
        "notes": f"[Trade: {trade_name}] {record.get('measurement_basis') or ''} {record.get('notes') or ''}".strip(),
    }


def sync_to_takeoff(app: Any, workspace_id: int, trade_name: str) -> int:
    marker = f"[Trade: {trade_name}]%"
    app.lexecute(
        "DELETE FROM takeoff_rows WHERE workspace_id=? AND notes LIKE ?",
        (workspace_id, marker),
    )
    count = 0
    for record in _records(app, workspace_id, trade_name):
        row = _takeoff_row(record, trade_name)
        values = [row.get(col, "") for col in app.TAKEOFF_COLUMNS]
        app.lexecute(
            """INSERT INTO takeoff_rows(
               workspace_id,section,element,location,substrate,finish_system,quantity,unit,
               quantity_status,source_page,source_reference,inclusion_status,coats,
               coverage_m2_per_litre,productivity_m2_per_hour,rate_per_unit,confidence,notes,
               created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (workspace_id, *values, app.now_stamp(), app.now_stamp()),
        )
        count += 1
    return count


def render(app: Any, workspace: Dict[str, Any], trade_name: str) -> None:
    ensure_schema(app)
    st = app.st
    workspace_id = int(workspace["id"])
    profile = TRADE_PROFILES.get(trade_name) or TRADE_PROFILES.get("Custom trade", {})
    modes = TRADE_MODES.get(trade_name) or TRADE_MODES["Custom trade"]
    systems = system_types(trade_name)

    st.subheader(f"{trade_name} specialist measurement tools")
    st.info(
        "Enter only dimensions/counts supported by the current drawings or schedules. "
        "These tools derive reviewable quantities and never create rates."
    )
    with st.form(f"universal_trade_{trade_name}", clear_on_submit=True):
        c1, c2 = st.columns(2)
        section = c1.selectbox("Scope section", systems)
        element = c2.text_input("Element / item", placeholder="Fixture, route, wall type, slab, finish...")
        c1, c2 = st.columns(2)
        location = c1.text_input("Location", placeholder="Level / room / grid / elevation / zone")
        mode = c2.selectbox("Measurement method", modes)
        c1, c2, c3 = st.columns(3)
        length_m = c1.number_input("Length / route (m)", min_value=0.0, step=0.1)
        width_m = c2.number_input("Width (m)", min_value=0.0, step=0.1)
        height_m = c3.number_input("Height (m)", min_value=0.0, step=0.1)
        c1, c2, c3 = st.columns(3)
        depth_m = c1.number_input("Depth / thickness (m)", min_value=0.0, step=0.01)
        plan_area_m2 = c2.number_input("Known plan area (m²)", min_value=0.0, step=0.1)
        deductions = c3.number_input("Supported deductions", min_value=0.0, step=0.1)
        c1, c2, c3 = st.columns(3)
        count = c1.number_input("Count / points", min_value=0, step=1)
        runs = c2.number_input("Parallel runs / repeated lengths", min_value=1, step=1, value=1)
        waste_percent = c3.number_input("Waste / slack (%)", min_value=0.0, step=1.0)
        pitch = st.number_input("Roof pitch (degrees, only where applicable)", min_value=0.0, max_value=89.0, step=1.0)
        c1, c2 = st.columns(2)
        source_reference = c1.text_input("Source reference", placeholder="Drawing/page/detail/schedule/spec clause")
        confidence = c2.selectbox("Confidence", CONFIDENCE, index=3)
        notes = st.text_area("Notes / assumptions")
        submitted = st.form_submit_button("Add supported measurement", type="primary")
        if submitted:
            if not element.strip():
                st.error("Element / item is required.")
            elif not source_reference.strip() and confidence in {"Measured", "Derived", "Provisional measured"}:
                st.error("A measured/derived quantity needs a source reference.")
            else:
                payload = {
                    "length_m": length_m, "width_m": width_m, "height_m": height_m,
                    "depth_m": depth_m, "plan_area_m2": plan_area_m2, "deductions": deductions,
                    "count": count, "runs": runs, "waste_percent": waste_percent,
                    "pitch_degrees": pitch,
                }
                derived = calculate(mode, payload)
                app.lexecute(
                    f"""INSERT INTO {_table()}(
                       workspace_id,trade_name,section,element,location,system_type,mode,
                       source_reference,confidence,payload_json,derived_json,notes,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        workspace_id, trade_name, section, element.strip(), location.strip(), section, mode,
                        source_reference.strip(), confidence, json.dumps(payload), json.dumps(derived),
                        notes.strip(), app.now_stamp(),
                    ),
                )
                st.success(f"Added {derived['quantity']:.3f} {derived['unit']} · {derived['measurement_basis']}")
                st.rerun()

    records = _records(app, workspace_id, trade_name)
    if not records:
        st.info("No specialist measurements yet for this trade.")
        st.caption(f"AI focus: {profile.get('focus','')}")
        return

    display = [
        {
            "ID": row.get("id"), "Section": row.get("section"), "Element": row.get("element"),
            "Location": row.get("location"), "Method": row.get("mode"),
            "Quantity": row.get("quantity"), "Unit": row.get("unit"),
            "Source": row.get("source_reference"), "Confidence": row.get("confidence"),
            "Basis": row.get("measurement_basis"),
        }
        for row in records
    ]
    st.dataframe(display, use_container_width=True, hide_index=True, height=420)
    c1, c2 = st.columns(2)
    selected_id = c1.selectbox("Measurement to delete", [int(row["id"]) for row in records])
    if c1.button("Delete selected measurement", use_container_width=True):
        app.lexecute(f"DELETE FROM {_table()} WHERE id=? AND workspace_id=?", (selected_id, workspace_id))
        st.rerun()
    if c2.button("Sync this trade to common take-off", type="primary", use_container_width=True):
        count_synced = sync_to_takeoff(app, workspace_id, trade_name)
        st.success(f"Synced {count_synced} {trade_name} line(s). Rates remain zero.")
        st.rerun()
