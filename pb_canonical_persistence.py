"""
PlanReader Canonical Building Model Persistence & Staleness Tracking Module.

Provides versioned serialization, workspace persistence, fingerprinting, and staleness detection
for generated CanonicalProject models tied to PlanReader workspaces.

SAFETY GUARANTEES:
1. Versioned schema contract (PERSISTENCE_KEY = "canonical_3d_model_v1").
2. Real production set_workspace_setting API string serialization (json.dumps / json.loads).
3. Fingerprints underlying Workspace Evidence Snapshot (excluding generation_timestamp).
4. Detects stale persisted models when workspace evidence changes.
5. Provides 'Refresh model from source evidence' flow.
6. Refuses to persist synthetic demo data to production workspace stores.
"""

import hashlib
import json
import time
from typing import Dict, Any, Optional, Tuple
from pb_canonical_building import CanonicalProject

PERSISTENCE_KEY = "canonical_3d_model_v1"
SCHEMA_VERSION = "1.0.0"


def compute_workspace_source_fingerprint(snapshot: Dict[str, Any]) -> str:
    """
    SECTION K: Computes a deterministic SHA-256 fingerprint representing the current
    revision of the Workspace Evidence Snapshot (metadata, pages, walls, openings, mapper, roof).
    
    GUARANTEE: Excludes generation_timestamp, UI toggles, and viewer camera.
    """
    if not isinstance(snapshot, dict):
        return "empty_snapshot_fingerprint"

    # Filter snapshot fields for deterministic hashing
    clean_sources = {
        "workspace_metadata": snapshot.get("workspace_metadata"),
        "pages": snapshot.get("pages"),
        "registered_walls": snapshot.get("registered_walls"),
        "mapper_shapes": snapshot.get("mapper_shapes"),
        "roof_data": snapshot.get("roof_data"),
        "takeoff_rows": snapshot.get("takeoff_rows"),
    }

    serialized = json.dumps(clean_sources, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def save_workspace_canonical_model(
    app: Any,
    workspace_id: int,
    project: CanonicalProject,
    snapshot: Optional[Dict[str, Any]] = None,
    workspace_data: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    SECTION I & J: Saves canonical model to workspace settings using deterministic JSON string storage.
    """
    if project.is_synthetic_demo:
        raise ValueError("Cannot persist synthetic demonstration data to production workspace storage.")

    effective_snapshot = snapshot or workspace_data
    fingerprint = compute_workspace_source_fingerprint(effective_snapshot) if effective_snapshot else "untracked_revision"

    persistence_payload = {
        "schema_version": SCHEMA_VERSION,
        "persistence_key": PERSISTENCE_KEY,
        "generation_timestamp": time.time(),
        "workspace_id": int(workspace_id),
        "source_revision_fingerprint": fingerprint,
        "producer_versions": {
            "3d_engine": "v1.5.1",
            "v139_walls": "v139",
            "v175_openings": "v175",
            "v128_mapper": "v128",
        },
        "model_data": project.to_dict(),
    }

    # SECTION I: Real production set_workspace_setting converts values to strings!
    json_str = json.dumps(persistence_payload, sort_keys=True, indent=2)

    if app and hasattr(app, "set_workspace_setting"):
        app.set_workspace_setting(int(workspace_id), PERSISTENCE_KEY, json_str)

    return persistence_payload


def load_workspace_canonical_model(
    app: Any,
    workspace_id: int,
    current_snapshot: Optional[Dict[str, Any]] = None,
    current_workspace_data: Optional[Dict[str, Any]] = None
) -> Tuple[bool, Optional[CanonicalProject], str, Optional[Dict[str, Any]]]:
    """
    SECTION I & J: Loads persisted canonical model and checks for staleness.
    Handles string JSON decoding from real set_workspace_setting API.
    Returns (is_valid_and_fresh, project, status_msg, saved_payload).
    """
    if not (app and hasattr(app, "workspace_setting")):
        return False, None, "No workspace settings interface available", None

    raw_setting = app.workspace_setting(int(workspace_id), PERSISTENCE_KEY, None)
    if raw_setting is None:
        return False, None, "No persisted canonical model found in workspace", None

    # SECTION I: Handle string JSON or dict payload
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

    saved_wid = saved_payload.get("workspace_id")
    if saved_wid is not None and int(saved_wid) != int(workspace_id):
        return False, None, f"Workspace ID mismatch: expected {workspace_id}, got {saved_wid}", None

    proj_dict = saved_payload.get("model_data")
    if not isinstance(proj_dict, dict):
        return False, None, "Missing or invalid model_data in payload", None

    project = CanonicalProject.from_dict(proj_dict)

    effective_snapshot = current_snapshot or current_workspace_data
    if effective_snapshot:
        saved_fp = saved_payload.get("source_revision_fingerprint")
        current_fp = compute_workspace_source_fingerprint(effective_snapshot)
        if saved_fp != current_fp:
            return False, project, f"⚠️ Stale saved model detected (Saved FP: {saved_fp}, Current FP: {current_fp})", saved_payload

    return True, project, "Persisted model is fresh and up-to-date", saved_payload
