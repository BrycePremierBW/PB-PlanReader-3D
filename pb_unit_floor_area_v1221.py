"""PlanReader v1.2.21 reliable automatic unit floor-area detection.

Fixes a v1.2.19 logic error where every unit label could select the same whole-
building contour and then all units were rejected as ambiguous.  Unit boundaries
are now selected from contours that contain exactly one recognised unit label.
The patch also recognises common partition/unit-plan sheet names and searches a
wider text neighbourhood for documented unit areas.
"""
from __future__ import annotations

import gc
import re
from typing import Any, Dict, List, Optional, Tuple

import pb_auto_geometry_v1219 as auto
import pb_memory_stability_v1220 as memory

VERSION = "1.2.21"

# Keep the explicit prefixes conservative, but accept common Australian drawing
# labels and a shorthand U-101 / U101 style where the sheet is clearly a unit plan.
UNIT_LABEL_RE = re.compile(
    r"\b(?:UNIT|APT|APARTMENT|VILLA|TOWNHOUSE|TENANCY|RESIDENCE|LOT)\s*(?:NO\.?\s*)?[-#:]*\s*([A-Z0-9][A-Z0-9.-]*)\b",
    re.IGNORECASE,
)
SHORT_UNIT_RE = re.compile(r"\bU[-\s]*([0-9]{2,4}(?:\.[0-9]{1,2})?)\b", re.IGNORECASE)
UNIT_PLAN_WORDS = (
    "partition plan",
    "unit plan",
    "unit layout",
    "apartment plan",
    "apartment layout",
    "residential plan",
    "general arrangement",
    "ga plan",
)


def _match_unit_label(text: Any, allow_short: bool = True) -> Optional[str]:
    raw = str(text or "")
    match = UNIT_LABEL_RE.search(raw)
    if match:
        return f"Unit {match.group(1)}"
    if allow_short:
        match = SHORT_UNIT_RE.search(raw)
        if match:
            return f"Unit {match.group(1)}"
    return None


def page_has_unit_plan_evidence(page_type: Any, text: Any = "", label: Any = "") -> bool:
    kind = str(page_type or "").lower()
    low = f"{label or ''} {text or ''}".lower()
    if any(token in kind for token in ("elevation", "section", "ceiling", "rcp", "schedule", "roof", "service", "structural")):
        return False
    if "floor" in kind or "partition" in kind or "unit plan" in kind:
        return True
    if any(token in low for token in UNIT_PLAN_WORDS):
        return True
    # Multiple explicit unit labels are strong floor/layout evidence even when a
    # generic classifier called the sheet "Other".
    labels = {m.group(1).upper() for m in UNIT_LABEL_RE.finditer(low)}
    return len(labels) >= 2


def enhanced_page_relevance(base_relevance):
    def _relevance(page_type: Any, text: Any = "", label: Any = ""):
        if page_has_unit_plan_evidence(page_type, text, label):
            return True, "Unit/partition layout is required for internal floor-area take-off", 100
        return base_relevance(page_type, text, label)
    return _relevance


def extract_unit_area_candidates(text: Any) -> List[Dict[str, Any]]:
    """Extract documented per-unit m² from a wider local text neighbourhood."""
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(text or "").splitlines() if line.strip()]
    out: List[Dict[str, Any]] = []
    used: set[str] = set()
    for idx, line in enumerate(lines):
        label = _match_unit_label(line, allow_short=True)
        if not label:
            continue
        # Schedules often put the area two or three rows away from the unit name.
        candidates: List[Tuple[int, str]] = []
        for delta in range(0, 4):
            for pos in ({idx + delta, idx - delta} if delta else {idx}):
                if 0 <= pos < len(lines):
                    candidates.append((abs(pos - idx), lines[pos]))
        area = 0.0
        source = line
        for _distance, candidate in sorted(candidates, key=lambda item: item[0]):
            match = auto._AREA_RE.search(candidate)
            if not match:
                continue
            value = auto._num(match.group(1))
            if 8.0 <= value <= 1000.0:
                area = value
                source = candidate
                break
        key = label.lower()
        if area > 0 and key not in used:
            out.append({"label": label, "area_m2": round(area, 2), "confidence": "Documented", "source": source})
            used.add(key)
    return out


def _positioned_unit_labels(app: Any, page: Dict[str, Any]) -> List[Dict[str, Any]]:
    labels: List[Dict[str, Any]] = []
    seen: set[str] = set()
    allow_short = page_has_unit_plan_evidence(page.get("page_type"), page.get("extracted_text"), page.get("page_label"))
    for line in auto._pdf_word_lines(app, page):
        label = _match_unit_label(line.get("text"), allow_short=allow_short)
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        labels.append({"label": label, "center": list(line["center"])})
    return labels


