"""Production 3D adapter import surface for final Phase 5M semantics.

The substantial implementation is isolated in ``pb_production_3d_adapter_phase5m``.
This small surface adds compatibility with earlier call sites without restoring
unsafe assumptions (especially implicit Ground = global Z 0).
"""
from __future__ import annotations

import copy
from typing import Any, Dict

import pb_production_3d_adapter_phase5m as _phase5m
from pb_production_3d_adapter_phase5m import *  # noqa: F401,F403

from pb_canonical_building import CanonicalEvidenceObservation, ReviewState
from pb_3d_diagnostics import generate_production_diagnostics_report

require_workspace_id = _phase5m.require_workspace_id
WorkspaceCanonicalResult = _phase5m.WorkspaceCanonicalResult


def _norm_name(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def resolve_canonical_level(level_val: Any, levels_map: Dict[str, Any], diagnostics_log=None):
    """Reuse one objectively declared level by name, otherwise stay fail-closed.

    A weak name can identify an already-declared unique storey, but it can never
    create an elevation. Duplicate same-name storeys remain ambiguous and are not
    collapsed. This preserves Phase 5M zero-made-up-data while supporting v135/v140
    records that carry only the display name after a level registry is established.
    """
    if isinstance(level_val, str):
        raw = _norm_name(level_val)
        if raw and "unregistered" not in raw:
            matches = [
                (key, level)
                for key, level in levels_map.items()
                if _norm_name(getattr(level, "name", None)) == raw
            ]
            if len(matches) == 1:
                key, level = matches[0]
                return level, key
    return _phase5m.resolve_canonical_level(level_val, levels_map, diagnostics_log)


def _is_free_form_floor_level_claim(shape: Dict[str, Any]) -> bool:
    weak = shape.get("_source_level")
    if not isinstance(weak, str) or not weak.strip():
        return False
    for key in ("storey_id", "level_id", "level", "level_name"):
        value = shape.get(key)
        if value is not None and (not isinstance(value, str) or value.strip()):
            return False
    return True


def planreader_to_canonical_model(payload: Dict[str, Any], is_validated_internal_workspace: bool = False):
    prepared = copy.deepcopy(payload if isinstance(payload, dict) else {})
    rejected_weak_floors = []
    shapes = prepared.get("mapper_shapes")
    if isinstance(shapes, list):
        kept = []
        for shape in shapes:
            if isinstance(shape, dict) and _is_free_form_floor_level_claim(shape):
                rejected_weak_floors.append(shape)
            else:
                kept.append(shape)
        prepared["mapper_shapes"] = kept

    # The underlying implementation patches its preserved legacy converter with
    # whatever resolver is installed here at call time.
    _phase5m.resolve_canonical_level = resolve_canonical_level
    _phase5m._legacy.resolve_canonical_level = resolve_canonical_level
    project, skipped = _phase5m.planreader_to_canonical_model(
        prepared,
        is_validated_internal_workspace=is_validated_internal_workspace,
    )

    for index, shape in enumerate(rejected_weak_floors):
        floor_id = str(shape.get("box_id") or shape.get("id") or f"weak_floor_{index+1}")
        reason = "Mapper floor has no explicit storey identity; free-form _source_level is not authority"
        project.evidence_observations.append(CanonicalEvidenceObservation.from_dict({
            "id": floor_id,
            "kind": "unresolved_floor_level",
            "workspace_id": prepared.get("workspace_id"),
            "document_id": shape.get("document_id"),
            "page_id": shape.get("page_id"),
            "level_name": shape.get("_source_level_label") or shape.get("_source_level"),
            "producer": "pb_floor_mapper_v128",
            "producer_version": "v128",
            "review_state": ReviewState.REVIEW_REQUIRED.value,
            "reason_physical_unavailable": reason,
            "deduction_authority": False,
            "no_instance_creation": True,
        }))
        skipped.append({"id": floor_id, "type": "FLOOR", "reason": reason})
    return project, skipped


def collect_workspace_3d_evidence(app: Any, workspace_id: int) -> Dict[str, Any]:
    snapshot = _phase5m.collect_workspace_3d_evidence(app, workspace_id)
    versions = snapshot.get("producer_versions") or {}
    for obs in snapshot.get("evidence_observations") or []:
        if not isinstance(obs, dict):
            continue
        if obs.get("persisted_by") == "pb_opening_production_v175" or obs.get("producer") == "pb_elevation_production_bridge_v178":
            coords = copy.deepcopy(obs.get("source_coords")) if isinstance(obs.get("source_coords"), dict) else {}
            coords.setdefault("source_producer", "pb_elevation_production_bridge_v178")
            coords.setdefault("source_producer_version", obs.get("producer_version") or versions.get("v178_elevation_bridge") or "1.7.8")
            obs["source_coords"] = coords
            # The persisted record itself is produced by v175; its extraction
            # origin remains explicitly preserved above rather than being lost.
            obs["producer"] = "pb_opening_production_v175"
            obs["producer_version"] = versions.get("v175_openings") or "1.7.5"
    return snapshot


def planreader_workspace_to_canonical(app: Any, workspace_id: int):
    wid = require_workspace_id(workspace_id)
    snapshot = collect_workspace_3d_evidence(app, wid)
    from pb_canonical_persistence import compute_workspace_source_fingerprint

    fingerprint = compute_workspace_source_fingerprint(snapshot)
    project, skipped = planreader_to_canonical_model(snapshot, is_validated_internal_workspace=True)
    project.id = f"ws_{wid}_canonical"
    project.name = f"Workspace #{wid} Canonical BIM Model"
    diagnostics = generate_production_diagnostics_report(project, workspace_data=snapshot, skipped_items=skipped)
    diagnostics["workspace_id"] = wid
    diagnostics["source_revision_fingerprint"] = fingerprint
    return WorkspaceCanonicalResult(
        project=project,
        snapshot=snapshot,
        snapshot_fingerprint=fingerprint,
        diagnostics=diagnostics,
        skipped_items=skipped,
    )


def __getattr__(name: str):
    return getattr(_phase5m, name)
