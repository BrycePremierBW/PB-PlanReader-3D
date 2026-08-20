"""PlanReader v1.2.25 reliable sheet registration and mapper preflight.

Architectural sheets contain many references to other drawings.  Registration must
therefore prefer the issued title block over arbitrary words elsewhere on the page.
The permanent sheet identity remains document_id + page_no; detected labels/titles
are editable metadata and manual edits are never overwritten automatically.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pb_auto_geometry_v1219 as auto
import pb_memory_stability_v1220 as memory

VERSION = "1.2.25"
SETTING_PREFIX = "page_registration_v1225_"

_DRAWING_CODE_RE = re.compile(r"\b([A-Z]{1,5}(?:[-_.]?[A-Z]{0,3})?[-_.]?\d{2,4}(?:[-_.][A-Z0-9]{1,4})?)\b", re.I)
_SCALE_RE = re.compile(r"(?<!\d)1\s*:\s*(\d{2,4})(?!\d)", re.I)
_REV_RE = re.compile(r"\b(?:REV(?:ISION)?\s*[:#-]?\s*)([A-Z0-9]{1,4})\b", re.I)
_EXCLUDED_CODE_PREFIXES = {
    "PT", "PF", "WF", "EC", "FC", "IP", "EP", "CEIL", "SOF", "RBL", "SCR",
    "SHD", "DP", "GD", "RS", "BC", "BA", "SK", "D", "W",
}

_TYPE_RULES: Dict[str, Tuple[str, ...]] = {
    "Title / Drawing Register": ("drawing register", "drawing schedule", "cover sheet", "title sheet", "sheet index"),
    "Reflected Ceiling Plan": ("reflected ceiling plan", "reflected ceiling", "rcp", "ceiling layout"),
    "Floor Plan": ("floor plan", "partition plan", "unit plan", "apartment plan", "general arrangement", "ga plan", "tenancy plan", "layout plan"),
    "Roof Plan": ("roof plan", "roof layout"),
    "Elevation": ("external elevation", "building elevation", "elevations", "north elevation", "south elevation", "east elevation", "west elevation", "street elevation"),
    "Section": ("building section", "wall section", "cross section", "sections"),
    "Render / Artist's Impression": ("artist's impression", "artists impression", "artist impression", "perspective", "3d view", "3d render", "visualisation", "visualization"),
    "Door / Window Schedule": ("door schedule", "window schedule", "door and window schedule", "door elevations", "window elevations"),
    "Finishes Schedule": ("finish schedule", "finishes schedule", "finishing schedule", "material schedule", "colour schedule", "color schedule", "paint schedule", "schedule of finishes"),
    "Specification": ("painting specification", "architectural specification", "specification"),
    "Structural": ("structural plan", "structural", "steel framing", "footing plan"),
    "Services": ("mechanical services", "electrical services", "hydraulic services", "fire services", "services plan"),
    "Landscape / Civil": ("landscape plan", "civil plan", "stormwater", "pavement plan"),
}

# More specific types win ties.  A generic word like "section" elsewhere on a
# floor-plan sheet must never beat a title-block "LEVEL 03 PARTITION PLAN".
_TYPE_PRIORITY = [
    "Reflected Ceiling Plan", "Door / Window Schedule", "Finishes Schedule",
    "Render / Artist's Impression", "Floor Plan", "Roof Plan", "Elevation", "Section",
    "Title / Drawing Register", "Specification", "Structural", "Services", "Landscape / Civil",
]


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _low(value: Any) -> str:
    return _norm(value).lower()


def _manual_key(page_id: int) -> str:
    return f"{SETTING_PREFIX}{int(page_id)}_manual"


def _meta_key(page_id: int) -> str:
    return f"{SETTING_PREFIX}{int(page_id)}_meta"


def _candidate_code(text: Any) -> str:
    """Pick a drawing/sheet number while rejecting finish/door/detail tags."""
    raw = str(text or "")
    labelled = re.search(
        r"(?:DRAWING|DWG|SHEET)\s*(?:NO\.?|NUMBER|#)?\s*[:\-]?\s*([A-Z][A-Z0-9._-]{1,14}\d[A-Z0-9._-]*)",
        raw,
        re.I,
    )
    candidates = [labelled.group(1)] if labelled else []
    candidates.extend(match.group(1) for match in _DRAWING_CODE_RE.finditer(raw))
    for value in candidates:
        code = value.upper().strip(" ._-:")
        if not code or ":" in code:
            continue
        prefix_match = re.match(r"[A-Z]+", code)
        prefix = prefix_match.group(0) if prefix_match else ""
        if prefix in _EXCLUDED_CODE_PREFIXES:
            continue
        if code.isdigit() or len(code) < 3:
            continue
        return code
    return ""


def _group_words(words: Iterable[Sequence[Any]], y_tolerance: float = 4.5) -> List[Dict[str, Any]]:
    usable = []
    for word in words or []:
        if len(word) < 5:
            continue
        try:
            x0, y0, x1, y1 = map(float, word[:4])
            text = str(word[4]).strip()
        except Exception:
            continue
        if text:
            usable.append((x0, y0, x1, y1, text))
    usable.sort(key=lambda item: (item[1], item[0]))
    lines: List[List[Tuple[float, float, float, float, str]]] = []
    for item in usable:
        cy = (item[1] + item[3]) / 2.0
        placed = False
        for line in lines[-8:]:
            ly = sum((part[1] + part[3]) / 2.0 for part in line) / len(line)
            if abs(cy - ly) <= y_tolerance:
                line.append(item); placed = True; break
        if not placed:
            lines.append([item])
    out = []
    for line in lines:
        line.sort(key=lambda item: item[0])
        out.append({
            "text": _norm(" ".join(item[4] for item in line)),
            "bbox": [min(item[0] for item in line), min(item[1] for item in line), max(item[2] for item in line), max(item[3] for item in line)],
        })
    return out


def title_block_evidence(pdf_page: Any) -> Dict[str, Any]:
    """Extract title-block text from the lower/right sheet bands.

    Different architects place title blocks either across the bottom or down the
    right edge.  We score both regions and retain their line geometry for audit.
    """
    rect = pdf_page.rect
    width, height = float(rect.width), float(rect.height)
    words = pdf_page.get_text("words") or []
    title_words = []
    for word in words:
        if len(word) < 5:
            continue
        x0, y0, x1, y1 = map(float, word[:4])
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        if cy >= height * 0.70 or cx >= width * 0.68:
            title_words.append(word)
    lines = _group_words(title_words)
    text = "\n".join(line["text"] for line in lines if line["text"])
    full_text = pdf_page.get_text("text") or ""
    code = _candidate_code(text)
    if not code:
        code = _candidate_code("\n".join(line["text"] for line in lines[-12:]))
    scale_match = _SCALE_RE.search(text)
    revision = (_REV_RE.search(text).group(1).upper() if _REV_RE.search(text) else "")
    return {
        "text": text,
        "lines": lines,
        "drawing_no": code,
        "scale": f"1:{int(scale_match.group(1))}" if scale_match else "",
        "revision": revision,
        "full_text": full_text,
    }


def weighted_page_type(full_text: Any, file_name: Any = "", title_block: Any = "") -> Tuple[str, int, str]:
    sources = [(_low(title_block), 12, "title block"), (_low(full_text), 2, "page text"), (_low(file_name), 1, "file name")]
    scores: Dict[str, int] = {name: 0 for name in _TYPE_RULES}
    evidence: Dict[str, List[str]] = {name: [] for name in _TYPE_RULES}
    for name, phrases in _TYPE_RULES.items():
        for haystack, weight, source_name in sources:
            for phrase in phrases:
                if phrase in haystack:
                    scores[name] += weight + min(3, len(phrase) // 8)
                    evidence[name].append(f"{source_name}: {phrase}")
    # A title-block plan/elevation heading should dominate reference bubbles.
    tb = _low(title_block)
    if "partition plan" in tb or "unit plan" in tb or "floor plan" in tb or "general arrangement" in tb:
        scores["Floor Plan"] += 20
    if "reflected ceiling" in tb or re.search(r"\brcp\b", tb):
        scores["Reflected Ceiling Plan"] += 22
    if "elevation" in tb:
        scores["Elevation"] += 18
    if "finish" in tb and "schedule" in tb:
        scores["Finishes Schedule"] += 22
    best = max(_TYPE_PRIORITY, key=lambda name: (scores.get(name, 0), -_TYPE_PRIORITY.index(name)))
    score = scores.get(best, 0)
    if score <= 0:
        return "Other", 0, "No reliable sheet-title evidence"
    runner_up = max((value for name, value in scores.items() if name != best), default=0)
    confidence = max(0, min(100, 55 + score * 3 - runner_up * 2))
    return best, confidence, "; ".join(evidence.get(best, [])[:4])


def classify_page(text: str, file_name: str, page_no: int) -> Tuple[str, str]:
    """Fallback classifier used before native title-block geometry is available."""
    page_type, _confidence, _evidence = weighted_page_type(text, file_name, "")
    code = _candidate_code(text)
    return page_type, code or f"Page {int(page_no)}"


def _sheet_title(title_text: Any, drawing_no: str, page_type: str) -> str:
    lines = [_norm(line) for line in str(title_text or "").splitlines() if _norm(line)]
    reject = ("project", "client", "drawing no", "sheet no", "scale", "revision", "rev ", "date", "drawn", "checked", "copyright")
    candidates = []
    for line in lines:
        low = line.lower()
        if drawing_no and drawing_no.lower() in low and len(line) <= len(drawing_no) + 12:
            continue
        if any(token in low for token in reject):
            continue
        type_hit = any(phrase in low for phrase in _TYPE_RULES.get(page_type, ()))
        if type_hit:
            candidates.append((3, line))
        elif 4 <= len(line) <= 80:
            candidates.append((1, line))
    return max(candidates, key=lambda item: (item[0], len(item[1])), default=(0, ""))[1]


def sync_drawing_register(app: Any, workspace_id: int) -> int:
    """Upsert by immutable document/page source reference, removing stale duplicates."""
    pages = app.lquery(
        """SELECT p.id,p.page_no,p.page_label,p.page_type,p.scale_text,d.file_name
           FROM pages p JOIN documents d ON d.id=p.document_id
           WHERE p.workspace_id=? ORDER BY p.document_id,p.page_no,p.id""",
        (int(workspace_id),),
    )
    changed = 0
    for page in pages:
        source = f"{page.get('file_name')} p{page.get('page_no')}"
        meta_raw = app.workspace_setting(workspace_id, _meta_key(int(page["id"])), "{}")
        try:
            meta = json.loads(str(meta_raw or "{}"))
        except Exception:
            meta = {}
        title = str(meta.get("title") or page.get("page_label") or "")
        detail = str(page.get("page_type") or "Other")
        if meta.get("title"):
            detail = f"{detail} · {meta['title']}"
        existing = app.lquery(
            "SELECT id FROM register_items WHERE workspace_id=? AND register_name='drawing_register' AND source_reference=? ORDER BY id",
            (int(workspace_id), source),
        )
        status = "Reviewed" if page.get("page_type") != "Other" else "To classify"
        priority = str(page.get("scale_text") or "")
        if existing:
            keep_id = int(existing[0]["id"])
            app.lexecute(
                "UPDATE register_items SET item_no=?,title=?,detail=?,priority=?,status=? WHERE id=?",
                (str(page.get("page_label") or ""), title, detail, priority, status, keep_id),
            )
            for duplicate in existing[1:]:
                app.lexecute("DELETE FROM register_items WHERE id=?", (int(duplicate["id"]),))
            changed += 1
        else:
            app.lexecute(
                """INSERT INTO register_items(workspace_id,register_name,item_no,title,detail,priority,source_reference,status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (int(workspace_id), "drawing_register", str(page.get("page_label") or ""), title, detail, priority, source, status, app.now_stamp()),
            )
            changed += 1
    return changed


