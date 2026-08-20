"""PlanReader v1.2.26 selected evidence and internal floor-area intelligence.

Estimator page selection is authoritative. Deselected sheets are removed from the
active material dictionary, material occurrences and Review Issues queue.

Internal floor-area hierarchy:
1. selected Partition Plans (primary),
2. selected Floor Finishes / Finishes Plans (cross-check),
3. selected Unit/Apartment/Floor Plans,
4. existing conservative geometry fallback.

Where native PDF positions are available, printed m² is paired to the nearest unit
label by drawing position rather than arbitrary extracted-text order.
"""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pb_auto_geometry_guard_v1219 as auto_guard
import pb_auto_geometry_v1219 as auto
import pb_code_register_v1225 as code_register
import pb_context_floorarea_v1224 as floor_context
import pb_material_schedule_v1222 as material
import pb_unit_floor_area_v1221 as unit

VERSION = "1.2.26"
PROVENANCE_SETTING_KEY = "takeoff_provenance_v1226"

_PARTITION_WORDS = ("partition plan", "partition layout")
_FLOOR_FINISH_WORDS = (
    "floor finishes plan", "floor finish plan", "floor finishes layout",
    "floor finish layout", "finishes plan", "finish plan",
)
_UNIT_PLAN_WORDS = ("unit plan", "unit layout", "apartment plan", "apartment layout")


