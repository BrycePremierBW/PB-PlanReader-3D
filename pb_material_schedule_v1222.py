"""PlanReader v1.2.22 finishing-schedule resolver and drawing review queue.

The schedule defines what a material/finish code means. Drawing geometry defines
where it occurs and how much can be measured. Unresolved or conflicting evidence
is never guessed: it is surfaced as a review issue on the referenced sheet.
"""
from __future__ import annotations

import contextvars
import io
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw

import pb_auto_geometry_v1219 as auto
import pb_memory_stability_v1220 as memory

VERSION = "1.2.22"
SETTING_KEY = "material_schedule_v1222"
CODE_RE = re.compile(
    r"\b(?:EC\d+|FC\d+|RBL\d*|SOF\d*|CL\d+|PT\d+|PF\d+|WF\d+|BA\d+|SCR\d*|SHD\d*|DP\d*|GD\d*|RS\d*|BC\d*)\b",
    re.IGNORECASE,
)
SCHEDULE_WORDS = (
    "finish schedule", "finishes schedule", "finishing schedule", "material schedule",
    "colour schedule", "color schedule", "external finishes", "paint schedule",
)
_MATERIAL_HINTS: Sequence[Tuple[Tuple[str, ...], str]] = (
    (("lineaboard", "linea"), "Lineaboard Cladding"),
    (("textureboard",), "Textureboard Cladding"),
    (("easylap",), "Easylap Cladding"),
    (("fibre cement", "fiber cement", "fc sheet", "fc cladding"), "Fibre Cement Cladding"),
    (("render", "rendered", "blockwork", "masonry"), "Rendered / Blockwork"),
    (("timber", "weatherboard"), "Timber / Weatherboard Cladding"),
    (("soffit", "eave"), "Soffits / Eaves"),
    (("screen",), "Screens"),
    (("balustrade",), "Balustrade"),
    (("sunhood", "sun hood"), "Sunhoods"),
    (("downpipe",), "Downpipes"),
    (("garage door",), "Garage Doors"),
    (("roof sheet", "roofing"), "Roof Sheet"),
    (("gutter", "capping", "parapet cap"), "Cappings & Gutters"),
)
_FINISH_HINTS = (
    "dulux", "haymes", "taubmans", "resene", "wattyl", "low sheen", "semi gloss",
    "semigloss", "matt", "matte", "gloss", "satin", "paint", "colour", "color",
    "primer", "undercoat", "topcoat", "clear finish", "stain",
)
_resolver_context: contextvars.ContextVar[Dict[str, Dict[str, Any]]] = contextvars.ContextVar(
    "planreader_material_resolver_v1222", default={}
)


