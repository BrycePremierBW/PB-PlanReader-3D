"""Production 3D adapter import surface.

Phase 5M implementation lives in :mod:`pb_production_3d_adapter_phase5m`.
Keeping this wrapper small makes static analysis and downstream imports
straightforward while the prior full implementation remains available in the
explicit legacy module for audit/history.
"""
from __future__ import annotations

import pb_production_3d_adapter_phase5m as _phase5m
from pb_production_3d_adapter_phase5m import *  # noqa: F401,F403

# Explicit aliases for static analyzers and common direct imports.  Private
# compatibility symbols are resolved through __getattr__ below.
require_workspace_id = _phase5m.require_workspace_id
WorkspaceCanonicalResult = _phase5m.WorkspaceCanonicalResult
resolve_canonical_level = _phase5m.resolve_canonical_level
planreader_to_canonical_model = _phase5m.planreader_to_canonical_model
collect_workspace_3d_evidence = _phase5m.collect_workspace_3d_evidence
planreader_workspace_to_canonical = _phase5m.planreader_workspace_to_canonical


def __getattr__(name: str):
    return getattr(_phase5m, name)
