from __future__ import annotations

"""
Premier Brushworks take-off method overrides for PB PlanReader 3D.

This module is intentionally kept separate from the large Streamlit app so the
estimating method can evolve without repeatedly rewriting the whole application.

The launcher imports pb_planreader_3d_app, calls install(app), then starts app.main().
"""

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


PB_METHOD_VERSION = "2026.08.07-1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _lower(value: Any) -> str:
    return _text(value).lower()


def _contains_any(text: str, words: Iterable[str]) -> bool:
    low = text.lower()
    return any(word.lower() in low for word in words)


def pb_section(section: Any, element: Any, location: Any, substrate: Any, internal_external: Any = "") -> str:
    """Normalise a row into a Premier Brushworks estimating section."""
    blob = " ".join(map(_text, [section, element, location, substrate, internal_external])).lower()

    if _contains_any(blob, ["exterior", "external", "facade", "façade", "elevation"]):
        if _contains_any(blob, ["soffit", "eave", "alfresco ceiling", "entry ceiling", "canopy ceiling"]):
            return "External - Soffits / Eaves / External Ceilings"
        if _contains_any(blob, ["cladding", "linea", "weatherboard", "easylap", "textureboard", "fibre cement", "fc sheet"]):
            return "External - Cladding"
        if _contains_any(blob, ["post", "column", "pier"]):
            return "External - Posts / Columns"
        if _contains_any(blob, ["downpipe", "gutter", "fascia", "barge", "capping", "meter box", "metalwork"]):
            return "External - Metalwork / Rainwater Goods"
        if _contains_any(blob, ["door", "garage"]):
            return "External - Doors / Garage Doors"
        return "External - Walls / Render / Masonry"

    if _contains_any(blob, ["soffit", "eave", "external ceiling", "facade", "façade", "render", "external wall"]):
        if _contains_any(blob, ["soffit", "eave", "ceiling"]):
            return "External - Soffits / Eaves / External Ceilings"
        return "External - Walls / Render / Masonry"

    if _contains_any(blob, ["wet area", "bathroom", "ensuite", "powder", "laundry"]):
        if _contains_any(blob, ["ceiling"]):
            return "Internal - Ceilings"
        return "Internal - Wet Area Walls"

    if _contains_any(blob, ["ceiling", "bulkhead"]):
        return "Internal - Ceilings / Bulkheads"

    if _contains_any(blob, ["door", "frame", "jamb"]):
        return "Internal - Doors / Frames"

    if _contains_any(blob, ["skirting", "architrave", "trim", "joinery", "timberwork", "window reveal"]):
        return "Internal - Trim / Joinery"

    if _contains_any(blob, ["floor coating", "epoxy", "concrete coating", "garage floor"]):
        return "Internal - Floor Coatings"

    return "Internal - Walls"


def pb_substrate(substrate: Any, element: Any = "", location: Any = "") -> str:
    """Map common builder/JobHub substrate wording to the app's substrate vocabulary."""
    blob = " ".join(map(_text, [substrate, element, location])).lower()

    if _contains_any(blob, ["wet area plaster", "wet area board", "villaboard", "aqua", "wetboard"]):
        return "Wet-area plasterboard"
    if _contains_any(blob, ["plasterboard", "plaster board", "gyprock", "gyp", "drywall", "pb wall"]):
        return "Plasterboard"
    if _contains_any(blob, ["fibre cement", "fiber cement", "fc sheet", "fc cladding", "linea", "easylap", "textureboard", "weatherboard"]):
        return "Fibre cement"
    if _contains_any(blob, ["render", "masonry", "blockwork", "block wall", "aac", "hebel", "brick"]):
        return "Masonry / blockwork"
    if _contains_any(blob, ["precast"]):
        return "Precast concrete"
    if _contains_any(blob, ["concrete floor", "slab", "garage floor", "floor coating"]):
        return "Concrete floor"
    if _contains_any(blob, ["soffit", "eave"]):
        return "Soffit"
    if _contains_any(blob, ["door leaf", "timber door", "door"]):
        return "Timber door"
    if _contains_any(blob, ["skirting", "architrave", "trim", "joinery", "timber", "mdf"]):
        return "Timber trim / joinery"
    if _contains_any(blob, ["structural steel", "steel column", "steel post"]):
        return "Structural steel"
    if _contains_any(blob, ["metal", "aluminium", "aluminum", "downpipe", "meter box", "gutter", "fascia"]):
        return "Metalwork"
    if _contains_any(blob, ["previously painted", "repaint", "existing painted"]):
        return "Previously painted substrate"
    return "Other"


