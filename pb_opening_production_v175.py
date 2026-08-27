"""PlanReader v1.7.5 production integration for the P5 opening-evidence pipeline.

This module does two deliberately separate jobs:

1. Fence legacy opening deductions so the old v134/v137/v139/v145 paths cannot
   subtract an automatically detected opening unless it carries the same proof
   required by the approved B5 gate. Explicit estimator-created/manual records
   remain supported.
2. Run the approved B1->B5 pipeline from the existing native-vector analysis
   command. The bridge uses the real PDF vectors/words and calibrated page scale,
   persists the resulting evidence separately, and fails closed on any error.

It does NOT fabricate schedule/elevation evidence and does NOT change benchmark
expectations. Plan-only detections therefore normally remain review/no-deduction
until corroborating evidence is available.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import math
from typing import Any, Dict, Iterable, List, Sequence

from pb_opening_evidence_v170 import (
    DEDUCTION_AUTO_ELIGIBLE,
    DEDUCTION_DERIVED_ELIGIBLE,
    DEDUCTION_DEDUCTED,
    DIMENSION_BASIS_ROUGH_OPENING,
)
from pb_opening_deduction_v174 import run_opening_pipeline
from pb_plan_opening_detection_v171 import Segment, TextWord

VERSION = "1.7.5"
SETTING_PREFIX = "opening_evidence_v175_page_"
MIN_DEDUCTION_CONFIDENCE = 0.70


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _assigned_wall(raw: Dict[str, Any]) -> str:
    value = str(raw.get("resolved_wall_ref") or raw.get("wall_ref") or "").strip()
    if value.lower() in {"", "unassigned", "unassigned wall", "unknown", "none"}:
        return ""
    return value


def is_authorised_deduction(raw: Dict[str, Any]) -> bool:
    """Return True only for an explicit manual decision or a proven B5 decision.

    Legacy ``deduct=True`` by itself is never authority. This prevents old
    defaults in v134/v139/v145 from becoming automatic wall-area subtraction.
    """
    raw = dict(raw or {})
    if not bool(raw.get("deduct", False)):
        return False

    if not _assigned_wall(raw):
        return False
    if _num(raw.get("width_m")) <= 0 or _num(raw.get("height_m")) <= 0:
        return False

    # Explicit estimator-created records remain a supported commercial override.
    confidence_label = str(raw.get("confidence") or "").strip().lower()
    if bool(raw.get("manual_override_confirmed", False)) or confidence_label == "manual estimator entry":
        return True

    # Automatic deductions require the complete approved P5/B5 proof bundle.
    if not bool(raw.get("reconciliation_complete", False)):
        return False
    if str(raw.get("deduction_status") or "") not in {
        DEDUCTION_AUTO_ELIGIBLE,
        DEDUCTION_DERIVED_ELIGIBLE,
    }:
        return False
    if str(raw.get("deduction_decision") or "") != DEDUCTION_DEDUCTED:
        return False
    if str(raw.get("dimension_basis") or "") != DIMENSION_BASIS_ROUGH_OPENING:
        return False
    minimum = min(
        _num(raw.get("geometry_confidence")),
        _num(raw.get("dimension_confidence")),
        _num(raw.get("association_confidence")),
    )
    return minimum >= MIN_DEDUCTION_CONFIDENCE


def _safe_legacy_normaliser(original_normalise):
    def safe_normalise(raw: Dict[str, Any]) -> Dict[str, Any]:
        source = dict(raw or {})
        item = dict(original_normalise(source))
        # Preserve P5/manual provenance that legacy v134 did not know about.
        for key in (
            "manual_override_confirmed",
            "opening_instance_id",
            "page_id",
            "page_no",
            "position_along_wall_m",
            "reconciliation_complete",
            "deduction_status",
            "deduction_decision",
            "dimension_basis",
            "geometry_confidence",
            "dimension_confidence",
            "association_confidence",
        ):
            if key in source:
                item[key] = source[key]
        proof = dict(source)
        proof.update(item)
        item["deduct"] = is_authorised_deduction(proof)
        return item

    return safe_normalise


def _safe_legacy_save(original_save, safe_normalise):
    def safe_save(app: Any, workspace_id: int, openings: Iterable[Dict[str, Any]]) -> None:
        payload: List[Dict[str, Any]] = []
        for raw in openings or []:
            row = dict(raw or {})
            # A user pressing Save/Deduct all is an explicit estimator decision.
            # Unsafe historical defaults have already been normalised false on load,
            # so a true value reaching this save path was actively selected now.
            row["manual_override_confirmed"] = bool(row.get("deduct", False))
            payload.append(safe_normalise(row))
        original_save(app, int(workspace_id), payload)

    return safe_save


def _safe_deducted_area(openings: Iterable[Dict[str, Any]]) -> float:
    total = 0.0
    for raw in openings or []:
        row = dict(raw or {})
        if not is_authorised_deduction(row):
            continue
        total += _num(row.get("width_m")) * _num(row.get("height_m")) * max(1, int(_num(row.get("quantity"), 1)))
    return round(total, 4)


def _safe_net_wall_area(gross_wall_m2: float, openings: Iterable[Dict[str, Any]]) -> float:
    return round(max(0.0, _num(gross_wall_m2) - _safe_deducted_area(openings)), 4)


def _safe_v145_detect(original_detect):
    def detect(candidates: Sequence[Dict[str, Any]], schedules: Sequence[Dict[str, Any]] | None = None) -> List[Dict[str, Any]]:
        rows = original_detect(candidates, schedules)
        result: List[Dict[str, Any]] = []
        for raw in rows or []:
            row = dict(raw)
            # v145 is an automatic detector. It may report geometry, but it has no
            # authority to make the commercial subtraction decision.
            row["deduct"] = False
            row["deduction_status"] = "review"
            row["deduction_decision"] = "not_deducted"
            row.setdefault("dimension_basis", "unknown")
            row["reconciliation_complete"] = False
            result.append(row)
        return result

    return detect


def _safe_v145_room_summary(original_summary):
    def room_summary(room: Dict[str, Any], openings: Sequence[Dict[str, Any]] | None = None, *args, **kwargs):
        safe = []
        for raw in openings or []:
            row = dict(raw or {})
            row["deduct"] = is_authorised_deduction(row)
            safe.append(row)
        return original_summary(room, safe, *args, **kwargs)

    return room_summary


def _safe_v145_facade_net(original_facade):
    def facade(regions: Sequence[Dict[str, Any]], openings: Sequence[Dict[str, Any]] | None = None):
        safe = []
        for raw in openings or []:
            row = dict(raw or {})
            row["deduct"] = is_authorised_deduction(row)
            safe.append(row)
        return original_facade(regions, safe)

    return facade


def install_legacy_safety_fence(app: Any) -> None:
    """Fence both module globals AND already-bound ``app`` aliases."""
    if getattr(app, "_pb_opening_legacy_safety_v175", False):
        return

    # v134: normalisation/display/save and area consumers.
    try:
        import pb_opening_deductions_v134 as legacy

        original_normalise = legacy.normalise_opening
        original_save = legacy._save
        safe_normalise = _safe_legacy_normaliser(original_normalise)
        safe_save = _safe_legacy_save(original_save, safe_normalise)
        legacy.normalise_opening = safe_normalise
        legacy._save = safe_save
        legacy.deducted_area_m2 = _safe_deducted_area
        legacy.net_wall_area_m2 = _safe_net_wall_area
        app.normalise_opening = safe_normalise
        app.deducted_opening_area_m2 = _safe_deducted_area
        app.net_wall_area_m2 = _safe_net_wall_area
    except Exception:
        # Absence of a legacy module is safe; do not create a replacement bypass.
        pass

    # v145: apply() binds functions onto app, so patch BOTH locations after the
    # reconstruction stack has finished applying v145.
    try:
        import pb_accuracy_v13_engines_v145 as accuracy

        original_detect = accuracy.detect_openings
        original_room = accuracy.room_quantity_summary
        original_facade = accuracy.facade_net_area
        safe_detect = _safe_v145_detect(original_detect)
        safe_room = _safe_v145_room_summary(original_room)
        safe_facade = _safe_v145_facade_net(original_facade)
        accuracy.detect_openings = safe_detect
        accuracy.room_quantity_summary = safe_room
        accuracy.facade_net_area = safe_facade
        app.detect_openings_v145 = safe_detect
        app.room_quantity_summary_v145 = safe_room
        app.facade_net_area_v145 = safe_facade
    except Exception:
        pass

    # v137/v139 consume the app aliases dynamically. Wrapping attachment makes
    # the boolean explicit on every opening before v139 calculates net wall m2.
    if hasattr(app, "attach_openings_v137"):
        original_attach = app.attach_openings_v137

        def safe_attach(workspace_id: int, walls: List[Dict[str, Any]]):
            attached = original_attach(workspace_id, walls)
            out = []
            for raw in attached or []:
                row = dict(raw or {})
                row["deduct"] = is_authorised_deduction(row)
                out.append(row)
            return out

        app.attach_openings_v137 = safe_attach

    app._pb_opening_legacy_safety_v175 = True


def _segment_from_native(raw: Dict[str, Any]) -> Segment:
    return Segment(
        x1=_num(raw.get("x1")),
        y1=_num(raw.get("y1")),
        x2=_num(raw.get("x2")),
        y2=_num(raw.get("y2")),
        layer=str(raw.get("layer") or ""),
        drawing_index=int(_num(str(raw.get("id") or "").split("i", 1)[0].lstrip("d"), 0)),
    )


def _word_from_native(raw: Dict[str, Any], page_no: int) -> TextWord:
    bbox = list(raw.get("bbox") or [0, 0, 0, 0])
    while len(bbox) < 4:
        bbox.append(0)
    return TextWord(
        text=str(raw.get("text") or ""),
        x0=_num(bbox[0]),
        y0=_num(bbox[1]),
        x1=_num(bbox[2]),
        y1=_num(bbox[3]),
        page_no=int(page_no),
    )


def run_p5_native_payload(
    native: Dict[str, Any],
    *,
    page_no: int,
    page_id: int = 0,
    workspace_id: int = 0,
    scale_info: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Run approved B1->B5 against one real native-vector page payload."""
    segments = [_segment_from_native(row) for row in native.get("segments") or []]
    words = [_word_from_native(row, page_no) for row in native.get("words") or []]
    pipeline = run_opening_pipeline(
        segments=segments,
        words=words,
        schedule_entries=None,
        elevation_openings=None,
        scale_info=scale_info or {},
        page_no=int(page_no),
    )
    for inst in pipeline.get("instances") or []:
        inst.workspace_id = int(workspace_id)
        inst.page_id = int(page_id) if page_id else None

    serialised_instances = [asdict(inst) for inst in pipeline.get("instances") or []]
    serialised_conflicts = [asdict(conflict) for conflict in pipeline.get("conflicts") or []]
    return {
        "version": VERSION,
        "workspace_id": int(workspace_id),
        "page_id": int(page_id),
        "page_no": int(page_no),
        "instances": serialised_instances,
        "conflicts": serialised_conflicts,
        "deducted_area_m2": float(pipeline.get("deducted_area_m2") or 0.0),
        "pipeline_notes": list(pipeline.get("pipeline_notes") or []),
        "candidate_count": len(serialised_instances),
        "deducted_count": sum(1 for row in serialised_instances if bool(row.get("deduct"))),
        "review_count": sum(1 for row in serialised_instances if str(row.get("deduction_status")) == "review"),
        "status": "ok",
        "error": "",
    }


