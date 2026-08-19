"""PlanReader v1.2.23 end-to-end Autopilot.

Goal: make the normal estimator workflow upload-first rather than tool-first.
Newly indexed drawing sets are automatically rendered, triaged, calibrated,
cross-referenced, measured and turned into a reviewable take-off + 3D model.

Accuracy policy
---------------
Measured/documented drawing evidence always outranks inferred visual evidence.
Artist impressions may contribute orientation, palette and visual-reference hints,
but never override calibrated plan/elevation dimensions.  Where independent
sources disagree, Autopilot raises a page-linked review issue instead of silently
choosing one answer.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import math
import re
import statistics
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from PIL import Image, ImageFilter

import pb_auto_geometry_v1219 as auto
import pb_material_schedule_v1222 as material
import pb_memory_stability_v1220 as memory
import pb_unit_floor_area_v1221 as unit

VERSION = "1.2.23"
SETTING_KEY = "autopilot_v1223"
MODEL_SOURCE_PREFIX = f"PB Autopilot v{VERSION} · model:"
AUTO_NOTE_PREFIX = f"[AUTOPILOT v{VERSION}]"

_SCALE_RE = re.compile(r"(?<!\d)1\s*:\s*(\d{2,4})(?!\d)", re.IGNORECASE)
_LEVEL_RE = re.compile(r"\bLEVEL\s*0*([0-9]{1,2})\b", re.IGNORECASE)
_BASEMENT_RE = re.compile(r"\b(?:BASEMENT|B)\s*0*([0-9]{1,2})\b", re.IGNORECASE)
_HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}\b")

_defer_analysis: contextvars.ContextVar[bool] = contextvars.ContextVar("pb_autopilot_defer_analysis", default=False)
_processing_document: contextvars.ContextVar[int] = contextvars.ContextVar("pb_autopilot_processing_document", default=0)
_artist_cache: Dict[str, Dict[str, Any]] = {}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _json_load(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except Exception:
        return fallback


def _state_get(app: Any, workspace_id: int) -> Dict[str, Any]:
    rows = app.lquery("SELECT value FROM workspace_settings WHERE workspace_id=? AND key=?", (int(workspace_id), SETTING_KEY))
    return _json_load(rows[0].get("value") if rows else "{}", {})


def _state_set(app: Any, workspace_id: int, state: Dict[str, Any]) -> None:
    app.lexecute(
        """INSERT INTO workspace_settings(workspace_id,key,value,updated_at) VALUES(?,?,?,?)
           ON CONFLICT(workspace_id,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
        (int(workspace_id), SETTING_KEY, json.dumps(state, separators=(",", ":"), default=str), app.now_stamp()),
    )


def _regular_file(value: Any) -> Optional[Path]:
    try:
        return memory.regular_file(value)
    except Exception:
        raw = str(value or "").strip()
        if not raw:
            return None
        path = Path(raw)
        return path if path.is_file() else None


def _selected_pages(app: Any, workspace_id: int) -> List[Dict[str, Any]]:
    return [dict(row) for row in app.lquery(
        """SELECT p.*,d.file_name,d.sha256 AS document_sha FROM pages p
           JOIN documents d ON d.id=p.document_id
           WHERE p.workspace_id=? AND COALESCE(p.selected,0)=1 ORDER BY p.document_id,p.page_no,p.id""",
        (int(workspace_id),),
    )]


def _all_pages(app: Any, workspace_id: int) -> List[Dict[str, Any]]:
    return [dict(row) for row in app.lquery(
        """SELECT p.*,d.file_name,d.sha256 AS document_sha FROM pages p
           JOIN documents d ON d.id=p.document_id WHERE p.workspace_id=?
           ORDER BY p.document_id,p.page_no,p.id""",
        (int(workspace_id),),
    )]


def source_signature(app: Any, workspace_id: int) -> str:
    """Stable signature used to report whether the current results match source files."""
    digest = hashlib.sha256()
    for page in _all_pages(app, workspace_id):
        fields = (
            page.get("id"), page.get("document_id"), page.get("document_sha"), page.get("page_no"),
            page.get("page_label"), page.get("page_type"), page.get("selected"), page.get("px_per_m"),
            page.get("scale_text"), hashlib.sha1(str(page.get("extracted_text") or "").encode("utf-8", "ignore")).hexdigest(),
        )
        digest.update(repr(fields).encode("utf-8", "ignore"))
        path = _regular_file(page.get("image_path"))
        if path is not None:
            try:
                stat = path.stat()
                digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
            except OSError:
                pass
    return digest.hexdigest()