def pb_finish_system(section: Any, element: Any, substrate: Any, finish_system: Any = "", notes: Any = "") -> str:
    """Use explicit finish first, otherwise derive a practical PB finish family."""
    explicit = _text(finish_system)
    valid = {
        "Ceiling flat",
        "Low sheen wall system",
        "Semi-gloss / enamel",
        "Exterior acrylic",
        "Elastomeric / membrane",
        "Concrete coating",
        "Specialist floor coating",
        "Metal primer + topcoats",
        "Clear / stain system",
        "To be confirmed",
    }
    if explicit in valid:
        return explicit

    blob = " ".join(map(_text, [section, element, substrate, finish_system, notes])).lower()
    if _contains_any(blob, ["stain", "clear coat", "clear finish"]):
        return "Clear / stain system"
    if _contains_any(blob, ["epoxy", "floor coating", "garage floor"]):
        return "Specialist floor coating"
    if _contains_any(blob, ["membrane", "elastomeric"]):
        return "Elastomeric / membrane"
    if _contains_any(blob, ["steel", "metalwork", "metal primer", "downpipe", "meter box"]):
        return "Metal primer + topcoats"
    if _contains_any(blob, ["door", "frame", "architrave", "skirting", "trim", "joinery", "semi gloss", "semigloss", "enamel"]):
        return "Semi-gloss / enamel"
    if _contains_any(blob, ["external", "exterior", "facade", "façade", "render", "cladding", "soffit", "eave"]):
        return "Exterior acrylic"
    if _contains_any(blob, ["ceiling", "flat"]):
        return "Ceiling flat"
    if _contains_any(blob, ["wall", "plasterboard", "low sheen"]):
        return "Low sheen wall system"
    return "To be confirmed"


def pb_unit(unit: Any, element: Any, quantity: Any) -> str:
    raw = _text(unit).lower().replace("²", "2")
    if raw in {"m2", "sqm", "sq m", "square metre", "square metres"}:
        return "m²"
    if raw in {"lm", "lin m", "linear m", "lineal m", "m"}:
        return "lm"
    if raw in {"no", "no.", "each", "ea", "count"}:
        return "No."
    if raw in {"item", "allowance", "l"}:
        return "L" if raw == "l" else raw

    blob = _lower(element)
    if _contains_any(blob, ["door", "window", "item", "unit", "count"]):
        return "No."
    if _contains_any(blob, ["skirting", "architrave", "downpipe", "gutter", "fascia", "handrail", "trim"]):
        return "lm"
    return "m²"


def pb_inclusion_status(raw: Any, notes: Any = "") -> str:
    blob = f"{_text(raw)} {_text(notes)}".lower()
    if _contains_any(blob, ["exclude", "excluded", "not included", "by others", "prefinished", "pre-finished"]):
        return "EXCLUSION"
    if _contains_any(blob, ["separate", "variation", "optional"]):
        return "SEPARATE ITEM"
    if _contains_any(blob, ["provisional", "allowance", "tbc", "to confirm"]):
        return "PROVISIONAL"
    if _contains_any(blob, ["clarification", "rfi"]):
        return "CLARIFICATION"
    return "INCLUSION"


def pb_quantity_status(raw: Any, quantity: Any, confidence: Any = "") -> str:
    status = _text(raw)
    allowed = {"Measured", "Provisional measured", "To measure", "Allowance", "Excluded", "Not applicable"}
    if status in allowed:
        return status
    q = _float(quantity)
    conf = _lower(confidence)
    if q <= 0:
        return "To measure"
    if _contains_any(conf, ["measured", "verified"]):
        return "Measured"
    if _contains_any(conf, ["derived", "provisional", "estimated", "assumed"]):
        return "Provisional measured"
    return "Provisional measured"


