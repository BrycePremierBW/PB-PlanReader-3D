from __future__ import annotations

"""Plastering / linings estimating module for TradeReader 3D.

The generic take-off schedule remains the commercial hand-off. This module adds
trade-specific measurement fields and calculations, then syncs reviewed results
back to the common schedule without applying rates.
"""

import math
import re
from typing import Any, Dict, List

import pandas as pd


MODULE_NAME = "Plastering / Linings"
MODULE_VERSION = "1.1"
MODULE_NOTE_PREFIX = "[Plastering Module]"

SYSTEM_TYPES = [
    "Standard plasterboard lining",
    "Ceiling plasterboard system",
    "Wet-area lining system",
    "Fibre-cement lining system",
    "Fire-rated lining system",
    "Acoustic lining system",
    "Fire + acoustic lining system",
    "Impact / high-abuse lining system",
    "External lining system",
    "Shaft / service lining system",
    "To be confirmed",
]

ELEMENT_TYPES = [
    "Wall lining",
    "Ceiling",
    "Bulkhead / drop",
    "Wet-area lining",
    "External lining",
    "Fire / acoustic wall",
    "Shaft / service lining",
    "Feature / curved lining",
]

BOARD_TYPES = [
    "Standard plasterboard",
    "Ceiling plasterboard",
    "Moisture-resistant plasterboard",
    "Fire-rated plasterboard",
    "Acoustic plasterboard",
    "Impact-resistant plasterboard",
    "Fibre cement",
    "External fibre cement",
    "Other / specified board",
]

LEVELS_OF_FINISH = [
    "To be confirmed",
    "Level 3",
    "Level 4",
    "Level 5",
    "Set / finish requirement by specification",
]

CONFIDENCE_OPTIONS = ["Measured", "Derived", "Provisional measured", "To review", "To measure"]
YES_NO_TBC = ["No", "Yes", "To be confirmed"]
RESPONSIBILITY_OPTIONS = [
    "Plastering trade",
    "Carpentry / framing trade",
    "Builder / head contractor",
    "Other trade",
    "To be confirmed",
    "Not applicable",
]

BUILTIN_ASSEMBLIES: dict[str, dict[str, Any]] = {
    "10mm standard wall — one side": {
        "system_type": "Standard plasterboard lining",
        "board_type": "Standard plasterboard",
        "board_thickness_mm": 10.0,
        "board_width_mm": 1200.0,
        "board_length_mm": 2400.0,
        "layers": 1,
        "sides": 1,
        "waste_percent": 8.0,
        "level_of_finish": "Level 4",
        "notes": "Starter assembly only — verify board, framing, finish level and project specification.",
    },
    "10mm standard wall — both sides": {
        "system_type": "Standard plasterboard lining",
        "board_type": "Standard plasterboard",
        "board_thickness_mm": 10.0,
        "board_width_mm": 1200.0,
        "board_length_mm": 2400.0,
        "layers": 1,
        "sides": 2,
        "waste_percent": 8.0,
        "level_of_finish": "Level 4",
        "notes": "Starter assembly only — verify board, framing, finish level and project specification.",
    },
    "10mm ceiling — single layer": {
        "system_type": "Ceiling plasterboard system",
        "board_type": "Ceiling plasterboard",
        "board_thickness_mm": 10.0,
        "board_width_mm": 1200.0,
        "board_length_mm": 2400.0,
        "layers": 1,
        "sides": 1,
        "waste_percent": 8.0,
        "level_of_finish": "Level 4",
        "notes": "Verify ceiling board type, support spacing, control joints and finish requirement.",
    },
    "13mm fire-rated — one layer each side": {
        "system_type": "Fire-rated lining system",
        "board_type": "Fire-rated plasterboard",
        "board_thickness_mm": 13.0,
        "board_width_mm": 1200.0,
        "board_length_mm": 2400.0,
        "layers": 1,
        "sides": 2,
        "waste_percent": 10.0,
        "level_of_finish": "Level 4",
        "notes": "Fire rating is not assumed. Confirm tested system, framing, insulation, layers and junction details.",
    },
    "13mm fire-rated — two layers each side": {
        "system_type": "Fire-rated lining system",
        "board_type": "Fire-rated plasterboard",
        "board_thickness_mm": 13.0,
        "board_width_mm": 1200.0,
        "board_length_mm": 2400.0,
        "layers": 2,
        "sides": 2,
        "waste_percent": 10.0,
        "level_of_finish": "Level 4",
        "notes": "Fire rating is not assumed. Confirm tested system, framing, insulation, layers and junction details.",
    },
    "Wet-area fibre cement — one side": {
        "system_type": "Wet-area lining system",
        "board_type": "Fibre cement",
        "board_thickness_mm": 6.0,
        "board_width_mm": 1200.0,
        "board_length_mm": 2400.0,
        "layers": 1,
        "sides": 1,
        "waste_percent": 10.0,
        "level_of_finish": "Set / finish requirement by specification",
        "notes": "Confirm wet-area board, waterproofing interface, tile backing requirements and joint treatment.",
    },
    "External fibre cement — one layer": {
        "system_type": "External lining system",
        "board_type": "External fibre cement",
        "board_thickness_mm": 7.5,
        "board_width_mm": 1200.0,
        "board_length_mm": 2400.0,
        "layers": 1,
        "sides": 1,
        "waste_percent": 10.0,
        "level_of_finish": "Set / finish requirement by specification",
        "notes": "Confirm proprietary external system, joint layout, expressed joints, flashings and coating interface.",
    },
}


