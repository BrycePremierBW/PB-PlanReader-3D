"""Phase 5M fail-closed production adapter facade.

The previous Phase 5 implementation is preserved in
``pb_production_3d_adapter_legacy.py`` so the complete implementation history
remains inspectable.  This module is the production import surface.  It keeps
all legacy symbols available, but overrides the safety-critical conversion,
level-resolution, persisted-evidence and workspace entry points with the final
zero-made-up-data contract.
"""
from __future__ import annotations

import copy
import json
import math
import re
from typing import Any, Dict, List, Optional, Tuple

import pb_production_3d_adapter_legacy as _legacy

# Preserve every existing symbol, including private helpers used by the
# established regression suite.  Safety-critical functions are overridden
# below after the compatibility surface is populated.
for _name in dir(_legacy):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_legacy, _name)

from pb_canonical_building import (  # noqa: E402
    CanonicalEvidenceObservation,
    CanonicalLevel,
    ReviewState,
    parse_optional_confidence,
    parse_optional_float,
)
from pb_3d_diagnostics import generate_production_diagnostics_report  # noqa: E402


_UNRESOLVED_LEVEL_ID = "lvl_unresolved_review"
_UNRESOLVED_LEVEL_NAME = "Unresolved Level Container (Review Required)"
_SHEET_TEXT_PATTERNS = (
    r"^a\d+$",
    r"^sheet\s*\d+$",
    r"^floor\s*plan$",
    r"^ground\s+floor\s+plan$",
    r"^general\s*arrangement$",
    r"^drawing.*",
)
_PROVISIONAL_HEIGHT_TOKENS = (
    "provisional",
    "review",
    "unresolved",
    "needs estimator",
    "needs review",
    "inferred",
    "default",
)
_OBJECTIVE_HEIGHT_TOKENS = (
    "confirmed",
    "verified",
)


def _finite_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def _unresolved_level(levels_map: Dict[str, CanonicalLevel]) -> Tuple[CanonicalLevel, str]:
    if _UNRESOLVED_LEVEL_ID not in levels_map:
        lvl = CanonicalLevel(
            id=_UNRESOLVED_LEVEL_ID,
            name=_UNRESOLVED_LEVEL_NAME,
            level_index=0,
            elevation_m=None,
            review_state=ReviewState.REVIEW_REQUIRED,
        )
        lvl.metadata["registered_storey"] = False
        lvl.metadata["elevation_authority"] = "unresolved"
        levels_map[_UNRESOLVED_LEVEL_ID] = lvl
    return levels_map[_UNRESOLVED_LEVEL_ID], "unresolved"