def normalise_takeoff_row(row: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(row or {})
    section = pb_section(
        row.get("section"),
        row.get("element"),
        row.get("location"),
        row.get("substrate"),
        row.get("internal_external"),
    )
    substrate = pb_substrate(row.get("substrate"), row.get("element"), row.get("location"))
    finish = pb_finish_system(section, row.get("element"), substrate, row.get("finish_system"), row.get("notes"))
    quantity = max(0.0, _float(row.get("quantity")))
    unit = pb_unit(row.get("unit"), row.get("element"), quantity)
    inclusion = pb_inclusion_status(row.get("inclusion_status"), row.get("notes"))
    qstatus = "Excluded" if inclusion == "EXCLUSION" else pb_quantity_status(
        row.get("quantity_status"), quantity, row.get("confidence")
    )

    row.update(
        {
            "section": section,
            "element": _text(row.get("element")) or "Paintable surface",
            "location": _text(row.get("location")) or "Unallocated / review",
            "substrate": substrate,
            "finish_system": finish,
            "quantity": quantity,
            "unit": unit,
            "quantity_status": qstatus,
            "inclusion_status": inclusion,
            "coats": _float(row.get("coats"), 3.0) or 3.0,
            "coverage_m2_per_litre": _float(row.get("coverage_m2_per_litre"), 12.0) or 12.0,
            "productivity_m2_per_hour": _float(row.get("productivity_m2_per_hour"), 8.0) or 8.0,
            "rate_per_unit": _float(row.get("rate_per_unit"), 0.0),
            "confidence": _text(row.get("confidence")) or ("To review" if quantity <= 0 else "Derived"),
            "notes": _text(row.get("notes")),
            "source_page": _text(row.get("source_page")),
            "source_reference": _text(row.get("source_reference")),
        }
    )
    return row


def normalise_ai_result(data: Dict[str, Any]) -> Dict[str, Any]:
    """Clean AI output into a consistent PB take-off structure and de-duplicate rows."""
    result = dict(data or {})
    cleaned: List[Dict[str, Any]] = []
    seen = set()

    for raw in result.get("takeoff_rows", []) or []:
        row = normalise_takeoff_row(raw)
        key = (
            _lower(row.get("section")),
            _lower(row.get("element")),
            _lower(row.get("location")),
            _lower(row.get("substrate")),
            row.get("unit"),
            round(_float(row.get("quantity")), 3),
            _lower(row.get("source_reference")),
        )
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(row)

    order = {
        "Internal - Walls": 10,
        "Internal - Wet Area Walls": 20,
        "Internal - Ceilings / Bulkheads": 30,
        "Internal - Doors / Frames": 40,
        "Internal - Trim / Joinery": 50,
        "Internal - Floor Coatings": 60,
        "External - Walls / Render / Masonry": 70,
        "External - Cladding": 80,
        "External - Soffits / Eaves / External Ceilings": 90,
        "External - Posts / Columns": 100,
        "External - Metalwork / Rainwater Goods": 110,
        "External - Doors / Garage Doors": 120,
    }
    cleaned.sort(key=lambda r: (order.get(_text(r.get("section")), 999), _lower(r.get("location")), _lower(r.get("element"))))
    result["takeoff_rows"] = cleaned

    regs = list(result.get("register_items", []) or [])
    for unknown in result.get("unknowns", []) or []:
        text = _text(unknown)
        if not text:
            continue
        regs.append(
            {
                "register_name": "rfis",
                "item_no": "",
                "title": "Estimator verification required",
                "detail": text,
                "priority": "High",
                "source_reference": "AI plan review",
                "status": "Open",
            }
        )
    result["register_items"] = regs
    return result


PB_PLAN_READ_PROMPT = r"""
You are the estimating engine for Premier Brushworks, an Australian painting contractor.

Your job is NOT to write a generic summary of the plans. Your job is to produce a practical,
reviewable painting take-off draft in the same structure an experienced painting estimator would use.

EVIDENCE HIERARCHY
Use evidence in this order when sources conflict:
1. Current painting / construction specification and addenda.
2. Colour / finishes schedules and material schedules.
3. Door, window and joinery schedules.
4. Reflected ceiling plans.
5. Dimensioned floor plans.
6. Dimensioned elevations and sections.
7. Renders / artist impressions only as visual secondary evidence, never as measured dimensions.

PREMIER BRUSHWORKS TAKE-OFF STRUCTURE
Build take-off rows under these sections wherever relevant:
- Internal - Walls
- Internal - Wet Area Walls
- Internal - Ceilings / Bulkheads
- Internal - Doors / Frames
- Internal - Trim / Joinery
- Internal - Floor Coatings
- External - Walls / Render / Masonry
- External - Cladding
- External - Soffits / Eaves / External Ceilings
- External - Posts / Columns
- External - Metalwork / Rainwater Goods
- External - Doors / Garage Doors

For each row identify:
section, element, location, substrate, finish system, quantity, unit, quantity status,
source page, source reference, inclusion status, coats, coverage, productivity,
confidence and estimator notes.

PAINTABLE SCOPE REVIEW
Actively look for and classify:
- plasterboard walls and ceilings;
- wet-area Villaboard / fibre cement where painted;
- bulkheads;
- window reveals;
- skirtings and architraves;
- internal and external doors, door frames and jambs;
- timber/MDF trims and joinery that are painter-applied;
- rendered AAC / Hebel / blockwork / masonry;
- fibre-cement and weatherboard cladding;
- soffits, eaves, alfresco ceilings, entry ceilings and canopies;
- painted posts, columns and slab edges;
- downpipes, meter boxes and other nominated paintable metal/PVC items;
- specialist coatings and concrete floor coatings when specifically documented.

EXCLUSIONS / BY OTHERS
Do not silently include prefinished or factory-finished items. Record them as EXCLUSION or CLARIFICATION
where appropriate, including powder-coated aluminium, Colorbond roofing/gutters/fascia, glazing, tiles,
stone, laminate, factory-finished garage doors, proprietary prefinished cladding, signage and landscaping,
unless the documents explicitly require painter-applied coating.

MEASUREMENT RULES
- Never invent dimensions.
- Quantity must be greater than zero only when dimensions, a reliable calibrated scale, schedule counts,
  or an explicit documented quantity supports it.
- If a surface is clearly in scope but cannot be measured from the supplied evidence, create the row with
  quantity=0 and quantity_status='To measure'.
- If a quantity is calculated from clear dimensions, state the calculation basis in notes and use
  confidence='Derived' unless directly scheduled/measured.
- Distinguish gross and net areas. State opening deductions, tile deductions, joinery deductions and other
  deductions. If deductions cannot be verified, keep the quantity provisional.
- Doors/items use No. where appropriate; trims/downpipes/gutters use lm where appropriate; surface coatings
  normally use m².
- Do not double count the same surface from plan and elevation views.

COLOUR / FINISH REVIEW
Create colour_finish_schedule register items for every painter-applied colour/finish/product that can be
identified. Keep factory finishes separate and note them as coordination/exclusion items. If the same named
colour is used in different sheens, record separate surface entries.

SCOPE REGISTERS
Create register_items for:
- inclusions;
- exclusions;
- clarifications;
- assumptions;
- rfis;
- door_schedule;
- colour_finish_schedule;
- access_constraints;
- risks;
- source_basis.
Use source references for every important decision.

JOBHUB / PRICING RULE
Leave rate_per_unit=0. PlanReader applies Premier Brushworks editable default rates after import.
Use practical editable coats / coverage / productivity defaults only when the documents do not specify them,
and clearly mark them as estimating defaults in notes.

3D MODEL RULE
The take-off is the priority. Only create model_masses / model_openings when geometry is genuinely supported.
Do not invent a 3D model just to fill the schema.

QUALITY CHECK BEFORE RETURNING
Before returning data, check that:
- internal and external scope are separated;
- substrate wording is specific;
- paintable versus prefinished/by-others scope is separated;
- all obvious ceilings/soffits/doors/trim are considered;
- unknown dimensions are zero / To measure rather than guessed;
- sources are cited;
- colour and finish information has been captured into the register;
- unresolved conflicts are turned into RFIs.

Return structured data only.
"""


def make_run_ai_plan_read(app):
    def run_ai_plan_read(
        workspace_id: int,
        page_ids: Sequence[int],
        api_key: str,
        model: str,
        provider: str = "OpenAI",
    ) -> Dict[str, Any]:
        if not page_ids:
            raise RuntimeError("Select at least one page.")

        placeholders = ",".join("?" for _ in page_ids)
        pages = app.lquery(
            f"""SELECT p.*,d.file_name
                FROM pages p JOIN documents d ON d.id=p.document_id
                WHERE p.id IN ({placeholders}) ORDER BY p.id""",
            tuple(page_ids),
        )

        blocks: List[Tuple[str, str]] = []
        for page in pages:
            text_excerpt = _text(page.get("extracted_text"))[:16000]
            blocks.append(
                (
                    "text",
                    "SOURCE PAGE: "
                    f"{page.get('file_name')} · {page.get('page_label')} · page {page.get('page_no')} "
                    f"· classified {page.get('page_type')}\n"
                    f"EXTRACTED TEXT:\n{text_excerpt}",
                )
            )
            image_path = app.Path(_text(page.get("image_path")))
            if image_path.exists():
                blocks.append(("image", str(image_path)))

        data = app.run_ai_structured(
            provider,
            api_key,
            model,
            PB_PLAN_READ_PROMPT,
            blocks,
            app.ai_schema(),
            "premier_brushworks_takeoff",
        )
        data = normalise_ai_result(data)

        app.lexecute(
            """INSERT INTO ai_runs(
                   workspace_id,run_type,model,source_pages,status,response_json,error_message,created_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                workspace_id,
                f"Premier Brushworks take-off · {PB_METHOD_VERSION}",
                f"{provider} · {model}",
                app.json.dumps(list(page_ids)),
                "Completed",
                app.json.dumps(data),
                "",
                app.now_stamp(),
            ),
        )
        return data

    return run_ai_plan_read


def _jobhub_row_to_takeoff(app, row: Dict[str, Any]) -> Dict[str, Any]:
    location = _text(row.get("area_location")) or "Unallocated / review"
    labour = _text(row.get("labour_category")) or "Paintable surface"
    raw_substrate = _text(row.get("substrate"))
    int_ext = _text(row.get("internal_external")) or "Internal"
    source_note = _text(row.get("source_note"))
    confidence = _text(row.get("confidence"))

    qty_m2 = max(0.0, _float(row.get("qty_m2")))
    lineal_m = max(0.0, _float(row.get("lineal_m")))
    count = max(0.0, _float(row.get("count")))
    if qty_m2 > 0:
        quantity, unit = qty_m2, "m²"
    elif lineal_m > 0:
        quantity, unit = lineal_m, "lm"
    elif count > 0:
        quantity, unit = count, "No."
    else:
        quantity = 0.0
        unit = pb_unit("", labour, 0)

    section = pb_section("", labour, location, raw_substrate, int_ext)
    substrate = pb_substrate(raw_substrate, labour, location)
    finish_system = pb_finish_system(section, labour, substrate, "", source_note)
    inclusion = pb_inclusion_status("", source_note)
    qstatus = "Excluded" if inclusion == "EXCLUSION" else pb_quantity_status("", quantity, confidence)

    rate = _float(row.get("rate_ex_gst"))
    if rate <= 0:
        rate = app.default_rate_for(substrate, labour, finish_system, unit)

    row_id = row.get("id")
    source_ref = f"JobHub take-off row #{row_id}" if row_id is not None else "JobHub take-off"
    if source_note:
        source_ref += f" · {source_note}"

    return {
        "section": section,
        "element": labour,
        "location": location,
        "substrate": substrate,
        "finish_system": finish_system,
        "quantity": quantity,
        "unit": unit,
        "quantity_status": qstatus,
        "source_page": "JobHub import",
        "source_reference": source_ref,
        "inclusion_status": inclusion,
        "coats": _float(row.get("coats"), 3.0) or 3.0,
        "coverage_m2_per_litre": 12.0,
        "productivity_m2_per_hour": 8.0,
        "rate_per_unit": rate,
        "confidence": confidence or ("To review" if quantity <= 0 else "Imported"),
        "notes": (
            f"Imported from JobHub"
            f" · stored labour {_float(row.get('labour_hours')):.1f} hrs"
            f" · stored paint {_float(row.get('paint_litres')):.1f} L"
            f" · stored value ${_float(row.get('value_ex_gst')):,.2f}"
        ),
    }


def make_pull_takeoff_from_jobhub(app):
    def pull_takeoff_from_jobhub(workspace_id: int, bridge) -> int:
        """
        Pull the shared JobHub take-off into the PB schedule without throwing away
        rows just because old JobHub records have incomplete labels.
        """
        workspace = app.lquery("SELECT * FROM workspaces WHERE id=?", (workspace_id,))[0]
        job_id = workspace.get("jobhub_job_id")
        if not job_id:
            raise RuntimeError("This workspace is not linked to a JobHub job.")

        tables = set(bridge.table_names())
        if "job_takeoff_rows" not in tables:
            raise RuntimeError("JobHub does not have a job_takeoff_rows table for linked take-offs.")

        cols = set(bridge.columns("job_takeoff_rows"))
        selectable = [
            c
            for c in [
                "id",
                "internal_external",
                "area_location",
                "substrate",
                "labour_category",
                "qty_m2",
                "lineal_m",
                "count",
                "coats",
                "rate_ex_gst",
                "labour_hours",
                "paint_litres",
                "value_ex_gst",
                "source_note",
                "confidence",
                "updated_at",
            ]
            if c in cols
        ]
        if not selectable:
            raise RuntimeError("JobHub take-off table exists but has no compatible columns.")

        rows = bridge.query(
            f"SELECT {', '.join(selectable)} FROM job_takeoff_rows WHERE job_id=? ORDER BY id",
            (int(job_id),),
        )
        if not rows:
            return 0

        app.lexecute(
            "DELETE FROM takeoff_rows WHERE workspace_id=? AND source_page='JobHub import'",
            (workspace_id,),
        )

        app.lexecute(
            """DELETE FROM register_items
               WHERE workspace_id=? AND register_name='clarifications'
               AND source_reference='JobHub take-off import'""",
            (workspace_id,),
        )

        created = 0
        for raw in rows:
            mapped = _jobhub_row_to_takeoff(app, raw)
            values = [mapped.get(col, "") for col in app.TAKEOFF_COLUMNS]
            app.lexecute(
                """INSERT INTO takeoff_rows(
                       workspace_id,section,element,location,substrate,finish_system,
                       quantity,unit,quantity_status,source_page,source_reference,inclusion_status,
                       coats,coverage_m2_per_litre,productivity_m2_per_hour,rate_per_unit,
                       confidence,notes,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (workspace_id, *values, app.now_stamp(), app.now_stamp()),
            )
            created += 1

            needs_review = (
                mapped["location"] == "Unallocated / review"
                or mapped["substrate"] == "Other"
                or (mapped["quantity_status"] == "To measure" and _float(mapped["quantity"]) <= 0)
            )
            if needs_review:
                app.lexecute(
                    """INSERT INTO register_items(
                           workspace_id,register_name,item_no,title,detail,priority,
                           source_reference,status,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        workspace_id,
                        "clarifications",
                        "",
                        "Review imported JobHub take-off row",
                        f"{mapped['element']} · {mapped['location']} · {mapped['substrate']}. "
                        "Confirm the intended surface, location and quantity before pricing.",
                        "High",
                        "JobHub take-off import",
                        "Open",
                        app.now_stamp(),
                    ),
                )

        app.lexecute(
            "UPDATE workspaces SET status='Draft', updated_at=? WHERE id=?",
            (app.now_stamp(), workspace_id),
        )
        return created

    return pull_takeoff_from_jobhub


def install(app) -> None:
    """Install Premier Brushworks overrides into the imported PlanReader module."""
    app.PB_METHOD_VERSION = PB_METHOD_VERSION
    app.run_ai_plan_read = make_run_ai_plan_read(app)
    app.pull_takeoff_from_jobhub = make_pull_takeoff_from_jobhub(app)
