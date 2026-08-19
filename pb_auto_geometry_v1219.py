"""PlanReader v1.2.19 automatic drawing triage, self-calibration and geometry.

This module makes the non-AI workflow materially more automatic while keeping a
strict evidence hierarchy:

* pages that are irrelevant to painting take-off are auto-deselected before
  raster processing (they remain in the drawing register and can be restored);
* manual page calibration is never overwritten;
* vector-PDF dimension lines are preferred for automatic calibration;
* printed scale is a provisional fallback and calibrated floor-plan geometry can
  cross-reference elevation width where the facade orientation is identifiable;
* clearly documented unit areas become floor-area reference rows automatically;
* unit polygons detected from closed drawing boundaries remain provisional until
  reviewed;
* elevation gross areas / explicitly documented substrate areas become external
  take-off rows and are linked to an automatically derived 3D envelope.

No commercial rates, coating systems, coats or productivity are invented here.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

try:
    import cv2
except Exception:  # pragma: no cover - production dependency exists
    cv2 = None

try:
    import pb_3d_surface_editor_v1212 as surface_v1212
except Exception:  # pragma: no cover
    surface_v1212 = None

VERSION = "1.2.19"
SOURCE_PREFIX = f"PB Auto Geometry v{VERSION}"
MODEL_SOURCE_PREFIX = f"{SOURCE_PREFIX} · envelope"
SETTING_KEY = "auto_geometry_v1219"

_KEEP_TYPES = {
    "Floor Plan",
    "Reflected Ceiling Plan",
    "Elevation",
    "Section",
    "Door / Window Schedule",
    "Finishes Schedule",
    "Specification",
    "Render / Artist's Impression",
}
_DROP_TYPES = {"Structural", "Services", "Landscape / Civil"}
_RELEVANT_WORDS = (
    "paint", "painting", "finish", "colour", "color", "cladding", "render",
    "soffit", "eave", "fascia", "balustrade", "external", "elevation",
    "floor plan", "ceiling", "door schedule", "window schedule", "substrate",
    "linea", "easylap", "textureboard", "weatherboard", "blockwork",
)
_ROOF_RELEVANT_WORDS = ("soffit", "eave", "fascia", "canopy", "awning", "paint")
_UNIT_LABEL_RE = re.compile(
    r"\b(?:UNIT|APT|APARTMENT|VILLA|TOWNHOUSE|TENANCY)\s*[-#:]*\s*([A-Z0-9][A-Z0-9.-]*)\b",
    re.IGNORECASE,
)
_AREA_RE = re.compile(r"\b(\d{1,4}(?:\.\d{1,2})?)\s*(?:m\s*[²2]|sqm|sq\.?\s*m)\b", re.IGNORECASE)
_DIM_RE = re.compile(r"(?<![:\d])(?P<num>\d{2,5}(?:\.\d{1,3})?)\s*(?P<unit>mm|m)?(?!\s*[:\d])", re.IGNORECASE)

_SUBSTRATE_RULES: Sequence[Tuple[Tuple[str, ...], str, str]] = (
    (("lineaboard", "linea"), "EC1", "Lineaboard Cladding"),
    (("textureboard",), "EC2", "Textureboard Cladding"),
    (("easylap",), "EC3", "Easylap Cladding"),
    (("render", "rendered block", "blockwork"), "RBL", "Rendered / Blockwork"),
    (("timber look", "timber cladding", "weatherboard"), "EC5", "Timber / Weatherboard Cladding"),
    (("soffit", "eave"), "SOF", "Soffits / Eaves"),
    (("screen",), "SCR", "Screens"),
    (("balustrade",), "BA1", "Balustrade"),
    (("sunhood", "sun hood"), "SHD", "Sunhoods"),
    (("downpipe",), "DP", "Downpipes"),
    (("garage door",), "GD", "Garage Doors"),
)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _safe_json(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except Exception:
        return fallback


def _is_auto_scale(page: Dict[str, Any]) -> bool:
    return str(page.get("scale_text") or "").startswith("Auto ")


def page_relevance(page_type: Any, text: Any = "", label: Any = "") -> Tuple[bool, str, int]:
    """Conservatively decide whether a sheet is useful for painting take-off.

    Auto-discard means ``selected=0`` only. The sheet row and source document are
    retained so an estimator can reselect it at any time.
    """
    kind = str(page_type or "Other").strip() or "Other"
    low = f"{text or ''} {label or ''}".lower()
    if kind in _DROP_TYPES:
        return False, f"{kind} is normally outside painting take-off", 0
    if kind in _KEEP_TYPES:
        return True, f"{kind} is a painting take-off source", 100
    if kind == "Roof Plan":
        keep = any(word in low for word in _ROOF_RELEVANT_WORDS)
        return keep, ("Roof sheet contains eave/soffit/fascia scope" if keep else "Roof plan has no painting-scope keywords"), 70 if keep else 10
    if kind == "Title / Drawing Register":
        return False, "Reference sheet retained in register but not rasterised for take-off", 20
    if any(word in low for word in _RELEVANT_WORDS):
        return True, "Painting-scope keywords found on otherwise unclassified sheet", 60
    return False, "No painting take-off evidence found", 5


def auto_select_document_pages(app: Any, document_id: int) -> Dict[str, Any]:
    rows = app.lquery(
        "SELECT id,page_no,page_label,page_type,extracted_text,selected FROM pages WHERE document_id=? ORDER BY page_no,id",
        (int(document_id),),
    )
    decisions: List[Dict[str, Any]] = []
    conn = app.local_connect()
    try:
        for row in rows:
            keep, reason, score = page_relevance(row.get("page_type"), row.get("extracted_text"), row.get("page_label"))
            conn.execute("UPDATE pages SET selected=? WHERE id=?", (1 if keep else 0, int(row["id"])))
            decisions.append({
                "page_id": int(row["id"]), "page_no": int(row.get("page_no") or 0),
                "label": str(row.get("page_label") or ""), "type": str(row.get("page_type") or ""),
                "selected": bool(keep), "reason": reason, "score": score,
            })
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "document_id": int(document_id),
        "kept": sum(1 for item in decisions if item["selected"]),
        "discarded": sum(1 for item in decisions if not item["selected"]),
        "pages": decisions,
    }


def _dimension_value_m(token: Any) -> Optional[float]:
    text = str(token or "").strip().lower().replace(",", "")
    match = _DIM_RE.fullmatch(text)
    if not match:
        return None
    value = _num(match.group("num"))
    unit = str(match.group("unit") or "").lower()
    if unit == "mm" or (not unit and value >= 100):
        value /= 1000.0
    elif unit != "m":
        return None
    if not (0.30 <= value <= 100.0):
        return None
    return value


def _line_distance_to_box(x1: float, y1: float, x2: float, y2: float, box: Sequence[float]) -> float:
    x0, y0, bx1, by1 = [float(v) for v in box[:4]]
    cx, cy = (x0 + bx1) / 2.0, (y0 + by1) / 2.0
    if abs(y2 - y1) <= abs(x2 - x1):
        outside = max(0.0, min(x1, x2) - cx, cx - max(x1, x2))
        return abs((y1 + y2) / 2.0 - cy) + outside
    outside = max(0.0, min(y1, y2) - cy, cy - max(y1, y2))
    return abs((x1 + x2) / 2.0 - cx) + outside


def _iter_pdf_lines(drawing_items: Iterable[Any]) -> Iterable[Tuple[float, float, float, float]]:
    for drawing in drawing_items or []:
        for item in drawing.get("items", []) if isinstance(drawing, dict) else []:
            if not item or item[0] != "l" or len(item) < 3:
                continue
            p1, p2 = item[1], item[2]
            try:
                yield float(p1.x), float(p1.y), float(p2.x), float(p2.y)
            except Exception:
                try:
                    yield float(p1[0]), float(p1[1]), float(p2[0]), float(p2[1])
                except Exception:
                    continue


def choose_dimension_calibration(candidates: Sequence[Dict[str, Any]], expected_px_per_m: float = 0.0) -> Optional[Dict[str, Any]]:
    """Choose a dimension-line calibration using score plus consensus.

    Candidates within 7% of one another reinforce each other. A printed-scale
    expectation can increase confidence, but never creates a dimension result on
    its own.
    """
    valid = [dict(c) for c in candidates if 5.0 <= _num(c.get("px_per_m")) <= 5000.0]
    if not valid:
        return None
    for candidate in valid:
        pxpm = _num(candidate.get("px_per_m"))
        consensus = sum(1 for other in valid if abs(_num(other.get("px_per_m")) - pxpm) / max(pxpm, 1e-9) <= 0.07)
        candidate["consensus"] = consensus
        candidate["rank"] = _num(candidate.get("score")) + min(consensus, 4) * 2.0
        if expected_px_per_m > 0:
            rel = abs(pxpm - expected_px_per_m) / expected_px_per_m
            candidate["rank"] += 5.0 if rel <= 0.10 else (2.0 if rel <= 0.25 else 0.0)
    best = max(valid, key=lambda item: (_num(item.get("rank")), _num(item.get("score"))))
    group = [c for c in valid if abs(_num(c.get("px_per_m")) - _num(best.get("px_per_m"))) / max(_num(best.get("px_per_m")), 1e-9) <= 0.07]
    weights = [max(1.0, _num(c.get("score"), 1.0)) for c in group]
    pxpm = sum(_num(c.get("px_per_m")) * w for c, w in zip(group, weights)) / sum(weights)
    result = dict(best)
    result["px_per_m"] = round(pxpm, 4)
    result["consensus"] = len(group)
    result["confidence"] = "High" if len(group) >= 2 or _num(best.get("rank")) >= 10 else "Medium"
    return result


def detect_dimension_calibration(app: Any, page: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Read native PDF words and dimension lines to infer pixels-per-metre."""
    fitz = getattr(app, "fitz", None)
    if fitz is None:
        return None
    docs = app.lquery("SELECT path FROM documents WHERE id=?", (int(page.get("document_id") or 0),))
    if not docs:
        return None
    path = Path(str(docs[0].get("path") or ""))
    if path.suffix.lower() != ".pdf" or not path.is_file():
        return None
    page_no = int(page.get("page_no") or 0)
    if page_no <= 0:
        return None
    pdf = fitz.open(path)
    try:
        pdf_page = pdf.load_page(page_no - 1)
        words = pdf_page.get_text("words") or []
        lines = list(_iter_pdf_lines(pdf_page.get_drawings() or []))
    finally:
        pdf.close()
    if not words or not lines:
        return None

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
        real_m = _dimension_value_m(word[4])
        if real_m is None:
            continue
        box = word[:4]
        box_h = max(1.0, float(box[3]) - float(box[1]))
        near_limit = max(22.0, box_h * 3.5)
        for x1, y1, x2, y2 in lines:
            length_pt = math.hypot(x2 - x1, y2 - y1)
            if not (8.0 <= length_pt <= 1600.0):
                continue
            distance = _line_distance_to_box(x1, y1, x2, y2, box)
            if distance > near_limit:
                continue
            pxpm = length_pt * zoom / real_m
            if not (5.0 <= pxpm <= 5000.0):
                continue
            score = max(0.0, 6.0 - distance / max(near_limit / 6.0, 1.0))
            if expected > 0:
                rel = abs(pxpm - expected) / expected
                score += 4.0 if rel <= 0.10 else (1.5 if rel <= 0.25 else 0.0)
            candidates.append({
                "px_per_m": pxpm, "score": score, "dimension_m": real_m,
                "dimension_text": str(word[4]), "line_length_pt": length_pt,
            })
    return choose_dimension_calibration(candidates, expected)