def analyse_stored_page_openings(app: Any, page_id: int, vector_result: Dict[str, Any]) -> Dict[str, Any]:
    """Re-open the same stored PDF page and run P5 from its native evidence."""
    rows = app.lquery(
        "SELECT p.*,d.path FROM pages p JOIN documents d ON d.id=p.document_id WHERE p.id=?",
        (int(page_id),),
    )
    if not rows:
        raise ValueError("Page not found for P5 opening analysis")
    row = dict(rows[0])
    path_value = str(row.get("path") or "")
    if not path_value or getattr(app, "fitz", None) is None:
        raise ValueError("P5 opening analysis requires the original PDF")

    from pathlib import Path

    path = Path(path_value)
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise ValueError("P5 opening analysis requires an available PDF file")

    stored_page_no = max(1, int(_num(row.get("page_no"), 1)))
    render_zoom = max(0.01, _num(row.get("render_zoom"), 1.0))
    solved_px_per_m = _num((vector_result.get("scale") or {}).get("px_per_m"), _num(row.get("px_per_m"), 0.0))
    scale_info = {
        "px_per_m": solved_px_per_m,
        "render_zoom": render_zoom,
    }

    pdf = app.fitz.open(path)
    try:
        pdf_page = pdf.load_page(stored_page_no - 1)
        native = app.extract_native_page_v130(pdf_page)
    finally:
        pdf.close()

    return run_p5_native_payload(
        native,
        page_no=stored_page_no,
        page_id=int(page_id),
        workspace_id=int(row.get("workspace_id") or 0),
        scale_info=scale_info,
    )


