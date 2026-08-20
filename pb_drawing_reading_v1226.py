"""PlanReader v1.2.26 drawing-reading upgrades.

Improves two foundations used by every downstream estimator feature:
- issued sheet title-block fields are preferred over arbitrary page text;
- dimension calibration looks for dimension-line geometry/witness marks around a
  printed dimension before accepting the numeric value.

The existing v1.2.25/v1.2.19 readers remain fallbacks for unusual drawing sets.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pb_auto_geometry_v1219 as auto
import pb_page_registration_v1225 as registration

VERSION = "1.2.26"

_DRAWING_FIELD = re.compile(r"\b(?:DRAWING|DWG|SHEET)\s*(?:NO\.?|NUMBER|#)\s*[:\-]?\s*([A-Z][A-Z0-9._-]{1,18})", re.I)
_SCALE_FIELD = re.compile(r"\bSCALE\s*[:\-]?\s*(1\s*:\s*\d{2,4})", re.I)
_REV_FIELD = re.compile(r"\bREV(?:ISION)?\s*[:\-]?\s*([A-Z0-9]{1,4})\b", re.I)
_TITLE_FIELD = re.compile(r"\b(?:DRAWING\s+TITLE|SHEET\s+TITLE|TITLE)\s*[:\-]?\s*(.{3,100})", re.I)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _title_blocks(pdf_page: Any) -> List[Dict[str, Any]]:
    rect = pdf_page.rect
    width, height = float(rect.width), float(rect.height)
    out: List[Dict[str, Any]] = []
    try:
        blocks = pdf_page.get_text("blocks") or []
    except Exception:
        blocks = []
    for block in blocks:
        if len(block) < 5:
            continue
        try:
            x0, y0, x1, y1 = map(float, block[:4])
        except Exception:
            continue
        text = _norm(block[4])
        if not text:
            continue
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        if cy >= height * 0.66 or cx >= width * 0.64:
            out.append({"text": text, "bbox": [x0, y0, x1, y1]})
    # Right/bottom corner is normally the strongest architectural title-block zone.
    out.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))
    return out


def enhanced_title_block_evidence(pdf_page: Any, base_reader) -> Dict[str, Any]:
    base = dict(base_reader(pdf_page) or {})
    blocks = _title_blocks(pdf_page)
    block_text = "\n".join(item["text"] for item in blocks)
    if not block_text:
        return base

    drawing_match = _DRAWING_FIELD.search(block_text)
    if drawing_match:
        candidate = drawing_match.group(1).upper().strip(" ._-:")
        # Reuse v1.2.25's architectural-code rejection rules.
        verified = registration._candidate_code(f"DRAWING NO {candidate}")
        if verified:
            base["drawing_no"] = verified

    scale_match = _SCALE_FIELD.search(block_text)
    if scale_match:
        base["scale"] = re.sub(r"\s+", "", scale_match.group(1))
    revision_match = _REV_FIELD.search(block_text)
    if revision_match:
        base["revision"] = revision_match.group(1).upper()

    title_match = _TITLE_FIELD.search(block_text)
    explicit_title = _norm(title_match.group(1)) if title_match else ""
    # Put explicit title text first so v1.2.25's title chooser sees it ahead of
    # project/client/general notes in the same title-block band.
    if explicit_title:
        base["text"] = explicit_title + "\n" + block_text
        base["drawing_title"] = explicit_title
    else:
        base["text"] = block_text
    base["title_blocks"] = blocks
    return base


def _near(a: Tuple[float, float], b: Tuple[float, float], tolerance: float) -> bool:
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= tolerance


def _witness_count(base_line: Tuple[float, float, float, float], lines: Sequence[Tuple[float, float, float, float]]) -> int:
    x1, y1, x2, y2 = base_line
    horizontal = abs(x2 - x1) >= abs(y2 - y1)
    length = math.hypot(x2 - x1, y2 - y1)
    tolerance = max(5.0, min(24.0, length * 0.06))
    endpoints = [(x1, y1), (x2, y2)]
    hits = 0
    for endpoint in endpoints:
        found = False
        for ax, ay, bx, by in lines:
            if (ax, ay, bx, by) == base_line:
                continue
            candidate_len = math.hypot(bx - ax, by - ay)
            if not (2.0 <= candidate_len <= max(80.0, length * 0.30)):
                continue
            candidate_horizontal = abs(bx - ax) >= abs(by - ay)
            if candidate_horizontal == horizontal:
                continue
            if _near(endpoint, (ax, ay), tolerance) or _near(endpoint, (bx, by), tolerance):
                found = True
                break
        if found:
            hits += 1
    return hits


def detect_dimension_calibration(app: Any, page: Dict[str, Any], base_reader) -> Optional[Dict[str, Any]]:
    fitz = getattr(app, "fitz", None)
    if fitz is None:
        return base_reader(app, page)
    docs = app.lquery("SELECT path FROM documents WHERE id=?", (int(page.get("document_id") or 0),))
    if not docs:
        return base_reader(app, page)
    path = Path(str(docs[0].get("path") or ""))
    if path.suffix.lower() != ".pdf" or not path.is_file():
        return base_reader(app, page)
    page_no = int(page.get("page_no") or 0)
    if page_no <= 0:
        return base_reader(app, page)

    pdf = fitz.open(path)
    try:
        pdf_page = pdf.load_page(page_no - 1)
        words = pdf_page.get_text("words") or []
        lines = list(auto._iter_pdf_lines(pdf_page.get_drawings() or []))
    finally:
        pdf.close()
    if not words or not lines:
        return base_reader(app, page)

    expected = 0.0
    try:
        scale = app.auto_detect_scale(page)
        expected = _num((scale or {}).get("px_per_m"))
    except Exception:
        pass
    zoom = max(0.05, _num(page.get("render_zoom"), 1.0))
    candidates: List[Dict[str, Any]] = []
    for word in words:
        if len(word) < 5:
            continue
        token = str(word[4])
        real_m = auto._dimension_value_m(token)
        if real_m is None:
            continue
        box = [float(v) for v in word[:4]]
        cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
        explicit_unit = bool(re.search(r"(?:mm|m)\s*$", token, re.I))
        for base_line in lines:
            x1, y1, x2, y2 = base_line
            length_pt = math.hypot(x2 - x1, y2 - y1)
            if not (8.0 <= length_pt <= 1800.0):
                continue
            distance = auto._line_distance_to_box(x1, y1, x2, y2, box)
            if distance > max(24.0, (box[3] - box[1]) * 4.5):
                continue
            pxpm = length_pt * zoom / real_m
            if not (5.0 <= pxpm <= 5000.0):
                continue
            witnesses = _witness_count(base_line, lines)
            score = max(0.0, 7.0 - distance / 5.0) + witnesses * 3.0 + (2.0 if explicit_unit else 0.0)
            if expected > 0:
                rel = abs(pxpm - expected) / expected
                score += 4.0 if rel <= 0.10 else (1.0 if rel <= 0.25 else 0.0)
            # Unlabelled numeric strings need at least one endpoint witness unless
            # they agree very strongly with the printed scale.
            if not explicit_unit and witnesses == 0 and not (expected > 0 and abs(pxpm - expected) / expected <= 0.08):
                continue
            candidates.append({
                "px_per_m": pxpm, "score": score, "dimension_m": real_m,
                "dimension_text": token, "line_length_pt": length_pt,
                "witness_count": witnesses, "bbox": box, "center": [cx * zoom, cy * zoom],
            })

    result = auto.choose_dimension_calibration(candidates, expected)
    if result:
        result["evidence"] = "Printed dimension matched to dimension line" + (f" with {result.get('witness_count')} endpoint witness mark(s)" if result.get("witness_count") is not None else "")
        return result
    return base_reader(app, page)


def apply(app: Any) -> None:
    if getattr(app, "_pb_drawing_reading_v1226_applied", False):
        return
    app._pb_drawing_reading_v1226_applied = True

    base_title_reader = registration.title_block_evidence
    registration.title_block_evidence = lambda pdf_page: enhanced_title_block_evidence(pdf_page, base_title_reader)

    base_dimension_reader = auto.detect_dimension_calibration
    auto.detect_dimension_calibration = lambda app_obj, page: detect_dimension_calibration(app_obj, dict(page), base_dimension_reader)

    app.read_title_block_v1226 = registration.title_block_evidence
    app.detect_dimension_calibration_v1226 = lambda page: auto.detect_dimension_calibration(app, dict(page))