def _regular_image(path_value: Any) -> Optional[Path]:
    raw = str(path_value or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_file() else None


def _drawing_component(image_path: Path, *, elevation: bool = False) -> Optional[Dict[str, Any]]:
    """Return the dominant drawing cluster and an approximate outer contour.

    The bottom title-block band is excluded. The result is a geometry candidate,
    not a claim of measured accuracy; downstream rows remain provisional unless
    supported by explicit document quantities.
    """
    if cv2 is None:
        return None
    image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if image is None or image.size == 0:
        return None
    height, width = image.shape[:2]
    y_end = max(1, int(height * (0.82 if elevation else 0.86)))
    x_start, x_end = int(width * 0.03), int(width * 0.97)
    roi = image[:y_end, x_start:x_end]
    _, ink = cv2.threshold(roi, 205, 255, cv2.THRESH_BINARY_INV)
    kernel_size = 3 if max(width, height) < 2200 else 5
    kernel = np.ones((kernel_size, kernel_size), np.uint8)
    connected = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, kernel, iterations=1)
    count, labels, stats, _ = cv2.connectedComponentsWithStats((connected > 0).astype(np.uint8), 8)
    best = None
    roi_area = float(max(1, roi.shape[0] * roi.shape[1]))
    for label_id in range(1, count):
        x, y, w, h, pixels = [int(v) for v in stats[label_id]]
        bbox_area = float(w * h)
        frac = bbox_area / roi_area
        if w < roi.shape[1] * 0.12 or h < roi.shape[0] * 0.10 or not (0.015 <= frac <= 0.78):
            continue
        density = pixels / max(bbox_area, 1.0)
        if not (0.006 <= density <= 0.45):
            continue
        score = bbox_area * (0.45 + min(density, 0.12) * 5.0)
        if best is None or score > best["score"]:
            best = {"label": label_id, "score": score, "bbox": (x + x_start, y, w, h), "density": density}
    if best is None:
        return None
    component_mask = np.zeros_like(connected)
    component_mask[labels == best["label"]] = 255
    contours, _ = cv2.findContours(component_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    contour[:, 0, 0] += x_start
    perimeter = cv2.arcLength(contour, True)
    approx = cv2.approxPolyDP(contour, max(2.0, perimeter * 0.006), True)
    points = [[float(p[0][0]), float(p[0][1])] for p in approx]
    x, y, w, h = best["bbox"]
    return {
        "bbox": [float(x), float(y), float(w), float(h)],
        "polygon": points,
        "pixel_area": float(abs(cv2.contourArea(contour))),
        "density": float(best["density"]),
        "image_width": width,
        "image_height": height,
    }


def _page_face(text: Any, label: Any = "") -> str:
    low = f"{text or ''} {label or ''}".lower()
    for token, face in (
        ("north elevation", "rear"), ("north elev", "rear"),
        ("south elevation", "front"), ("south elev", "front"),
        ("east elevation", "right"), ("east elev", "right"),
        ("west elevation", "left"), ("west elev", "left"),
        ("front elevation", "front"), ("rear elevation", "rear"),
        ("back elevation", "rear"), ("left elevation", "left"), ("right elevation", "right"),
    ):
        if token in low:
            return face
    return ""


def _substrates_from_text(text: Any) -> List[Dict[str, str]]:
    low = str(text or "").lower()
    found: List[Dict[str, str]] = []
    for needles, code, name in _SUBSTRATE_RULES:
        if any(needle in low for needle in needles):
            found.append({"code": code, "name": name})
    # Preserve explicit EC/RBL/SOF style codes even when a project uses a custom legend.
    for code in sorted(set(re.findall(r"\b(?:EC\d+|RBL\d*|SOF\d*|FC\d+|CL\d+)\b", str(text or ""), flags=re.IGNORECASE))):
        upper = code.upper()
        if not any(item["code"] == upper for item in found):
            found.append({"code": upper, "name": upper})
    return found


def extract_unit_area_candidates(text: Any) -> List[Dict[str, Any]]:
    """Extract explicit UNIT/APARTMENT/VILLA floor areas from text/schedules."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(text or "").splitlines() if line.strip()]
    out: List[Dict[str, Any]] = []
    used: set[str] = set()
    for idx, line in enumerate(lines):
        unit_match = _UNIT_LABEL_RE.search(line)
        if not unit_match:
            continue
        label = f"Unit {unit_match.group(1)}"
        search_lines = [line]
        if idx + 1 < len(lines):
            search_lines.append(lines[idx + 1])
        if idx > 0:
            search_lines.append(lines[idx - 1])
        area = None
        source_line = line
        for candidate_line in search_lines:
            match = _AREA_RE.search(candidate_line)
            if match:
                area = _num(match.group(1))
                source_line = candidate_line
                break
        key = label.lower()
        if area and 8.0 <= area <= 1000.0 and key not in used:
            out.append({"label": label, "area_m2": round(area, 2), "confidence": "Documented", "source": source_line})
            used.add(key)
    return out


def extract_substrate_area_candidates(text: Any) -> List[Dict[str, Any]]:
    """Read explicit substrate + m² statements from schedules/elevation notes."""
    out: List[Dict[str, Any]] = []
    for raw_line in str(text or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        area_match = _AREA_RE.search(line)
        if not area_match:
            continue
        area = _num(area_match.group(1))
        if not (0.2 <= area <= 50000.0):
            continue
        subs = _substrates_from_text(line)
        if not subs:
            continue
        # A line with multiple material names is ambiguous and is left for review.
        if len(subs) == 1:
            out.append({"substrate": subs[0], "area_m2": round(area, 2), "source": line, "confidence": "Documented"})
    return out


def _pdf_word_lines(app: Any, page: Dict[str, Any]) -> List[Dict[str, Any]]:
    fitz = getattr(app, "fitz", None)
    if fitz is None:
        return []
    docs = app.lquery("SELECT path FROM documents WHERE id=?", (int(page.get("document_id") or 0),))
    if not docs:
        return []
    path = Path(str(docs[0].get("path") or ""))
    if path.suffix.lower() != ".pdf" or not path.is_file():
        return []
    pdf = fitz.open(path)
    try:
        pdf_page = pdf.load_page(int(page.get("page_no") or 1) - 1)
        words = pdf_page.get_text("words") or []
    finally:
        pdf.close()
    grouped: Dict[Tuple[int, int], List[Any]] = {}
    for word in words:
        if len(word) < 8:
            continue
        grouped.setdefault((int(word[5]), int(word[6])), []).append(word)
    lines: List[Dict[str, Any]] = []
    zoom = max(0.05, _num(page.get("render_zoom"), 1.0))
    for values in grouped.values():
        values.sort(key=lambda item: int(item[7]))
        text = " ".join(str(item[4]) for item in values)
        x0 = min(float(item[0]) for item in values) * zoom
        y0 = min(float(item[1]) for item in values) * zoom
        x1 = max(float(item[2]) for item in values) * zoom
        y1 = max(float(item[3]) for item in values) * zoom
        lines.append({"text": text, "bbox": [x0, y0, x1, y1], "center": [(x0 + x1) / 2.0, (y0 + y1) / 2.0]})
    return lines


def _unit_boundary_candidates(app: Any, page: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Detect closed unit boundaries around explicit unit labels.

    This is intentionally conservative. A contour shared by multiple unit labels
    is rejected rather than divided heuristically.
    """
    if cv2 is None or _num(page.get("px_per_m")) <= 0:
        return []
    image_path = _regular_image(page.get("image_path"))
    if image_path is None:
        return []
    lines = _pdf_word_lines(app, page)
    labels = []
    for line in lines:
        match = _UNIT_LABEL_RE.search(line["text"])
        if match:
            labels.append({"label": f"Unit {match.group(1)}", "center": line["center"]})
    if not labels:
        return []
    image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        return []
    height, width = image.shape[:2]
    _, ink = cv2.threshold(image, 205, 255, cv2.THRESH_BINARY_INV)
    ink[int(height * 0.88):, :] = 0  # ignore title block
    kernel = np.ones((3, 3), np.uint8)
    ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, kernel, iterations=1)
    contours, _ = cv2.findContours(ink, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    page_area = float(width * height)
    eligible = []
    for idx, contour in enumerate(contours):
        area = abs(float(cv2.contourArea(contour)))
        if not (page_area * 0.008 <= area <= page_area * 0.45):
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if w < width * 0.06 or h < height * 0.06 or y > height * 0.86:
            continue
        eligible.append((idx, contour, area, (x, y, w, h)))
    chosen: List[Tuple[Dict[str, Any], int, Any, float, Any]] = []
    for label in labels:
        cx, cy = label["center"]
        containing = [item for item in eligible if cv2.pointPolygonTest(item[1], (float(cx), float(cy)), False) >= 0]
        if not containing:
            continue
        # Prefer the largest plausible enclosing boundary; small inner room/text
        # loops are common around a unit label.
        item = max(containing, key=lambda candidate: candidate[2])
        chosen.append((label, item[0], item[1], item[2], item[3]))
    contour_use: Dict[int, int] = {}
    for _label, contour_id, *_rest in chosen:
        contour_use[contour_id] = contour_use.get(contour_id, 0) + 1
    pxpm = _num(page.get("px_per_m"))
    results: List[Dict[str, Any]] = []
    for label, contour_id, contour, area_px, bbox in chosen:
        if contour_use.get(contour_id, 0) != 1:
            continue
        area_m2 = area_px / (pxpm * pxpm)
        if not (8.0 <= area_m2 <= 1000.0):
            continue
        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, max(2.0, perimeter * 0.005), True)
        results.append({
            "label": label["label"], "area_m2": round(area_m2, 2), "confidence": "Derived",
            "source": "Closed drawing boundary around unit label", "bbox": [float(v) for v in bbox],
            "polygon": [[float(p[0][0]), float(p[0][1])] for p in approx],
        })
    return results


