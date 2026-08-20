"""PlanReader v1.2.27 legend-first drawing interpretation.

Architectural sets often define project-specific abbreviations, symbols and material
codes on one or more Legend / Abbreviations / Key sheets.  Those definitions must
be read before interpreting plans, elevations and schedules.

Evidence policy:
- estimator/manual corrections remain highest priority;
- explicit finishing/material schedules remain authoritative for coating systems;
- selected legend sheets define project abbreviations and material shorthand;
- conflicting legend definitions are surfaced instead of guessed;
- deselected legend sheets contribute no active evidence.
"""
from __future__ import annotations

import contextvars
import json
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pb_auto_geometry_v1219 as auto
import pb_autopilot_v1223 as autopilot
import pb_context_floorarea_v1224 as context_guard
import pb_material_schedule_v1222 as material
import pb_page_registration_v1225 as registration
import pb_registration_priority_guard_v1225 as registration_priority

VERSION = "1.2.27"
PAGE_TYPE = "Legend / Abbreviations / Key"
SETTING_KEY = "legend_register_v1227"

LEGEND_TITLE_PHRASES = (
    "legend & abbreviations", "legend and abbreviations", "abbreviations & symbols",
    "abbreviations and symbols", "symbols & abbreviations", "symbols and abbreviations",
    "architectural legend", "general legend", "drawing legend", "abbreviation legend",
    "abbreviations legend", "symbol legend", "symbols legend", "material legend",
    "materials legend", "finish legend", "finishes legend", "wall type legend",
    "partition legend", "drawing key & legend", "drawing key and legend",
)
LEGEND_STRONG_WORDS = ("legend", "abbreviations", "abbreviation", "symbols", "symbol key")
_HEADER_CODES = {
    "LEGEND", "ABBREVIATION", "ABBREVIATIONS", "SYMBOL", "SYMBOLS", "CODE", "CODES",
    "DESCRIPTION", "DESCRIPTIONS", "MEANING", "MEANINGS", "KEY", "NOTES", "NOTE",
    "TYPE", "TYPES", "GENERAL", "ARCHITECTURAL", "MATERIAL", "MATERIALS", "FINISH", "FINISHES",
}
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9._/+-]{0,14}$")
_START_RE = re.compile(r"^\s*([A-Z][A-Z0-9._/+-]{0,14})\s*(?:[:=\-–—]\s*|\s+)(.+?)\s*$")
_INLINE_PAIR_RE = re.compile(r"\b([A-Z][A-Z0-9._/+-]{1,10})\b\s*(?:[:=\-–—])\s*([^;|]{3,160})")

_level_codes = {"AFFL", "FFL", "FCL", "RL", "NGL", "SSL", "TOS", "TOC", "CL", "C/L"}
_general_drawing_codes = {"TYP", "UNO", "UON", "NTS", "COS", "EQ", "EX", "EXT", "INT", "CJ", "EJ"}
_material_words = (
    "cladding", "substrate", "render", "blockwork", "masonry", "plasterboard", "gypsum",
    "fibre cement", "fiber cement", "fc sheet", "weatherboard", "linea", "easylap",
    "textureboard", "timber", "steel", "metal", "aluminium", "aluminum", "soffit", "eave",
    "balustrade", "screen", "concrete", "precast", "brick", "roof sheet", "gutter", "capping",
)
_level_words = (
    "finished floor level", "above finished floor", "finished ceiling level", "reduced level",
    "natural ground level", "structural slab level", "top of steel", "top of concrete",
    "centre line", "center line", "dimension", "level datum", "datum level",
)
_drawing_tag_words = (
    "wall type", "partition type", "door type", "window type", "detail", "keynote", "key note",
    "panel type", "assembly type", "wall piece", "piece mark", "drawing reference", "section reference",
)

_legend_context: contextvars.ContextVar[Dict[str, Dict[str, Any]]] = contextvars.ContextVar(
    "planreader_legend_v1227", default={}
)
_material_legend_context: contextvars.ContextVar[Dict[str, Dict[str, Any]]] = contextvars.ContextVar(
    "planreader_material_legend_v1227", default={}
)


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _low(value: Any) -> str:
    return _norm(value).lower()