def prompt_addendum() -> str:
    return """
PLASTERING / LININGS TRADE RULES
- Cross-reference architectural wall-type schedules, reflected ceiling plans, sections, wet-area details, fire/acoustic details, door/window schedules and the specification.
- Separate wall linings, ceilings, bulkheads/drops, wet-area linings, external linings, shaft/service linings, cornices, trims/beads, control joints and access panels.
- For wall area, show the supported basis as wall length × wall height = gross area; subtract documented openings only when their dimensions/counts are supported.
- Do not assume wall height, ceiling level, number of lined sides, number of board layers, board thickness, fire rating, acoustic rating, insulation, framing responsibility or Level of Finish.
- When a wall-type code is visible, preserve that code in finish_system or notes and reconcile it against the wall-type detail/schedule.
- Fire/acoustic systems must remain separate by system/rating. Never merge different tested systems merely because the board looks similar.
- Wet-area and external fibre-cement linings must remain separate from standard plasterboard.
- Ceiling area, bulkhead faces/soffits and wall linings must not be double-counted.
- Keep cornice, angles/beads/control joints and access panels as separate lm/count quantities when supported.
- If openings or wall heights cannot be verified, use quantity_status='To measure' rather than guessing.
- Rates remain zero.
"""


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return int(default)
        return int(float(value))
    except Exception:
        return int(default)


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_") or "item"