def _strong_page_evidence(page: Dict[str, Any]) -> Tuple[bool, int, str]:
    """Second-opinion triage layered over the existing painting-page classifier."""
    kind = str(page.get("page_type") or "Other")
    text = f"{page.get('page_label') or ''}\n{page.get('extracted_text') or ''}"
    low = text.lower()
    try:
        keep, reason, score = auto.page_relevance(kind, page.get("extracted_text"), page.get("page_label"))
    except Exception:
        keep, reason, score = True, "Existing page selection", 50
    score = int(score or 0)

    if unit.page_has_unit_plan_evidence(kind, page.get("extracted_text"), page.get("page_label")):
        score = max(score, 100); keep = True; reason = "Unit/partition layout required for floor-area take-off"
    try:
        if material._schedule_page(page):
            score = max(score, 100); keep = True; reason = "Finishing/material schedule required for code resolution"
    except Exception:
        pass
    if any(token in low for token in ("artist's impression", "artists impression", "artist impression", "perspective", "3d render", "external render")):
        score = max(score, 92); keep = True; reason = "Artist/render reference for 3D cross-reference"
    if any(token in low for token in ("elevation", "floor plan", "reflected ceiling", "rcp", "section")):
        score = max(score, 80); keep = True
    try:
        if material._codes(text):
            score = max(score, 65)
    except Exception:
        pass
    # Keep services/structural/civil out unless they also contain very strong painting evidence.
    if kind.lower() in ("services", "structural", "landscape / civil") and score < 90:
        return False, min(score, 25), f"{kind} excluded unless it contains strong painting evidence"
    return bool(keep or score >= 60), score, str(reason)


def triage_workspace(app: Any, workspace_id: int) -> Dict[str, Any]:
    pages = _all_pages(app, workspace_id)
    decisions: List[Dict[str, Any]] = []
    updates: List[Tuple[int, int]] = []
    for page in pages:
        keep, score, reason = _strong_page_evidence(page)
        old = int(page.get("selected") or 0)
        new = 1 if keep else 0
        if old != new:
            updates.append((new, int(page["id"])))
        decisions.append({
            "page_id": int(page["id"]), "page_no": int(page.get("page_no") or 0),
            "page_label": str(page.get("page_label") or ""), "page_type": str(page.get("page_type") or ""),
            "selected": bool(new), "score": int(score), "reason": reason,
        })
    if updates:
        conn = app.local_connect()
        try:
            conn.executemany("UPDATE pages SET selected=? WHERE id=?", updates)
            conn.commit()
        except Exception:
            conn.rollback(); raise
        finally:
            conn.close()
    return {
        "kept": sum(1 for item in decisions if item["selected"]),
        "discarded": sum(1 for item in decisions if not item["selected"]),
        "changed": len(updates), "pages": decisions,
    }


def _printed_scale(page: Dict[str, Any]) -> str:
    haystack = f"{page.get('scale_text') or ''}\n{page.get('extracted_text') or ''}"
    match = _SCALE_RE.search(haystack)
    return f"1:{int(match.group(1))}" if match else ""


def _image_size(page: Dict[str, Any]) -> Tuple[int, int]:
    path = _regular_file(page.get("image_path"))
    if path is None:
        return 0, 0
    try:
        with Image.open(path) as image:
            return int(image.width), int(image.height)
    except Exception:
        return 0, 0


def cross_page_calibration(app: Any, workspace_id: int) -> List[Dict[str, Any]]:
    """Fill only *missing* scales from same-document pages at the same printed scale.

    This never overwrites any existing calibration. One donor is provisional; two
    agreeing donors promote the cross-page calibration to cross-verified.
    """
    pages = _selected_pages(app, workspace_id)
    meta: Dict[int, Tuple[str, Tuple[int, int]]] = {
        int(page["id"]): (_printed_scale(page), _image_size(page)) for page in pages
    }
    donors: Dict[Tuple[int, str], List[Dict[str, Any]]] = {}
    for page in pages:
        scale, size = meta[int(page["id"])]
        if scale and _num(page.get("px_per_m")) > 0 and size != (0, 0):
            donors.setdefault((int(page.get("document_id") or 0), scale), []).append(page)

    updates: List[Dict[str, Any]] = []
    conn = app.local_connect()
    try:
        for page in pages:
            if _num(page.get("px_per_m")) > 0:
                continue
            scale, size = meta[int(page["id"])]
            if not scale or size == (0, 0):
                continue
            candidates = []
            for donor in donors.get((int(page.get("document_id") or 0), scale), []):
                dsize = meta[int(donor["id"])][1]
                if not dsize[0] or not dsize[1]:
                    continue
                if abs(dsize[0] - size[0]) / max(size[0], 1) > 0.025 or abs(dsize[1] - size[1]) / max(size[1], 1) > 0.025:
                    continue
                candidates.append(_num(donor.get("px_per_m")))
            candidates = [value for value in candidates if value > 0]
            if not candidates:
                continue
            median = float(statistics.median(candidates))
            spread = (max(candidates) - min(candidates)) / median if len(candidates) > 1 and median else 0.0
            if len(candidates) > 1 and spread > 0.05:
                continue
            confidence = "Cross-verified" if len(candidates) >= 2 else "Provisional"
            label = f"Auto cross-page {scale} · {confidence} · {len(candidates)} donor page(s)"
            conn.execute("UPDATE pages SET px_per_m=?,scale_text=? WHERE id=? AND COALESCE(px_per_m,0)<=0", (median, label, int(page["id"])))
            updates.append({"page_id": int(page["id"]), "px_per_m": median, "method": "Cross-page scale", "confidence": confidence, "scale": scale})
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()
    return updates


