"""
PlanReader Canonical Building Model Persistence & Staleness Tracking Module.

Provides versioned serialization, workspace persistence, deterministic fingerprinting, and staleness detection
for generated CanonicalProject models tied to PlanReader workspaces.

SAFETY GUARANTEES:
1. Versioned schema contract (PERSISTENCE_KEY = "canonical_3d_model_v1", SCHEMA_VERSION = "1.0.0").
2. Real production set_workspace_setting API string serialization (json.dumps / json.loads).
3. Fingerprints underlying Workspace Evidence Snapshot using deterministic collection canonicalization.
4. Preserves ordered geometry (polygon vertices, points, triangles) so VERTEX ORDER changes alter fingerprint.
5. Sorts unordered collections (documents, pages, walls, takeoff_rows) by stable item identity.
6. Blocker #7: ONLY the actual geometric VALUE positions (polygon/points/triangles/vertices) are
   order-sensitive. A wall's a/b endpoint or an opening is NOT allowed to flip the whole enclosing
   dict subtree into order-sensitive mode (which previously made UNORDERED wall openings
   order-significant). roof_data is recursively canonicalized (never passed through raw).
"""

import hashlib
import json
import time
from typing import Dict, Any, Optional, Tuple
from pb_canonical_building import CanonicalProject

PERSISTENCE_KEY = "canonical_3d_model_v1"
SCHEMA_VERSION = "1.0.0"


from pb_production_3d_adapter import require_workspace_id


def _sort_item_key(item: Any) -> str:
    """Helper to derive a stable sorting key for list elements in snapshot canonicalization."""
    if isinstance(item, dict):
        for k in ("id", "candidate_id", "wall_ref", "page_id", "document_id", "setting_key", "name", "kind"):
            if item.get(k) is not None:
                return f"{k}:{item.get(k)}"
        return json.dumps(item, sort_keys=True)
    return str(item)


def canonicalize_evidence_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """
    SECTION R & 20: Canonicalizes evidence snapshot.
    Sorts UNORDERED collections (documents, pages, registered_walls, evidence_observations, takeoff_rows) by stable identity.
    PRESERVES ORDERED GEOMETRY (polygon vertices, wall endpoints, triangles) so geometric vertex order changes alter fingerprint!
    """
    if not isinstance(snapshot, dict):
        return {}

    def _canonicalize_unordered_list(lst: list) -> list:
        try:
            return sorted([_canonicalize_obj(x) for x in lst], key=_sort_item_key)
        except Exception:
            return [_canonicalize_obj(x) for x in lst]

    _GEOM_LIST_KEYS = frozenset({"polygon", "points", "triangles", "vertices", "source_coords"})

    def _canonicalize_obj(obj: Any, is_ordered_geometry: bool = False) -> Any:
        if isinstance(obj, dict):
            return {
                k: _canonicalize_obj(v, is_ordered_geometry=(k in _GEOM_LIST_KEYS))
                for k, v in sorted(obj.items())
            }
        elif isinstance(obj, list):
            if is_ordered_geometry:
                # Preserve every nested list in a geometry value.  This keeps
                # [x, y] coordinate order and [i, j, k] triangle winding as
                # well as the outer polygon/point/triangle sequence.
                return [_canonicalize_obj(x, is_ordered_geometry=True) for x in obj]
            return _canonicalize_unordered_list(obj)
        return obj

    clean_snapshot = {
        "workspace_metadata": snapshot.get("workspace_metadata"),
        "documents": _canonicalize_unordered_list(snapshot.get("documents") or []),
        "pages": _canonicalize_unordered_list(snapshot.get("pages") or []),
        "registered_walls": _canonicalize_unordered_list(snapshot.get("registered_walls") or []),
        "mapper_shapes": _canonicalize_unordered_list(snapshot.get("mapper_shapes") or []),
        "evidence_observations": _canonicalize_unordered_list(snapshot.get("evidence_observations") or []),
        "roof_data": _canonicalize_obj(snapshot.get("roof_data")) if snapshot.get("roof_data") is not None else None,
        "takeoff_rows": _canonicalize_unordered_list(snapshot.get("takeoff_rows") or []),
        "producer_versions": snapshot.get("producer_versions"),
    }

    return clean_snapshot