def _persist_p5_result(app: Any, workspace_id: int, page_id: int, payload: Dict[str, Any]) -> None:
    app.set_workspace_setting(
        int(workspace_id),
        f"{SETTING_PREFIX}{int(page_id)}",
        json.dumps(payload, separators=(",", ":")),
    )


def install_native_vector_bridge(app: Any) -> None:
    """Hook the existing production native-vector analysis command into P5."""
    if getattr(app, "_pb_opening_native_bridge_v175", False):
        return
    if not hasattr(app, "analyse_stored_page_v130"):
        raise RuntimeError("P5 production bridge requires analyse_stored_page_v130")

    base_analyse = app.analyse_stored_page_v130

    def analyse_with_openings(page_id: int):
        result = base_analyse(int(page_id))
        rows = app.lquery("SELECT workspace_id FROM pages WHERE id=?", (int(page_id),))
        workspace_id = int((rows[0] or {}).get("workspace_id") or 0) if rows else 0
        try:
            payload = analyse_stored_page_openings(app, int(page_id), result)
        except Exception as exc:
            # Fail closed: a P5 processing error never re-enables legacy automatic
            # deductions. Persist the error so the estimator can see that opening
            # evidence was not produced.
            payload = {
                "version": VERSION,
                "workspace_id": workspace_id,
                "page_id": int(page_id),
                "instances": [],
                "conflicts": [],
                "deducted_area_m2": 0.0,
                "candidate_count": 0,
                "deducted_count": 0,
                "review_count": 0,
                "pipeline_notes": ["P5 opening analysis failed closed"],
                "status": "error",
                "error": str(exc),
            }
        if workspace_id:
            _persist_p5_result(app, workspace_id, int(page_id), payload)
        result = dict(result or {})
        result["p5_openings"] = {
            "version": VERSION,
            "status": payload.get("status"),
            "candidate_count": int(payload.get("candidate_count") or 0),
            "deducted_count": int(payload.get("deducted_count") or 0),
            "review_count": int(payload.get("review_count") or 0),
            "deducted_area_m2": float(payload.get("deducted_area_m2") or 0.0),
            "error": str(payload.get("error") or ""),
        }
        return result

    app.analyse_stored_page_v130 = analyse_with_openings
    app.run_p5_opening_native_payload_v175 = run_p5_native_payload
    app.is_authorised_opening_deduction_v175 = is_authorised_deduction
    app._pb_opening_native_bridge_v175 = True


def apply(app: Any) -> None:
    """Install the safety fence first, then the real production producer hook."""
    if getattr(app, "_pb_opening_production_v175_applied", False):
        return
    install_legacy_safety_fence(app)
    install_native_vector_bridge(app)
    app._pb_opening_production_v175_applied = True