def _ensure_schema(app: Any) -> None:
    app.lexecute(
        """
        CREATE TABLE IF NOT EXISTS plastering_measurements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL,
            area_group TEXT,
            location TEXT,
            element_type TEXT,
            system_type TEXT,
            wall_type_code TEXT,
            board_type TEXT,
            board_thickness_mm REAL DEFAULT 0,
            board_width_mm REAL DEFAULT 1200,
            board_length_mm REAL DEFAULT 2400,
            layers INTEGER DEFAULT 1,
            sides INTEGER DEFAULT 1,
            wall_length_m REAL DEFAULT 0,
            height_m REAL DEFAULT 0,
            gross_area_m2 REAL DEFAULT 0,
            openings_m2 REAL DEFAULT 0,
            net_area_m2 REAL DEFAULT 0,
            waste_percent REAL DEFAULT 0,
            installed_board_area_m2 REAL DEFAULT 0,
            sheet_count INTEGER DEFAULT 0,
            cornice_lm REAL DEFAULT 0,
            trim_bead_lm REAL DEFAULT 0,
            control_joint_lm REAL DEFAULT 0,
            access_panels_count INTEGER DEFAULT 0,
            insulation_required TEXT,
            framing_responsibility TEXT,
            fire_rating TEXT,
            acoustic_rating TEXT,
            wet_area TEXT,
            level_of_finish TEXT,
            source_reference TEXT,
            confidence TEXT,
            notes TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    app.lexecute(
        "CREATE INDEX IF NOT EXISTS idx_plastering_measurements_workspace ON plastering_measurements(workspace_id, id)"
    )
    app.lexecute(
        """
        CREATE TABLE IF NOT EXISTS plastering_assemblies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL,
            assembly_name TEXT NOT NULL,
            system_type TEXT,
            board_type TEXT,
            board_thickness_mm REAL DEFAULT 0,
            board_width_mm REAL DEFAULT 1200,
            board_length_mm REAL DEFAULT 2400,
            layers INTEGER DEFAULT 1,
            sides INTEGER DEFAULT 1,
            waste_percent REAL DEFAULT 0,
            level_of_finish TEXT,
            notes TEXT,
            created_at TEXT,
            updated_at TEXT,
            UNIQUE(workspace_id, assembly_name)
        )
        """
    )


def _custom_assemblies(app: Any, workspace_id: int) -> dict[str, dict[str, Any]]:
    frame = app.ldf(
        "SELECT * FROM plastering_assemblies WHERE workspace_id=? ORDER BY assembly_name",
        (workspace_id,),
    )
    if frame.empty:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in frame.to_dict("records"):
        result[f"Custom · {row.get('assembly_name')}"] = dict(row)
    return result


def _assemblies(app: Any, workspace_id: int) -> dict[str, dict[str, Any]]:
    return {**BUILTIN_ASSEMBLIES, **_custom_assemblies(app, workspace_id)}


def calculate_measurement(values: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(values)
    element = str(result.get("element_type") or "")
    length = max(0.0, _to_float(result.get("wall_length_m")))
    height = max(0.0, _to_float(result.get("height_m")))
    entered_gross = max(0.0, _to_float(result.get("gross_area_m2")))
    openings = max(0.0, _to_float(result.get("openings_m2")))
    layers = max(1, _to_int(result.get("layers"), 1))
    sides = max(1, _to_int(result.get("sides"), 1))
    waste = max(0.0, _to_float(result.get("waste_percent")))
    board_width = max(0.0, _to_float(result.get("board_width_mm")))
    board_length = max(0.0, _to_float(result.get("board_length_mm")))

    wall_like = element in {
        "Wall lining", "Wet-area lining", "External lining", "Fire / acoustic wall",
        "Shaft / service lining", "Feature / curved lining",
    }
    calculated_gross = length * height if wall_like and length > 0 and height > 0 else 0.0
    gross = calculated_gross if calculated_gross > 0 else entered_gross
    net = max(0.0, gross - openings)
    installed = net * sides * layers
    sheet_area = (board_width / 1000.0) * (board_length / 1000.0)
    sheets = math.ceil(installed * (1.0 + waste / 100.0) / sheet_area) if installed > 0 and sheet_area > 0 else 0

    result.update(
        {
            "gross_area_m2": round(gross, 3),
            "net_area_m2": round(net, 3),
            "layers": layers,
            "sides": sides,
            "installed_board_area_m2": round(installed, 3),
            "sheet_count": int(sheets),
        }
    )
    return result


def _measurement_basis(row: Dict[str, Any]) -> str:
    element = str(row.get("element_type") or "")
    length = _to_float(row.get("wall_length_m"))
    height = _to_float(row.get("height_m"))
    gross = _to_float(row.get("gross_area_m2"))
    openings = _to_float(row.get("openings_m2"))
    net = _to_float(row.get("net_area_m2"))
    sides = max(1, _to_int(row.get("sides"), 1))
    layers = max(1, _to_int(row.get("layers"), 1))
    if length > 0 and height > 0 and element not in {"Ceiling", "Bulkhead / drop"}:
        base = f"{length:.3f}m × {height:.3f}m = {gross:.3f}m² gross"
    else:
        base = f"{gross:.3f}m² gross"
    return (
        f"{base} − {openings:.3f}m² openings = {net:.3f}m² net; "
        f"{sides} side(s) × {layers} layer(s) = {_to_float(row.get('installed_board_area_m2')):.3f}m² board."
    )


def _insert_measurement(app: Any, workspace_id: int, values: Dict[str, Any]) -> None:
    row = calculate_measurement(values)
    columns = [
        "area_group", "location", "element_type", "system_type", "wall_type_code",
        "board_type", "board_thickness_mm", "board_width_mm", "board_length_mm",
        "layers", "sides", "wall_length_m", "height_m", "gross_area_m2", "openings_m2",
        "net_area_m2", "waste_percent", "installed_board_area_m2", "sheet_count",
        "cornice_lm", "trim_bead_lm", "control_joint_lm", "access_panels_count",
        "insulation_required", "framing_responsibility", "fire_rating", "acoustic_rating",
        "wet_area", "level_of_finish", "source_reference", "confidence", "notes",
    ]
    app.lexecute(
        f"""INSERT INTO plastering_measurements(
            workspace_id,{','.join(columns)},created_at,updated_at
        ) VALUES ({','.join('?' for _ in range(len(columns)+3))})""",
        (workspace_id, *[row.get(col, "") for col in columns], app.now_stamp(), app.now_stamp()),
    )


def _delete_measurement(app: Any, measurement_id: int) -> None:
    app.lexecute("DELETE FROM plastering_measurements WHERE id=?", (measurement_id,))


def _measurements(app: Any, workspace_id: int) -> pd.DataFrame:
    return app.ldf(
        "SELECT * FROM plastering_measurements WHERE workspace_id=? ORDER BY area_group,location,id",
        (workspace_id,),
    )


def _qa_findings(frame: pd.DataFrame) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if frame.empty:
        return findings
    for row in frame.to_dict("records"):
        ident = f"#{row.get('id')} {row.get('location') or row.get('element_type') or 'measurement'}"
        gross = _to_float(row.get("gross_area_m2"))
        openings = _to_float(row.get("openings_m2"))
        height = _to_float(row.get("height_m"))
        system = str(row.get("system_type") or "").lower()
        board = str(row.get("board_type") or "").lower()
        source = str(row.get("source_reference") or "").strip()
        element = str(row.get("element_type") or "")
        fire = str(row.get("fire_rating") or "").strip()
        acoustic = str(row.get("acoustic_rating") or "").strip()
        wet = str(row.get("wet_area") or "").lower()
        level = str(row.get("level_of_finish") or "").strip().lower()

        def add(severity: str, check: str, detail: str) -> None:
            findings.append({"Severity": severity, "Check": check, "Detail": f"{ident}: {detail}"})

        if not source:
            add("High", "Source reference", "No drawing/specification reference is recorded.")
        if gross <= 0:
            add("High", "Measured area", "No supported gross area has been entered or calculated.")
        if openings > gross and gross > 0:
            add("High", "Opening deduction", "Opening deductions exceed the gross area.")
        if _to_int(row.get("layers"), 0) < 1 or _to_int(row.get("sides"), 0) < 1:
            add("High", "Layers / sides", "Layers and lined sides must be at least 1.")
        if _to_float(row.get("board_width_mm")) <= 0 or _to_float(row.get("board_length_mm")) <= 0:
            add("High", "Board size", "Board sheet dimensions are missing, so sheet count is not reliable.")
        if _to_float(row.get("board_thickness_mm")) <= 0:
            add("Medium", "Board thickness", "Board thickness has not been confirmed.")
        if "fire" in system and not fire:
            add("High", "Fire rating", "Fire-rated system selected but no tested fire rating/system reference is recorded.")
        if "acoustic" in system and not acoustic:
            add("High", "Acoustic rating", "Acoustic system selected but no acoustic rating/system reference is recorded.")
        if wet == "yes" and not any(token in board for token in ("moisture", "fibre", "cement", "wet")):
            add("Medium", "Wet-area board", "Wet area is marked Yes but the board type is not clearly a wet-area/fibre-cement board.")
        if element == "Ceiling" and _to_int(row.get("sides"), 1) != 1:
            add("Medium", "Ceiling sides", "Ceiling measurement has more than one lined side; verify this is intentional.")
        if level in {"", "to be confirmed"}:
            add("Medium", "Level of Finish", "Level of Finish is not confirmed.")
        if height > 3.0:
            add("Medium", "Access / high work", f"Recorded height is {height:.2f}m; confirm access, staging and productivity impact.")
        if str(row.get("framing_responsibility") or "") == "To be confirmed":
            add("Medium", "Framing interface", "Framing responsibility is not confirmed.")
        if str(row.get("insulation_required") or "") == "To be confirmed":
            add("Medium", "Insulation interface", "Insulation requirement/responsibility is not confirmed.")
    return findings


def _sync_to_takeoff(app: Any, workspace_id: int) -> int:
    frame = _measurements(app, workspace_id)
    app.lexecute(
        "DELETE FROM takeoff_rows WHERE workspace_id=? AND notes LIKE ?",
        (workspace_id, f"{MODULE_NOTE_PREFIX}%"),
    )
    inserted = 0
    if frame.empty:
        return inserted

    for source in frame.to_dict("records"):
        row = calculate_measurement(source)
        source_ref = str(row.get("source_reference") or "").strip()
        confidence = str(row.get("confidence") or "To review")
        status = "Measured" if _to_float(row.get("net_area_m2")) > 0 and source_ref else "To measure"
        if confidence.lower().startswith("provisional") and status == "Measured":
            status = "Provisional measured"
        system = str(row.get("system_type") or "To be confirmed")
        wall_code = str(row.get("wall_type_code") or "").strip()
        board = str(row.get("board_type") or "Other")
        substrate = "Fibre cement" if "cement" in board.lower() else "Plasterboard"
        element = str(row.get("element_type") or "Lining")
        if wall_code:
            element = f"{element} · {wall_code}"
        notes = (
            f"{MODULE_NOTE_PREFIX} {_measurement_basis(row)} "
            f"Board: {board}, {_to_float(row.get('board_thickness_mm')):g}mm; "
            f"waste {_to_float(row.get('waste_percent')):g}%; "
            f"sheet count {int(_to_int(row.get('sheet_count')))}. "
            f"{str(row.get('notes') or '').strip()}"
        ).strip()
        area_takeoff = {
            "section": str(row.get("area_group") or element),
            "element": element,
            "location": str(row.get("location") or ""),
            "substrate": substrate,
            "finish_system": system,
            "quantity": _to_float(row.get("net_area_m2")),
            "unit": "m²",
            "quantity_status": status,
            "source_page": "Plastering Module",
            "source_reference": source_ref,
            "inclusion_status": "INCLUSION",
            "coats": 0,
            "coverage_m2_per_litre": 0,
            "productivity_m2_per_hour": 0,
            "rate_per_unit": 0,
            "confidence": confidence,
            "notes": notes,
        }
        _insert_takeoff_row(app, workspace_id, area_takeoff)
        inserted += 1

        extras = [
            ("Cornice", "cornice_lm", "lm"),
            ("Angles / beads / trims", "trim_bead_lm", "lm"),
            ("Control joints", "control_joint_lm", "lm"),
            ("Access panels", "access_panels_count", "No."),
        ]
        for label, field, unit in extras:
            quantity = _to_float(row.get(field))
            if quantity <= 0:
                continue
            extra = dict(area_takeoff)
            extra.update(
                {
                    "section": label,
                    "element": label,
                    "quantity": quantity,
                    "unit": unit,
                    "notes": f"{MODULE_NOTE_PREFIX} Separate {label.lower()} quantity from plastering measurement #{row.get('id')}. {source_ref}",
                }
            )
            _insert_takeoff_row(app, workspace_id, extra)
            inserted += 1
    return inserted


def _insert_takeoff_row(app: Any, workspace_id: int, row: Dict[str, Any]) -> None:
    values = [row.get(col, "") for col in app.TAKEOFF_COLUMNS]
    app.lexecute(
        """
        INSERT INTO takeoff_rows(
            workspace_id,section,element,location,substrate,finish_system,quantity,unit,
            quantity_status,source_page,source_reference,inclusion_status,coats,
            coverage_m2_per_litre,productivity_m2_per_hour,rate_per_unit,confidence,notes,
            created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (workspace_id, *values, app.now_stamp(), app.now_stamp()),
    )