def repair_document_registration(app: Any, document_id: int) -> Dict[str, Any]:
    docs = app.lquery("SELECT id,workspace_id,path,file_name FROM documents WHERE id=?", (int(document_id),))
    if not docs:
        return {"updated": 0, "pages": []}
    doc = docs[0]
    path = Path(str(doc.get("path") or ""))
    rows = app.lquery("SELECT * FROM pages WHERE document_id=? ORDER BY page_no,id", (int(document_id),))
    by_no = {int(row.get("page_no") or 0): dict(row) for row in rows}
    updated: List[Dict[str, Any]] = []
    pdf = None
    if path.is_file() and path.suffix.lower() == ".pdf" and getattr(app, "fitz", None) is not None:
        try:
            pdf = app.fitz.open(path)
        except Exception:
            pdf = None
    try:
        for page_no, page in by_no.items():
            if str(app.workspace_setting(int(doc["workspace_id"]), _manual_key(int(page["id"])), "")) == "1":
                continue
            title = {"text": "", "drawing_no": "", "scale": "", "revision": "", "full_text": str(page.get("extracted_text") or "")}
            if pdf is not None and 1 <= page_no <= len(pdf):
                try:
                    title = title_block_evidence(pdf.load_page(page_no - 1))
                except Exception:
                    pass
            full_text = str(title.get("full_text") or page.get("extracted_text") or "")
            page_type, confidence, evidence = weighted_page_type(full_text, str(doc.get("file_name") or ""), title.get("text"))
            drawing_no = str(title.get("drawing_no") or _candidate_code(title.get("text")) or "")
            if not drawing_no:
                current = str(page.get("page_label") or "")
                drawing_no = current if current and not current.lower().startswith("page ") else f"Page {page_no}"
            sheet_title = _sheet_title(title.get("text"), drawing_no, page_type)
            scale = str(page.get("scale_text") or "")
            if not scale and title.get("scale"):
                scale = str(title["scale"])
            app.lexecute(
                "UPDATE pages SET page_label=?,page_type=?,scale_text=? WHERE id=?",
                (drawing_no, page_type, scale, int(page["id"])),
            )
            meta = {
                "version": VERSION, "title": sheet_title, "confidence": int(confidence), "evidence": evidence,
                "drawing_no": drawing_no, "revision": str(title.get("revision") or ""), "detected_scale": str(title.get("scale") or ""),
            }
            app.set_workspace_setting(int(doc["workspace_id"]), _meta_key(int(page["id"])), json.dumps(meta, separators=(",", ":")))
            updated.append({"page_id": int(page["id"]), "page_no": page_no, "label": drawing_no, "title": sheet_title, "type": page_type, "confidence": confidence})
    finally:
        if pdf is not None:
            try:
                pdf.close()
            except Exception:
                pass
    try:
        auto.auto_select_document_pages(app, int(document_id))
    except Exception:
        pass
    sync_drawing_register(app, int(doc["workspace_id"]))
    return {"updated": len(updated), "pages": updated, "workspace_id": int(doc["workspace_id"])}