def compute_workspace_source_fingerprint(snapshot: Dict[str, Any]) -> str:
    """
    SECTION R: Computes a deterministic SHA-256 fingerprint representing the current
    revision of the Workspace Evidence Snapshot after collection canonicalization.
    """
    canonical_dict = canonicalize_evidence_snapshot(snapshot)
    serialized = json.dumps(canonical_dict, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def save_workspace_canonical_model(
    app: Any,
    workspace_id: int,
    project: CanonicalProject,
    snapshot: Optional[Dict[str, Any]] = None,
    workspace_data: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """SECTION 19 & S: Saves canonical model to workspace settings with strict require_workspace_id validation."""
    wid = require_workspace_id(workspace_id)

    if project.is_synthetic_demo:
        raise ValueError("Cannot persist synthetic demonstration data to production workspace storage.")

    effective_snapshot = snapshot or workspace_data
    if not effective_snapshot:
        raise ValueError("Cannot persist canonical model without a valid source evidence snapshot.")

    fingerprint = compute_workspace_source_fingerprint(effective_snapshot)

    persistence_payload = {
        "schema_version": SCHEMA_VERSION,
        "persistence_key": PERSISTENCE_KEY,
        "generation_timestamp": time.time(),
        "workspace_id": wid,
        "source_revision_fingerprint": fingerprint,
        "producer_versions": {
            "3d_engine": "v1.5.1",
            "v127_mapper": "v127",
            "v128_mapper": "v128",
            "v135_levels": "v135",
            "v139_walls": "v139",
            "v140_roof": "v140",
            "v175_openings": "v175",
            "v178_elevation_bridge": "v178",
            "v172_elevation_evidence": "v172",
        },
        "model_data": project.to_dict(),
    }

    json_str = json.dumps(persistence_payload, sort_keys=True, indent=2)

    if app and hasattr(app, "set_workspace_setting"):
        app.set_workspace_setting(wid, PERSISTENCE_KEY, json_str)

    return persistence_payload


def load_workspace_canonical_model(
    app: Any,
    workspace_id: int,
    current_snapshot: Optional[Dict[str, Any]] = None,
    current_workspace_data: Optional[Dict[str, Any]] = None
) -> Tuple[bool, Optional[CanonicalProject], str, Optional[Dict[str, Any]]]:
    """SECTION 19, Z & S: Loads persisted canonical model with strict require_workspace_id validation."""
    try:
        wid = require_workspace_id(workspace_id)
    except Exception as e:
        return False, None, f"Invalid workspace ID: {e}", None

    if not (app and hasattr(app, "workspace_setting")):
        return False, None, "No workspace settings interface available", None

    raw_setting = app.workspace_setting(wid, PERSISTENCE_KEY, None)
    if raw_setting is None:
        return False, None, "No persisted canonical model found in workspace", None

    if isinstance(raw_setting, str):
        try:
            saved_payload = json.loads(raw_setting)
        except Exception as e:
            return False, None, f"Corrupted JSON in persistence setting: {e}", None
    elif isinstance(raw_setting, dict):
        saved_payload = raw_setting
    else:
        return False, None, "Invalid persistence payload format", None

    if saved_payload.get("persistence_key") != PERSISTENCE_KEY:
        return False, None, f"Invalid persistence key: {saved_payload.get('persistence_key')}", None

    if saved_payload.get("schema_version") != SCHEMA_VERSION:
        return False, None, f"Schema version mismatch: expected {SCHEMA_VERSION}, got {saved_payload.get('schema_version')}", saved_payload

    saved_wid = saved_payload.get("workspace_id")
    try:
        valid_saved_wid = require_workspace_id(saved_wid)
        if valid_saved_wid != wid:
            return False, None, f"Workspace ID mismatch: expected {wid}, got {valid_saved_wid}", None
    except Exception as e:
        return False, None, f"Corrupt or invalid workspace_id in saved model: {e}", None

    saved_fp = saved_payload.get("source_revision_fingerprint")
    if not saved_fp or not isinstance(saved_fp, str) or not saved_fp.strip():
        return False, None, "Missing or invalid source_revision_fingerprint in saved model payload", saved_payload

    proj_dict = saved_payload.get("model_data")
    if not isinstance(proj_dict, dict):
        return False, None, "Missing or invalid model_data in payload", None

    project = CanonicalProject.from_dict(proj_dict)

    effective_snapshot = current_snapshot or current_workspace_data
    if effective_snapshot:
        current_fp = compute_workspace_source_fingerprint(effective_snapshot)
        if saved_fp != current_fp:
            return False, project, f"⚠️ Stale saved model detected (Saved FP: {saved_fp}, Current FP: {current_fp})", saved_payload

    return True, project, "Persisted model is fresh and up-to-date", saved_payload