def bounded_unit_boundary_candidates(app: Any, page: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return one calibrated unit contour for each uniquely enclosed unit label.

    The key difference from v1.2.19 is that we count unit labels inside every
    contour *before* choosing a contour.  The whole building therefore cannot be
    selected on a multi-unit plan because it contains several unit labels.
    """
    cv2 = getattr(auto, "cv2", None)
    np = getattr(auto, "np", None)
    pxpm = auto._num(page.get("px_per_m"))
    if cv2 is None or np is None or pxpm <= 0:
        return []
    image_path = auto._regular_image(page.get("image_path"))
    if image_path is None:
        return []
    labels = _positioned_unit_labels(app, page)
    if not labels:
        return []
    loaded = memory._bounded_gray(image_path)
    if loaded is None:
        return []
    image, sx, sy, original_w, original_h = loaded
    try:
        height, width = image.shape[:2]
        _, ink = cv2.threshold(image, 205, 255, cv2.THRESH_BINARY_INV)
        ink[int(height * 0.88):, :] = 0
        kernel = np.ones((3, 3), np.uint8)
        ink = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, kernel, iterations=1)
        contours, _ = cv2.findContours(ink, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        page_area = float(max(1, width * height))
        work_labels = [
            {"label": item["label"], "point": (float(item["center"][0]) / sx, float(item["center"][1]) / sy)}
            for item in labels
        ]

        eligible: List[Dict[str, Any]] = []
        for contour in contours:
            area_work = abs(float(cv2.contourArea(contour)))
            if not (page_area * 0.004 <= area_work <= page_area * 0.60):
                continue
            x, y, w, h = cv2.boundingRect(contour)
            if w < width * 0.04 or h < height * 0.05 or y > height * 0.86:
                continue
            area_m2 = area_work * sx * sy / (pxpm * pxpm)
            if not (15.0 <= area_m2 <= 1000.0):
                continue
            inside = [item for item in work_labels if cv2.pointPolygonTest(contour, item["point"], False) >= 0]
            # Building outlines and shared corridors typically contain several
            # unit labels.  Never use those as a per-unit floor area.
            if len(inside) != 1:
                continue
            eligible.append({
                "contour": contour,
                "area_work": area_work,
                "area_m2": area_m2,
                "bbox": (x, y, w, h),
                "label": inside[0]["label"],
            })

        results: List[Dict[str, Any]] = []
        for label in labels:
            matches = [item for item in eligible if item["label"].lower() == label["label"].lower()]
            if not matches:
                continue
            # Small room loops can also contain a unit label.  Once multi-unit
            # outlines have been removed, the largest remaining exclusive loop is
            # normally the apartment/unit perimeter rather than a room/text box.
            chosen = max(matches, key=lambda item: item["area_work"])
            contour = chosen["contour"]
            perimeter = cv2.arcLength(contour, True)
            approx = cv2.approxPolyDP(contour, max(2.0, perimeter * 0.005), True)
            x, y, w, h = chosen["bbox"]
            results.append({
                "label": label["label"],
                "area_m2": round(chosen["area_m2"], 2),
                "confidence": "Derived",
                "source": "Unique closed unit boundary around unit label",
                "bbox": [float(x) * sx, float(y) * sy, float(w) * sx, float(h) * sy],
                "polygon": [[float(p[0][0]) * sx, float(p[0][1]) * sy] for p in approx],
                "image_width": original_w,
                "image_height": original_h,
            })
        # Stable ordering makes the take-off easier to read and refresh.
        results.sort(key=lambda item: item["label"].lower())
        return results
    finally:
        del image
        gc.collect()


def restore_obvious_unit_plan_pages(app: Any, workspace_id: int) -> int:
    """Undo an earlier auto-discard when the sheet clearly contains unit-layout evidence."""
    rows = app.lquery(
        "SELECT id,page_type,page_label,extracted_text,selected FROM pages WHERE workspace_id=? ORDER BY id",
        (int(workspace_id),),
    )
    restore = [
        int(row["id"]) for row in rows
        if not int(row.get("selected") or 0)
        and page_has_unit_plan_evidence(row.get("page_type"), row.get("extracted_text"), row.get("page_label"))
    ]
    if not restore:
        return 0
    conn = app.local_connect()
    try:
        conn.executemany("UPDATE pages SET selected=1 WHERE id=?", [(page_id,) for page_id in restore])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return len(restore)


def apply(app: Any) -> None:
    if getattr(app, "_pb_unit_floor_area_v1221_applied", False):
        return
    app._pb_unit_floor_area_v1221_applied = True

    # Existing auto-selection calls this global at runtime, so future uploads keep
    # partition/unit layouts instead of discarding them as generic sheets.
    base_relevance = auto.page_relevance
    relevance = enhanced_page_relevance(base_relevance)
    auto.page_relevance = relevance
    app.page_takeoff_relevance = relevance

    # The v1.2.19 unit-row builder and the manual-precedence guard resolve these
    # globals at runtime.  Replacing only these helpers preserves all existing
    # row precedence and refresh semantics.
    auto._UNIT_LABEL_RE = UNIT_LABEL_RE
    auto.extract_unit_area_candidates = extract_unit_area_candidates
    auto._unit_boundary_candidates = bounded_unit_boundary_candidates

    base_analyse = auto.analyse_workspace

    def _analyse_with_unit_page_recovery(app_obj: Any, workspace_id: int):
        restored = restore_obvious_unit_plan_pages(app_obj, int(workspace_id))
        report = base_analyse(app_obj, int(workspace_id))
        report["restored_unit_plan_pages"] = restored
        # If an old v1.2.19 run discarded these sheets before they were rendered,
        # flag that fact instead of pretending a contour could be measured.
        missing = app_obj.lquery(
            """SELECT id,page_label FROM pages WHERE workspace_id=? AND selected=1
               AND COALESCE(image_path,'')=''""",
            (int(workspace_id),),
        )
        report["selected_pages_needing_render"] = [dict(row) for row in missing]
        try:
            auto._setting_set(app_obj, int(workspace_id), report)
        except Exception:
            pass
        return report

    auto.analyse_workspace = _analyse_with_unit_page_recovery
    app.run_auto_geometry = lambda workspace_id: auto.analyse_workspace(app, int(workspace_id))
    app.restore_unit_plan_pages = lambda workspace_id: restore_obvious_unit_plan_pages(app, int(workspace_id))
