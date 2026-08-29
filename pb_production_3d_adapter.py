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

from pb_3d_diagnostics import generate_production_diagnostics_report

require_workspace_id = _phase5m.require_workspace_id
WorkspaceCanonicalResult = _phase5m.WorkspaceCanonicalResult

# Capture the fail-closed Phase 5M resolver BEFORE this compatibility surface
# is patched into the preserved legacy converter.  The wrapper below may be
# installed as _phase5m.resolve_canonical_level for legacy call sites, so it
# must never call that mutable module attribute or it would recurse into itself.
_BASE_RESOLVE_CANONICAL_LEVEL = _phase5m.resolve_canonical_level


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
    return _BASE_RESOLVE_CANONICAL_LEVEL(level_val, levels_map, diagnostics_log)


def planreader_to_canonical_model(payload: Dict[str, Any], is_validated_internal_workspace: bool = False):
    prepared = copy.deepcopy(payload if isinstance(payload, dict) else {})

    # Install the compatibility resolver into both module layers. It delegates
    # to the captured immutable Phase 5M base resolver, so this does not recurse.
    # Do not pre-filter mapper floors here: the Phase 5M preparation layer keeps
    # calibrated metric XY geometry and assigns an unresolved/review storey when
    # no trusted level identity exists. Weak sheet text is provenance, not storey
    # authority, but it is also not a reason to discard valid XY geometry.
    _phase5m.resolve_canonical_level = resolve_canonical_level
    _phase5m._legacy.resolve_canonical_level = resolve_canonical_level
    return _phase5m.planreader_to_canonical_model(
        prepared,
        is_validated_internal_workspace=is_validated_internal_workspace,
    )


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