def _meta_for_pages(app: Any, workspace_id: int, pages) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    for row in pages.itertuples():
        try:
            out[int(row.id)] = json.loads(str(app.workspace_setting(workspace_id, _meta_key(int(row.id)), "{}") or "{}"))
        except Exception:
            out[int(row.id)] = {}
    return out


def drawing_register_page(app: Any, workspace: Dict[str, Any]) -> None:
    workspace_id = int(workspace["id"])
    app.hero(workspace)
    pages = app.ldf(
        """SELECT p.id,p.document_id,p.page_no,p.page_label,p.page_type,p.scale_text,p.px_per_m,p.selected,d.file_name,p.image_path
           FROM pages p JOIN documents d ON d.id=p.document_id WHERE p.workspace_id=? ORDER BY p.document_id,p.page_no,p.id""",
        (workspace_id,),
    )
    if pages.empty:
        app.st.info("Upload/process documents first."); return
    metadata = _meta_for_pages(app, workspace_id, pages)
    pages["drawing_title"] = [metadata.get(int(pid), {}).get("title", "") for pid in pages["id"]]
    pages["confidence"] = [metadata.get(int(pid), {}).get("confidence", "") for pid in pages["id"]]
    pages["manual"] = [str(app.workspace_setting(workspace_id, _manual_key(int(pid)), "")) == "1" for pid in pages["id"]]
    c1, c2, c3, c4 = app.st.columns(4)
    c1.metric("Sheets", len(pages)); c2.metric("Take-off selected", int(pages["selected"].fillna(0).astype(bool).sum()))
    c3.metric("Needs classification", int((pages["page_type"].astype(str) == "Other").sum()))
    c4.metric("Manual overrides", int(pages["manual"].sum()))
    if app.st.button("Re-register all non-manual sheets from title blocks", key=f"rereg_{workspace_id}", use_container_width=True):
        with app.st.spinner("Reading title blocks and rebuilding the drawing register…"):
            for doc_id in sorted({int(v) for v in pages["document_id"].tolist()}):
                repair_document_registration(app, doc_id)
        app.st.success("Drawing register rebuilt from the issued sheet title blocks."); app.st.rerun()

    mode = app.st.radio("Show", ["Selected", "Discarded / reference", "All"], horizontal=True, key=f"reg_mode_v1225_{workspace_id}")
    filtered = pages if mode == "All" else pages[pages["selected"].fillna(0).astype(bool) == (mode == "Selected")]
    if filtered.empty:
        app.st.info("No sheets match this filter."); return
    page_size = 30
    page_count = max(1, math.ceil(len(filtered) / page_size))
    register_page = int(app.st.number_input("Register page", min_value=1, max_value=page_count, value=1, step=1, key=f"reg_pg_v1225_{workspace_id}_{mode}"))
    start = (register_page - 1) * page_size
    visible = filtered.iloc[start:start + page_size].copy()
    editable = visible[["id", "file_name", "page_no", "page_label", "drawing_title", "page_type", "scale_text", "confidence", "selected"]].copy()
    edited = app.st.data_editor(
        editable, hide_index=True, use_container_width=True, num_rows="fixed",
        column_config={
            "id": app.st.column_config.NumberColumn(disabled=True), "file_name": app.st.column_config.TextColumn(disabled=True),
            "page_no": app.st.column_config.NumberColumn(disabled=True), "confidence": app.st.column_config.NumberColumn(disabled=True),
            "page_type": app.st.column_config.SelectboxColumn(options=app.PAGE_TYPES), "selected": app.st.column_config.CheckboxColumn(),
        }, key=f"reg_editor_v1225_{workspace_id}_{mode}_{register_page}",
    )
    if app.st.button("Save register changes", type="primary", key=f"reg_save_v1225_{workspace_id}_{mode}_{register_page}"):
        originals = {int(row.id): row for row in visible.itertuples()}
        for row in edited.to_dict("records"):
            pid = int(row["id"]); original = originals[pid]
            app.lexecute("UPDATE pages SET page_label=?,page_type=?,scale_text=?,selected=? WHERE id=?", (str(row.get("page_label") or ""), str(row.get("page_type") or "Other"), str(row.get("scale_text") or ""), 1 if row.get("selected") else 0, pid))
            title_changed = _norm(row.get("drawing_title")) != _norm(getattr(original, "drawing_title", ""))
            identity_changed = _norm(row.get("page_label")) != _norm(original.page_label) or str(row.get("page_type")) != str(original.page_type)
            if title_changed or identity_changed:
                app.set_workspace_setting(workspace_id, _manual_key(pid), "1")
                meta = metadata.get(pid, {})
                meta.update({"title": str(row.get("drawing_title") or ""), "manual": True, "confidence": 100})
                app.set_workspace_setting(workspace_id, _meta_key(pid), json.dumps(meta, separators=(",", ":")))
        sync_drawing_register(app, workspace_id)
        app.st.success("Register saved. Manual drawing number/type edits are protected from automatic reclassification."); app.st.rerun()

    app.st.subheader("Drawing preview")
    labels = [f"#{int(r.id)} · {r.page_label} · {r.drawing_title or r.page_type}" for r in visible.itertuples()]
    chosen = app.st.selectbox("Preview sheet", labels, key=f"reg_preview_v1225_{workspace_id}_{mode}_{register_page}")
    row = visible.iloc[labels.index(chosen)].to_dict()
    path = memory.regular_file(row.get("image_path"))
    if path is None:
        app.st.info("This sheet is registered but has not been rendered yet.")
        if bool(row.get("selected")) and app.st.button("Render this selected sheet now", key=f"render_reg_v1225_{int(row['id'])}"):
            with app.st.spinner("Rendering sheet…"):
                app.process_document(int(row["document_id"]), page_ids=[int(row["page_no"])])
            app.st.rerun()
        return
    payload = memory.thumbnail_bytes(path)
    if payload:
        app.st.image(payload, caption=f"{row.get('file_name')} · p{row.get('page_no')} · {row.get('page_label')}", use_container_width=True)