def _auto_calibrate_page(app: Any, page: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    current = _num(page.get("px_per_m"))
    if current > 0 and not _is_auto_scale(page):
        return {"page_id": int(page["id"]), "method": "Manual/existing", "px_per_m": current, "confidence": "Manual"}
    detected = detect_dimension_calibration(app, page)
    if detected:
        label = f"Auto dimension {detected.get('dimension_text')} · {detected.get('confidence')} confidence"
        app.lexecute("UPDATE pages SET px_per_m=?,scale_text=? WHERE id=?", (_num(detected["px_per_m"]), label, int(page["id"])))
        return {"page_id": int(page["id"]), "method": "Dimension line", "px_per_m": _num(detected["px_per_m"]), "confidence": detected.get("confidence", "Medium")}
    try:
        scale = app.auto_detect_scale(page)
    except Exception:
        scale = None
    if scale and _num(scale.get("px_per_m")) > 0:
        pxpm = _num(scale.get("px_per_m"))
        label = f"Auto provisional printed scale {scale.get('source') or ''}".strip()
        app.lexecute("UPDATE pages SET px_per_m=?,scale_text=? WHERE id=?", (pxpm, label, int(page["id"])))
        return {"page_id": int(page["id"]), "method": "Printed scale", "px_per_m": pxpm, "confidence": "Provisional"}
    return None


def _detect_footprint(app: Any, pages: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    candidates = []
    for page in pages:
        if "floor" not in str(page.get("page_type") or "").lower() or _num(page.get("px_per_m")) <= 0:
            continue
        path = _regular_image(page.get("image_path"))
        if path is None:
            continue
        component = _drawing_component(path, elevation=False)
        if component is None:
            continue
        x, y, w, h = component["bbox"]
        pxpm = _num(page.get("px_per_m"))
        width_m, depth_m = w / pxpm, h / pxpm
        if not (1.0 <= width_m <= 500.0 and 1.0 <= depth_m <= 500.0):
            continue
        candidates.append({
            "page_id": int(page["id"]), "page_label": str(page.get("page_label") or ""),
            "bbox": component["bbox"], "polygon": component["polygon"],
            "width_m": round(width_m, 3), "depth_m": round(depth_m, 3),
            "px_per_m": pxpm, "density": component["density"],
        })
    if not candidates:
        return None
    # Largest floor-plan drawing cluster is the safest automatic building envelope candidate.
    return max(candidates, key=lambda item: item["width_m"] * item["depth_m"])


def _cross_calibrate_elevations(app: Any, pages: Sequence[Dict[str, Any]], footprint: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not footprint:
        return []
    results = []
    for page in pages:
        if "elevation" not in str(page.get("page_type") or "").lower():
            continue
        if _num(page.get("px_per_m")) > 0 and not _is_auto_scale(page):
            continue
        face = _page_face(page.get("extracted_text"), page.get("page_label"))
        if not face:
            continue
        path = _regular_image(page.get("image_path"))
        if path is None:
            continue
        component = _drawing_component(path, elevation=True)
        if component is None:
            continue
        real_width = footprint["width_m"] if face in {"front", "rear"} else footprint["depth_m"]
        image_width_px = _num(component["bbox"][2])
        if real_width <= 0 or image_width_px <= 0:
            continue
        pxpm = image_width_px / real_width
        if not (5.0 <= pxpm <= 5000.0):
            continue
        app.lexecute(
            "UPDATE pages SET px_per_m=?,scale_text=? WHERE id=?",
            (pxpm, f"Auto cross-reference from floor perimeter · {face}", int(page["id"])),
        )
        results.append({"page_id": int(page["id"]), "method": "Floor/elevation cross-reference", "px_per_m": round(pxpm, 4), "confidence": "Derived", "face": face})
    return results


def _takeoff_row(*, workspace_id: int, section: str, element: str, location: str, substrate: str,
                 quantity: float, status: str, source_page: str, source_reference: str,
                 confidence: str, notes: str, row_role: str = "") -> Tuple[Any, ...]:
    stamp = ""  # replaced by caller
    return (
        workspace_id, section, element, location, substrate, "To be confirmed", round(max(0.0, quantity), 2), "m²",
        status, source_page, source_reference, "INCLUSION" if row_role == "floor_area" else "PROVISIONAL",
        0, 0, 0, 0, confidence, notes, row_role, stamp, stamp,
    )


_TAKEOFF_INSERT = """INSERT INTO takeoff_rows(
    workspace_id,section,element,location,substrate,finish_system,quantity,unit,
    quantity_status,source_page,source_reference,inclusion_status,coats,
    coverage_m2_per_litre,productivity_m2_per_hour,rate_per_unit,confidence,notes,
    row_role,created_at,updated_at
) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


def _replace_auto_rows(app: Any, workspace_id: int, rows: Sequence[Tuple[Any, ...]]) -> None:
    conn = app.local_connect()
    try:
        conn.execute("DELETE FROM takeoff_rows WHERE workspace_id=? AND source_reference LIKE ?", (workspace_id, SOURCE_PREFIX + "%"))
        stamp = app.now_stamp()
        values = [tuple(list(row[:-2]) + [stamp, stamp]) for row in rows]
        conn.executemany(_TAKEOFF_INSERT, values)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _setting_get(app: Any, workspace_id: int) -> Dict[str, Any]:
    rows = app.lquery("SELECT value FROM workspace_settings WHERE workspace_id=? AND key=?", (workspace_id, SETTING_KEY))
    return _safe_json(rows[0].get("value") if rows else "{}", {})


def _setting_set(app: Any, workspace_id: int, data: Dict[str, Any]) -> None:
    value = json.dumps(data, separators=(",", ":"))
    app.lexecute(
        """INSERT INTO workspace_settings(workspace_id,key,value,updated_at) VALUES(?,?,?,?)
           ON CONFLICT(workspace_id,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
        (workspace_id, SETTING_KEY, value, app.now_stamp()),
    )


def _build_unit_rows(app: Any, workspace_id: int, pages: Sequence[Dict[str, Any]]) -> Tuple[List[Tuple[Any, ...]], List[Dict[str, Any]]]:
    rows: List[Tuple[Any, ...]] = []
    summary: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for page in pages:
        if "floor" not in str(page.get("page_type") or "").lower():
            continue
        explicit = extract_unit_area_candidates(page.get("extracted_text"))
        provisional = _unit_boundary_candidates(app, page)
        for candidate in explicit + provisional:
            key = candidate["label"].lower()
            if key in seen:
                continue
            seen.add(key)
            documented = candidate.get("confidence") == "Documented"
            status = "Measured" if documented else "Provisional measured"
            confidence = "Documented" if documented else "Derived"
            source_ref = f"{SOURCE_PREFIX} · unit:{candidate['label']} · page:{int(page['id'])}"
            notes = (
                "Floor area read directly from the drawing/schedule." if documented
                else "Floor area derived from a unique closed boundary around the unit label. Review the highlighted unit boundary before pricing."
            )
            rows.append(_takeoff_row(
                workspace_id=workspace_id, section="Internal", element="Floor area", location=candidate["label"],
                substrate="Other", quantity=_num(candidate["area_m2"]), status=status,
                source_page=str(page.get("page_label") or ""), source_reference=source_ref,
                confidence=confidence, notes=notes, row_role="floor_area",
            ))
            item = dict(candidate)
            item.update({"page_id": int(page["id"]), "page_label": str(page.get("page_label") or ""), "quantity_status": status})
            summary.append(item)
    return rows, summary


def _build_facade_rows(app: Any, workspace_id: int, pages: Sequence[Dict[str, Any]]) -> Tuple[List[Tuple[Any, ...]], List[Dict[str, Any]]]:
    rows: List[Tuple[Any, ...]] = []
    facades: List[Dict[str, Any]] = []
    for page in pages:
        if "elevation" not in str(page.get("page_type") or "").lower():
            continue
        path = _regular_image(page.get("image_path"))
        pxpm = _num(page.get("px_per_m"))
        component = _drawing_component(path, elevation=True) if path else None
        face = _page_face(page.get("extracted_text"), page.get("page_label"))
        explicit = extract_substrate_area_candidates(page.get("extracted_text"))
        substrates = _substrates_from_text(page.get("extracted_text"))
        facade: Dict[str, Any] = {
            "page_id": int(page["id"]), "page_label": str(page.get("page_label") or ""), "face": face,
            "substrates": substrates, "explicit_areas": explicit, "gross_m2": 0.0, "height_m": 0.0,
        }
        if component and pxpm > 0:
            _x, _y, w, h = component["bbox"]
            facade["width_m"] = round(w / pxpm, 3)
            facade["height_m"] = round(h / pxpm, 3)
            facade["gross_m2"] = round((w * h) / (pxpm * pxpm), 2)
            facade["bbox"] = component["bbox"]
            facade["polygon"] = component["polygon"]

        if explicit:
            for item in explicit:
                sub = item["substrate"]
                rows.append(_takeoff_row(
                    workspace_id=workspace_id, section="External", element="External walls / cladding",
                    location=f"{face.title() if face else 'Elevation'} · {sub['name']}", substrate=sub["name"],
                    quantity=_num(item["area_m2"]), status="Measured", source_page=facade["page_label"],
                    source_reference=f"{SOURCE_PREFIX} · facade:{int(page['id'])} · {sub['code']}",
                    confidence="Documented", notes=f"Substrate area read directly from drawing text: {item['source']}",
                ))
        elif facade["gross_m2"] > 0:
            if len(substrates) == 1:
                sub = substrates[0]
                location = f"{face.title() if face else 'Elevation'} · {sub['name']}"
                substrate = sub["name"]
                note = "Gross facade area derived from calibrated elevation drawing cluster; opening deductions and edge conditions require review."
            else:
                location = f"{face.title() if face else 'Elevation'} · Mixed external substrate"
                substrate = "Other"
                names = ", ".join(item["name"] for item in substrates) or "No reliable material label found"
                note = f"Gross calibrated facade area. Mixed substrate split requires review ({names}). No automatic split is invented."
            rows.append(_takeoff_row(
                workspace_id=workspace_id, section="External", element="External walls / cladding", location=location,
                substrate=substrate, quantity=_num(facade["gross_m2"]), status="Provisional measured",
                source_page=facade["page_label"], source_reference=f"{SOURCE_PREFIX} · facade:{int(page['id'])} · gross",
                confidence="Derived", notes=note,
            ))
        facades.append(facade)
    return rows, facades


def _surface_code_for(substrates: Sequence[Dict[str, str]]) -> str:
    if len(substrates) != 1:
        return "OTHER"
    code = str(substrates[0].get("code") or "OTHER")
    if surface_v1212 is not None:
        valid = {str(item.get("code")) for item in surface_v1212.substrate_presets()}
        if code in valid:
            return code
        inferred = surface_v1212.infer_substrate(substrates[0].get("name"))
        return inferred or "OTHER"
    return code


def _refresh_auto_model(app: Any, workspace_id: int, footprint: Optional[Dict[str, Any]], facades: Sequence[Dict[str, Any]]) -> Optional[int]:
    existing = app.lquery(
        "SELECT * FROM model_masses WHERE workspace_id=? AND source_reference LIKE ? ORDER BY id",
        (workspace_id, MODEL_SOURCE_PREFIX + "%"),
    )
    if not footprint:
        return int(existing[0]["id"]) if existing else None
    heights = [_num(item.get("height_m")) for item in facades if 1.5 <= _num(item.get("height_m")) <= 100.0]
    height = float(np.median(heights)) if heights else 2.7
    width = max(0.1, _num(footprint.get("width_m")))
    depth = max(0.1, _num(footprint.get("depth_m")))
    source = f"{MODEL_SOURCE_PREFIX} · floor:{footprint.get('page_id')}"
    if existing:
        mass_id = int(existing[0]["id"])
        app.lexecute(
            "UPDATE model_masses SET label=?,x=0,y=0,z=0,width=?,depth=?,height=?,finish=?,source_reference=?,confidence=?,notes=? WHERE id=?",
            ("Automatic building envelope", width, depth, height, "External envelope", source, "Derived",
             "Bounding envelope cross-referenced from calibrated floor plan and selected elevations. Review before final quantity issue.", mass_id),
        )
        for duplicate in existing[1:]:
            app.lexecute("DELETE FROM model_openings WHERE mass_id=?", (int(duplicate["id"]),))
            app.lexecute("DELETE FROM model_masses WHERE id=?", (int(duplicate["id"]),))
    else:
        mass_id = int(app.lexecute(
            """INSERT INTO model_masses(workspace_id,label,level_name,x,y,z,width,depth,height,finish,source_reference,confidence,notes,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (workspace_id, "Automatic building envelope", "Ground", 0, 0, 0, width, depth, height, "External envelope",
             source, "Derived", "Bounding envelope cross-referenced from calibrated floor plan and selected elevations. Review before final quantity issue.", app.now_stamp()),
        ))

    # Populate face metadata without overwriting an estimator's later manual edits.
    raw = app.lquery("SELECT value FROM workspace_settings WHERE workspace_id=? AND key=?", (workspace_id, "3d_surface_editor_v1212"))
    state = _safe_json(raw[0].get("value") if raw else "{}", {})
    overrides = dict(state.get("surfaces") or {}) if isinstance(state, dict) else {}
    by_face = {str(item.get("face")): item for item in facades if item.get("face")}
    for face in ("front", "rear", "left", "right"):
        item = by_face.get(face)
        if not item:
            continue
        surface_id = f"mass:{mass_id}:{face}"
        existing_override = dict(overrides.get(surface_id) or {})
        existing_notes = str(existing_override.get("notes") or "")
        if existing_override and not existing_notes.startswith(f"[AUTO v{VERSION}]"):
            continue
        names = ", ".join(sub.get("name", "") for sub in item.get("substrates") or []) or "substrate to confirm"
        overrides[surface_id] = {
            "substrate": _surface_code_for(item.get("substrates") or []),
            "status": "Provisional",
            "progress_pct": _num(existing_override.get("progress_pct")),
            "notes": f"[AUTO v{VERSION}] {item.get('page_label') or face}: {names}; gross elevation {_num(item.get('gross_m2')):.2f} m². Review mixed-substrate splits/openings.",
        }
    app.lexecute(
        """INSERT INTO workspace_settings(workspace_id,key,value,updated_at) VALUES(?,?,?,?)
           ON CONFLICT(workspace_id,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
        (workspace_id, "3d_surface_editor_v1212", json.dumps({"surfaces": overrides, "saved_at": app.now_stamp()}, separators=(",", ":")), app.now_stamp()),
    )
    return mass_id


def analyse_workspace(app: Any, workspace_id: int) -> Dict[str, Any]:
    """Run the automatic non-AI geometry pipeline on selected, rendered sheets."""
    pages = app.lquery(
        """SELECT p.*,d.file_name FROM pages p JOIN documents d ON d.id=p.document_id
           WHERE p.workspace_id=? AND COALESCE(p.selected,0)=1 ORDER BY p.id""",
        (int(workspace_id),),
    )
    calibrations = []
    for page in pages:
        result = _auto_calibrate_page(app, dict(page))
        if result:
            calibrations.append(result)
    # Refresh page rows after calibration updates.
    pages = app.lquery(
        """SELECT p.*,d.file_name FROM pages p JOIN documents d ON d.id=p.document_id
           WHERE p.workspace_id=? AND COALESCE(p.selected,0)=1 ORDER BY p.id""",
        (int(workspace_id),),
    )
    footprint = _detect_footprint(app, [dict(p) for p in pages])
    cross = _cross_calibrate_elevations(app, [dict(p) for p in pages], footprint)
    if cross:
        calibrations.extend(cross)
        pages = app.lquery(
            """SELECT p.*,d.file_name FROM pages p JOIN documents d ON d.id=p.document_id
               WHERE p.workspace_id=? AND COALESCE(p.selected,0)=1 ORDER BY p.id""",
            (int(workspace_id),),
        )
    unit_rows, units = _build_unit_rows(app, int(workspace_id), [dict(p) for p in pages])
    facade_rows, facades = _build_facade_rows(app, int(workspace_id), [dict(p) for p in pages])
    _replace_auto_rows(app, int(workspace_id), unit_rows + facade_rows)
    mass_id = _refresh_auto_model(app, int(workspace_id), footprint, facades)
    report = {
        "version": VERSION, "analysed_at": app.now_stamp(), "selected_pages": len(pages),
        "calibrations": calibrations, "footprint": footprint, "units": units, "facades": facades,
        "auto_takeoff_rows": len(unit_rows) + len(facade_rows), "model_mass_id": mass_id,
    }
    _setting_set(app, int(workspace_id), report)
    return report


def auto_geometry_panel(app: Any, workspace: Dict[str, Any]) -> None:
    workspace_id = int(workspace["id"])
    report = _setting_get(app, workspace_id)
    with app.st.expander("⚙️ Automatic plan geometry", expanded=not bool(report)):
        app.st.caption(
            "No AI required. PlanReader triages sheets, self-calibrates from drawing dimensions where possible, "
            "reads documented unit floor areas, derives reviewable unit boundaries, cross-references floor/elevation geometry, "
            "and prepares the external envelope/3D face metadata. Manual measurements always take priority."
        )
        pages = app.lquery("SELECT selected,page_type FROM pages WHERE workspace_id=?", (workspace_id,))
        kept = sum(1 for row in pages if int(row.get("selected") or 0) == 1)
        discarded = len(pages) - kept
        c1, c2, c3, c4 = app.st.columns(4)
        c1.metric("Take-off sheets", kept)
        c2.metric("Auto-discarded", discarded)
        c3.metric("Unit areas found", len(report.get("units") or []))
        c4.metric("External rows", len([f for f in report.get("facades") or [] if _num(f.get("gross_m2")) > 0 or f.get("explicit_areas")]))
        if app.st.button("Re-run automatic geometry", type="secondary", use_container_width=True, key=f"auto_geometry_refresh_{workspace_id}"):
            with app.st.spinner("Cross-referencing selected plans and elevations…"):
                result = analyse_workspace(app, workspace_id)
            app.st.success(
                f"Automatic geometry refreshed: {len(result.get('units') or [])} unit area(s), "
                f"{len(result.get('facades') or [])} elevation(s), {result.get('auto_takeoff_rows', 0)} take-off row(s)."
            )
            app.st.rerun()
        if report:
            methods: Dict[str, int] = {}
            for item in report.get("calibrations") or []:
                method = str(item.get("method") or "Other")
                methods[method] = methods.get(method, 0) + 1
            if methods:
                app.st.caption("Calibration: " + " · ".join(f"{name} {count}" for name, count in methods.items()))
            unresolved = [f for f in report.get("facades") or [] if len(f.get("substrates") or []) != 1 and not f.get("explicit_areas")]
            if unresolved:
                app.st.info(
                    f"{len(unresolved)} elevation(s) contain mixed/unclear substrate information. PlanReader keeps those gross areas provisional instead of inventing a material split."
                )


def apply(app: Any) -> None:
    if getattr(app, "_pb_auto_geometry_v1219_applied", False):
        return
    app._pb_auto_geometry_v1219_applied = True

    base_index = app.index_document_pages
    base_process = app.process_document

    def _auto_index_document_pages(document_id: int, *args, **kwargs):
        result = base_index(document_id, *args, **kwargs)
        try:
            auto_select_document_pages(app, int(document_id))
        except Exception:
            # Page indexing must remain usable even if an optional heuristic fails.
            pass
        return result

    def _auto_process_document(document_id: int, force: bool = False, page_ids=None, progress_cb=None):
        requested = page_ids
        if requested is None:
            indexed = app.lquery(
                "SELECT page_no FROM pages WHERE document_id=? AND COALESCE(selected,0)=1 ORDER BY page_no",
                (int(document_id),),
            )
            if indexed:
                requested = [int(row["page_no"]) for row in indexed]
        result = base_process(document_id, force=force, page_ids=requested, progress_cb=progress_cb)
        try:
            docs = app.lquery("SELECT workspace_id FROM documents WHERE id=?", (int(document_id),))
            if docs:
                analyse_workspace(app, int(docs[0]["workspace_id"]))
        except Exception:
            # Rendering a drawing must not fail because an optional automatic
            # measurement heuristic could not interpret one unusual sheet.
            pass
        return result

    app.index_document_pages = _auto_index_document_pages
    app.process_document = _auto_process_document
    app.auto_select_document_pages = lambda document_id: auto_select_document_pages(app, int(document_id))
    app.run_auto_geometry = lambda workspace_id: analyse_workspace(app, int(workspace_id))
    app.page_takeoff_relevance = page_relevance

    # The selected-page wrapper resolves this module global at runtime, so an
    # additive panel here appears in the default no-AI take-off without changing
    # its proven save/build logic.
    try:
        import pb_no_ai_takeoff_v1216 as noai
        if not getattr(noai, "_pb_auto_geometry_panel_v1219", False):
            base_panel = noai.no_ai_takeoff_panel

            def _panel_with_auto_geometry(app_obj, workspace):
                auto_geometry_panel(app_obj, workspace)
                return base_panel(app_obj, workspace)

            noai.no_ai_takeoff_panel = _panel_with_auto_geometry
            noai._pb_auto_geometry_panel_v1219 = True
    except Exception:
        pass