def _normalise(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _codes(value: Any) -> List[str]:
    return sorted({match.group(0).upper() for match in CODE_RE.finditer(str(value or ""))})


def _schedule_page(page: Dict[str, Any]) -> bool:
    kind = str(page.get("page_type") or "").lower()
    text = f"{page.get('page_label') or ''} {page.get('extracted_text') or ''}".lower()
    return any(token in kind for token in ("finish", "specification", "colour", "color", "material schedule")) or any(
        token in text for token in SCHEDULE_WORDS
    )


def _infer_substrate(description: Any) -> str:
    low = str(description or "").lower()
    for needles, name in _MATERIAL_HINTS:
        if any(token in low for token in needles):
            return name
    return ""


def _infer_finish(description: Any, code: str = "") -> str:
    low = str(description or "").lower()
    if code.upper().startswith(("PT", "PF", "WF")) or any(token in low for token in _FINISH_HINTS):
        return re.sub(r"\s+", " ", str(description or "")).strip()
    return ""


def _compatible_descriptions(a: Any, b: Any) -> bool:
    left, right = _normalise(a), _normalise(b)
    if not left or not right:
        return False
    return left == right or (len(left) >= 8 and left in right) or (len(right) >= 8 and right in left)


def parse_schedule_text(text: Any, page_id: int = 0, page_label: str = "") -> List[Dict[str, Any]]:
    """Extract code definitions, allowing schedule descriptions to wrap onto following lines."""
    lines = [re.sub(r"\s+", " ", raw).strip() for raw in str(text or "").splitlines() if str(raw).strip()]
    out: List[Dict[str, Any]] = []
    for idx, line in enumerate(lines):
        codes = _codes(line)
        if len(codes) != 1:
            continue
        code = codes[0]
        desc = CODE_RE.sub(" ", line)
        desc = re.sub(r"^[\s:;|\-–—]+|[\s:;|\-–—]+$", "", desc).strip()
        parts = [desc] if len(_normalise(desc)) >= 3 else []
        # Many schedules use one cell/line for the code and the next cells/lines for description.
        for nxt in range(idx + 1, min(len(lines), idx + 4)):
            if _codes(lines[nxt]):
                break
            if len(_normalise(lines[nxt])) >= 3:
                parts.append(lines[nxt])
            if len(" ".join(parts)) >= 40:
                break
        description = re.sub(r"\s+", " ", " ".join(parts)).strip()
        if not description:
            continue
        out.append({
            "code": code,
            "description": description[:300],
            "substrate": _infer_substrate(description),
            "finish": _infer_finish(description, code),
            "page_id": int(page_id or 0),
            "page_label": str(page_label or ""),
            "source_line": line,
        })
    return out


def build_material_dictionary(app: Any, workspace_id: int) -> Dict[str, Any]:
    pages = app.lquery(
        "SELECT id,page_label,page_type,extracted_text,image_path,document_id,page_no,render_zoom FROM pages WHERE workspace_id=? ORDER BY id",
        (int(workspace_id),),
    )
    definitions: Dict[str, List[Dict[str, Any]]] = {}
    schedule_pages: List[int] = []
    for raw in pages:
        page = dict(raw)
        if not _schedule_page(page):
            continue
        schedule_pages.append(int(page["id"]))
        for item in parse_schedule_text(page.get("extracted_text"), int(page["id"]), str(page.get("page_label") or "")):
            definitions.setdefault(item["code"], []).append(item)

    dictionary: Dict[str, Dict[str, Any]] = {}
    issues: List[Dict[str, Any]] = []
    for code, items in sorted(definitions.items()):
        representative = items[0]
        conflicting = [item for item in items[1:] if not _compatible_descriptions(representative["description"], item["description"])]
        status = "Conflict" if conflicting else "Confirmed"
        substrate_names = {item.get("substrate") for item in items if item.get("substrate")}
        finish_names = {item.get("finish") for item in items if item.get("finish")}
        dictionary[code] = {
            "code": code,
            "description": representative["description"],
            "substrate": next(iter(substrate_names)) if len(substrate_names) == 1 else "",
            "finish": next(iter(finish_names)) if len(finish_names) == 1 else "",
            "status": status,
            "sources": items,
        }
        if conflicting or len(substrate_names) > 1 or len(finish_names) > 1:
            issues.append({
                "category": "Schedule conflict", "severity": "High", "code": code,
                "page_id": int(representative.get("page_id") or 0), "page_label": representative.get("page_label", ""),
                "message": f"{code} has conflicting definitions in the finishing/material schedule.",
                "bbox": None, "bbox_mode": "xyxy", "source": representative.get("source_line", ""),
            })
    return {"dictionary": dictionary, "schedule_pages": schedule_pages, "issues": issues}


def _page_occurrences(app: Any, page: Dict[str, Any], dictionary: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    occurrences: List[Dict[str, Any]] = []
    lines = []
    try:
        lines = auto._pdf_word_lines(app, page)
    except Exception:
        lines = []
    if lines:
        for line in lines:
            for code in _codes(line.get("text")):
                entry = dictionary.get(code)
                occurrences.append({
                    "code": code, "page_id": int(page["id"]), "page_label": str(page.get("page_label") or ""),
                    "page_type": str(page.get("page_type") or ""), "text": str(line.get("text") or ""),
                    "bbox": list(line.get("bbox") or []) or None, "bbox_mode": "xyxy",
                    "status": entry.get("status") if entry else "Unknown",
                    "substrate": entry.get("substrate", "") if entry else "",
                    "finish": entry.get("finish", "") if entry else "",
                    "description": entry.get("description", "") if entry else "",
                })
        return occurrences
    for code in _codes(page.get("extracted_text")):
        entry = dictionary.get(code)
        occurrences.append({
            "code": code, "page_id": int(page["id"]), "page_label": str(page.get("page_label") or ""),
            "page_type": str(page.get("page_type") or ""), "text": code, "bbox": None, "bbox_mode": "xyxy",
            "status": entry.get("status") if entry else "Unknown",
            "substrate": entry.get("substrate", "") if entry else "",
            "finish": entry.get("finish", "") if entry else "",
            "description": entry.get("description", "") if entry else "",
        })
    return occurrences


def build_material_state(app: Any, workspace_id: int) -> Dict[str, Any]:
    state = build_material_dictionary(app, int(workspace_id))
    dictionary = state["dictionary"]
    pages = app.lquery(
        """SELECT p.id,p.page_label,p.page_type,p.extracted_text,p.image_path,p.document_id,p.page_no,p.render_zoom,p.px_per_m
           FROM pages p WHERE p.workspace_id=? AND COALESCE(p.selected,0)=1 ORDER BY p.id""",
        (int(workspace_id),),
    )
    occurrences: List[Dict[str, Any]] = []
    schedule_ids = set(state.get("schedule_pages") or [])
    for raw in pages:
        page = dict(raw)
        if int(page["id"]) in schedule_ids:
            continue
        occurrences.extend(_page_occurrences(app, page, dictionary))
    state["occurrences"] = occurrences
    for item in occurrences:
        if item["status"] == "Unknown":
            state["issues"].append({
                "category": "Unknown material code", "severity": "High", "code": item["code"],
                "page_id": item["page_id"], "page_label": item["page_label"],
                "message": f"{item['code']} is referenced on this drawing but no confirmed definition was found in the finishing/material schedule.",
                "bbox": item.get("bbox"), "bbox_mode": item.get("bbox_mode", "xyxy"), "source": item.get("text", ""),
            })
        elif item["status"] == "Conflict":
            state["issues"].append({
                "category": "Conflicting material code", "severity": "High", "code": item["code"],
                "page_id": item["page_id"], "page_label": item["page_label"],
                "message": f"{item['code']} is used here but its schedule definition conflicts elsewhere.",
                "bbox": item.get("bbox"), "bbox_mode": item.get("bbox_mode", "xyxy"), "source": item.get("text", ""),
            })
    return state


def resolved_substrates_from_text(base_substrates, text: Any) -> List[Dict[str, str]]:
    base = list(base_substrates(text) or [])
    resolver = _resolver_context.get()
    codes = _codes(text)
    resolved: List[Dict[str, str]] = []
    resolved_codes: set[str] = set()
    for code in codes:
        entry = resolver.get(code)
        if entry and entry.get("status") == "Confirmed" and entry.get("substrate"):
            resolved.append({"code": code, "name": str(entry["substrate"])})
            resolved_codes.add(code)
    if not resolved:
        return base
    names = {_normalise(item["name"]) for item in resolved}
    for item in base:
        code = str(item.get("code") or "").upper()
        name = _normalise(item.get("name"))
        if code in resolved_codes:
            continue
        if any(name == existing or (name and existing and (name in existing or existing in name)) for existing in names):
            continue
        resolved.append({"code": code, "name": str(item.get("name") or code)})
    return resolved


def _page_map(app: Any, workspace_id: int) -> Tuple[Dict[int, Dict[str, Any]], Dict[str, int]]:
    rows = app.lquery(
        "SELECT id,page_label,page_type,image_path,px_per_m FROM pages WHERE workspace_id=? ORDER BY id",
        (int(workspace_id),),
    )
    by_id = {int(row["id"]): dict(row) for row in rows}
    by_label = {str(row.get("page_label") or "").strip(): int(row["id"]) for row in rows if str(row.get("page_label") or "").strip()}
    return by_id, by_label


def build_review_issues(app: Any, workspace_id: int, report: Dict[str, Any], state: Dict[str, Any]) -> List[Dict[str, Any]]:
    issues = [dict(item) for item in state.get("issues") or []]
    by_id, by_label = _page_map(app, workspace_id)

    for item in report.get("calibrations") or []:
        confidence = str(item.get("confidence") or "").lower()
        method = str(item.get("method") or "")
        if "provisional" in confidence or "printed scale" in method.lower():
            page_id = int(item.get("page_id") or 0)
            issues.append({
                "category": "Calibration", "severity": "Medium", "page_id": page_id,
                "page_label": by_id.get(page_id, {}).get("page_label", ""), "code": "",
                "message": "Page scale is provisional. Confirm against a documented dimension before final issue.",
                "bbox": None, "bbox_mode": "xyxy", "source": method,
            })

    for unit in report.get("units") or []:
        if str(unit.get("confidence") or "") == "Derived":
            issues.append({
                "category": "Unit floor area", "severity": "Medium", "page_id": int(unit.get("page_id") or 0),
                "page_label": str(unit.get("page_label") or ""), "code": "",
                "message": f"{unit.get('label') or 'Unit'} floor area is derived from a detected boundary and needs visual confirmation.",
                "bbox": unit.get("bbox"), "bbox_mode": "xywh", "source": str(unit.get("source") or ""),
            })

    occurrences_by_page: Dict[int, List[Dict[str, Any]]] = {}
    for occurrence in state.get("occurrences") or []:
        occurrences_by_page.setdefault(int(occurrence.get("page_id") or 0), []).append(occurrence)
    for facade in report.get("facades") or []:
        page_id = int(facade.get("page_id") or 0)
        materials = [o for o in occurrences_by_page.get(page_id, []) if o.get("substrate") and o.get("status") == "Confirmed"]
        unique_materials = {o.get("substrate") for o in materials if o.get("substrate")}
        if len(unique_materials) > 1 and not facade.get("explicit_areas"):
            for occurrence in materials:
                issues.append({
                    "category": "Material split", "severity": "Medium", "code": occurrence.get("code", ""),
                    "page_id": page_id, "page_label": str(facade.get("page_label") or ""),
                    "message": f"{occurrence.get('code')} = {occurrence.get('substrate')}, but the exact substrate region boundary/m² is not isolated yet.",
                    "bbox": occurrence.get("bbox"), "bbox_mode": occurrence.get("bbox_mode", "xyxy"),
                    "source": occurrence.get("text", ""),
                })
        elif not unique_materials and auto._num(facade.get("gross_m2")) > 0 and not facade.get("explicit_areas"):
            issues.append({
                "category": "External substrate", "severity": "High", "code": "", "page_id": page_id,
                "page_label": str(facade.get("page_label") or ""),
                "message": "External elevation area was measured but no confirmed substrate code could be resolved for it.",
                "bbox": facade.get("bbox"), "bbox_mode": "xywh", "source": "Gross elevation geometry",
            })

    # Take-off rows that remain provisional are visible in the same page-linked queue.
    rows = app.lquery(
        """SELECT section,element,location,substrate,finish_system,quantity_status,confidence,source_page,source_reference
           FROM takeoff_rows WHERE workspace_id=? ORDER BY id""",
        (int(workspace_id),),
    )
    seen_row_issue: set[Tuple[str, str, str]] = set()
    for row in rows:
        page_label = str(row.get("source_page") or "").strip()
        page_id = by_label.get(page_label, 0)
        status = str(row.get("quantity_status") or "")
        substrate = str(row.get("substrate") or "")
        finish = str(row.get("finish_system") or "")
        messages: List[Tuple[str, str]] = []
        if "provisional" in status.lower() or "derived" in str(row.get("confidence") or "").lower():
            messages.append(("Quantity", "Quantity remains provisional/derived and needs confirmation."))
        if str(row.get("section") or "").lower() == "external" and substrate.lower() in ("", "other", "to confirm", "to be confirmed"):
            messages.append(("External substrate", "External substrate is not confirmed."))
        if str(row.get("section") or "").lower() == "external" and finish.lower() in ("", "to confirm", "to be confirmed"):
            messages.append(("Finish system", "External finish system is not confirmed."))
        for category, message in messages:
            key = (category, page_label, str(row.get("location") or ""))
            if key in seen_row_issue:
                continue
            seen_row_issue.add(key)
            issues.append({
                "category": category, "severity": "Low", "code": "", "page_id": int(page_id or 0),
                "page_label": page_label, "message": f"{row.get('location') or row.get('element')}: {message}",
                "bbox": None, "bbox_mode": "xyxy", "source": str(row.get("source_reference") or ""),
            })

    for idx, issue in enumerate(issues, 1):
        issue["id"] = idx
    return issues


def _apply_unique_page_finishes(app: Any, workspace_id: int, state: Dict[str, Any]) -> None:
    by_page: Dict[str, set[str]] = {}
    for occurrence in state.get("occurrences") or []:
        if occurrence.get("status") != "Confirmed" or not occurrence.get("finish"):
            continue
        by_page.setdefault(str(occurrence.get("page_label") or ""), set()).add(str(occurrence["finish"]))
    conn = app.local_connect()
    try:
        for page_label, finishes in by_page.items():
            if len(finishes) != 1 or not page_label:
                continue
            finish = next(iter(finishes))
            conn.execute(
                """UPDATE takeoff_rows SET finish_system=?,updated_at=?
                   WHERE workspace_id=? AND source_page=? AND section='External'
                     AND (COALESCE(finish_system,'')='' OR LOWER(finish_system) IN ('to confirm','to be confirmed'))
                     AND source_reference LIKE ?""",
                (finish, app.now_stamp(), int(workspace_id), page_label, auto.SOURCE_PREFIX + "%"),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
        (int(workspace_id), SETTING_KEY, json.dumps(state, separators=(",", ":")), app.now_stamp()),
    )


def issue_preview_bytes(page: Dict[str, Any], issue: Dict[str, Any], max_long_edge: int = 1200) -> bytes:
    path = memory.regular_file(page.get("image_path"))
    if path is None:
        return b""
    with Image.open(path) as source:
        image = source.convert("RGB")
        original_w, original_h = image.size
        limit = max(640, min(int(max_long_edge or 1200), 1400))
        ratio = min(1.0, limit / float(max(original_w, original_h)))
        if ratio < 1.0:
            image = image.resize((max(1, round(original_w * ratio)), max(1, round(original_h * ratio))), Image.Resampling.LANCZOS)
        draw = ImageDraw.Draw(image)
        bbox = issue.get("bbox")
        label = f"REVIEW: {issue.get('category') or 'Issue'}"
        if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
            x0, y0, a, b = [float(v) for v in bbox[:4]]
            if issue.get("bbox_mode") == "xywh":
                x1, y1 = x0 + a, y0 + b
            else:
                x1, y1 = a, b
            box = [round(x0 * ratio), round(y0 * ratio), round(x1 * ratio), round(y1 * ratio)]
            width = max(3, round(5 * ratio))
            draw.rectangle(box, outline=(220, 35, 35), width=width)
            tx, ty = max(2, box[0]), max(2, box[1] - 24)
            draw.rectangle([tx, ty, min(image.width - 2, tx + max(160, len(label) * 7)), min(image.height - 2, ty + 22)], fill=(220, 35, 35))
            draw.text((tx + 4, ty + 4), label, fill=(255, 255, 255))
        else:
            banner_h = min(60, max(34, image.height // 12))
            draw.rectangle([0, 0, image.width, banner_h], fill=(220, 35, 35))
            draw.text((8, 8), label, fill=(255, 255, 255))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=82, optimize=False)
        return buffer.getvalue()


def review_panel(app: Any, workspace: Dict[str, Any]) -> None:
    workspace_id = int(workspace["id"])
    state = _setting_get(app, workspace_id)
    dictionary = state.get("dictionary") or {}
    issues = state.get("review_issues") or []
    app.st.markdown("### 🔎 Material cross-reference & Review Issues")
    c1, c2, c3 = app.st.columns(3)
    c1.metric("Schedule codes", len(dictionary))
    c2.metric("Confirmed codes", sum(1 for item in dictionary.values() if item.get("status") == "Confirmed"))
    c3.metric("Needs review", len(issues))

    if dictionary:
        with app.st.expander("Finishing/material code dictionary", expanded=False):
            rows = [
                {"Code": code, "Substrate": item.get("substrate") or "To confirm", "Finish": item.get("finish") or "", "Description": item.get("description") or "", "Status": item.get("status")}
                for code, item in sorted(dictionary.items())
            ]
            app.st.dataframe(app.pd.DataFrame(rows), use_container_width=True, hide_index=True)
    if not issues:
        app.st.success("No unresolved material/geometry review issues are currently recorded. Re-run automatic geometry after changing drawings or schedules.")
        return

    table = app.pd.DataFrame([
        {"#": item.get("id"), "Severity": item.get("severity"), "Category": item.get("category"), "Code": item.get("code"), "Page": item.get("page_label"), "Issue": item.get("message")}
        for item in issues
    ])
    app.st.dataframe(table, use_container_width=True, hide_index=True, height=min(420, 38 + len(table) * 35))
    labels = [f"#{item.get('id')} · {item.get('page_label') or 'No page'} · {item.get('category')} · {item.get('code') or ''}" for item in issues]
    chosen = app.st.selectbox("Show issue on drawing", labels, key=f"material_review_issue_{workspace_id}")
    issue = issues[labels.index(chosen)]
    app.st.warning(issue.get("message") or "Needs review")
    if issue.get("source"):
        app.st.caption(f"Evidence: {issue.get('source')}")
    page_id = int(issue.get("page_id") or 0)
    if not page_id:
        app.st.info("This issue is project-level and does not have a single drawing-sheet location.")
        return
    rows = app.lquery("SELECT id,page_label,page_type,image_path FROM pages WHERE id=? AND workspace_id=?", (page_id, workspace_id))
    if not rows:
        app.st.info("The referenced drawing sheet is no longer available in this workspace.")
        return
    payload = issue_preview_bytes(dict(rows[0]), issue)
    if not payload:
        app.st.info("The referenced sheet is known, but its rendered image is not available. Process that selected page to display the marker.")
        return
    app.st.image(payload, caption=f"{rows[0].get('page_label') or 'Drawing'} · review marker", use_container_width=True)
    app.st.caption("Red box = exact evidence location where available. Red banner = sheet-level issue when the source text has no reliable coordinates.")


def apply(app: Any) -> None:
    if getattr(app, "_pb_material_schedule_v1222_applied", False):
        return
    app._pb_material_schedule_v1222_applied = True

    base_substrates = auto._substrates_from_text
    auto._substrates_from_text = lambda text: resolved_substrates_from_text(base_substrates, text)

    base_analyse = auto.analyse_workspace

    def _analyse_with_material_schedule(app_obj: Any, workspace_id: int):
        state = build_material_state(app_obj, int(workspace_id))
        token = _resolver_context.set(state.get("dictionary") or {})
        try:
            report = base_analyse(app_obj, int(workspace_id))
        finally:
            _resolver_context.reset(token)
        _apply_unique_page_finishes(app_obj, int(workspace_id), state)
        issues = build_review_issues(app_obj, int(workspace_id), report, state)
        state["review_issues"] = issues
        state["analysed_at"] = app_obj.now_stamp()
        _setting_set(app_obj, int(workspace_id), state)
        report["material_codes"] = len(state.get("dictionary") or {})
        report["review_issues"] = len(issues)
        try:
            auto._setting_set(app_obj, int(workspace_id), report)
        except Exception:
            pass
        return report

    auto.analyse_workspace = _analyse_with_material_schedule

    # v1.2.19's no-AI wrapper resolves auto_geometry_panel by module global at runtime.
    base_panel = auto.auto_geometry_panel

    def _panel_with_review(app_obj: Any, workspace: Dict[str, Any]):
        base_panel(app_obj, workspace)
        review_panel(app_obj, workspace)

    auto.auto_geometry_panel = _panel_with_review

    app.build_material_schedule_dictionary = lambda workspace_id: build_material_dictionary(app, int(workspace_id))
    app.build_material_cross_reference = lambda workspace_id: build_material_state(app, int(workspace_id))
    app.material_review_issues = lambda workspace_id: (_setting_get(app, int(workspace_id)).get("review_issues") or [])
