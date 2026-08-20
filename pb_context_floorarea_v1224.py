"""PlanReader v1.2.24 context-aware finish-code and internal floor-area guards.

This patch fixes two automation failure modes:

1. Architectural/key-plan tags such as ``PT01 KEY PLAN WALL PIECES`` must not be
   promoted to paint/finish codes merely because they start with PT/PF/WF.
2. Internal floor m² must still be produced when a useful floor/unit-plan sheet
   has no clean UNIT/APARTMENT label or written per-unit area.

The evidence hierarchy remains conservative: documented internal area text wins,
then calibrated closed-plan geometry is allowed as a provisional fallback. Manual
floor-area rows and reviewed geometry retain precedence.
"""
from __future__ import annotations

import gc
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pb_auto_geometry_guard_v1219 as auto_guard
import pb_auto_geometry_v1219 as auto
import pb_material_schedule_v1222 as material
import pb_memory_stability_v1220 as memory
import pb_unit_floor_area_v1221 as unit

VERSION = "1.2.24"
SOURCE_SUFFIX = f"PB Internal Floor Fallback v{VERSION}"

_FINISH_PREFIX_RE = re.compile(r"^(?:PT|PF|WF)\d+$", re.IGNORECASE)

# Strong evidence that a PT/PF/WF-looking token is actually a drawing/key tag.
_NON_FINISH_CONTEXT = (
    "key plan", "wall piece", "wall pieces", "wall type", "wall types",
    "wall tag", "wall tags", "partition type", "partition types", "partition tag",
    "key note", "keynote", "detail tag", "detail reference", "drawing key",
    "plan key", "legend symbol", "panel type", "panel types", "wall panel",
    "wall panels", "piece mark", "piece marks", "assembly type", "assembly types",
)

# Positive evidence intentionally includes brands, coating terminology and common
# finish descriptors. A generic PT token with none of this evidence is not enough
# to invent a paint code unless it is located in a clearly named paint/finish schedule.
_FINISH_CONTEXT = (
    "paint", "painting", "paint finish", "finish schedule", "finishes schedule",
    "finishing schedule", "paint schedule", "colour schedule", "color schedule",
    "dulux", "haymes", "taubmans", "resene", "wattyl", "intergrain", "sikkens",
    "low sheen", "semi gloss", "semi-gloss", "semigloss", "matt", "matte",
    "gloss", "satin", "acrylic", "enamel", "epoxy", "two pack", "2 pack",
    "2-pack", "primer", "undercoat", "topcoat", "sealer", "coating", "stain",
    "clear finish", "texture coat", "membrane", "elastomeric",
)

_INTERNAL_AREA_TERMS: Sequence[Tuple[str, int]] = (
    ("internal floor area", 120),
    ("internal area", 115),
    ("unit floor area", 110),
    ("apartment floor area", 110),
    ("unit area", 105),
    ("apartment area", 105),
    ("enclosed floor area", 100),
    ("enclosed area", 95),
    ("living area", 90),
    ("floor area", 80),
    ("gross floor area", 60),
    ("gfa", 55),
)
_INTERNAL_AREA_EXCLUDES = (
    "external area", "external floor area", "balcony", "terrace", "deck",
    "verandah", "veranda", "courtyard", "garage", "carpark", "car park",
    "roof area", "site area", "landscape", "landscaping", "paving", "driveway",
    "awning", "canopy", "patio", "pool area",
)
_LEVEL_RE = re.compile(r"\b(?:LEVEL|LVL)\s*0*([0-9]{1,2})\b", re.IGNORECASE)


