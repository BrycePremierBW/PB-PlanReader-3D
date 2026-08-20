"""PlanReader v1.2.28 spatial architectural drawing reader.

Earlier releases improved interpretation but still consumed PyMuPDF's ordinary text
stream for most downstream work. Architectural PDFs are highly spatial documents:
title-block labels/values live in separate cells, schedule code/description columns
are separate text objects, and dimension strings can be split across several words.

v1.2.28 rebuilds selected PDF page text from native word/span geometry before the
existing registration, legend, schedule, floor-area, elevation and take-off layers
run. It also strengthens title-block field extraction and split dimension labels.
Raster-heavy pages retain a conservative optional visual read for sheet identity and
legend wording only; visual AI is never used as authoritative measured geometry.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pb_auto_geometry_v1219 as auto
import pb_drawing_reading_v1226 as reading
import pb_memory_stability_v1220 as memory
import pb_page_registration_v1225 as registration

VERSION = "1.2.28"
SETTING_PREFIX = "page_read_v1228_"
VISUAL_PREFIX = "visual_page_read_v1228_"

_FIELD_DRAWING = re.compile(r"\b(?:DRAWING|DWG|SHEET)\s*(?:NO\.?|NUMBER|#)?\b", re.I)
_FIELD_TITLE = re.compile(r"\b(?:DRAWING\s+TITLE|SHEET\s+TITLE|TITLE)\b", re.I)
_FIELD_SCALE = re.compile(r"\bSCALE\b", re.I)
_FIELD_REV = re.compile(r"\bREV(?:ISION)?\b", re.I)
_SCALE_VALUE = re.compile(r"(?<!\d)1\s*:\s*(\d{2,4})(?!\d)")
_REV_VALUE = re.compile(r"^[A-Z0-9]{1,4}$", re.I)

_ADMIN_TITLE_WORDS = (
    "project", "client", "architect", "consultant", "copyright", "drawn", "checked",
    "approved", "date", "issue", "status", "revision", "rev", "scale", "drawing no",
    "sheet no", "job no", "project no", "address",
)
_TABLE_PAGE_WORDS = (
    "schedule", "legend", "abbreviation", "abbreviations", "material key", "finish key",
    "finishes", "colour schedule", "color schedule", "door schedule", "window schedule",
)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _regular_file(value: Any) -> Optional[Path]:
    try:
        return memory.regular_file(value)
    except Exception:
        raw = str(value or "").strip()
        if not raw:
            return None
        path = Path(raw)
        return path if path.is_file() else None


def _span_lines(pdf_page: Any) -> List[Dict[str, Any]]:
    """Return native PDF lines with font/bbox metadata in visual block order."""
    try:
        payload = pdf_page.get_text("dict") or {}
    except Exception:
        payload = {}
    lines: List[Dict[str, Any]] = []
    for block_no, block in enumerate(payload.get("blocks") or []):
        if int(block.get("type", 0) or 0) != 0:
            continue
        for line_no, line in enumerate(block.get("lines") or []):
            spans = []
            for span in line.get("spans") or []:
                text = str(span.get("text") or "")
                if not text.strip():
                    continue
                bbox = list(span.get("bbox") or [0, 0, 0, 0])
                spans.append({
                    "text": text,
                    "bbox": [float(v) for v in bbox[:4]],
                    "size": _num(span.get("size")),
                    "font": str(span.get("font") or ""),
                })
            if not spans:
                continue
            spans.sort(key=lambda item: item["bbox"][0])
            pieces: List[str] = []
            previous_x1: Optional[float] = None
            median_size = median([max(1.0, item["size"]) for item in spans]) if spans else 8.0
            for span in spans:
                if previous_x1 is not None:
                    gap = span["bbox"][0] - previous_x1
                    if gap > max(18.0, median_size * 2.2):
                        pieces.append("\t")
                    elif pieces and not pieces[-1].endswith((" ", "\t")):
                        pieces.append(" ")
                pieces.append(span["text"].strip())
                previous_x1 = span["bbox"][2]
            bbox = [
                min(item["bbox"][0] for item in spans), min(item["bbox"][1] for item in spans),
                max(item["bbox"][2] for item in spans), max(item["bbox"][3] for item in spans),
            ]
            lines.append({
                "text": "".join(pieces).strip(), "bbox": bbox,
                "size": max(item["size"] for item in spans),
                "block": block_no, "line": line_no, "spans": spans,
            })
    return lines


def _word_rows(pdf_page: Any) -> List[Dict[str, Any]]:
    """Reconstruct table-like visual rows across separate PDF text objects."""
    try:
        words = list(pdf_page.get_text("words") or [])
    except Exception:
        words = []
    usable = []
    heights = []
    for word in words:
        if len(word) < 5:
            continue
        try:
            x0, y0, x1, y1 = map(float, word[:4])
        except Exception:
            continue
        text = str(word[4]).strip()
        if not text:
            continue
        usable.append((x0, y0, x1, y1, text))
        heights.append(max(1.0, y1 - y0))
    tolerance = max(2.5, min(8.0, (median(heights) if heights else 7.0) * 0.48))
    usable.sort(key=lambda item: (((item[1] + item[3]) / 2.0), item[0]))
    rows: List[List[Tuple[float, float, float, float, str]]] = []
    row_centres: List[float] = []
    for item in usable:
        cy = (item[1] + item[3]) / 2.0
        best = -1
        best_distance = 1e9
        for idx in range(max(0, len(rows) - 8), len(rows)):
            distance = abs(cy - row_centres[idx])
            if distance <= tolerance and distance < best_distance:
                best, best_distance = idx, distance
        if best < 0:
            rows.append([item]); row_centres.append(cy)
        else:
            rows[best].append(item)
            row_centres[best] = sum((part[1] + part[3]) / 2.0 for part in rows[best]) / len(rows[best])
    output = []
    for row in rows:
        row.sort(key=lambda item: item[0])
        heights_local = [max(1.0, item[3] - item[1]) for item in row]
        typical_h = median(heights_local) if heights_local else 7.0
        pieces: List[str] = []
        previous_x1: Optional[float] = None
        for x0, y0, x1, y1, text in row:
            if previous_x1 is not None:
                gap = x0 - previous_x1
                pieces.append("\t" if gap > max(18.0, typical_h * 2.6) else " ")
            pieces.append(text)
            previous_x1 = x1
        output.append({
            "text": "".join(pieces).strip(),
            "bbox": [min(x[0] for x in row), min(x[1] for x in row), max(x[2] for x in row), max(x[3] for x in row)],
            "words": row,
        })
    output.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))
    return output


def _table_like(page: Dict[str, Any], text: str) -> bool:
    hay = f"{page.get('page_type') or ''} {page.get('page_label') or ''} {text}".lower()
    return any(token in hay for token in _TABLE_PAGE_WORDS)


def reconstruct_page_text(pdf_page: Any, page: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    page = dict(page or {})
    span_lines = _span_lines(pdf_page)
    row_lines = _word_rows(pdf_page)
    block_text = "\n".join(line["text"] for line in span_lines if line["text"])
    table_text = "\n".join(row["text"] for row in row_lines if row["text"])
    preferred = table_text if _table_like(page, block_text) and len(table_text) >= 30 else block_text
    if len(preferred) < 30 and len(table_text) > len(preferred):
        preferred = table_text
    words = re.findall(r"[A-Za-z0-9]+", preferred)
    return {
        "text": preferred,
        "block_text": block_text,
        "table_text": table_text,
        "span_lines": span_lines,
        "rows": row_lines,
        "word_count": len(words),
        "char_count": len(preferred),
    }


def _title_zone_lines(pdf_page: Any) -> List[Dict[str, Any]]:
    width, height = float(pdf_page.rect.width), float(pdf_page.rect.height)
    lines = _span_lines(pdf_page)
    selected = []
    for line in lines:
        x0, y0, x1, y1 = line["bbox"]
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        # Architectural title blocks are commonly a bottom strip, a right strip,
        # or a bottom-right cell stack. Keep both bands but score corner lines later.
        if cy >= height * 0.62 or cx >= width * 0.62:
            item = dict(line)
            item["corner_score"] = (cy / max(height, 1.0)) + (cx / max(width, 1.0))
            selected.append(item)
    return selected


def _nearest_value(lines: Sequence[Dict[str, Any]], label_index: int, label_re: re.Pattern, validator=None) -> str:
    label = lines[label_index]
    label_text = str(label.get("text") or "")
    # First try value appearing in the same visual line/cell.
    same = label_re.sub(" ", label_text, count=1).strip(" :#-\t")
    if same and (validator is None or validator(same)):
        return same
    lx0, ly0, lx1, ly1 = label["bbox"]
    candidates: List[Tuple[float, str]] = []
    for idx, line in enumerate(lines):
        if idx == label_index:
            continue
        text = _norm(line.get("text"))
        if not text or label_re.search(text):
            continue
        if validator is not None and not validator(text):
            continue
        x0, y0, x1, y1 = line["bbox"]
        # Prefer cell immediately right, then immediately below the field label.
        same_row = abs(((y0 + y1) / 2.0) - ((ly0 + ly1) / 2.0)) <= max(8.0, ly1 - ly0)
        right_gap = x0 - lx1
        below_gap = y0 - ly1
        if same_row and -4.0 <= right_gap <= 220.0:
            score = max(0.0, right_gap) + abs(y0 - ly0) * 2.0
            candidates.append((score, text))
        elif -10.0 <= x0 - lx0 <= 180.0 and 0.0 <= below_gap <= 80.0:
            score = 70.0 + below_gap + abs(x0 - lx0) * 0.4
            candidates.append((score, text))
    return min(candidates, default=(0.0, ""), key=lambda item: item[0])[1]


def spatial_title_block_evidence(pdf_page: Any, base_reader) -> Dict[str, Any]:
    base = dict(base_reader(pdf_page) or {})
    lines = _title_zone_lines(pdf_page)
    if not lines:
        return base
    title_text = "\n".join(str(item.get("text") or "") for item in lines if item.get("text"))

    def valid_drawing(value: str) -> bool:
        return bool(registration._candidate_code(f"DRAWING NO {value}"))

    for idx, line in enumerate(lines):
        text = str(line.get("text") or "")
        if _FIELD_DRAWING.search(text):
            value = _nearest_value(lines, idx, _FIELD_DRAWING, valid_drawing)
            candidate = registration._candidate_code(f"DRAWING NO {value}") if value else ""
            if candidate:
                base["drawing_no"] = candidate
                break
    if not base.get("drawing_no"):
        # Rank architectural-looking codes by title-block corner proximity.
        ranked = sorted(lines, key=lambda item: (-_num(item.get("corner_score")), -_num(item.get("size"))))
        for line in ranked:
            candidate = registration._candidate_code(line.get("text"))
            if candidate:
                base["drawing_no"] = candidate; break

    for idx, line in enumerate(lines):
        text = str(line.get("text") or "")
        if _FIELD_SCALE.search(text):
            value = _nearest_value(lines, idx, _FIELD_SCALE, lambda v: bool(_SCALE_VALUE.search(v)))
            match = _SCALE_VALUE.search(value or text)
            if match:
                base["scale"] = f"1:{int(match.group(1))}"; break

    for idx, line in enumerate(lines):
        text = str(line.get("text") or "")
        if _FIELD_REV.search(text):
            value = _nearest_value(lines, idx, _FIELD_REV, lambda v: bool(_REV_VALUE.fullmatch(_norm(v))))
            if value:
                base["revision"] = _norm(value).upper(); break

    explicit_title = ""
    for idx, line in enumerate(lines):
        if _FIELD_TITLE.search(str(line.get("text") or "")):
            value = _nearest_value(lines, idx, _FIELD_TITLE, lambda v: 3 <= len(_norm(v)) <= 140)
            if value:
                explicit_title = _norm(value); break
    if not explicit_title:
        # Largest-font non-admin title-block line with drawing-type language wins.
        candidates: List[Tuple[float, str]] = []
        for line in lines:
            text = _norm(line.get("text"))
            low = text.lower()
            if not (3 <= len(text) <= 140) or any(word in low for word in _ADMIN_TITLE_WORDS):
                continue
            type_bonus = 8.0 if any(token in low for token in (
                "plan", "elevation", "section", "schedule", "legend", "abbreviation", "detail", "perspective"
            )) else 0.0
            candidates.append((_num(line.get("size")) + type_bonus + _num(line.get("corner_score")), text))
        if candidates:
            explicit_title = max(candidates, key=lambda item: item[0])[1]

    if explicit_title:
        base["drawing_title"] = explicit_title
        base["text"] = explicit_title + "\n" + title_text
    else:
        base["text"] = title_text
    base["spatial_title_lines"] = lines
    return base


def _page_read_key(page_id: int) -> str:
    return f"{SETTING_PREFIX}{int(page_id)}"


def _visual_key(page_id: int) -> str:
    return f"{VISUAL_PREFIX}{int(page_id)}"


def _visual_signature(path: Path) -> str:
    try:
        stat = path.stat()
        return hashlib.sha1(f"{path}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()
    except OSError:
        return hashlib.sha1(str(path).encode()).hexdigest()


def _visual_read_allowed(app: Any, page: Dict[str, Any], native: Dict[str, Any]) -> bool:
    if str(os.environ.get("PLANREADER_VISUAL_READ", "1")).strip().lower() in {"0", "false", "off", "no"}:
        return False
    if not hasattr(app, "_gemini_generate"):
        return False
    if not str(os.environ.get("GEMINI_API_KEY", "")).strip():
        return False
    if int(page.get("selected") or 0) != 1:
        return False
    if _regular_file(page.get("image_path")) is None:
        return False
    kind = str(page.get("page_type") or "").lower()
    if "artist" in kind or "render" in kind:
        return False
    # Native text is preferred. Visual read is a sparse/raster fallback only.
    return int(native.get("word_count") or 0) < 28 or int(native.get("char_count") or 0) < 160


def _visual_sheet_read(app: Any, workspace_id: int, page: Dict[str, Any]) -> Dict[str, Any]:
    path = _regular_file(page.get("image_path"))
    if path is None:
        return {}
    signature = _visual_signature(path)
    cached_raw = app.workspace_setting(int(workspace_id), _visual_key(int(page["id"])), "{}")
    try:
        cached = json.loads(str(cached_raw or "{}"))
    except Exception:
        cached = {}
    if cached.get("signature") == signature and isinstance(cached.get("result"), dict):
        return dict(cached["result"])

    schema = {
        "drawing_no": "", "drawing_title": "", "page_type": "", "scale": "", "revision": "",
        "confidence": 0,
        "legend_rows": [{"code": "", "meaning": ""}],
        "finish_material_rows": [{"code": "", "meaning": ""}],
        "visible_headings": [""],
    }
    prompt = (
        "Read this architectural drawing sheet visually because its native PDF text layer is sparse. "
        "Return only text that is clearly visible. Do not infer quantities, dimensions, areas, materials or codes that you cannot read. "
        "Identify the issued drawing number/title/type/scale/revision and, only if this is a legend/schedule/key, transcribe clearly visible code-to-meaning rows. "
        "Do not calculate take-off quantities. Confidence is 0-100 and must be below 80 if the title block is not clearly legible."
    )
    try:
        result = app._gemini_generate(
            str(os.environ.get("GEMINI_API_KEY") or ""),
            str(os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash"),
            prompt, [("image", str(path))], schema, "planreader_visual_sheet_read_v1228",
        )
    except Exception as exc:
        result = {"error": str(exc)[:500], "confidence": 0}
    app.set_workspace_setting(int(workspace_id), _visual_key(int(page["id"])), json.dumps({"signature": signature, "result": result}, separators=(",", ":"), default=str))
    return dict(result or {})


def _visual_text(result: Dict[str, Any]) -> str:
    if _num(result.get("confidence")) < 80:
        return ""
    lines = ["[VISUAL SHEET READ - PROVISIONAL IDENTITY / LEGEND TEXT]"]
    for key, label in (("drawing_no", "DRAWING NO"), ("drawing_title", "DRAWING TITLE"), ("page_type", "PAGE TYPE"), ("scale", "SCALE"), ("revision", "REVISION")):
        value = _norm(result.get(key))
        if value:
            lines.append(f"{label}: {value}")
    for item in result.get("legend_rows") or []:
        if isinstance(item, dict) and _norm(item.get("code")) and _norm(item.get("meaning")):
            lines.append(f"{_norm(item['code']).upper()} - {_norm(item['meaning'])}")
    for item in result.get("finish_material_rows") or []:
        if isinstance(item, dict) and _norm(item.get("code")) and _norm(item.get("meaning")):
            lines.append(f"{_norm(item['code']).upper()} - {_norm(item['meaning'])}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _manual_registration(app: Any, page_id: int) -> bool:
    try:
        return str(app.workspace_setting(int(app.lquery("SELECT workspace_id FROM pages WHERE id=?", (int(page_id),))[0]["workspace_id"]), registration._manual_key(int(page_id)), "")) == "1"
    except Exception:
        return False


def enhance_document_pages(app: Any, document_id: int, *, visual_budget: int = 4) -> Dict[str, Any]:
    docs = app.lquery("SELECT id,workspace_id,path,file_name FROM documents WHERE id=?", (int(document_id),))
    if not docs:
        return {"updated": 0, "visual": 0}
    doc = dict(docs[0]); workspace_id = int(doc["workspace_id"])
    path = Path(str(doc.get("path") or ""))
    if path.suffix.lower() != ".pdf" or not path.is_file() or getattr(app, "fitz", None) is None:
        return {"updated": 0, "visual": 0}
    pages = [dict(row) for row in app.lquery("SELECT * FROM pages WHERE document_id=? ORDER BY page_no,id", (int(document_id),))]
    updated = 0; visual_used = 0
    pdf = app.fitz.open(path)
    try:
        for page in pages:
            page_no = int(page.get("page_no") or 0)
            if not (1 <= page_no <= len(pdf)):
                continue
            pdf_page = pdf.load_page(page_no - 1)
            native = reconstruct_page_text(pdf_page, page)
            text = str(native.get("text") or "")
            visual = {}
            if visual_used < visual_budget and _visual_read_allowed(app, page, native):
                visual = _visual_sheet_read(app, workspace_id, page)
                extra = _visual_text(visual)
                if extra:
                    text = (text + "\n" + extra).strip(); visual_used += 1
            # Never replace a richer existing text stream with something clearly
            # poorer. Otherwise spatially reconstructed text becomes canonical.
            old = str(page.get("extracted_text") or "")
            if len(text) >= max(40, int(len(old) * 0.55)) or len(old) < 80:
                app.lexecute("UPDATE pages SET extracted_text=? WHERE id=?", (text, int(page["id"])))
                page["extracted_text"] = text; updated += 1

            title = spatial_title_block_evidence(pdf_page, registration._pb_v1228_base_title_reader)
            manual = str(app.workspace_setting(workspace_id, registration._manual_key(int(page["id"])), "")) == "1"
            if not manual:
                drawing_no = str(title.get("drawing_no") or page.get("page_label") or f"Page {page_no}")
                page_type, confidence, evidence = registration.weighted_page_type(text, str(doc.get("file_name") or ""), title.get("text"))
                drawing_title = str(title.get("drawing_title") or registration._sheet_title(title.get("text"), drawing_no, page_type) or "")
                scale_text = str(page.get("scale_text") or title.get("scale") or "")
                # For sparse raster pages, a high-confidence visual identity can fill
                # missing title fields but never override a manual registration.
                if visual and _num(visual.get("confidence")) >= 90:
                    if not drawing_no or drawing_no.lower().startswith("page "):
                        drawing_no = _norm(visual.get("drawing_no")) or drawing_no
                    if page_type == "Other" and _norm(visual.get("page_type")):
                        page_type = _norm(visual.get("page_type"))
                    if not drawing_title:
                        drawing_title = _norm(visual.get("drawing_title"))
                    if not scale_text:
                        scale_text = _norm(visual.get("scale"))
                app.lexecute("UPDATE pages SET page_label=?,page_type=?,scale_text=? WHERE id=?", (drawing_no, page_type, scale_text, int(page["id"])))
                meta = {
                    "version": VERSION, "title": drawing_title, "confidence": int(confidence), "evidence": evidence,
                    "drawing_no": drawing_no, "revision": str(title.get("revision") or ""), "detected_scale": str(title.get("scale") or ""),
                    "native_word_count": int(native.get("word_count") or 0), "visual_fallback": bool(visual and _num(visual.get("confidence")) >= 80),
                }
                app.set_workspace_setting(workspace_id, registration._meta_key(int(page["id"])), json.dumps(meta, separators=(",", ":")))
            app.set_workspace_setting(workspace_id, _page_read_key(int(page["id"])), json.dumps({
                "version": VERSION, "word_count": int(native.get("word_count") or 0), "char_count": int(native.get("char_count") or 0),
                "table_mode": _table_like(page, native.get("block_text") or ""), "visual_fallback": bool(visual),
            }, separators=(",", ":")))
    finally:
        pdf.close()
    try:
        registration.sync_drawing_register(app, workspace_id)
    except Exception:
        pass
    return {"updated": updated, "visual": visual_used, "workspace_id": workspace_id}


def enhance_selected_workspace(app: Any, workspace_id: int) -> Dict[str, Any]:
    rows = app.lquery("SELECT DISTINCT document_id FROM pages WHERE workspace_id=? AND COALESCE(selected,0)=1 ORDER BY document_id", (int(workspace_id),))
    total = 0; visual = 0
    remaining_visual = max(0, int(_num(os.environ.get("PLANREADER_VISUAL_READ_MAX_PAGES"), 4)))
    for row in rows:
        report = enhance_document_pages(app, int(row["document_id"]), visual_budget=remaining_visual)
        total += int(report.get("updated") or 0); used = int(report.get("visual") or 0); visual += used
        remaining_visual = max(0, remaining_visual - used)
    return {"updated": total, "visual": visual}


def _dimension_tokens(pdf_page: Any) -> List[Dict[str, Any]]:
    rows = _word_rows(pdf_page)
    candidates: List[Dict[str, Any]] = []
    for row in rows:
        words = row.get("words") or []
        for start in range(len(words)):
            for count in (1, 2, 3):
                parts = words[start:start + count]
                if len(parts) != count:
                    continue
                gaps_ok = True
                for a, b in zip(parts, parts[1:]):
                    if b[0] - a[2] > max(14.0, (a[3] - a[1]) * 1.8):
                        gaps_ok = False; break
                if not gaps_ok:
                    continue
                raw = " ".join(str(item[4]) for item in parts).strip()
                compact = re.sub(r"\s+", "", raw)
                if not re.fullmatch(r"(?:\d{1,2}[ .]?\d{3}|\d{2,5})(?:mm)?|\d+(?:\.\d+)?m", compact, re.I):
                    continue
                x0, y0 = parts[0][0], min(item[1] for item in parts)
                x1, y1 = parts[-1][2], max(item[3] for item in parts)
                candidates.append({"text": raw, "compact": compact, "bbox": [x0, y0, x1, y1]})
    # De-duplicate overlapping 1/2/3-word candidates by bbox/text.
    unique = []
    seen = set()
    for item in candidates:
        key = (item["compact"].lower(), tuple(round(v, 1) for v in item["bbox"]))
        if key not in seen:
            seen.add(key); unique.append(item)
    return unique


def _dimension_value_m(raw: str) -> Optional[float]:
    compact = re.sub(r"\s+", "", str(raw or "")).lower()
    try:
        if compact.endswith("mm"):
            return float(compact[:-2]) / 1000.0
        if compact.endswith("m"):
            return float(compact[:-1])
        # Architectural dimensions such as 3 600 / 3600 are millimetres.
        if re.fullmatch(r"\d{3,5}", compact):
            value = float(compact) / 1000.0
            return value if 0.05 <= value <= 200.0 else None
        # Split thousands such as 3 600 become 3600 after whitespace removal.
        if re.fullmatch(r"\d{1,2}\d{3}", compact):
            value = float(compact) / 1000.0
            return value if 0.05 <= value <= 200.0 else None
    except ValueError:
        return None
    return None


def split_dimension_calibration(app: Any, page: Dict[str, Any], base_reader) -> Optional[Dict[str, Any]]:
    base = base_reader(app, page)
    # Keep an already strong geometry-matched result.
    if base and _num(base.get("score")) >= 8.0:
        return base
    docs = app.lquery("SELECT path FROM documents WHERE id=?", (int(page.get("document_id") or 0),))
    if not docs or getattr(app, "fitz", None) is None:
        return base
    path = Path(str(docs[0].get("path") or ""))
    page_no = int(page.get("page_no") or 0)
    if not path.is_file() or path.suffix.lower() != ".pdf" or page_no <= 0:
        return base
    pdf = app.fitz.open(path)
    try:
        pdf_page = pdf.load_page(page_no - 1)
        tokens = _dimension_tokens(pdf_page)
        lines = list(auto._iter_pdf_lines(pdf_page.get_drawings() or []))
    finally:
        pdf.close()
    if not tokens or not lines:
        return base
    zoom = max(0.05, _num(page.get("render_zoom"), 1.0))
    expected = 0.0
    try:
        expected = _num((app.auto_detect_scale(page) or {}).get("px_per_m"))
    except Exception:
        pass
    candidates = []
    for token in tokens:
        real_m = _dimension_value_m(token["text"])
        if not real_m:
            continue
        box = token["bbox"]
        for base_line in lines:
            x1, y1, x2, y2 = base_line
            length_pt = math.hypot(x2 - x1, y2 - y1)
            if not (8.0 <= length_pt <= 1800.0):
                continue
            distance = auto._line_distance_to_box(x1, y1, x2, y2, box)
            if distance > max(26.0, (box[3] - box[1]) * 5.0):
                continue
            witnesses = reading._witness_count(base_line, lines)
            if witnesses == 0:
                continue
            pxpm = length_pt * zoom / real_m
            if not (5.0 <= pxpm <= 5000.0):
                continue
            score = 5.0 + witnesses * 3.0 - distance / 8.0
            if expected > 0:
                rel = abs(pxpm - expected) / expected
                score += 4.0 if rel <= 0.10 else (1.0 if rel <= 0.25 else -2.0)
            candidates.append({
                "px_per_m": pxpm, "score": score, "dimension_m": real_m,
                "dimension_text": token["text"], "line_length_pt": length_pt,
                "witness_count": witnesses, "bbox": box,
            })
    result = auto.choose_dimension_calibration(candidates, expected)
    if result and (not base or _num(result.get("score")) > _num(base.get("score"))):
        result["evidence"] = "Spatially reconstructed split dimension matched to dimension line/witness geometry"
        return result
    return base


def apply(app: Any) -> None:
    if getattr(app, "_pb_plan_read_engine_v1228_applied", False):
        return
    app._pb_plan_read_engine_v1228_applied = True

    # Keep a stable reference so enhancement can call the pre-v1.2.28 title reader
    # without recursing through this wrapper.
    if not hasattr(registration, "_pb_v1228_base_title_reader"):
        registration._pb_v1228_base_title_reader = registration.title_block_evidence
    base_title = registration._pb_v1228_base_title_reader
    registration.title_block_evidence = lambda pdf_page: spatial_title_block_evidence(pdf_page, base_title)

    base_dimension = auto.detect_dimension_calibration
    auto.detect_dimension_calibration = lambda app_obj, page: split_dimension_calibration(app_obj, dict(page), base_dimension)

    base_process = app.process_document
    def _process(document_id: int, *args, **kwargs):
        result = base_process(document_id, *args, **kwargs)
        try:
            enhance_document_pages(app, int(document_id), visual_budget=2)
        except Exception:
            pass
        return result
    app.process_document = _process

    # This is the important ordering change: every automatic geometry/take-off run
    # receives the spatially reconstructed selected-page text before any material,
    # floor, elevation, legend or 3D analysis starts.
    base_analyse = auto.analyse_workspace
    def _analyse(app_obj: Any, workspace_id: int):
        read_report = enhance_selected_workspace(app_obj, int(workspace_id))
        report = base_analyse(app_obj, int(workspace_id))
        if isinstance(report, dict):
            report["plan_read_engine"] = {"version": VERSION, **read_report}
        return report
    auto.analyse_workspace = _analyse
    app.run_auto_geometry = lambda workspace_id: auto.analyse_workspace(app, int(workspace_id))

    app.reconstruct_page_text_v1228 = reconstruct_page_text
    app.enhance_plan_reading_v1228 = lambda workspace_id: enhance_selected_workspace(app, int(workspace_id))
    app.read_title_block_v1228 = registration.title_block_evidence
    app.detect_dimension_calibration_v1228 = lambda page: auto.detect_dimension_calibration(app, dict(page))