def _json(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except Exception:
        return fallback


def _legend_heading_text(page: Dict[str, Any]) -> str:
    return f"{page.get('page_label') or ''}\n{page.get('page_type') or ''}\n{page.get('extracted_text') or ''}"


def is_legend_page(page: Dict[str, Any]) -> bool:
    kind = str(page.get("page_type") or "").strip()
    if kind == PAGE_TYPE:
        return True
    text = _low(_legend_heading_text(page))
    if any(phrase in text for phrase in LEGEND_TITLE_PHRASES):
        return True
    # A standalone heading such as "ABBREVIATIONS" is common.  Requiring several
    # short code-definition lines prevents an ordinary plan containing a small key
    # from becoming a project legend by accident.
    heading = any(re.search(rf"(?im)^\s*{re.escape(word)}\s*$", str(page.get("extracted_text") or "")) for word in ("legend", "abbreviations", "symbols"))
    if heading:
        return len(parse_legend_text(page.get("extracted_text"), int(page.get("id") or 0), str(page.get("page_label") or ""))) >= 2
    return False


def infer_category(code: str, description: Any) -> str:
    code_u = str(code or "").upper()
    low = _low(description)
    if code_u in _level_codes or any(word in low for word in _level_words):
        return "level / dimension"
    if context_guard.has_non_finish_context(low) or any(word in low for word in _drawing_tag_words):
        return "drawing tag"
    if context_guard.has_finish_context(low):
        return "finish"
    if any(word in low for word in _material_words):
        return "material / substrate"
    if code_u in _general_drawing_codes:
        return "drawing abbreviation"
    return "general abbreviation"


def _valid_definition(code: str, description: str) -> bool:
    code = str(code or "").strip().upper()
    description = _norm(description)
    if not _CODE_RE.fullmatch(code) or code in _HEADER_CODES:
        return False
    if len(code) < 2 and not any(ch.isdigit() for ch in code):
        return False
    if len(description) < 3 or not re.search(r"[A-Za-z]", description):
        return False
    if description.upper() in _HEADER_CODES:
        return False
    return True


def parse_legend_text(text: Any, page_id: int = 0, page_label: str = "") -> List[Dict[str, Any]]:
    """Extract project abbreviation definitions from text-preserving PDF lines."""
    raw_lines = [str(line).strip() for line in str(text or "").splitlines() if str(line).strip()]
    out: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()
    for raw in raw_lines:
        line = _norm(raw)
        candidates: List[Tuple[str, str]] = []
        match = _START_RE.match(line)
        if match:
            candidates.append((match.group(1), match.group(2)))
        candidates.extend((m.group(1), m.group(2)) for m in _INLINE_PAIR_RE.finditer(line))
        for code, description in candidates:
            code = str(code).strip().upper()
            description = _norm(description).strip(" .;|:-–—")
            if not _valid_definition(code, description):
                continue
            key = (code, _low(description))
            if key in seen:
                continue
            seen.add(key)
            category = infer_category(code, description)
            out.append({
                "code": code,
                "description": description[:400],
                "category": category,
                "substrate": material._infer_substrate(description) if category == "material / substrate" else "",
                "finish": description if category == "finish" else "",
                "page_id": int(page_id or 0),
                "page_label": str(page_label or ""),
                "source_line": raw[:500],
            })
    return out


def _compatible(a: Any, b: Any) -> bool:
    left, right = material._normalise(a), material._normalise(b)
    if not left or not right:
        return False
    if left == right:
        return True
    if len(left) >= 8 and (left in right or right in left):
        return True
    left_words, right_words = set(left.split()), set(right.split())
    shared = left_words & right_words
    return bool(shared and len(shared) / max(1, min(len(left_words), len(right_words))) >= 0.65)


def build_legend_register(app: Any, workspace_id: int) -> Dict[str, Any]:
    pages = [dict(row) for row in app.lquery(
        """SELECT id,page_no,page_label,page_type,extracted_text,document_id,image_path,render_zoom
           FROM pages WHERE workspace_id=? AND COALESCE(selected,0)=1 ORDER BY document_id,page_no,id""",
        (int(workspace_id),),
    )]
    legend_pages = [page for page in pages if is_legend_page(page)]
    definitions: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for page in legend_pages:
        for item in parse_legend_text(page.get("extracted_text"), int(page["id"]), str(page.get("page_label") or "")):
            definitions[item["code"]].append(item)

    dictionary: Dict[str, Dict[str, Any]] = {}
    conflicts: List[Dict[str, Any]] = []
    for code, items in sorted(definitions.items()):
        representative = dict(items[0])
        disagreements = [item for item in items[1:] if not _compatible(representative.get("description"), item.get("description"))]
        categories = {str(item.get("category") or "") for item in items if item.get("category")}
        substrates = {str(item.get("substrate") or "") for item in items if item.get("substrate")}
        finishes = {str(item.get("finish") or "") for item in items if item.get("finish")}
        status = "Conflict" if disagreements or len(categories) > 1 or len(substrates) > 1 or len(finishes) > 1 else "Confirmed"
        entry = {
            "code": code,
            "description": str(representative.get("description") or ""),
            "category": next(iter(categories)) if len(categories) == 1 else str(representative.get("category") or "general abbreviation"),
            "substrate": next(iter(substrates)) if len(substrates) == 1 else "",
            "finish": next(iter(finishes)) if len(finishes) == 1 else "",
            "status": status,
            "sources": [dict(item) for item in items],
        }
        dictionary[code] = entry
        if status == "Conflict":
            conflicts.append({
                "category": "Legend conflict", "severity": "High", "code": code,
                "page_id": int(representative.get("page_id") or 0), "page_label": str(representative.get("page_label") or ""),
                "message": f"{code} has conflicting meanings across the selected legend/abbreviation sheets.",
                "source": str(representative.get("source_line") or ""), "bbox": None, "bbox_mode": "xyxy",
            })

    # Register where each known abbreviation is actually used on selected non-legend drawings.
    occurrences: List[Dict[str, Any]] = []
    confirmed_codes = {code for code, entry in dictionary.items() if entry.get("status") == "Confirmed"}
    for page in pages:
        if page in legend_pages or not confirmed_codes:
            continue
        lines: List[Dict[str, Any]] = []
        try:
            lines = list(auto._pdf_word_lines(app, page) or [])
        except Exception:
            lines = []
        if lines:
            iterable = [(str(line.get("text") or ""), list(line.get("bbox") or []) or None) for line in lines]
        else:
            iterable = [(line, None) for line in str(page.get("extracted_text") or "").splitlines()]
        for line_text, bbox in iterable:
            upper = str(line_text or "").upper()
            for code in confirmed_codes:
                if re.search(rf"(?<![A-Z0-9]){re.escape(code)}(?![A-Z0-9])", upper):
                    occurrences.append({
                        "code": code, "page_id": int(page["id"]), "page_label": str(page.get("page_label") or ""),
                        "page_type": str(page.get("page_type") or ""), "text": str(line_text or ""), "bbox": bbox,
                        "meaning": dictionary[code].get("description", ""), "category": dictionary[code].get("category", ""),
                    })

    state = {
        "version": VERSION,
        "legend_pages": [int(page["id"]) for page in legend_pages],
        "dictionary": dictionary,
        "occurrences": occurrences,
        "conflicts": conflicts,
        "analysed_at": app.now_stamp(),
    }
    app.set_workspace_setting(int(workspace_id), SETTING_KEY, json.dumps(state, separators=(",", ":"), default=str))
    return state


def get_legend_register(app: Any, workspace_id: int) -> Dict[str, Any]:
    raw = app.workspace_setting(int(workspace_id), SETTING_KEY, "{}")
    return dict(_json(raw, {}) or {})


def expand_abbreviations(text: Any, dictionary: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    """Append only definitions that are actually referenced in the supplied text."""
    original = str(text or "")
    dictionary = dictionary if dictionary is not None else _legend_context.get()
    expansions = []
    upper = original.upper()
    for code, entry in (dictionary or {}).items():
        if entry.get("status") != "Confirmed":
            continue
        if re.search(rf"(?<![A-Z0-9]){re.escape(str(code).upper())}(?![A-Z0-9])", upper):
            expansions.append(f"{code} = {entry.get('description')}")
    if not expansions:
        return original
    return original + "\n[PROJECT LEGEND INTERPRETATION]\n" + "\n".join(expansions)


def legend_material_entries(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for code, raw in (state.get("dictionary") or {}).items():
        item = dict(raw or {})
        if item.get("status") != "Confirmed" or item.get("category") not in {"material / substrate", "finish"}:
            continue
        description = str(item.get("description") or "")
        source = dict((item.get("sources") or [{}])[0])
        candidate = {
            "code": str(code).upper(), "description": description,
            "substrate": str(item.get("substrate") or material._infer_substrate(description) or ""),
            "finish": str(item.get("finish") or ""), "status": "Confirmed", "legend": True,
            "sources": [{
                "code": str(code).upper(), "description": description,
                "substrate": str(item.get("substrate") or material._infer_substrate(description) or ""),
                "finish": str(item.get("finish") or ""), "page_id": int(source.get("page_id") or 0),
                "page_label": str(source.get("page_label") or ""), "source_line": str(source.get("source_line") or ""),
            }],
        }
        # A PT/PF/WF-looking legend item must still pass the anti-false-positive guard.
        guard_item = dict(candidate["sources"][0])
        if not context_guard.accept_finish_schedule_item(guard_item, description, source.get("page_label")):
            if context_guard.is_finish_style_code(code):
                continue
        out[str(code).upper()] = candidate
    return out


def merge_legend_into_material_dictionary(base_state: Dict[str, Any], legend_state: Dict[str, Any]) -> Dict[str, Any]:
    """Explicit finish/material schedules win; legend fills project-specific gaps."""
    state = dict(base_state or {})
    dictionary = {str(code).upper(): dict(item) for code, item in (state.get("dictionary") or {}).items()}
    for code, legend_item in legend_material_entries(legend_state).items():
        existing = dictionary.get(code)
        if existing and existing.get("manual"):
            continue
        if existing and existing.get("status") in {"Confirmed", "Conflict"}:
            # Preserve an explicit schedule definition but retain legend provenance.
            sources = list(existing.get("sources") or []) + list(legend_item.get("sources") or [])
            existing["sources"] = sources
            existing["legend_cross_reference"] = legend_item.get("description")
            dictionary[code] = existing
        else:
            dictionary[code] = legend_item
    state["dictionary"] = dictionary
    state["legend_pages"] = list(legend_state.get("legend_pages") or [])
    return state


def _material_codes_from_context(text: Any, base_codes) -> List[str]:
    codes = set(base_codes(text) or [])
    upper = str(text or "").upper()
    for code, entry in _material_legend_context.get().items():
        if re.search(rf"(?<![A-Z0-9]){re.escape(code.upper())}(?![A-Z0-9])", upper):
            codes.add(code.upper())
    return sorted(codes)


def _legend_page_relevance(base_relevance, page_type: Any, text: Any = "", label: Any = ""):
    kind = str(page_type or "")
    hay = _low(f"{label or ''}\n{text or ''}")
    if kind == PAGE_TYPE or any(phrase in hay for phrase in LEGEND_TITLE_PHRASES):
        return True, "Legend/abbreviation sheet required before drawing interpretation", 100
    return base_relevance(page_type, text, label)


def _legend_strong_evidence(base_evidence, page: Dict[str, Any]):
    if is_legend_page(dict(page)):
        return True, 100, "Project legend/abbreviations required before downstream drawing interpretation"
    return base_evidence(page)


def legend_panel(app: Any, workspace: Dict[str, Any]) -> None:
    workspace_id = int(workspace["id"])
    state = get_legend_register(app, workspace_id)
    dictionary = state.get("dictionary") or {}
    legend_pages = set(int(v) for v in state.get("legend_pages") or [])
    selected_legend_count = len(legend_pages)
    app.st.markdown("### 📖 Project Legend / Abbreviation Register")
    c1, c2, c3, c4 = app.st.columns(4)
    c1.metric("Legend sheets", selected_legend_count)
    c2.metric("Abbreviations", len(dictionary))
    c3.metric("Confirmed", sum(1 for item in dictionary.values() if item.get("status") == "Confirmed"))
    c4.metric("Conflicts", len(state.get("conflicts") or []))
    if not dictionary:
        app.st.info("No selected Legend / Abbreviations / Key sheet has produced a project dictionary yet. Select the legend sheet in Drawing Register and re-run Autopilot.")
        return
    rows = []
    occurrence_counts: Dict[str, int] = defaultdict(int)
    for occurrence in state.get("occurrences") or []:
        occurrence_counts[str(occurrence.get("code") or "").upper()] += 1
    for code, item in sorted(dictionary.items()):
        rows.append({
            "Abbreviation / code": code, "Meaning": item.get("description") or "",
            "Category": item.get("category") or "", "Substrate": item.get("substrate") or "",
            "Status": item.get("status") or "", "Drawing uses": occurrence_counts.get(code, 0),
            "Source": ", ".join(sorted({str(src.get('page_label') or '') for src in item.get('sources') or [] if src.get('page_label')})),
        })
    app.st.dataframe(app.pd.DataFrame(rows), hide_index=True, use_container_width=True, height=min(520, 75 + len(rows) * 34))
    if state.get("conflicts"):
        app.st.warning("Conflicting abbreviation meanings were found. Those abbreviations are not automatically expanded until corrected.")
        app.st.dataframe(app.pd.DataFrame(state["conflicts"]), hide_index=True, use_container_width=True)
    app.st.caption("Legend definitions are read before plans/elevations. Explicit finishing schedules still control coating-system details, and manual estimator corrections remain highest priority.")


def apply(app: Any) -> None:
    if getattr(app, "_pb_legend_register_v1227_applied", False):
        return
    app._pb_legend_register_v1227_applied = True

    # Expose the new sheet type in Drawing Register editing and make automatic
    # registration strongly recognise actual legend/abbreviation sheet headings.
    if PAGE_TYPE not in app.PAGE_TYPES:
        insert_at = 1 if "Title / Drawing Register" in app.PAGE_TYPES else 0
        app.PAGE_TYPES.insert(insert_at, PAGE_TYPE)
    registration._TYPE_RULES[PAGE_TYPE] = tuple(LEGEND_TITLE_PHRASES) + ("abbreviations", "architectural symbols")
    if PAGE_TYPE not in registration._TYPE_PRIORITY:
        registration._TYPE_PRIORITY.insert(0, PAGE_TYPE)
    heading_rule = (PAGE_TYPE, tuple(LEGEND_TITLE_PHRASES) + ("abbreviations", "architectural legend", "general legend"))
    if not any(item[0] == PAGE_TYPE for item in registration_priority._HEADING_RULES):
        registration_priority._HEADING_RULES = (heading_rule,) + tuple(registration_priority._HEADING_RULES)

    # Legend sheets are painting-estimating evidence even though they usually have
    # no measurable geometry of their own.
    auto._KEEP_TYPES.add(PAGE_TYPE)
    base_relevance = auto.page_relevance
    auto.page_relevance = lambda page_type, text="", label="": _legend_page_relevance(base_relevance, page_type, text, label)
    base_strong_evidence = autopilot._strong_page_evidence
    autopilot._strong_page_evidence = lambda page: _legend_strong_evidence(base_strong_evidence, dict(page))

    # Material/code resolver keeps its explicit-schedule precedence, while project
    # legend material shorthand fills gaps. Only material/finish legend codes enter
    # material occurrence scanning; general abbreviations do not create fake paint issues.
    base_material_builder = material.build_material_dictionary
    def _legend_material_builder(app_obj: Any, workspace_id: int):
        legend_state = get_legend_register(app_obj, int(workspace_id))
        if not legend_state:
            legend_state = build_legend_register(app_obj, int(workspace_id))
        return merge_legend_into_material_dictionary(base_material_builder(app_obj, int(workspace_id)), legend_state)
    material.build_material_dictionary = _legend_material_builder

    base_codes = material._codes
    material._codes = lambda text: _material_codes_from_context(text, base_codes)

    base_substrates = auto._substrates_from_text
    def _legend_substrates(text: Any):
        return base_substrates(expand_abbreviations(text, _legend_context.get()))
    auto._substrates_from_text = _legend_substrates

    # This is the core ordering guarantee: every automatic geometry/take-off/3D run
    # rebuilds the selected legend dictionary first and holds it in context for all
    # downstream interpretation during that run.
    base_analyse = auto.analyse_workspace
    def _legend_first_analyse(app_obj: Any, workspace_id: int):
        state = build_legend_register(app_obj, int(workspace_id))
        full_dictionary = dict(state.get("dictionary") or {})
        material_dictionary = legend_material_entries(state)
        token_all = _legend_context.set(full_dictionary)
        token_material = _material_legend_context.set(material_dictionary)
        try:
            report = base_analyse(app_obj, int(workspace_id))
        finally:
            _material_legend_context.reset(token_material)
            _legend_context.reset(token_all)
        if isinstance(report, dict):
            report["legend_pages"] = len(state.get("legend_pages") or [])
            report["legend_abbreviations"] = len(full_dictionary)
            report["legend_conflicts"] = len(state.get("conflicts") or [])
        return report
    auto.analyse_workspace = _legend_first_analyse

    # Show the project language before material-code issues so the estimator can see
    # how PlanReader is interpreting the drawings before reviewing downstream rows.
    base_review_panel = material.review_panel
    def _review_with_legend(app_obj: Any, workspace: Dict[str, Any]):
        legend_panel(app_obj, workspace)
        app_obj.st.divider()
        return base_review_panel(app_obj, workspace)
    material.review_panel = _review_with_legend

    app.build_legend_register = lambda workspace_id: build_legend_register(app, int(workspace_id))
    app.legend_register = lambda workspace_id: get_legend_register(app, int(workspace_id))
    app.interpret_drawing_text = lambda workspace_id, text: expand_abbreviations(text, (get_legend_register(app, int(workspace_id)).get("dictionary") or {}))
