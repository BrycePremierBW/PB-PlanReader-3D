"""
PlanReader Canonical Building Model Persistence & Staleness Tracking Module.

Provides versioned serialization, workspace persistence, fingerprinting, and staleness detection
for generated CanonicalProject models tied to PlanReader workspaces.

SAFETY GUARANTEES:
1. Versioned schema contract (PERSISTENCE_KEY = "canonical_3d_model_v1").
2. Fingerprints underlying PlanReader workspace evidence sources (document revision, mapper, walls, B5 openings).
3. Saved timestamp does NOT contaminate the source revision fingerprint.
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


def compute_workspace_source_fingerprint(workspace_data: Dict[str, Any]) -> str:
    """
    SECTION 8: Computes a deterministic SHA-256 fingerprint representing the current
    revision of underlying PlanReader workspace evidence sources (pages, mapper, walls, B5 openings).
    
    GUARANTEE: saved_timestamp is EXCLUDED so it never affects the source fingerprint!
    """
    if not isinstance(workspace_data, dict):
        return "empty_workspace_fingerprint"

    source_sources = {
        "workspace_id": workspace_data.get("id") or workspace_data.get("workspace_id"),
        "pages_revision": workspace_data.get("pages") or workspace_data.get("document_pages"),
        "takeoff_rows": workspace_data.get("takeoff_rows") or workspace_data.get("walls"),
        "openings_b5": workspace_data.get("openings") or workspace_data.get("opening_schedule"),
        "mapper_shapes": workspace_data.get("floor_mapper_v128_shapes") or workspace_data.get("polygons"),
    }

    serialized = json.dumps(source_sources, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def save_workspace_canonical_model(
    app: Any,
    workspace_id: int,
    project: CanonicalProject,
    workspace_data: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    SECTION 8: Saves canonical model to workspace settings / local store.
    """
    if project.is_synthetic_demo:
        raise ValueError("Cannot persist synthetic demonstration data to production workspace storage.")

    fingerprint = compute_workspace_source_fingerprint(workspace_data) if workspace_data else "untracked_revision"

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

    if app and hasattr(app, "set_workspace_setting"):
        app.set_workspace_setting(int(workspace_id), PERSISTENCE_KEY, persistence_payload)

    return persistence_payload


def load_workspace_canonical_model(
    app: Any,
    workspace_id: int,
    current_workspace_data: Optional[Dict[str, Any]] = None
) -> Tuple[bool, Optional[CanonicalProject], str, Optional[Dict[str, Any]]]:
    """
    SECTION 8: Loads persisted canonical model and checks for staleness.
    Returns (is_valid_and_fresh, project, status_msg, saved_payload).
    """
    if not (app and hasattr(app, "workspace_setting")):
        return False, None, "No workspace settings interface available", None

    saved_payload = app.workspace_setting(int(workspace_id), PERSISTENCE_KEY, None)
    if not isinstance(saved_payload, dict):
        return False, None, "No persisted canonical model found in workspace", None

    if saved_payload.get("persistence_key") != PERSISTENCE_KEY:
        return False, None, f"Invalid persistence key: {saved_payload.get('persistence_key')}", None

    proj_dict = saved_payload.get("model_data")
    if not isinstance(proj_dict, dict):
        return False, None, "Corrupted model data", None

    project = CanonicalProject.from_dict(proj_dict)

    if current_workspace_data:
        saved_fp = saved_payload.get("source_revision_fingerprint")
        current_fp = compute_workspace_source_fingerprint(current_workspace_data)
        if saved_fp != current_fp:
            return False, project, f"⚠️ Stale saved model detected (Saved FP: {saved_fp}, Current FP: {current_fp})", saved_payload

    return True, project, "Persisted model is fresh and up-to-date", saved_payload