def mapper_preflight(app: Any, workspace_id: int) -> Tuple[int, List[Dict[str, Any]]]:
    """Ensure selected mapper sheets have real files; never allow Path('') -> '.'."""
    pages = app.lquery(
        "SELECT id,document_id,page_no,page_label,image_path FROM pages WHERE workspace_id=? AND COALESCE(selected,0)=1 ORDER BY document_id,page_no,id",
        (int(workspace_id),),
    )
    missing = [dict(row) for row in pages if memory.regular_file(row.get("image_path")) is None]
    rendered = 0
    by_doc: Dict[int, List[int]] = {}
    for row in missing:
        by_doc.setdefault(int(row["document_id"]), []).append(int(row["page_no"]))
    for doc_id, page_nos in by_doc.items():
        try:
            count, _msg = app.process_document(doc_id, page_ids=page_nos)
            rendered += int(count or 0)
        except Exception:
            pass
    remaining = app.lquery(
        "SELECT id,document_id,page_no,page_label,image_path FROM pages WHERE workspace_id=? AND COALESCE(selected,0)=1 ORDER BY document_id,page_no,id",
        (int(workspace_id),),
    )
    remaining = [dict(row) for row in remaining if memory.regular_file(row.get("image_path")) is None]
    return rendered, remaining


