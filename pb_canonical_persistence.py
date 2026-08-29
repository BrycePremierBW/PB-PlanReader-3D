"""
PlanReader Canonical Building Model Persistence & Staleness Tracking Module.

Provides versioned serialization, fingerprinting, and staleness detection
for generated CanonicalProject models tied to PlanReader workspaces.

SAFETY GUARANTEES:
1. Versioned schema contract (schema_version = "1.0.0").
2. Deterministic serialization and deserialization.
3. Fingerprints underlying PlanReader workspace state (revision hash).
4. Detects stale persisted models when workspace evidence changes.
5. NEVER persists synthetic demo fixture data into a production workspace store.
"""

import hashlib
import json
import time
from typing import Dict, Any, Optional, Tuple
from pb_canonical_building import CanonicalProject, parse_strict_bool

SCHEMA_VERSION = "1.0.0"


def compute_workspace_fingerprint(workspace_data: Dict[str, Any]) -> str:
    """
    Computes a deterministic SHA-256 fingerprint representing the current revision
    of underlying PlanReader workspace evidence (takeoff rows, openings, pages).
    """
    if not isinstance(workspace_data, dict):
        return "empty_workspace_fingerprint"

    core_evidence = {
        "workspace_id": workspace_data.get("id") or workspace_data.get("workspace_id"),
        "pages": workspace_data.get("pages") or workspace_data.get("document_pages"),
        "takeoff_rows": workspace_data.get("takeoff_rows") or workspace_data.get("lines"),
        "openings": workspace_data.get("openings") or workspace_data.get("opening_schedule"),
        "polygons": workspace_data.get("floor_polygons") or workspace_data.get("polygons"),
    }

    evidence_str = json.dumps(core_evidence, sort_keys=True, default=str)
    return hashlib.sha256(evidence_str.encode("utf-8")).hexdigest()[:16]


def save_canonical_project_to_dict(project: CanonicalProject, workspace_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Serializes a CanonicalProject into a versioned persistence dictionary payload.
    Fails closed: Refuses to persist synthetic demo data into production workspace stores.
    """
    if project.is_synthetic_demo:
        raise ValueError("Cannot persist synthetic demonstration data to production workspace storage.")

    fingerprint = compute_workspace_fingerprint(workspace_data) if workspace_data else "untracked_revision"

    return {
        "schema_version": SCHEMA_VERSION,
        "saved_timestamp": time.time(),
        "workspace_id": project.id,
        "revision_fingerprint": fingerprint,
        "producer": "PlanReader Production 3D Engine v1.0",
        "project_data": project.to_dict(),
    }


def load_canonical_project_from_dict(saved_payload: Dict[str, Any], current_workspace_data: Optional[Dict[str, Any]] = None) -> Tuple[bool, Optional[CanonicalProject], str]:
    """
    Deserializes a versioned persistence dictionary back into a CanonicalProject.
    Returns (is_valid, project_instance, status_message).
    Detects stale saved models when underlying workspace evidence has changed.
    """
    if not isinstance(saved_payload, dict):
        return False, None, "Invalid persistence payload format"

    schema_ver = saved_payload.get("schema_version")
    if schema_ver != SCHEMA_VERSION:
        return False, None, f"Schema version mismatch: expected {SCHEMA_VERSION}, got {schema_ver}"

    proj_dict = saved_payload.get("project_data")
    if not isinstance(proj_dict, dict):
        return False, None, "Missing project_data object"

    project = CanonicalProject.from_dict(proj_dict)

    if current_workspace_data:
        saved_fp = saved_payload.get("revision_fingerprint")
        current_fp = compute_workspace_fingerprint(current_workspace_data)
        if saved_fp != current_fp:
            return False, project, f"Stale model detected (Saved FP: {saved_fp}, Current FP: {current_fp})"

    return True, project, "Model loaded cleanly and is up-to-date"