def _add_findings_to_rfis(app: Any, workspace_id: int, findings: list[dict[str, str]]) -> int:
    inserted = 0
    for finding in findings:
        detail = str(finding.get("Detail") or "")
        title = f"Plastering QA · {finding.get('Check') or 'Review'}"
        exists = app.lquery(
            """SELECT id FROM register_items
               WHERE workspace_id=? AND register_name='rfis' AND title=? AND detail=? LIMIT 1""",
            (workspace_id, title, detail),
        )
        if exists:
            continue
        app.lexecute(
            """INSERT INTO register_items(
               workspace_id,register_name,item_no,title,detail,priority,source_reference,status,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                workspace_id,
                "rfis",
                "",
                title,
                detail,
                "High" if finding.get("Severity") == "High" else "Medium",
                "Plastering / Linings module",
                "Open",
                app.now_stamp(),
            ),
        )
        inserted += 1
    return inserted


def _render_measurement_form(app: Any, workspace_id: int) -> None:
    assemblies = _assemblies(app, workspace_id)
    assembly_name = app.st.selectbox(
        "Starter assembly",
        list(assemblies),
        key="plastering_starter_assembly",
        help="Starter values only. Project wall types, specifications and tested systems remain the authority.",
    )
    assembly = assemblies[assembly_name]
    suffix = _slug(assembly_name)

    with app.st.form(f"plastering_measurement_form_{suffix}", clear_on_submit=True):
        c1, c2, c3 = app.st.columns(3)
        area_group = c1.text_input("Area / section", placeholder="e.g. Level 1 · Apartments")
        location = c2.text_input("Location", placeholder="e.g. Unit 3 · Bedroom 1")
        element_type = c3.selectbox("Element", ELEMENT_TYPES)

        c1, c2, c3 = app.st.columns(3)
        wall_type_code = c1.text_input("Wall / ceiling type code", placeholder="e.g. W03, C02")
        system_type = c2.selectbox(
            "System",
            SYSTEM_TYPES,
            index=SYSTEM_TYPES.index(assembly.get("system_type")) if assembly.get("system_type") in SYSTEM_TYPES else 0,
        )
        board_type = c3.selectbox(
            "Board / lining type",
            BOARD_TYPES,
            index=BOARD_TYPES.index(assembly.get("board_type")) if assembly.get("board_type") in BOARD_TYPES else 0,
        )

        c1, c2, c3, c4 = app.st.columns(4)
        thickness = c1.number_input("Board thickness (mm)", min_value=0.0, value=float(assembly.get("board_thickness_mm") or 0), step=0.5)
        board_width = c2.number_input("Sheet width (mm)", min_value=0.0, value=float(assembly.get("board_width_mm") or 1200), step=50.0)
        board_length = c3.number_input("Sheet length (mm)", min_value=0.0, value=float(assembly.get("board_length_mm") or 2400), step=100.0)
        waste = c4.number_input("Board waste (%)", min_value=0.0, max_value=100.0, value=float(assembly.get("waste_percent") or 0), step=1.0)

        c1, c2, c3, c4 = app.st.columns(4)
        layers = c1.number_input("Layers per side", min_value=1, max_value=8, value=max(1, _to_int(assembly.get("layers"), 1)), step=1)
        sides = c2.number_input("Lined sides", min_value=1, max_value=4, value=max(1, _to_int(assembly.get("sides"), 1)), step=1)
        wall_length = c3.number_input("Wall length (m)", min_value=0.0, value=0.0, step=0.1)
        height = c4.number_input("Height (m)", min_value=0.0, value=0.0, step=0.1)

        c1, c2 = app.st.columns(2)
        gross_area = c1.number_input(
            "Gross area override / ceiling area (m²)",
            min_value=0.0,
            value=0.0,
            step=0.5,
            help="Walls calculate from length × height when both are entered. Use this field for ceilings/bulkheads or where area is directly measured.",
        )
        openings = c2.number_input("Supported opening deductions (m²)", min_value=0.0, value=0.0, step=0.1)

        c1, c2, c3, c4 = app.st.columns(4)
        cornice = c1.number_input("Cornice (lm)", min_value=0.0, value=0.0, step=0.5)
        trim_bead = c2.number_input("Angles / beads / trims (lm)", min_value=0.0, value=0.0, step=0.5)
        control_joint = c3.number_input("Control joints (lm)", min_value=0.0, value=0.0, step=0.5)
        access_panels = c4.number_input("Access panels (No.)", min_value=0, value=0, step=1)

        c1, c2, c3 = app.st.columns(3)
        insulation = c1.selectbox("Insulation required", YES_NO_TBC, index=2)
        framing = c2.selectbox("Framing responsibility", RESPONSIBILITY_OPTIONS, index=4)
        wet_area = c3.selectbox("Wet area", YES_NO_TBC, index=0)

        c1, c2, c3 = app.st.columns(3)
        fire_rating = c1.text_input("Fire rating / system reference", placeholder="Do not guess")
        acoustic_rating = c2.text_input("Acoustic rating / system reference", placeholder="Do not guess")
        level_finish_default = str(assembly.get("level_of_finish") or "To be confirmed")
        level_finish = c3.selectbox(
            "Level of Finish",
            LEVELS_OF_FINISH,
            index=LEVELS_OF_FINISH.index(level_finish_default) if level_finish_default in LEVELS_OF_FINISH else 0,
        )

        c1, c2 = app.st.columns(2)
        source_reference = c1.text_input("Source reference", placeholder="e.g. A-210 p4 · wall type W03 · Spec 09 29 00")
        confidence = c2.selectbox("Confidence", CONFIDENCE_OPTIONS, index=3)
        notes = app.st.text_area("Notes", value=str(assembly.get("notes") or ""))

        submitted = app.st.form_submit_button("Add plastering measurement", type="primary")
        if submitted:
            values = {
                "area_group": area_group.strip(),
                "location": location.strip(),
                "element_type": element_type,
                "system_type": system_type,
                "wall_type_code": wall_type_code.strip(),
                "board_type": board_type,
                "board_thickness_mm": thickness,
                "board_width_mm": board_width,
                "board_length_mm": board_length,
                "layers": layers,
                "sides": sides,
                "wall_length_m": wall_length,
                "height_m": height,
                "gross_area_m2": gross_area,
                "openings_m2": openings,
                "waste_percent": waste,
                "cornice_lm": cornice,
                "trim_bead_lm": trim_bead,
                "control_joint_lm": control_joint,
                "access_panels_count": access_panels,
                "insulation_required": insulation,
                "framing_responsibility": framing,
                "fire_rating": fire_rating.strip(),
                "acoustic_rating": acoustic_rating.strip(),
                "wet_area": wet_area,
                "level_of_finish": level_finish,
                "source_reference": source_reference.strip(),
                "confidence": confidence,
                "notes": notes.strip(),
            }
            calculated = calculate_measurement(values)
            if _to_float(calculated.get("gross_area_m2")) <= 0:
                app.st.error("Enter supported wall length + height, or a directly measured gross area.")
            elif _to_float(calculated.get("openings_m2")) > _to_float(calculated.get("gross_area_m2")):
                app.st.error("Opening deductions cannot exceed the gross area.")
            else:
                _insert_measurement(app, workspace_id, calculated)
                app.st.success(
                    f"Added {calculated['net_area_m2']:.2f} m² net · {calculated['installed_board_area_m2']:.2f} m² installed board · {calculated['sheet_count']} sheets including waste."
                )
                app.st.rerun()


def _render_measurements_tab(app: Any, workspace_id: int) -> None:
    app.st.markdown("### Plastering measurement builder")
    app.st.caption(
        "Wall quantities calculate from supported length × height; ceilings/bulkheads can use direct measured area. "
        "Opening deductions, sides and board layers stay visible in the audit trail."
    )
    _render_measurement_form(app, workspace_id)

    frame = _measurements(app, workspace_id)
    if frame.empty:
        app.st.info("No plastering measurements yet.")
        return
    app.st.markdown("### Measured schedule")
    display_cols = [
        "id", "area_group", "location", "element_type", "wall_type_code", "system_type",
        "board_type", "board_thickness_mm", "layers", "sides", "wall_length_m", "height_m",
        "gross_area_m2", "openings_m2", "net_area_m2", "installed_board_area_m2",
        "waste_percent", "sheet_count", "cornice_lm", "trim_bead_lm", "control_joint_lm",
        "access_panels_count", "fire_rating", "acoustic_rating", "wet_area",
        "level_of_finish", "source_reference", "confidence",
    ]
    app.st.dataframe(frame[display_cols], use_container_width=True, hide_index=True, height=430)

    options = {
        f"#{int(row.id)} · {row.location or row.element_type} · {row.net_area_m2:.2f}m²": int(row.id)
        for row in frame.itertuples()
    }
    selected = app.st.selectbox("Measurement actions", list(options), key="plastering_measurement_action")
    c1, c2 = app.st.columns(2)
    if c1.button("Delete selected measurement", use_container_width=True):
        _delete_measurement(app, options[selected])
        app.st.rerun()
    if c2.button("Sync plastering measurements to common take-off", type="primary", use_container_width=True):
        count = _sync_to_takeoff(app, workspace_id)
        app.st.success(f"Synced {count} reviewed plastering take-off lines. Rates remain zero.")
        app.st.rerun()


def _render_assemblies_tab(app: Any, workspace_id: int) -> None:
    app.st.markdown("### Starter assemblies")
    builtin_rows = []
    for name, row in BUILTIN_ASSEMBLIES.items():
        builtin_rows.append(
            {
                "Assembly": name,
                "System": row.get("system_type"),
                "Board": row.get("board_type"),
                "Thickness mm": row.get("board_thickness_mm"),
                "Layers / side": row.get("layers"),
                "Sides": row.get("sides"),
                "Waste %": row.get("waste_percent"),
                "Finish": row.get("level_of_finish"),
            }
        )
    app.st.dataframe(pd.DataFrame(builtin_rows), use_container_width=True, hide_index=True)
    app.st.info("Assemblies provide calculation defaults only. They do not assert a compliant fire/acoustic system and never contain commercial rates.")

    app.st.markdown("### Save a project assembly")
    with app.st.form("plastering_custom_assembly"):
        name = app.st.text_input("Assembly name", placeholder="e.g. W07 · 2x 13mm fire board both sides")
        c1, c2 = app.st.columns(2)
        system = c1.selectbox("System type", SYSTEM_TYPES, key="plastering_custom_system")
        board = c2.selectbox("Board type", BOARD_TYPES, key="plastering_custom_board")
        c1, c2, c3 = app.st.columns(3)
        thickness = c1.number_input("Thickness (mm)", min_value=0.0, value=13.0, step=0.5)
        width = c2.number_input("Sheet width (mm)", min_value=0.0, value=1200.0, step=50.0)
        length = c3.number_input("Sheet length (mm)", min_value=0.0, value=2400.0, step=100.0)
        c1, c2, c3 = app.st.columns(3)
        layers = c1.number_input("Layers / side", min_value=1, max_value=8, value=1, step=1)
        sides = c2.number_input("Sides", min_value=1, max_value=4, value=1, step=1)
        waste = c3.number_input("Waste %", min_value=0.0, max_value=100.0, value=8.0, step=1.0)
        finish = app.st.selectbox("Level of Finish", LEVELS_OF_FINISH)
        notes = app.st.text_area("Assembly notes", placeholder="Specification/system reference, framing, insulation, junction requirements...")
        save = app.st.form_submit_button("Save assembly", type="primary")
        if save:
            clean_name = name.strip()
            if not clean_name:
                app.st.error("Assembly name is required.")
            else:
                existing = app.lquery(
                    "SELECT id FROM plastering_assemblies WHERE workspace_id=? AND assembly_name=?",
                    (workspace_id, clean_name),
                )
                if existing:
                    app.lexecute(
                        """UPDATE plastering_assemblies SET system_type=?,board_type=?,board_thickness_mm=?,
                           board_width_mm=?,board_length_mm=?,layers=?,sides=?,waste_percent=?,level_of_finish=?,
                           notes=?,updated_at=? WHERE id=?""",
                        (system, board, thickness, width, length, layers, sides, waste, finish, notes.strip(), app.now_stamp(), existing[0]["id"]),
                    )
                else:
                    app.lexecute(
                        """INSERT INTO plastering_assemblies(
                           workspace_id,assembly_name,system_type,board_type,board_thickness_mm,
                           board_width_mm,board_length_mm,layers,sides,waste_percent,level_of_finish,
                           notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (workspace_id, clean_name, system, board, thickness, width, length, layers, sides, waste, finish, notes.strip(), app.now_stamp(), app.now_stamp()),
                    )
                app.st.success(f"Saved assembly {clean_name}.")
                app.st.rerun()

    custom = app.ldf(
        "SELECT id,assembly_name,system_type,board_type,board_thickness_mm,layers,sides,waste_percent,level_of_finish,notes FROM plastering_assemblies WHERE workspace_id=? ORDER BY assembly_name",
        (workspace_id,),
    )
    if not custom.empty:
        app.st.markdown("### Project assemblies")
        app.st.dataframe(custom, use_container_width=True, hide_index=True)


def _render_materials_tab(app: Any, workspace_id: int) -> None:
    frame = _measurements(app, workspace_id)
    app.st.markdown("### Material quantity summary")
    if frame.empty:
        app.st.info("Add measurements first.")
        return
    material = (
        frame.groupby(["system_type", "board_type", "board_thickness_mm", "board_width_mm", "board_length_mm"], dropna=False)
        .agg(
            net_area_m2=("net_area_m2", "sum"),
            installed_board_area_m2=("installed_board_area_m2", "sum"),
            sheets=("sheet_count", "sum"),
        )
        .reset_index()
    )
    app.st.dataframe(material, use_container_width=True, hide_index=True)

    extras = pd.DataFrame(
        [
            ["Cornice", float(frame["cornice_lm"].fillna(0).sum()), "lm"],
            ["Angles / beads / trims", float(frame["trim_bead_lm"].fillna(0).sum()), "lm"],
            ["Control joints", float(frame["control_joint_lm"].fillna(0).sum()), "lm"],
            ["Access panels", float(frame["access_panels_count"].fillna(0).sum()), "No."],
        ],
        columns=["Item", "Quantity", "Unit"],
    )
    extras = extras[extras["Quantity"] > 0]
    if not extras.empty:
        app.st.markdown("### Sundry quantities")
        app.st.dataframe(extras, use_container_width=True, hide_index=True)
    app.st.caption(
        "Sheet count includes the waste percentage saved on each measurement. Compound, screws, adhesive, framing and insulation are not auto-invented; add them only when the chosen system/assembly supports a calculation."
    )


def _render_qa_tab(app: Any, workspace_id: int) -> None:
    frame = _measurements(app, workspace_id)
    findings = _qa_findings(frame)
    app.st.markdown("### Plastering QA / RFI checks")
    app.st.caption(
        "Checks are designed to expose missing estimating evidence rather than silently filling gaps."
    )
    if not findings:
        if frame.empty:
            app.st.info("Add measurements to run the plastering QA checks.")
        else:
            app.st.success("No current plastering QA flags were generated. Estimator review is still required.")
        return
    findings_df = pd.DataFrame(findings)
    severity_order = {"High": 0, "Medium": 1, "Low": 2}
    findings_df["_order"] = findings_df["Severity"].map(severity_order).fillna(9)
    findings_df = findings_df.sort_values(["_order", "Check"]).drop(columns=["_order"])
    app.st.dataframe(findings_df, use_container_width=True, hide_index=True, height=430)
    if app.st.button("Add these QA findings to the project RFI register", type="primary"):
        count = _add_findings_to_rfis(app, workspace_id, findings)
        app.st.success(f"Added {count} new plastering RFI item(s).")
        app.st.rerun()


def render(app: Any, workspace: Dict[str, Any]) -> None:
    _ensure_schema(app)
    workspace_id = int(workspace["id"])
    app.hero(workspace)
    app.st.subheader(f"Plastering / Linings Trade Tools · v{MODULE_VERSION}")
    app.st.info(
        "This module calculates plastering quantities from estimator-entered/document-supported evidence. "
        "It does not apply default rates and does not assume compliant fire/acoustic systems."
    )
    tabs = app.st.tabs(["Measurements", "Assemblies", "Material summary", "QA / RFIs"])
    with tabs[0]:
        _render_measurements_tab(app, workspace_id)
    with tabs[1]:
        _render_assemblies_tab(app, workspace_id)
    with tabs[2]:
        _render_materials_tab(app, workspace_id)
    with tabs[3]:
        _render_qa_tab(app, workspace_id)