def apply(app: Any) -> None:
    if getattr(app, "_pb_page_registration_v1225_applied", False):
        return
    app._pb_page_registration_v1225_applied = True

    app.classify_page = classify_page
    base_index = app.index_document_pages
    base_process = app.process_document

    def _indexed(document_id: int, *args, **kwargs):
        result = base_index(document_id, *args, **kwargs)
        repair_document_registration(app, int(document_id))
        return result

    def _processed(document_id: int, *args, **kwargs):
        result = base_process(document_id, *args, **kwargs)
        repair_document_registration(app, int(document_id))
        return result

    app.index_document_pages = _indexed
    app.process_document = _processed
    app.repair_document_registration = lambda document_id: repair_document_registration(app, int(document_id))
    app.sync_drawing_register_v1225 = lambda workspace_id: sync_drawing_register(app, int(workspace_id))
    app.drawing_register_page = lambda workspace: drawing_register_page(app, workspace)

    base_mapper = app.plan_mapper_page
    def _safe_mapper(workspace):
        workspace_id = int(workspace["id"])
        with app.st.spinner("Checking selected drawing images…"):
            _rendered, remaining = mapper_preflight(app, workspace_id)
        if remaining:
            app.hero(workspace)
            app.st.error("Plan Mapper cannot open one or more selected sheets because their rendered image is missing. The blank-path crash has been blocked.")
            app.st.dataframe(app.pd.DataFrame([{"Page": row.get("page_label"), "PDF page": row.get("page_no"), "Page ID": row.get("id")} for row in remaining]), hide_index=True, use_container_width=True)
            app.st.info("Reprocess these selected sheets from Job & Documents, or deselect them in Drawing Register if they are not required.")
            return None
        return base_mapper(workspace)
    app.plan_mapper_page = _safe_mapper