def _render_reference_page(page: Dict[str, Any]) -> bool:
    text = f"{page.get('page_type') or ''} {page.get('page_label') or ''} {page.get('extracted_text') or ''}".lower()
    return any(token in text for token in ("render", "artist", "impression", "perspective", "3d view", "3d image"))


def _hex(rgb: Sequence[int]) -> str:
    return "#%02X%02X%02X" % tuple(max(0, min(255, int(v))) for v in rgb[:3])


def analyse_artist_image(path: Path) -> Dict[str, Any]:
    """Bounded-memory visual reference analysis; never used as measured geometry."""
    try:
        stat = path.stat()
        key = f"{path}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        key = str(path)
    if key in _artist_cache:
        return dict(_artist_cache[key])

    with Image.open(path) as source:
        image = source.convert("RGB")
        original = image.size
        image.thumbnail((640, 640), Image.Resampling.LANCZOS)
        width, height = image.size
        if width < 8 or height < 8:
            return {}
        # Palette: discard extreme white/black colours which are commonly paper/sky/title block.
        quant = image.quantize(colors=12, method=Image.Quantize.MEDIANCUT).convert("RGB")
        counts = quant.getcolors(maxcolors=width * height) or []
        colours = []
        for count, rgb in sorted(counts, reverse=True):
            r, g, b = rgb
            brightness = (r + g + b) / 3.0
            chroma = max(rgb) - min(rgb)
            if brightness < 22 or brightness > 246:
                continue
            colours.append({"hex": _hex(rgb), "count": int(count), "brightness": round(brightness, 1), "chroma": int(chroma)})
            if len(colours) >= 6:
                break

        # Silhouette candidate: compare pixels with the average of the four corners.
        pixels = image.load()
        corners = [pixels[0, 0], pixels[width - 1, 0], pixels[0, height - 1], pixels[width - 1, height - 1]]
        bg = tuple(sum(c[i] for c in corners) / 4.0 for i in range(3))
        xs: List[int] = []; ys: List[int] = []
        stride = max(1, max(width, height) // 320)
        y_limit = int(height * 0.92)
        for y in range(0, y_limit, stride):
            for x in range(0, width, stride):
                rgb = pixels[x, y]
                delta = math.sqrt(sum((float(rgb[i]) - bg[i]) ** 2 for i in range(3)))
                if delta >= 42:
                    xs.append(x); ys.append(y)
        bbox = None
        if xs and ys:
            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
            frac = ((x1 - x0 + 1) * (y1 - y0 + 1)) / float(width * height)
            if 0.03 <= frac <= 0.95:
                bbox = [round(x0 / width, 4), round(y0 / height, 4), round((x1 - x0) / width, 4), round((y1 - y0) / height, 4)]

        edges = image.convert("L").filter(ImageFilter.FIND_EDGES)
        hist = edges.histogram()
        edge_pixels = sum(hist[45:])
        edge_density = edge_pixels / float(max(1, width * height))

    result = {
        "original_width": int(original[0]), "original_height": int(original[1]),
        "analysis_width": width, "analysis_height": height,
        "palette": colours, "silhouette_bbox_norm": bbox,
        "edge_density": round(edge_density, 4),
    }
    _artist_cache.clear() if len(_artist_cache) > 64 else None
    _artist_cache[key] = dict(result)
    return result


def artist_references(app: Any, workspace_id: int) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    for page in _selected_pages(app, workspace_id):
        if not _render_reference_page(page):
            continue
        path = _regular_file(page.get("image_path"))
        if path is None:
            continue
        data = analyse_artist_image(path)
        face = auto._page_face(page.get("extracted_text"), page.get("page_label"))
        refs.append({
            "page_id": int(page["id"]), "page_label": str(page.get("page_label") or ""),
            "face": face, "image_path": str(path), **data,
        })
    return refs


def _canonical_level(page: Dict[str, Any]) -> Optional[Tuple[float, str]]:
    text = f"{page.get('page_label') or ''} {page.get('extracted_text') or ''}".upper()
    if "ROOF" in text and "ROOF PLAN" in text:
        return None
    if "GROUND FLOOR" in text or "GROUND LEVEL" in text or re.search(r"\bGROUND\b", text):
        return 0.0, "Ground"
    basement = _BASEMENT_RE.search(text)
    if basement:
        value = int(basement.group(1))
        return float(-value), f"Basement {value}"
    level = _LEVEL_RE.search(text)
    if level:
        value = int(level.group(1))
        return float(value), f"Level {value}"
    return None


def detect_levels(pages: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    found: Dict[str, Dict[str, Any]] = {}
    for page in pages:
        kind = str(page.get("page_type") or "").lower()
        if not ("floor" in kind or "partition" in kind or unit.page_has_unit_plan_evidence(kind, page.get("extracted_text"), page.get("page_label"))):
            continue
        result = _canonical_level(page)
        if result is None:
            continue
        order, name = result
        found.setdefault(name, {"name": name, "order": order, "page_ids": []})["page_ids"].append(int(page["id"]))
    levels = sorted(found.values(), key=lambda item: item["order"])
    return levels or [{"name": "Ground", "order": 0.0, "page_ids": []}]


def _artist_face_palette(refs: Sequence[Dict[str, Any]], face: str) -> str:
    candidates = [ref for ref in refs if ref.get("face") == face] or [ref for ref in refs if not ref.get("face")] or list(refs)
    for ref in candidates:
        palette = ref.get("palette") or []
        if palette:
            return str(palette[0].get("hex") or "")
    return ""


def _facade_by_face(report: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {str(item.get("face")): item for item in report.get("facades") or [] if item.get("face")}


def geometry_reconciliation(report: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    footprint = report.get("footprint") or {}
    width = _num(footprint.get("width_m")); depth = _num(footprint.get("depth_m"))
    checks: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    for facade in report.get("facades") or []:
        face = str(facade.get("face") or "")
        measured = _num(facade.get("width_m"))
        expected = width if face in ("front", "rear") else depth if face in ("left", "right") else 0.0
        if expected <= 0 or measured <= 0:
            continue
        diff = abs(measured - expected) / expected
        status = "Cross-verified" if diff <= 0.03 else "Close" if diff <= 0.08 else "Mismatch"
        check = {
            "page_id": int(facade.get("page_id") or 0), "page_label": str(facade.get("page_label") or ""),
            "face": face, "plan_width_m": round(expected, 3), "elevation_width_m": round(measured, 3),
            "difference_pct": round(diff * 100.0, 1), "status": status,
        }
        checks.append(check)
        if diff > 0.08:
            issues.append({
                "category": "Plan/elevation geometry", "severity": "High", "code": "",
                "page_id": int(facade.get("page_id") or 0), "page_label": str(facade.get("page_label") or ""),
                "message": f"{face or 'Elevation'} width differs from the matching floor-plan perimeter by {diff*100:.1f}% ({measured:.2f} m vs {expected:.2f} m).",
                "bbox": facade.get("bbox"), "bbox_mode": "xywh", "source": "Floor-plan perimeter vs calibrated elevation",
            })
        explicit = facade.get("explicit_areas") or []
        gross = _num(facade.get("gross_m2"))
        if explicit and gross > 0:
            total = sum(_num(item.get("area_m2")) for item in explicit)
            area_diff = abs(total - gross) / gross
            if area_diff > 0.15:
                issues.append({
                    "category": "Facade material reconciliation", "severity": "Medium", "code": "",
                    "page_id": int(facade.get("page_id") or 0), "page_label": str(facade.get("page_label") or ""),
                    "message": f"Documented substrate areas total {total:.2f} m² but gross calibrated facade geometry is {gross:.2f} m² ({area_diff*100:.1f}% difference). Check openings/exclusions or schedule basis.",
                    "bbox": facade.get("bbox"), "bbox_mode": "xywh", "source": "Material-area sum vs gross elevation",
                })
    return checks, issues


def unit_coverage_issues(pages: Sequence[Dict[str, Any]], report: Dict[str, Any]) -> Tuple[int, int, List[Dict[str, Any]]]:
    labelled: Dict[str, Dict[str, Any]] = {}
    for page in pages:
        if not unit.page_has_unit_plan_evidence(page.get("page_type"), page.get("extracted_text"), page.get("page_label")):
            continue
        for match in unit.UNIT_LABEL_RE.finditer(str(page.get("extracted_text") or "")):
            label = f"Unit {match.group(1)}"
            labelled.setdefault(label.lower(), {"label": label, "page_id": int(page["id"]), "page_label": str(page.get("page_label") or "")})
    found = {str(item.get("label") or "").lower() for item in report.get("units") or [] if item.get("label")}
    issues = []
    for key, item in labelled.items():
        if key in found:
            continue
        issues.append({
            "category": "Missing unit floor area", "severity": "High", "code": "",
            "page_id": item["page_id"], "page_label": item["page_label"],
            "message": f"{item['label']} is clearly referenced on the unit/floor plan but no floor-area quantity was confirmed or derived.",
            "bbox": None, "bbox_mode": "xyxy", "source": "Unit label without matching floor-area row",
        })
    return len(labelled), len(found & set(labelled)), issues


def _material_face_map(state: Dict[str, Any], report: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    page_face = {int(f.get("page_id") or 0): str(f.get("face") or "") for f in report.get("facades") or []}
    mapping: Dict[str, List[Dict[str, Any]]] = {}
    seen: set[Tuple[str, str, str]] = set()
    for occurrence in state.get("occurrences") or []:
        if occurrence.get("status") != "Confirmed" or not occurrence.get("substrate"):
            continue
        face = page_face.get(int(occurrence.get("page_id") or 0), "")
        if not face:
            continue
        key = (face, str(occurrence.get("code") or ""), str(occurrence.get("substrate") or ""))
        if key in seen:
            continue
        seen.add(key)
        mapping.setdefault(face, []).append(dict(occurrence))
    return mapping


def _surface_code(items: Sequence[Dict[str, Any]]) -> str:
    if len(items) != 1:
        return "OTHER"
    item = items[0]
    try:
        return auto._surface_code_for([{"code": item.get("code"), "name": item.get("substrate")}])
    except Exception:
        return "OTHER"


def build_autopilot_model(app: Any, workspace_id: int, report: Dict[str, Any], state: Dict[str, Any], refs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    footprint = report.get("footprint") or {}
    width = _num(footprint.get("width_m")); depth = _num(footprint.get("depth_m"))
    if width <= 0 or depth <= 0:
        return {"created": 0, "reason": "No calibrated floor-plan footprint", "levels": []}

    pages = _selected_pages(app, workspace_id)
    levels = detect_levels(pages)
    count = max(1, len(levels))
    facade_heights = [_num(item.get("height_m")) for item in report.get("facades") or [] if 2.0 <= _num(item.get("height_m")) <= 100.0]
    total_height = float(statistics.median(facade_heights)) if facade_heights else 0.0
    if total_height > 0 and 2.2 <= total_height / count <= 4.8:
        storey_height = total_height / count
        height_basis = "calibrated elevation total height"
    elif count == 1 and 2.2 <= total_height <= 8.0:
        storey_height = total_height
        height_basis = "calibrated elevation height"
    else:
        storey_height = 3.0
        height_basis = "3.0 m provisional storey height"

    # Preserve estimator-created measured/verified masses. Autopilot replaces only its
    # own model and the older single automatic envelope.
    manual = app.lquery(
        """SELECT id FROM model_masses WHERE workspace_id=?
           AND source_reference NOT LIKE ? AND source_reference NOT LIKE ?
           AND LOWER(COALESCE(confidence,'')) IN ('measured','verified')""",
        (int(workspace_id), MODEL_SOURCE_PREFIX + "%", auto.MODEL_SOURCE_PREFIX + "%"),
    )
    if manual:
        return {"created": 0, "reason": "Measured/verified estimator model preserved", "levels": levels, "storey_height_m": round(storey_height, 3)}

    conn = app.local_connect()
    mass_ids: List[int] = []
    try:
        old = conn.execute("SELECT id FROM model_masses WHERE workspace_id=? AND source_reference LIKE ?", (int(workspace_id), MODEL_SOURCE_PREFIX + "%")).fetchall()
        for row in old:
            conn.execute("DELETE FROM model_openings WHERE mass_id=?", (int(row[0]),))
        conn.execute("DELETE FROM model_masses WHERE workspace_id=? AND source_reference LIKE ?", (int(workspace_id), MODEL_SOURCE_PREFIX + "%"))
        # Remove only the legacy automatic envelope after the richer replacement is ready.
        legacy = conn.execute("SELECT id FROM model_masses WHERE workspace_id=? AND source_reference LIKE ?", (int(workspace_id), auto.MODEL_SOURCE_PREFIX + "%")).fetchall()
        for row in legacy:
            conn.execute("DELETE FROM model_openings WHERE mass_id=?", (int(row[0]),))
        conn.execute("DELETE FROM model_masses WHERE workspace_id=? AND source_reference LIKE ?", (int(workspace_id), auto.MODEL_SOURCE_PREFIX + "%"))

        artist_palette = [str(col.get("hex") or "") for ref in refs for col in (ref.get("palette") or []) if col.get("hex")]
        palette = []
        for colour in artist_palette:
            if colour not in palette:
                palette.append(colour)
        for idx, level in enumerate(levels):
            colour = palette[idx % len(palette)] if palette else ""
            finish = f"Artist reference {colour}" if colour else "External envelope"
            source = f"{MODEL_SOURCE_PREFIX}{level['name']}"
            notes = f"{AUTO_NOTE_PREFIX} Geometry from calibrated floor-plan footprint; height basis: {height_basis}. Artist impression contributes visual reference only."
            cur = conn.execute(
                """INSERT INTO model_masses(workspace_id,label,level_name,x,y,z,width,depth,height,finish,source_reference,confidence,notes,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (int(workspace_id), f"Automatic {level['name']}", level["name"], 0.0, 0.0, idx * storey_height,
                 width, depth, storey_height, finish, source, "Derived", notes, app.now_stamp()),
            )
            mass_ids.append(int(cur.lastrowid))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()

    face_materials = _material_face_map(state, report)
    raw = app.lquery("SELECT value FROM workspace_settings WHERE workspace_id=? AND key=?", (int(workspace_id), "3d_surface_editor_v1212"))
    surface_state = _json_load(raw[0].get("value") if raw else "{}", {})
    overrides = dict(surface_state.get("surfaces") or {}) if isinstance(surface_state, dict) else {}
    for mass_id in mass_ids:
        for face in ("front", "rear", "left", "right"):
            surface_id = f"mass:{mass_id}:{face}"
            old = dict(overrides.get(surface_id) or {})
            if old and not str(old.get("notes") or "").startswith(("[AUTO", AUTO_NOTE_PREFIX)):
                continue
            items = face_materials.get(face, [])
            material_note = ", ".join(f"{item.get('code')}={item.get('substrate')}" for item in items) or "substrate unresolved"
            colour = _artist_face_palette(refs, face)
            visual_note = f"; artist palette {colour}" if colour else ""
            overrides[surface_id] = {
                "substrate": _surface_code(items), "status": "Provisional",
                "progress_pct": _num(old.get("progress_pct")),
                "notes": f"{AUTO_NOTE_PREFIX} {face} · {material_note}{visual_note}. Geometry is drawing-based; visual reference is non-dimensional.",
            }
    app.lexecute(
        """INSERT INTO workspace_settings(workspace_id,key,value,updated_at) VALUES(?,?,?,?)
           ON CONFLICT(workspace_id,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
        (int(workspace_id), "3d_surface_editor_v1212", json.dumps({"surfaces": overrides, "saved_at": app.now_stamp()}, separators=(",", ":")), app.now_stamp()),
    )
    return {
        "created": len(mass_ids), "mass_ids": mass_ids, "levels": levels,
        "storey_height_m": round(storey_height, 3), "height_basis": height_basis,
        "artist_reference_pages": [int(ref["page_id"]) for ref in refs],
    }


def _merge_review_issues(app: Any, workspace_id: int, additions: Sequence[Dict[str, Any]]) -> int:
    state = material._setting_get(app, int(workspace_id))
    issues = [dict(item) for item in state.get("review_issues") or []]
    fingerprints = {
        (str(i.get("category")), int(i.get("page_id") or 0), str(i.get("code") or ""), str(i.get("message") or ""))
        for i in issues
    }
    for raw in additions:
        item = dict(raw)
        key = (str(item.get("category")), int(item.get("page_id") or 0), str(item.get("code") or ""), str(item.get("message") or ""))
        if key not in fingerprints:
            issues.append(item); fingerprints.add(key)
    for idx, item in enumerate(issues, 1):
        item["id"] = idx
    state["review_issues"] = issues
    state["analysed_at"] = app.now_stamp()
    material._setting_set(app, int(workspace_id), state)
    return len(issues)


def evidence_completeness(app: Any, workspace_id: int, report: Dict[str, Any], state: Dict[str, Any], refs: Sequence[Dict[str, Any]], labelled_units: int, found_units: int) -> Dict[str, Any]:
    pages = _selected_pages(app, workspace_id)
    measurement_pages = [p for p in pages if any(t in str(p.get("page_type") or "").lower() for t in ("floor", "elevation", "ceiling", "section", "partition"))]
    calibrated = sum(1 for p in measurement_pages if _num(p.get("px_per_m")) > 0)
    calibration_pct = 100.0 * calibrated / max(1, len(measurement_pages))

    facades = report.get("facades") or []
    useful_facades = sum(1 for f in facades if _num(f.get("gross_m2")) > 0 or f.get("explicit_areas"))
    facade_pct = 100.0 * useful_facades / max(1, len(facades)) if facades else 0.0

    occurrences = state.get("occurrences") or []
    confirmed = sum(1 for o in occurrences if o.get("status") == "Confirmed")
    material_pct = 100.0 * confirmed / max(1, len(occurrences)) if occurrences else (100.0 if state.get("dictionary") else 0.0)
    unit_pct = 100.0 * found_units / max(1, labelled_units) if labelled_units else (100.0 if report.get("units") else 0.0)
    model_count = len(app.lquery("SELECT id FROM model_masses WHERE workspace_id=?", (int(workspace_id),)))
    model_pct = 100.0 if model_count else 0.0
    selection_pct = 100.0 if pages else 0.0

    score = (
        selection_pct * 0.08 + calibration_pct * 0.24 + unit_pct * 0.20 +
        facade_pct * 0.20 + material_pct * 0.18 + model_pct * 0.10
    )
    return {
        "evidence_completeness_pct": round(score, 1),
        "selection_pct": round(selection_pct, 1), "calibration_pct": round(calibration_pct, 1),
        "unit_coverage_pct": round(unit_pct, 1), "facade_coverage_pct": round(facade_pct, 1),
        "material_resolution_pct": round(material_pct, 1), "model_generated_pct": round(model_pct, 1),
        "artist_reference_count": len(refs),
        "note": "Evidence completeness is not a guarantee of take-off accuracy. Final quantities remain subject to the Review Issues gate.",
    }


def _pending_selected_pages(app: Any, workspace_id: int) -> List[Dict[str, Any]]:
    return [page for page in _selected_pages(app, workspace_id) if _regular_file(page.get("image_path")) is None]


def _process_missing_selected_pages(app: Any, workspace_id: int) -> List[str]:
    pending = _pending_selected_pages(app, workspace_id)
    by_doc: Dict[int, List[int]] = {}
    for page in pending:
        by_doc.setdefault(int(page.get("document_id") or 0), []).append(int(page.get("page_no") or 0))
    messages: List[str] = []
    token = _defer_analysis.set(True)
    try:
        for document_id, page_nos in by_doc.items():
            if document_id <= 0:
                continue
            try:
                count, msg = app.process_document(document_id, force=False, page_ids=sorted(set(page_nos)))
                messages.append(f"Document #{document_id}: {count} page(s) · {msg}")
            except Exception as exc:
                messages.append(f"Document #{document_id}: ERROR · {exc}")
    finally:
        _defer_analysis.reset(token)
    return messages


def run_autopilot(app: Any, workspace_id: int, *, force: bool = False) -> Dict[str, Any]:
    """Run the full post-render evidence fusion pipeline."""
    state_before = _state_get(app, workspace_id)
    signature_before = source_signature(app, workspace_id)
    if not force and state_before.get("source_signature") == signature_before and state_before.get("completed"):
        return dict(state_before)

    triage = triage_workspace(app, workspace_id)
    processing_messages = _process_missing_selected_pages(app, workspace_id)

    # auto.analyse_workspace is replaced by apply() with the Autopilot wrapper below.
    report = auto.analyse_workspace(app, int(workspace_id))
    result = _state_get(app, workspace_id)
    result["triage"] = triage
    result["processing_messages"] = processing_messages
    result["completed"] = True
    result["pending"] = False
    result["source_signature"] = source_signature(app, workspace_id)
    result["completed_at"] = app.now_stamp()
    if report:
        result.setdefault("report", report)
    _state_set(app, int(workspace_id), result)
    return result


def _post_analyse(app: Any, workspace_id: int, base_analyse) -> Dict[str, Any]:
    report = base_analyse(app, int(workspace_id))
    # A final cross-page calibration pass catches same-scale sheets that contain no
    # usable local dimension but have a reliable sibling drawing. Re-run the base
    # geometry once only when a new scale was actually filled.
    cross_scale = cross_page_calibration(app, int(workspace_id))
    if cross_scale:
        report = base_analyse(app, int(workspace_id))
        report.setdefault("calibrations", []).extend(cross_scale)

    material_state = material._setting_get(app, int(workspace_id))
    refs = artist_references(app, int(workspace_id))
    geometry_checks, geometry_issues = geometry_reconciliation(report)
    pages = _selected_pages(app, int(workspace_id))
    labelled_units, found_units, unit_issues = unit_coverage_issues(pages, report)
    model = build_autopilot_model(app, int(workspace_id), report, material_state, refs)

    extra_issues = list(geometry_issues) + list(unit_issues)
    if not refs:
        extra_issues.append({
            "category": "Artist reference", "severity": "Low", "code": "", "page_id": 0, "page_label": "",
            "message": "No processed artist impression/render was found. The 3D model is drawing-based only.",
            "bbox": None, "bbox_mode": "xyxy", "source": "No artist/render page selected",
        })
    review_count = _merge_review_issues(app, int(workspace_id), extra_issues)
    material_state = material._setting_get(app, int(workspace_id))
    completeness = evidence_completeness(app, int(workspace_id), report, material_state, refs, labelled_units, found_units)

    report["autopilot_version"] = VERSION
    report["cross_page_calibrations"] = cross_scale
    report["geometry_checks"] = geometry_checks
    report["artist_references"] = refs
    report["autopilot_model"] = model
    report["evidence"] = completeness
    report["review_issues"] = review_count
    try:
        auto._setting_set(app, int(workspace_id), report)
    except Exception:
        pass
    current = _state_get(app, int(workspace_id))
    current.update({
        "version": VERSION, "pending": False, "completed": True,
        "source_signature": source_signature(app, int(workspace_id)),
        "report": report, "artist_references": refs, "model": model,
        "evidence": completeness, "geometry_checks": geometry_checks,
        "review_issues": review_count, "completed_at": app.now_stamp(),
    })
    _state_set(app, int(workspace_id), current)
    return report


def autopilot_panel(app: Any, workspace: Dict[str, Any]) -> None:
    workspace_id = int(workspace["id"])
    state = _state_get(app, workspace_id)
    evidence = state.get("evidence") or {}
    with app.st.expander("🚀 PlanReader Autopilot", expanded=not bool(state.get("completed"))):
        app.st.caption(
            "Upload-first workflow: selected pages are rendered automatically, then PlanReader cross-references dimensions, unit plans, elevations, finishing schedules and artist impressions to create the take-off and 3D model. Artist images are visual references only; calibrated drawings control dimensions."
        )
        c1, c2, c3, c4, c5 = app.st.columns(5)
        c1.metric("Evidence completeness", f"{_num(evidence.get('evidence_completeness_pct')):.0f}%")
        c2.metric("Calibration", f"{_num(evidence.get('calibration_pct')):.0f}%")
        c3.metric("Unit coverage", f"{_num(evidence.get('unit_coverage_pct')):.0f}%")
        c4.metric("Material resolution", f"{_num(evidence.get('material_resolution_pct')):.0f}%")
        c5.metric("Review issues", int(state.get("review_issues") or 0))
        if evidence:
            app.st.caption(evidence.get("note") or "")
        if state.get("pending"):
            app.st.info("Autopilot has pending uploaded documents and will finish processing them on this page run.")
        if app.st.button("Run full Autopilot now", type="primary", use_container_width=True, key=f"autopilot_run_{workspace_id}"):
            with app.st.spinner("Processing drawings, cross-referencing evidence and rebuilding the take-off/3D model…"):
                result = run_autopilot(app, workspace_id, force=True)
            app.st.success(f"Autopilot complete. Evidence completeness {result.get('evidence', {}).get('evidence_completeness_pct', 0)}%; review issues {result.get('review_issues', 0)}.")
            app.st.rerun()

        refs = state.get("artist_references") or []
        if refs:
            with app.st.expander(f"Artist / render references ({len(refs)})", expanded=False):
                rows = []
                for ref in refs:
                    rows.append({
                        "Page": ref.get("page_label"), "Face": ref.get("face") or "Perspective",
                        "Palette": ", ".join(item.get("hex", "") for item in (ref.get("palette") or [])[:4]),
                        "Edge density": ref.get("edge_density"),
                    })
                app.st.dataframe(app.pd.DataFrame(rows), use_container_width=True, hide_index=True)
        checks = state.get("geometry_checks") or []
        if checks:
            with app.st.expander("Plan ↔ elevation dimensional reconciliation", expanded=False):
                app.st.dataframe(app.pd.DataFrame(checks), use_container_width=True, hide_index=True)
        model = state.get("model") or {}
        if model.get("created"):
            app.st.caption(f"3D Autopilot: {model.get('created')} level mass(es) · {model.get('storey_height_m')} m/storey · {model.get('height_basis')}")


def apply(app: Any) -> None:
    if getattr(app, "_pb_autopilot_v1223_applied", False):
        return
    app._pb_autopilot_v1223_applied = True

    # Let artist-reference hex colours pass through to the existing Plotly model.
    base_finish_colour = app.finish_to_colour
    def _finish_to_colour(value: Any):
        match = _HEX_RE.search(str(value or ""))
        return match.group(0).upper() if match else base_finish_colour(value)
    app.finish_to_colour = _finish_to_colour

    # Wrap the already material-aware v1.2.22 analyser. Intermediate per-document
    # rendering defers this expensive whole-workspace stage; the upload-page wrapper
    # runs it once after the complete batch has been indexed/rendered.
    base_analyse = auto.analyse_workspace
    def _analyse(app_obj: Any, workspace_id: int):
        if _defer_analysis.get():
            return {"version": VERSION, "deferred": True, "workspace_id": int(workspace_id)}
        return _post_analyse(app_obj, int(workspace_id), base_analyse)
    auto.analyse_workspace = _analyse
    app.run_auto_geometry = lambda workspace_id: auto.analyse_workspace(app, int(workspace_id))
    app.run_planreader_autopilot = lambda workspace_id, force=False: run_autopilot(app, int(workspace_id), force=bool(force))

    # New documents become render-ready automatically after indexing. The re-entry
    # guard prevents process_document -> index_document_pages recursion.
    base_index = app.index_document_pages
    def _index_and_process(document_id: int, *args, **kwargs):
        result = base_index(document_id, *args, **kwargs)
        doc_id = int(document_id)
        if _processing_document.get() == doc_id:
            return result
        docs = app.lquery("SELECT workspace_id FROM documents WHERE id=?", (doc_id,))
        if not docs:
            return result
        workspace_id = int(docs[0]["workspace_id"])
        selected = app.lquery("SELECT page_no,image_path FROM pages WHERE document_id=? AND COALESCE(selected,0)=1 ORDER BY page_no", (doc_id,))
        missing = [int(row.get("page_no") or 0) for row in selected if _regular_file(row.get("image_path")) is None]
        if missing:
            ptoken = _processing_document.set(doc_id)
            dtoken = _defer_analysis.set(True)
            try:
                app.process_document(doc_id, force=False, page_ids=missing)
            finally:
                _defer_analysis.reset(dtoken)
                _processing_document.reset(ptoken)
        state = _state_get(app, workspace_id)
        state.update({"version": VERSION, "pending": True, "completed": False, "last_indexed_document": doc_id, "updated_at": app.now_stamp()})
        _state_set(app, workspace_id, state)
        return result
    app.index_document_pages = _index_and_process

    # Finish the batch once on the Project & Documents rerun after upload. This is
    # what avoids running expensive CV/material/3D analysis once per uploaded PDF.
    base_documents_page = app.project_documents_page
    def _documents_page(workspace: Dict[str, Any], bridge: Any, user: Dict[str, Any]):
        workspace_id = int(workspace["id"])
        state = _state_get(app, workspace_id)
        if state.get("pending"):
            try:
                with app.st.spinner("Autopilot: finishing page selection, take-off and 3D model…"):
                    run_autopilot(app, workspace_id, force=True)
            except Exception as exc:
                state = _state_get(app, workspace_id)
                state.update({"pending": False, "completed": False, "last_error": str(exc), "updated_at": app.now_stamp()})
                _state_set(app, workspace_id, state)
                app.st.warning(f"Autopilot could not finish one optional stage: {exc}. Uploaded documents remain available for review/re-run.")
        return base_documents_page(workspace, bridge, user)
    app.project_documents_page = _documents_page

    # Place Autopilot status ahead of the existing automatic geometry + material
    # review panel in the default No-AI take-off route.
    base_auto_panel = auto.auto_geometry_panel
    def _panel(app_obj: Any, workspace: Dict[str, Any]):
        autopilot_panel(app_obj, workspace)
        return base_auto_panel(app_obj, workspace)
    auto.auto_geometry_panel = _panel

    app.autopilot_source_signature = lambda workspace_id: source_signature(app, int(workspace_id))
    app.autopilot_artist_references = lambda workspace_id: artist_references(app, int(workspace_id))
    app.autopilot_cross_page_calibration = lambda workspace_id: cross_page_calibration(app, int(workspace_id))
    app.autopilot_geometry_reconciliation = geometry_reconciliation