def _num(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _low(value: Any) -> str:
    return _norm(value).lower()


def _json_load(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except Exception:
        return fallback


def selected_page_identity(app: Any, workspace_id: int) -> Tuple[set[int], set[str]]:
    rows = app.lquery(
        "SELECT id,page_label FROM pages WHERE workspace_id=? AND COALESCE(selected,0)=1 ORDER BY id",
        (int(workspace_id),),
    )
    return (
        {int(row["id"]) for row in rows},
        {str(row.get("page_label") or "").strip() for row in rows if str(row.get("page_label") or "").strip()},
    )


def selected_pages(app: Any, workspace_id: int) -> List[Dict[str, Any]]:
    return [dict(row) for row in app.lquery(
        """SELECT p.*,d.file_name FROM pages p JOIN documents d ON d.id=p.document_id
           WHERE p.workspace_id=? AND COALESCE(p.selected,0)=1 ORDER BY p.document_id,p.page_no,p.id""",
        (int(workspace_id),),
    )]


def _rebuild_dictionary_item(code: str, sources: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    sources = [dict(item) for item in sources or []]
    if not sources:
        return None
    representative = sources[0]
    conflicts = [item for item in sources[1:] if not material._compatible_descriptions(representative.get("description"), item.get("description"))]
    substrates = {str(item.get("substrate") or "") for item in sources if item.get("substrate")}
    finishes = {str(item.get("finish") or "") for item in sources if item.get("finish")}
    return {
        "code": code,
        "description": str(representative.get("description") or ""),
        "substrate": next(iter(substrates)) if len(substrates) == 1 else "",
        "finish": next(iter(finishes)) if len(finishes) == 1 else "",
        "status": "Conflict" if conflicts or len(substrates) > 1 or len(finishes) > 1 else "Confirmed",
        "sources": sources,
    }


def selected_material_base_builder(app: Any, workspace_id: int) -> Dict[str, Any]:
    pages = app.lquery(
        """SELECT id,page_label,page_type,extracted_text,image_path,document_id,page_no,render_zoom
           FROM pages WHERE workspace_id=? AND COALESCE(selected,0)=1 ORDER BY id""",
        (int(workspace_id),),
    )
    definitions: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    schedule_pages: List[int] = []
    for raw in pages:
        page = dict(raw)
        if not material._schedule_page(page):
            continue
        schedule_pages.append(int(page["id"]))
        for item in material.parse_schedule_text(page.get("extracted_text"), int(page["id"]), str(page.get("page_label") or "")):
            definitions[str(item["code"]).upper()].append(dict(item))

    dictionary: Dict[str, Dict[str, Any]] = {}
    issues: List[Dict[str, Any]] = []
    for code, sources in sorted(definitions.items()):
        entry = _rebuild_dictionary_item(code, sources)
        if entry is None:
            continue
        dictionary[code] = entry
        if entry["status"] == "Conflict":
            first = sources[0]
            issues.append({
                "category": "Schedule conflict", "severity": "High", "code": code,
                "page_id": int(first.get("page_id") or 0), "page_label": str(first.get("page_label") or ""),
                "message": f"{code} has conflicting definitions in the selected finishing/material sheets.",
                "bbox": None, "bbox_mode": "xyxy", "source": str(first.get("source_line") or ""),
            })
    return {"dictionary": dictionary, "schedule_pages": schedule_pages, "issues": issues}


def selected_material_dictionary(app: Any, workspace_id: int) -> Dict[str, Any]:
    # Manual project-code overrides introduced in v1.2.25 remain authoritative.
    return code_register.merged_dictionary(app, int(workspace_id), selected_material_base_builder)


def _issue_selected(issue: Dict[str, Any], selected_ids: set[int], selected_labels: set[str]) -> bool:
    page_id = int(issue.get("page_id") or 0)
    page_label = str(issue.get("page_label") or "").strip()
    if page_id:
        return page_id in selected_ids
    if page_label:
        return page_label in selected_labels
    return True


def filter_material_state(app: Any, workspace_id: int, raw_state: Dict[str, Any]) -> Dict[str, Any]:
    selected_ids, selected_labels = selected_page_identity(app, int(workspace_id))
    state = dict(raw_state or {})
    state["schedule_pages"] = [int(pid) for pid in state.get("schedule_pages") or [] if int(pid or 0) in selected_ids]
    state["occurrences"] = [dict(item) for item in state.get("occurrences") or [] if int(item.get("page_id") or 0) in selected_ids]

    dictionary: Dict[str, Dict[str, Any]] = {}
    for code, raw in (state.get("dictionary") or {}).items():
        item = dict(raw or {})
        sources = [dict(source) for source in item.get("sources") or []]
        kept = [source for source in sources if int(source.get("page_id") or 0) == 0 or int(source.get("page_id") or 0) in selected_ids]
        if item.get("manual"):
            item["sources"] = kept or sources
            dictionary[str(code)] = item
        else:
            rebuilt = _rebuild_dictionary_item(str(code), kept)
            if rebuilt is not None:
                dictionary[str(code)] = rebuilt
    state["dictionary"] = dictionary
    state["issues"] = [dict(i) for i in state.get("issues") or [] if _issue_selected(dict(i), selected_ids, selected_labels)]
    state["review_issues"] = [dict(i) for i in state.get("review_issues") or [] if _issue_selected(dict(i), selected_ids, selected_labels)]
    for index, issue in enumerate(state.get("review_issues") or [], 1):
        issue["id"] = index
    return state


def filter_review_issues(app: Any, workspace_id: int, issues: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    selected_ids, selected_labels = selected_page_identity(app, int(workspace_id))
    out = [dict(i) for i in issues or [] if _issue_selected(dict(i), selected_ids, selected_labels)]
    for index, issue in enumerate(out, 1):
        issue["id"] = index
    return out


def floor_source_priority(page: Dict[str, Any]) -> Tuple[int, str]:
    text = _low(f"{page.get('page_label')} {page.get('page_type')} {page.get('extracted_text')}")
    if any(token in text for token in _PARTITION_WORDS):
        return 500, "Partition Plan"
    if any(token in text for token in _FLOOR_FINISH_WORDS):
        return 450, "Floor Finishes / Finishes Plan"
    if any(token in text for token in _UNIT_PLAN_WORDS):
        return 320, "Unit / Apartment Plan"
    if "floor plan" in text or "floor" in str(page.get("page_type") or "").lower():
        return 220, "Floor Plan"
    return 0, ""


def _area_from_line(text: Any) -> float:
    match = auto._AREA_RE.search(str(text or ""))
    if not match:
        return 0.0
    value = _num(match.group(1))
    return value if 8.0 <= value <= 1000.0 else 0.0


def positioned_unit_area_candidates(app: Any, page: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pair UNIT/APT labels with printed m² by native PDF position."""
    lines = list(auto._pdf_word_lines(app, page) or [])
    if not lines:
        return []
    units: List[Dict[str, Any]] = []
    areas: List[Dict[str, Any]] = []
    max_x = max((_num((line.get("bbox") or [0, 0, 0, 0])[2]) for line in lines), default=1.0)
    max_y = max((_num((line.get("bbox") or [0, 0, 0, 0])[3]) for line in lines), default=1.0)
    diagonal = max(1.0, math.hypot(max_x, max_y))

    for line in lines:
        text = str(line.get("text") or "")
        label = unit._match_unit_label(text, allow_short=True)
        area = _area_from_line(text)
        if label:
            units.append({"label": label, "center": list(line.get("center") or [0, 0]), "bbox": list(line.get("bbox") or []), "text": text, "direct_area": area})
        if area > 0:
            low = _low(text)
            if any(token in low for token in floor_context._INTERNAL_AREA_EXCLUDES):
                continue
            areas.append({"area_m2": area, "center": list(line.get("center") or [0, 0]), "bbox": list(line.get("bbox") or []), "text": text})

    if not units or not areas:
        return []

    results: List[Dict[str, Any]] = []
    used_area_indexes: set[int] = set()
    for item in units:
        if item["direct_area"] > 0:
            results.append({
                "label": item["label"], "area_m2": round(item["direct_area"], 2),
                "confidence": "Documented", "source": item["text"], "bbox": item["bbox"],
                "unit_bbox": item["bbox"], "pairing": "Unit label and m² on same PDF text line",
            })

    existing_labels = {item["label"].lower() for item in results}
    pairs: List[Tuple[float, int, int]] = []
    for unit_index, unit_item in enumerate(units):
        if unit_item["label"].lower() in existing_labels:
            continue
        ux, uy = map(_num, unit_item["center"][:2])
        for area_index, area_item in enumerate(areas):
            ax, ay = map(_num, area_item["center"][:2])
            distance = math.hypot(ax - ux, ay - uy) / diagonal
            if distance <= 0.18:
                pairs.append((distance, unit_index, area_index))

    for distance, unit_index, area_index in sorted(pairs):
        if area_index in used_area_indexes:
            continue
        unit_item = units[unit_index]
        if unit_item["label"].lower() in {item["label"].lower() for item in results}:
            continue
        area_item = areas[area_index]
        used_area_indexes.add(area_index)
        results.append({
            "label": unit_item["label"], "area_m2": round(area_item["area_m2"], 2),
            "confidence": "Documented", "source": area_item["text"], "bbox": area_item["bbox"],
            "unit_bbox": unit_item["bbox"],
            "pairing": f"Spatial UNIT↔m² match ({distance * 100:.1f}% of page diagonal)",
        })
    return sorted(results, key=lambda item: item["label"].lower())


def _manual_floor_blocked(app: Any, workspace_id: int, label: str) -> bool:
    manual = auto_guard._manual_floor_keys(app, int(workspace_id))
    key = auto_guard._normalise(label)
    return bool(key and any(key == item or key in item or item in key for item in manual))


def _load_provenance(app: Any, workspace_id: int) -> Dict[str, Any]:
    return dict(_json_load(app.workspace_setting(int(workspace_id), PROVENANCE_SETTING_KEY, "{}"), {}) or {})


def _save_provenance(app: Any, workspace_id: int, mapping: Dict[str, Any]) -> None:
    app.set_workspace_setting(int(workspace_id), PROVENANCE_SETTING_KEY, json.dumps(mapping, separators=(",", ":"), default=str))


def _boundary_for_label(app: Any, page: Dict[str, Any], label: str) -> Optional[Dict[str, Any]]:
    try:
        for item in unit.bounded_unit_boundary_candidates(app, page):
            if auto_guard._normalise(item.get("label")) == auto_guard._normalise(label):
                return dict(item)
    except Exception:
        return None
    return None


def priority_unit_rows(app: Any, workspace_id: int, pages: Sequence[Dict[str, Any]], fallback_builder):
    pages = [dict(page) for page in pages or []]
    ranked = []
    for page in pages:
        priority, source_kind = floor_source_priority(page)
        if priority:
            ranked.append((priority, source_kind, page))
    preferred = [item for item in ranked if item[0] >= 450]
    sources = preferred if preferred else ranked
    if not sources:
        return fallback_builder(app, int(workspace_id), pages)

    by_unit: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for priority, source_kind, page in sources:
        candidates = positioned_unit_area_candidates(app, page)
        if not candidates:
            candidates = list(unit.extract_unit_area_candidates(page.get("extracted_text")) or [])
        for candidate in candidates:
            item = dict(candidate)
            item.update({
                "priority": priority, "source_kind": source_kind, "page_id": int(page["id"]),
                "page_label": str(page.get("page_label") or ""), "page": page,
            })
            by_unit[str(item.get("label") or "").lower()].append(item)

    if not by_unit:
        # Keep fallback constrained to the estimator-selected preferred floor sources.
        return fallback_builder(app, int(workspace_id), [item[2] for item in sources])

    rows = []
    summary: List[Dict[str, Any]] = []
    provenance = _load_provenance(app, int(workspace_id))
    # Remove stale v1.2.26 auto-floor provenance; manual polygon provenance is retained elsewhere.
    provenance = {key: value for key, value in provenance.items() if not str(key).startswith("PB Auto Geometry v1.2.19 · v1.2.26 floor:")}

    for _unit_key, candidates in sorted(by_unit.items()):
        candidates.sort(key=lambda item: (-int(item.get("priority") or 0), str(item.get("page_label") or "")))
        chosen = candidates[0]
        label = str(chosen.get("label") or "")
        if not label or _manual_floor_blocked(app, int(workspace_id), label):
            continue
        primary_area = _num(chosen.get("area_m2"))
        if primary_area <= 0:
            continue

        source_records = []
        cross_verified = False
        conflict = False
        for candidate in candidates:
            area = _num(candidate.get("area_m2"))
            rel = abs(area - primary_area) / max(primary_area, 1e-9)
            if candidate is not chosen and rel <= 0.02:
                cross_verified = True
            elif candidate is not chosen and rel > 0.02:
                conflict = True
            boundary = _boundary_for_label(app, candidate["page"], label)
            source_records.append({
                "page_id": int(candidate["page_id"]), "page_label": str(candidate["page_label"]),
                "source_kind": str(candidate["source_kind"]), "area_m2": round(area, 2),
                "text_bbox": candidate.get("bbox"), "unit_bbox": candidate.get("unit_bbox"),
                "polygon": (boundary or {}).get("polygon"), "boundary_bbox": (boundary or {}).get("bbox"),
                "evidence_text": str(candidate.get("source") or ""), "pairing": str(candidate.get("pairing") or "Text block match"),
            })

        source_ref = f"{auto.SOURCE_PREFIX} · v1.2.26 floor:{label} · page:{int(chosen['page_id'])}"
        note = f"Documented unit floor area from selected {chosen['source_kind']}."
        confidence = "Cross-verified" if cross_verified and not conflict else "Documented"
        if cross_verified:
            note += " Matching value found on another selected preferred floor-area sheet."
        if conflict:
            note += " WARNING: another selected preferred sheet shows a different m²; review before pricing."
        rows.append(auto._takeoff_row(
            workspace_id=int(workspace_id), section="Internal", element="Floor area", location=label,
            substrate="Other", quantity=primary_area, status="Measured", source_page=str(chosen["page_label"]),
            source_reference=source_ref, confidence=confidence, notes=note, row_role="floor_area",
        ))
        item = dict(chosen)
        item.update({
            "quantity_status": "Measured", "confidence": confidence,
            "cross_verified": cross_verified, "cross_check_conflict": conflict,
            "sources": source_records,
        })
        summary.append(item)
        provenance[source_ref] = {
            "kind": "area", "unit": "m²", "location": label, "quantity": round(primary_area, 2),
            "authoritative_source": "documented", "sources": source_records,
        }

    _save_provenance(app, int(workspace_id), provenance)
    return rows, summary


def apply(app: Any) -> None:
    if getattr(app, "_pb_selected_evidence_floor_v1226_applied", False):
        return
    app._pb_selected_evidence_floor_v1226_applied = True

    # Schedule definitions and material codes now come only from estimator-selected sheets.
    material.build_material_dictionary = lambda app_obj, workspace_id: selected_material_dictionary(app_obj, int(workspace_id))

    base_review_builder = material.build_review_issues
    def _selected_review_builder(app_obj: Any, workspace_id: int, report: Dict[str, Any], state: Dict[str, Any]):
        issues = list(base_review_builder(app_obj, int(workspace_id), report, filter_material_state(app_obj, int(workspace_id), state)) or [])
        for unit_item in report.get("units") or []:
            if unit_item.get("cross_check_conflict"):
                issues.append({
                    "category": "Unit floor area conflict", "severity": "High", "code": "",
                    "page_id": int(unit_item.get("page_id") or 0), "page_label": str(unit_item.get("page_label") or ""),
                    "message": f"{unit_item.get('label')}: selected Partition/Finishes Plan floor-area values disagree. Confirm the correct documented m².",
                    "bbox": unit_item.get("bbox"), "bbox_mode": "xyxy", "source": str(unit_item.get("source") or ""),
                })
        return filter_review_issues(app_obj, int(workspace_id), issues)
    material.build_review_issues = _selected_review_builder

    base_review_panel = material.review_panel
    def _selected_review_panel(app_obj: Any, workspace: Dict[str, Any]):
        workspace_id = int(workspace["id"])
        state = filter_material_state(app_obj, workspace_id, material._setting_get(app_obj, workspace_id))
        try:
            material._setting_set(app_obj, workspace_id, state)
        except Exception:
            pass
        return base_review_panel(app_obj, workspace)
    material.review_panel = _selected_review_panel

    base_units = auto._build_unit_rows
    auto._build_unit_rows = lambda app_obj, workspace_id, pages: priority_unit_rows(app_obj, int(workspace_id), pages, base_units)

    app.selected_takeoff_pages = lambda workspace_id: selected_pages(app, int(workspace_id))
    app.positioned_unit_floor_areas = lambda page: positioned_unit_area_candidates(app, dict(page))
    app.takeoff_provenance_v1226 = lambda workspace_id: _load_provenance(app, int(workspace_id))
    app.material_review_issues = lambda workspace_id: filter_material_state(app, int(workspace_id), material._setting_get(app, int(workspace_id))).get("review_issues") or []