def _normalise(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _low(value: Any) -> str:
    return _normalise(value).lower()


def is_finish_style_code(code: Any) -> bool:
    return bool(_FINISH_PREFIX_RE.fullmatch(str(code or "").strip()))


def _has_any(text: Any, terms: Sequence[str]) -> bool:
    low = _low(text)
    return any(term in low for term in terms)


def has_non_finish_context(text: Any) -> bool:
    return _has_any(text, _NON_FINISH_CONTEXT)


def has_finish_context(text: Any) -> bool:
    return _has_any(text, _FINISH_CONTEXT)


def accept_finish_schedule_item(item: Dict[str, Any], full_text: Any = "", page_label: Any = "") -> bool:
    """Return whether a parsed PT/PF/WF item is genuinely supported as a finish.

    Non-PT/PF/WF codes are unaffected. For finish-style codes, negative key/tag
    wording on the source line wins over generic schedule words elsewhere on a page.
    This specifically prevents ``PT01 KEY PLAN WALL PIECES`` from becoming paint.
    """
    code = str(item.get("code") or "").upper()
    if not is_finish_style_code(code):
        return True

    source = _normalise(item.get("source_line"))
    description = _normalise(item.get("description"))
    local = f"{source} {description}".strip()

    if has_non_finish_context(source) and not has_finish_context(source):
        return False
    if has_non_finish_context(local) and not has_finish_context(local):
        return False
    if has_finish_context(local):
        return True

    # Some schedules contain compact rows such as "PT01 White". Accept these only
    # when the page itself is explicitly a paint/finish/colour schedule.
    page_context = f"{page_label or ''} {full_text or ''}"
    return has_finish_context(page_context) and not has_non_finish_context(local)


def parse_schedule_text(text: Any, page_id: int = 0, page_label: str = "", *, base_parser=None) -> List[Dict[str, Any]]:
    parser = base_parser or material.parse_schedule_text
    items = list(parser(text, page_id, page_label) or [])
    return [item for item in items if accept_finish_schedule_item(dict(item), text, page_label)]


def filter_page_occurrences(items: Sequence[Dict[str, Any]], dictionary: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop unproven PT/PF/WF drawing tags from page occurrences.

    If the project schedule already confirmed PT01 as paint, a plain PT01 callout
    on a plan remains valid. If no confirmed schedule definition exists, PT/PF/WF
    tokens require local paint/finish evidence rather than creating an Unknown code.
    """
    out: List[Dict[str, Any]] = []
    for raw in items or []:
        item = dict(raw)
        code = str(item.get("code") or "").upper()
        if not is_finish_style_code(code):
            out.append(item)
            continue
        entry = dictionary.get(code) or {}
        if str(entry.get("status") or "") in {"Confirmed", "Conflict"}:
            out.append(item)
            continue
        local = str(item.get("text") or "")
        if has_non_finish_context(local) and not has_finish_context(local):
            continue
        if has_finish_context(local):
            out.append(item)
    return out


def _area_value(line: str) -> float:
    match = auto._AREA_RE.search(str(line or ""))
    if not match:
        return 0.0
    value = auto._num(match.group(1))
    return value if 8.0 <= value <= 5000.0 else 0.0


def _best_unit_label_near(lines: Sequence[str], idx: int) -> str:
    for distance in (0, 1, 2):
        positions = [idx] if distance == 0 else [idx - distance, idx + distance]
        for pos in positions:
            if 0 <= pos < len(lines):
                label = unit._match_unit_label(lines[pos], allow_short=True)
                if label:
                    return label
    return ""


def floor_location(page: Dict[str, Any]) -> str:
    label_text = _normalise(page.get("page_label"))
    full = f"{label_text} {_normalise(page.get('extracted_text'))}"
    unit_label = unit._match_unit_label(label_text, allow_short=True)
    if unit_label:
        return unit_label
    low = full.lower()
    if "ground floor" in low or "ground level" in low:
        return "Ground Floor"
    match = _LEVEL_RE.search(full)
    if match:
        return f"Level {int(match.group(1))}"
    if label_text:
        return f"Internal Floor · {label_text}"
    return f"Internal Floor · Page {int(auto._num(page.get('page_no'), auto._num(page.get('id'), 0)))}"


def documented_internal_area_candidates(text: Any, page: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Extract explicit internal floor m² even when no UNIT label is present."""
    page = dict(page or {})
    lines = [_normalise(line) for line in str(text or "").splitlines() if _normalise(line)]
    candidates: List[Dict[str, Any]] = []
    for idx, line in enumerate(lines):
        low = line.lower()
        if any(term in low for term in _INTERNAL_AREA_EXCLUDES):
            continue
        area = _area_value(line)
        if area <= 0:
            continue
        matched = [(term, score) for term, score in _INTERNAL_AREA_TERMS if term in low]
        if not matched:
            continue
        term, score = max(matched, key=lambda item: item[1])
        near_label = _best_unit_label_near(lines, idx)
        candidates.append({
            "label": near_label or floor_location(page),
            "area_m2": round(area, 2),
            "confidence": "Documented",
            "source": line,
            "source_term": term,
            "score": score + (15 if near_label else 0),
            "bbox": None,
            "polygon": None,
        })

    # Keep one strongest documented area for each logical location. This avoids
    # double counting when a title block repeats INTERNAL AREA and FLOOR AREA.
    best: Dict[str, Dict[str, Any]] = {}
    for item in candidates:
        key = auto_guard._normalise(item.get("label")) or str(item.get("label") or "").lower()
        current = best.get(key)
        if current is None or int(item.get("score") or 0) > int(current.get("score") or 0):
            best[key] = item
    return sorted(best.values(), key=lambda item: (-int(item.get("score") or 0), str(item.get("label") or "")))


def _regular_file(value: Any) -> Optional[Path]:
    raw = str(value or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_file() else None


def _page_is_internal_plan(page: Dict[str, Any]) -> bool:
    page_type = str(page.get("page_type") or "")
    low = page_type.lower()
    if "floor" in low or "partition" in low or "unit plan" in low:
        return True
    return unit.page_has_unit_plan_evidence(page_type, page.get("extracted_text"), page.get("page_label"))


def geometry_internal_area_candidate(app: Any, page: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Measure a dominant closed internal-plan footprint with bounded memory.

    This is a fallback only. It never supersedes documented unit/internal areas.
    The result remains Derived / Provisional measured until an estimator reviews it.
    """
    if not _page_is_internal_plan(page):
        return None
    pxpm = auto._num(page.get("px_per_m"))
    if pxpm <= 0:
        return None
    image_path = _regular_file(page.get("image_path"))
    if image_path is None:
        return None
    cv2 = getattr(auto, "cv2", None)
    np = getattr(auto, "np", None)
    if cv2 is None or np is None:
        return None
    loaded = memory._bounded_gray(image_path)
    if loaded is None:
        return None
    image, sx, sy, original_w, original_h = loaded
    try:
        height, width = image.shape[:2]
        if height <= 0 or width <= 0:
            return None
        _, ink = cv2.threshold(image, 205, 255, cv2.THRESH_BINARY_INV)
        # Remove the common title-block band and very outer page frame.
        ink[int(height * 0.88):, :] = 0
        margin_x = max(1, int(width * 0.008))
        margin_y = max(1, int(height * 0.008))
        ink[:margin_y, :] = 0
        ink[:, :margin_x] = 0
        ink[:, max(0, width - margin_x):] = 0
        kernel = np.ones((3, 3), np.uint8)
        ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, kernel, iterations=1)
        contours, _ = cv2.findContours(ink, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        page_area = float(max(1, width * height))
        candidates: List[Dict[str, Any]] = []
        for contour in contours:
            area_work = abs(float(cv2.contourArea(contour)))
            frac = area_work / page_area
            if not (0.008 <= frac <= 0.68):
                continue
            x, y, w, h = cv2.boundingRect(contour)
            if w < width * 0.06 or h < height * 0.06 or y > height * 0.86:
                continue
            # Reject page/title frames that hug nearly every sheet edge.
            hugs_left = x <= width * 0.018
            hugs_top = y <= height * 0.018
            hugs_right = (x + w) >= width * 0.982
            hugs_bottom = (y + h) >= height * 0.86
            if sum((hugs_left, hugs_top, hugs_right, hugs_bottom)) >= 3:
                continue
            area_m2 = area_work * sx * sy / (pxpm * pxpm)
            if not (8.0 <= area_m2 <= 5000.0):
                continue
            bbox_area = float(max(1, w * h))
            extent = area_work / bbox_area
            if extent < 0.04:
                continue
            # Large, substantial closed contours are more likely to be the floor
            # perimeter than room labels or small internal rooms.
            score = area_work * (0.55 + min(extent, 0.85))
            candidates.append({"contour": contour, "area_work": area_work, "area_m2": area_m2, "bbox": (x, y, w, h), "score": score})
        if not candidates:
            return None
        chosen = max(candidates, key=lambda item: item["score"])
        contour = chosen["contour"]
        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, max(2.0, perimeter * 0.0045), True)
        x, y, w, h = chosen["bbox"]
        return {
            "label": floor_location(page),
            "area_m2": round(float(chosen["area_m2"]), 2),
            "confidence": "Derived",
            "source": "Calibrated dominant closed floor-plan boundary",
            "bbox": [float(x) * sx, float(y) * sy, float(w) * sx, float(h) * sy],
            "polygon": [[float(p[0][0]) * sx, float(p[0][1]) * sy] for p in approx],
            "image_width": original_w,
            "image_height": original_h,
        }
    finally:
        del image
        gc.collect()


def fallback_internal_candidates(app: Any, page: Dict[str, Any]) -> List[Dict[str, Any]]:
    documented = documented_internal_area_candidates(page.get("extracted_text"), page)
    if documented:
        return documented[:1]
    geometry = geometry_internal_area_candidate(app, page)
    return [geometry] if geometry else []


def _manual_location_blocked(app: Any, workspace_id: int, location: str) -> bool:
    manual = auto_guard._manual_floor_keys(app, int(workspace_id))
    key = auto_guard._normalise(location)
    if not key or not manual:
        return False
    return any(key == item or key in item or item in key for item in manual)


def extend_unit_rows(base_units, app_obj: Any, workspace_id: int, pages: Sequence[Dict[str, Any]]):
    """Add page-level internal m² only where the proven unit builder found none."""
    rows, summary = base_units(app_obj, int(workspace_id), pages)
    rows = list(rows or [])
    summary = list(summary or [])
    covered_pages = {int(item.get("page_id") or 0) for item in summary if int(item.get("page_id") or 0) > 0}

    for raw in pages or []:
        page = dict(raw)
        page_id = int(page.get("id") or 0)
        if page_id <= 0 or page_id in covered_pages or not _page_is_internal_plan(page):
            continue
        candidates = fallback_internal_candidates(app_obj, page)
        if not candidates:
            continue
        candidate = candidates[0]
        location = str(candidate.get("label") or floor_location(page))
        if _manual_location_blocked(app_obj, int(workspace_id), location):
            continue
        documented = str(candidate.get("confidence") or "") == "Documented"
        status = "Measured" if documented else "Provisional measured"
        confidence = "Documented" if documented else "Derived"
        source_ref = f"{auto.SOURCE_PREFIX} · {SOURCE_SUFFIX} · page:{page_id}"
        notes = (
            "Internal floor area read directly from documented drawing text."
            if documented
            else "Internal floor area derived from the largest plausible calibrated closed floor-plan boundary because no reliable per-unit/documented internal area was found. Review boundary before pricing."
        )
        rows.append(auto._takeoff_row(
            workspace_id=int(workspace_id), section="Internal", element="Floor area", location=location,
            substrate="Other", quantity=auto._num(candidate.get("area_m2")), status=status,
            source_page=str(page.get("page_label") or ""), source_reference=source_ref,
            confidence=confidence, notes=notes, row_role="floor_area",
        ))
        item = dict(candidate)
        item.update({
            "page_id": page_id,
            "page_label": str(page.get("page_label") or ""),
            "quantity_status": status,
            "fallback": True,
        })
        summary.append(item)
        covered_pages.add(page_id)
    return rows, summary


def apply(app: Any) -> None:
    if getattr(app, "_pb_context_floorarea_v1224_applied", False):
        return
    app._pb_context_floorarea_v1224_applied = True

    base_parser = material.parse_schedule_text
    def _context_parser(text: Any, page_id: int = 0, page_label: str = ""):
        return parse_schedule_text(text, page_id, page_label, base_parser=base_parser)
    material.parse_schedule_text = _context_parser

    base_occurrences = material._page_occurrences
    def _context_occurrences(app_obj: Any, page: Dict[str, Any], dictionary: Dict[str, Dict[str, Any]]):
        return filter_page_occurrences(base_occurrences(app_obj, page, dictionary), dictionary)
    material._page_occurrences = _context_occurrences

    base_units = auto._build_unit_rows
    def _extended_units(app_obj: Any, workspace_id: int, pages: Sequence[Dict[str, Any]]):
        return extend_unit_rows(base_units, app_obj, int(workspace_id), pages)
    auto._build_unit_rows = _extended_units

    app.is_real_finish_code_context = accept_finish_schedule_item
    app.extract_documented_internal_floor_areas = lambda text, page=None: documented_internal_area_candidates(text, page)
    app.measure_internal_floor_area_fallback = lambda page: geometry_internal_area_candidate(app, dict(page))