def resolve_canonical_level(
    level_val: Any,
    levels_map: Dict[str, CanonicalLevel],
    diagnostics_log: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[CanonicalLevel, str]:
    """Resolve storey identity without inventing Ground or elevation 0.0.

    Identity and vertical elevation are deliberately independent.  A named
    Ground/Level 1 storey can be a useful identity while elevation remains
    unknown.  ``Ground / unregistered`` is always review-required.
    """
    diagnostics_log = diagnostics_log if diagnostics_log is not None else []

    if isinstance(level_val, str) and level_val in levels_map:
        return levels_map[level_val], level_val

    explicit_id: Optional[str] = None
    source_polygon: Optional[str] = None
    raw_name = ""
    level_index: Optional[int] = None
    elevation_m: Optional[float] = None

    if isinstance(level_val, dict):
        raw_name = str(
            level_val.get("name")
            or level_val.get("level_name")
            or level_val.get("label")
            or level_val.get("level")
            or ""
        ).strip()
        raw_id = level_val.get("id") or level_val.get("level_id") or level_val.get("storey_id")
        explicit_id = str(raw_id).strip() if raw_id is not None and str(raw_id).strip() else None
        raw_poly = level_val.get("source_polygon") or level_val.get("prism_id")
        source_polygon = str(raw_poly).strip() if raw_poly is not None and str(raw_poly).strip() else None
        raw_idx = level_val.get("level_index") if "level_index" in level_val else level_val.get("index")
        try:
            level_index = int(raw_idx) if raw_idx is not None and not isinstance(raw_idx, bool) else None
        except (TypeError, ValueError):
            level_index = None
        if "elevation_m" in level_val:
            elevation_m = _finite_float(level_val.get("elevation_m"))
        elif "ffl_m" in level_val:
            elevation_m = _finite_float(level_val.get("ffl_m"))
    elif isinstance(level_val, str):
        raw_name = level_val.strip()
    elif isinstance(level_val, (int, float)) and not isinstance(level_val, bool):
        if _finite_float(level_val) is not None:
            level_index = int(float(level_val))
            raw_name = f"Level {level_index}"

    if not raw_name and not explicit_id and not source_polygon and level_index is None:
        return _unresolved_level(levels_map)

    norm_name = raw_name.lower().strip()
    is_unregistered = "unregistered" in norm_name
    is_sheet_text = bool(raw_name) and any(re.match(pattern, norm_name) for pattern in _SHEET_TEXT_PATTERNS)
    if is_sheet_text and not explicit_id and not source_polygon and level_index is None:
        diagnostics_log.append({"type": "level_review", "reason": "sheet_text_is_not_storey_identity", "value": raw_name})
        return _unresolved_level(levels_map)

    # Strong identity hierarchy: explicit id -> source polygon/prism -> index+name
    # -> weak normalized display name.  Source polygon alone is sufficient to
    # distinguish duplicate same-name v135 storeys.
    if explicit_id:
        key = explicit_id
    elif source_polygon:
        key = f"lvl_poly_{_slug(source_polygon)}"
        if level_index is not None:
            key += f"_idx_{level_index}"
        if raw_name:
            key += f"_{_slug(raw_name)}"
    elif level_index is not None and raw_name:
        key = f"lvl_idx_{level_index}_{_slug(raw_name)}"
    elif raw_name:
        token = _slug(raw_name)
        if norm_name in {"g", "gf", "ground", "ground_floor", "ground level", "ground floor"}:
            token = "ground"
        elif norm_name in {"l1", "lvl 1", "level 1", "first"}:
            token = "level_1"
        elif norm_name in {"l2", "lvl 2", "level 2", "second"}:
            token = "level_2"
        key = f"lvl_name_{token or 'unresolved'}"
    else:
        return _unresolved_level(levels_map)

    # Never allow the special v135 label to collapse into trusted Ground.
    if is_unregistered:
        key = f"lvl_unregistered_{_slug(source_polygon or explicit_id or raw_name or level_index)}"
        elevation_m = None

    if key in levels_map:
        existing = levels_map[key]
        if elevation_m is not None and existing.elevation_m is None and not is_unregistered:
            existing.elevation_m = elevation_m
            existing.review_state = ReviewState.CONFIRMED
            existing.metadata["elevation_authority"] = "explicit_source_elevation"
        return existing, key

    has_strong_identity = bool(explicit_id or source_polygon or level_index is not None)
    if is_unregistered:
        review_state = ReviewState.REVIEW_REQUIRED
    elif elevation_m is not None:
        review_state = ReviewState.CONFIRMED
    elif has_strong_identity:
        review_state = ReviewState.INFERRED
    else:
        review_state = ReviewState.REVIEW_REQUIRED

    level = CanonicalLevel(
        id=key,
        name=raw_name or explicit_id or _UNRESOLVED_LEVEL_NAME,
        level_index=level_index if level_index is not None else 0,
        elevation_m=elevation_m,
        review_state=review_state,
    )
    level.metadata["registered_storey"] = not is_unregistered and has_strong_identity
    level.metadata["source_polygon"] = source_polygon
    level.metadata["source_level_id"] = explicit_id
    level.metadata["elevation_authority"] = (
        "explicit_source_elevation" if elevation_m is not None else "unresolved"
    )
    if is_unregistered:
        level.metadata["registered_storey"] = False
    levels_map[key] = level
    return level, key


def _height_is_provisional(wall: Dict[str, Any]) -> bool:
    status = str(wall.get("height_status") or "").strip().lower()
    confidence = str(wall.get("height_confidence") or "").strip().lower()
    if confidence == "verified":
        return False
    if any(token in status for token in _OBJECTIVE_HEIGHT_TOKENS):
        return False
    if any(token in status for token in _PROVISIONAL_HEIGHT_TOKENS):
        return True
    if confidence in {"review", "inferred", "provisional"}:
        return True
    return False


def _explicit_thickness(wall: Dict[str, Any]) -> Optional[float]:
    for key in ("thickness_m", "wall_thickness_m"):
        if key in wall:
            value = _finite_float(wall.get(key))
            return value if value is not None and value > 0 else None
    return None


def _raw_walls(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = payload.get("walls") or payload.get("registered_walls") or []
    return [row for row in rows if isinstance(row, dict)]


def _prepare_payload(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    prepared = copy.deepcopy(payload if isinstance(payload, dict) else {})
    originals: Dict[str, Dict[str, Any]] = {}

    key = "walls" if isinstance(prepared.get("walls"), list) else "registered_walls"
    walls = prepared.get(key) or []
    new_walls: List[Any] = []
    for idx, wall in enumerate(walls):
        if not isinstance(wall, dict):
            new_walls.append(wall)
            continue
        original = copy.deepcopy(wall)
        wall_ref = str(wall.get("wall_ref") or wall.get("id") or f"W-{idx+1}")
        originals[wall_ref] = original

        # Missing storey identity is represented explicitly as unresolved so
        # the legacy implementation cannot fall back to Ground.
        has_level = any(
            wall.get(field) is not None and (not isinstance(wall.get(field), str) or wall.get(field).strip())
            for field in ("level", "level_name", "level_index", "source_polygon", "prism_id", "level_id", "storey_id")
        )
        if not has_level:
            wall["level"] = {"id": _UNRESOLVED_LEVEL_ID, "name": _UNRESOLVED_LEVEL_NAME, "elevation_m": None}

        if _height_is_provisional(original):
            wall["height_m"] = None
            wall["unconstrained_height_m"] = None

        new_walls.append(wall)
    prepared[key] = new_walls

    # Preserve calibrated XY floor geometry even when storey identity is not
    # established.  It is placed in the unresolved review container instead
    # of being dropped.
    mapper_shapes = prepared.get("mapper_shapes")
    if isinstance(mapper_shapes, list):
        for shape in mapper_shapes:
            if not isinstance(shape, dict):
                continue
            if _legacy._strong_floor_level_claim(shape) is None:
                shape["level"] = {
                    "id": _UNRESOLVED_LEVEL_ID,
                    "name": _UNRESOLVED_LEVEL_NAME,
                    "elevation_m": None,
                }
                shape["_phase5m_unresolved_level"] = True

    # The normalized observation collection is the canonical evidence input.
    # Raw elevation candidates stay in the snapshot for diagnostics/fingerprint
    # but are not converted a second time.
    if isinstance(prepared.get("evidence_observations"), list) and prepared["evidence_observations"]:
        prepared["elevation_opening_candidates"] = []

    return prepared, originals


def _append_normalized_observations(project: Any, payload: Dict[str, Any]) -> None:
    seen = {obs.id for obs in getattr(project, "evidence_observations", [])}
    for idx, raw in enumerate(payload.get("evidence_observations") or []):
        if not isinstance(raw, dict):
            continue
        obs_id = str(raw.get("candidate_id") or raw.get("id") or f"evidence_{idx+1}")
        if obs_id in seen:
            continue

        source_coords = copy.deepcopy(raw.get("source_coords")) if isinstance(raw.get("source_coords"), dict) else {}
        for key in (
            "source_filename",
            "source_page",
            "drawing_title",
            "bbox_px",
            "calibration",
            "calibration_source",
            "calibration_state",
            "render_dpi",
            "extraction_method",
            "label",
            "correlation_diagnostics",
        ):
            if raw.get(key) is not None:
                source_coords[key] = copy.deepcopy(raw.get(key))

        calibration_state = raw.get("calibration_state")
        calibration_status = calibration_state if isinstance(calibration_state, str) else (
            "structured" if calibration_state is not None else None
        )
        observation = CanonicalEvidenceObservation.from_dict({
            "id": obs_id,
            "kind": raw.get("kind") or "elevation_opening_candidate",
            "workspace_id": raw.get("workspace_id") or payload.get("workspace_id"),
            "document_id": raw.get("document_id"),
            "page_id": raw.get("page_id"),
            "page_no": raw.get("source_page") if raw.get("source_page") is not None else raw.get("page_no"),
            "drawing_reference": raw.get("drawing_reference") or raw.get("drawing_ref"),
            "side": raw.get("side") or raw.get("elevation_side"),
            "level_name": raw.get("level_name") or raw.get("level"),
            "wall_ref": raw.get("wall_ref"),
            "source_coords": source_coords or None,
            "coordinate_space": raw.get("coordinate_space") or raw.get("coord_space"),
            "width_m": raw.get("width_m"),
            "height_m": raw.get("height_m"),
            "producer": raw.get("producer"),
            "producer_version": raw.get("producer_version"),
            "confidence": raw.get("confidence"),
            "review_state": ReviewState.REVIEW_REQUIRED.value,
            "reason_physical_unavailable": raw.get("reason") or raw.get("reason_physical_unavailable") or "Read-only source evidence; no physical plan host authority",
            "dimension_basis": raw.get("dimension_basis") or "unknown",
            "deduction_authority": False,
            "no_instance_creation": True,
            "calibration_status": calibration_status,
        })
        project.evidence_observations.append(observation)
        seen.add(obs_id)


def _postprocess_project(project: Any, originals: Dict[str, Dict[str, Any]]) -> None:
    for building in getattr(project, "buildings", []):
        for level in getattr(building, "levels", []):
            if level.id == _UNRESOLVED_LEVEL_ID or level.name == _UNRESOLVED_LEVEL_NAME:
                level.elevation_m = None
                level.review_state = ReviewState.REVIEW_REQUIRED
                level.metadata["registered_storey"] = False
                level.metadata["elevation_authority"] = "unresolved"
            if "unregistered" in str(level.name or "").lower():
                level.elevation_m = None
                level.review_state = ReviewState.REVIEW_REQUIRED
                level.metadata["registered_storey"] = False
                level.metadata["elevation_authority"] = "unresolved"

            for floor in getattr(level, "floors", []):
                if level.id == _UNRESOLVED_LEVEL_ID:
                    floor.review_state = ReviewState.REVIEW_REQUIRED
                    floor.takeoff_eligible = False
                    floor.metadata["level_status"] = "unresolved"

            for wall in getattr(level, "walls", []):
                ref = str(getattr(getattr(wall, "provenance", None), "wall_ref", None) or wall.id)
                original = originals.get(ref)
                if original is None and ref.startswith("wall_"):
                    original = originals.get(ref[5:])
                if original is None:
                    continue

                observed_height = _finite_float(original.get("height_m") if "height_m" in original else original.get("unconstrained_height_m"))
                wall.metadata["observed_height_m"] = observed_height
                wall.metadata["height_status"] = original.get("height_status")
                wall.metadata["height_confidence"] = original.get("height_confidence")

                if _height_is_provisional(original):
                    wall.height_m = None
                    wall.review_state = ReviewState.REVIEW_REQUIRED
                    wall.takeoff_eligible = False
                    wall.deduction_authority = False
                    for opening in wall.openings:
                        opening.deduction_authority = False
                        opening.takeoff_eligible = False
                        opening.metadata["physical_state"] = "invalid_geometry"
                        opening.metadata["physical_reason"] = "Host wall height is provisional/unresolved"

                # Canonical schema is intentionally thickness-less when the
                # producer did not supply a real thickness.
                wall.thickness_m = _explicit_thickness(original)


def planreader_to_canonical_model(
    payload: Dict[str, Any],
    is_validated_internal_workspace: bool = False,
):
    prepared, originals = _prepare_payload(payload)

    # Force the preserved implementation to use the final fail-closed resolver.
    _legacy.resolve_canonical_level = resolve_canonical_level
    project, skipped = _legacy.planreader_to_canonical_model(
        prepared,
        is_validated_internal_workspace=is_validated_internal_workspace,
    )
    _postprocess_project(project, originals)
    _append_normalized_observations(project, prepared)
    return project, skipped


def _load_json_setting(app: Any, wid: int, key: str, default: Any) -> Any:
    if not hasattr(app, "workspace_setting"):
        return default
    raw = app.workspace_setting(wid, key, default)
    if isinstance(raw, (dict, list)):
        return copy.deepcopy(raw)
    try:
        return json.loads(str(raw if raw is not None else default))
    except Exception:
        return copy.deepcopy(default)


def _normalise_persisted_v178_observations(app: Any, wid: int, snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    page_map = {
        int(page["id"]): page
        for page in snapshot.get("pages") or []
        if isinstance(page, dict) and page.get("id") is not None
    }
    page_ids = _load_json_setting(app, wid, "opening_evidence_v175_pages", [])
    if not isinstance(page_ids, list):
        return []

    producer_versions = snapshot.get("producer_versions") or {}
    observations: List[Dict[str, Any]] = []
    for page_id in page_ids:
        try:
            pid = int(page_id)
        except (TypeError, ValueError):
            continue
        page = page_map.get(pid)
        if page is None:
            continue  # ownership/live-page gate

        data = _load_json_setting(app, wid, f"opening_evidence_v175_page_{pid}", {})
        if not isinstance(data, dict):
            continue
        openings = data.get("elevation_openings") or []
        provenance = data.get("elevation_provenance") or []
        if not isinstance(openings, list):
            continue

        for idx, item in enumerate(openings):
            if not isinstance(item, dict):
                continue
            prov = provenance[idx] if isinstance(provenance, list) and idx < len(provenance) and isinstance(provenance[idx], dict) else {}
            obs_id = str(item.get("id") or item.get("candidate_id") or f"v178_p{pid}_{idx+1}")
            bbox = item.get("bbox_px")
            source_coords: Dict[str, Any] = {}
            if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
                source_coords["bbox_px"] = list(bbox[:4])
            elif isinstance(bbox, dict):
                source_coords["bbox_px"] = copy.deepcopy(bbox)
            if isinstance(item.get("calibration"), dict):
                source_coords["calibration"] = copy.deepcopy(item.get("calibration"))
            if prov.get("calibration_source") is not None:
                source_coords["calibration_source"] = copy.deepcopy(prov.get("calibration_source"))
            if prov.get("calibration_state") is not None:
                source_coords["calibration_state"] = copy.deepcopy(prov.get("calibration_state"))
            if item.get("correlation_diagnostics") is not None:
                source_coords["correlation_diagnostics"] = copy.deepcopy(item.get("correlation_diagnostics"))
            if item.get("render_dpi") is not None:
                source_coords["render_dpi"] = item.get("render_dpi")
            if item.get("extraction_method") is not None:
                source_coords["extraction_method"] = item.get("extraction_method")
            if item.get("label") is not None:
                source_coords["label"] = item.get("label")

            observations.append({
                "candidate_id": obs_id,
                "kind": "elevation_opening_candidate",
                "workspace_id": str(wid),
                "document_id": str(page.get("document_id")) if page.get("document_id") is not None else None,
                "page_id": str(pid),
                "page_no": item.get("source_page_no") if item.get("source_page_no") is not None else page.get("page_no"),
                "source_filename": prov.get("source_filename"),
                "source_page": prov.get("source_page") if prov.get("source_page") is not None else item.get("source_page_no"),
                "drawing_reference": prov.get("drawing_ref") or item.get("drawing_ref") or None,
                "drawing_title": prov.get("drawing_title") or item.get("drawing_title") or None,
                "side": prov.get("elevation_side") or item.get("elevation_side") or None,
                "level_name": prov.get("level") if prov.get("level") is not None else item.get("level"),
                "wall_ref": prov.get("wall_ref") or item.get("wall_ref") or None,
                "source_coords": source_coords or None,
                "coordinate_space": prov.get("coord_space") or item.get("coord_space") or None,
                "calibration": copy.deepcopy(item.get("calibration")) if isinstance(item.get("calibration"), dict) else None,
                "calibration_source": copy.deepcopy(prov.get("calibration_source")),
                "calibration_state": copy.deepcopy(prov.get("calibration_state")),
                "width_m": _finite_float(item.get("width_m")),
                "height_m": _finite_float(item.get("height_m")),
                "confidence": parse_optional_confidence(item.get("confidence")),
                "dimension_basis": "unknown",
                "accepted_state": True,
                "rejected_state": False,
                "producer": "pb_elevation_production_bridge_v178",
                "producer_version": producer_versions.get("v178_elevation_bridge") or producer_versions.get("v178") or "1.7.8",
                "persisted_by": "pb_opening_production_v175",
                "deduction_authority": False,
                "no_instance_creation": True,
                "reason": "Qualified persisted elevation evidence; read-only for canonical 3D",
            })
    return observations


def collect_workspace_3d_evidence(app: Any, workspace_id: int) -> Dict[str, Any]:
    wid = require_workspace_id(workspace_id)
    snapshot = _legacy.collect_workspace_3d_evidence(app, wid)
    snapshot["workspace_id"] = wid

    normalized = _normalise_persisted_v178_observations(app, wid, snapshot)
    if normalized:
        snapshot["evidence_observations"] = normalized
        # Keep raw candidates only as source evidence; canonical conversion
        # consumes normalized observations exactly once.
        snapshot["elevation_opening_candidates"] = [
            copy.deepcopy(row) for row in normalized
        ]
    return snapshot


def planreader_workspace_to_canonical(app: Any, workspace_id: int):
    wid = require_workspace_id(workspace_id)
    snapshot = collect_workspace_3d_evidence(app, wid)

    from pb_canonical_persistence import compute_workspace_source_fingerprint

    snapshot_fp = compute_workspace_source_fingerprint(snapshot)
    project, skipped = planreader_to_canonical_model(
        snapshot,
        is_validated_internal_workspace=True,
    )
    project.id = f"ws_{wid}_canonical"
    project.name = f"Workspace #{wid} Canonical BIM Model"

    diagnostics = generate_production_diagnostics_report(
        project,
        workspace_data=snapshot,
        skipped_items=skipped,
    )
    diagnostics["workspace_id"] = wid
    diagnostics["source_revision_fingerprint"] = snapshot_fp

    return WorkspaceCanonicalResult(
        project=project,
        snapshot=snapshot,
        snapshot_fingerprint=snapshot_fp,
        diagnostics=diagnostics,
        skipped_items=skipped,
    )


# Make the preserved implementation use the same final resolver for callers
# that intentionally reach into legacy helpers through this compatibility
# module during tests.
_legacy.resolve_canonical_level = resolve_canonical_level
