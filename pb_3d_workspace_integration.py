"""PlanReader canonical 3D workspace integration.

Connects the reviewed canonical building model and Three.js viewer to the production
PlanReader workspace. The estimator-facing 3D page is canonical by default. The
historical manual/reconstruction editor is retained only as an explicit developer
escape hatch and is never executed during an ordinary production 3D render.

Architecture guarantees:
1. Uses the existing ``apply(app)`` extension pattern with an idempotency guard.
2. Canonical 3D is the only default estimator-facing 3D surface.
3. The legacy editor requires both an environment feature flag and an explicit
   per-session opt-in before it is called.
4. The existing shared ``hero(workspace)`` hook still renders exactly once on the
   ordinary canonical page, preserving the Phase 6A commercial workspace shell.
5. Workspace evidence is converted through ``planreader_workspace_to_canonical``.
6. Canonical HTML caching is bounded to 10 entries per Streamlit session.
7. Canonical persistence, staleness checks and Phase 5 diagnostics remain intact.
"""

from __future__ import annotations

import os
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

from pb_bim_viewer import generate_bim_viewer_html, project_to_viewer_payload
from pb_canonical_persistence import load_workspace_canonical_model, save_workspace_canonical_model
from pb_production_3d_adapter import planreader_workspace_to_canonical, require_workspace_id

VERSION = "1.6.2"
MAX_SESSION_CACHE_ENTRIES = 10
LEGACY_EDITOR_ENV = "PLANREADER_ENABLE_LEGACY_3D_EDITOR"
_TRUTHY_ENV = {"1", "true", "yes", "on"}


def legacy_editor_feature_enabled() -> bool:
    """Return whether the developer-only legacy editor escape hatch is enabled."""
    return str(os.environ.get(LEGACY_EDITOR_ENV, "")).strip().lower() in _TRUTHY_ENV


def _workspace_id_from_context(app: Any, workspace: Any = None) -> int:
    workspace_id = None
    if isinstance(workspace, dict):
        workspace_id = workspace.get("id")
    elif isinstance(workspace, (int, str, float)):
        workspace_id = workspace
    elif hasattr(app, "current_workspace"):
        try:
            curr = app.current_workspace() if callable(app.current_workspace) else app.current_workspace
            if isinstance(curr, dict):
                workspace_id = curr.get("id")
        except Exception:
            pass
    return require_workspace_id(workspace_id)


def _render_shared_workspace_header(app: Any, workspace: Any = None) -> None:
    """Preserve the normal shared hero/commercial shell without invoking legacy 3D."""
    hero = getattr(app, "hero", None)
    if callable(hero):
        hero(workspace)


def render_workspace_3d_canonical_view(app: Any, workspace: Any = None) -> None:
    """Render the reviewed canonical 3D BIM model for the active workspace."""
    try:
        wid_int = _workspace_id_from_context(app, workspace)
    except Exception as exc:
        st.error(f"Workspace Context Error: {exc}")
        return

    try:
        ws_result = planreader_workspace_to_canonical(app, wid_int)
        project = ws_result.project
        snapshot = ws_result.snapshot
        snapshot_fp = ws_result.snapshot_fingerprint
        diagnostics = ws_result.diagnostics
    except Exception as exc:
        st.error(f"Error building 3D canonical model for workspace #{wid_int}: {exc}")
        return

    st.markdown("### Canonical 3D Model")
    st.caption(
        "Built from the reviewed PlanReader source-evidence pipeline. Unknown geometry "
        "remains unresolved rather than being filled with convenience defaults."
    )

    if project.is_synthetic_demo:
        st.warning("SYNTHETIC VIEWER DEMONSTRATION FIXTURE — NOT BENCHMARK TRUTH / NOT TAKEOFF AUTHORITATIVE")

    is_fresh, _saved_proj, status_msg, saved_payload = load_workspace_canonical_model(
        app, wid_int, current_snapshot=snapshot
    )
    if saved_payload is None:
        save_workspace_canonical_model(app, wid_int, project, snapshot=snapshot)
        is_fresh = True
        st.caption("Initial canonical model snapshot saved for this workspace.")
    elif not is_fresh:
        st.warning(status_msg)
        if st.button("Refresh 3D model from source evidence", key=f"refresh_3d_model_{wid_int}"):
            save_workspace_canonical_model(app, wid_int, project, snapshot=snapshot)
            st.success("Canonical model refreshed from current source evidence.")
            st.rerun()

    qa = diagnostics.get("estimator_qa_summary", {})
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Physical walls", qa.get("physical_walls_rendered", 0))
    c2.metric("Physical openings", qa.get("physical_openings", 0))
    c3.metric("Authorised deductions", qa.get("authorised_b5_deductions", 0))
    c4.metric("Calibrated floors", qa.get("calibrated_floors", 0))
    st.caption(f"Source revision: `{snapshot_fp}` · Saved snapshot: {'current' if is_fresh else 'stale'}")

    if "_CANONICAL_MODEL_SESSION_CACHE" not in st.session_state:
        st.session_state["_CANONICAL_MODEL_SESSION_CACHE"] = {}

    cache_dict = st.session_state["_CANONICAL_MODEL_SESSION_CACHE"]
    cache_key = (wid_int, snapshot_fp, "1.0.0")

    if cache_key in cache_dict:
        html_code = cache_dict[cache_key]
    else:
        if len(cache_dict) >= MAX_SESSION_CACHE_ENTRIES:
            oldest_key = next(iter(cache_dict))
            cache_dict.pop(oldest_key, None)

        payload = project_to_viewer_payload(project)
        html_code = generate_bim_viewer_html(payload, height_px=750)
        cache_dict[cache_key] = html_code

    components.html(html_code, height=760, scrolling=False)

    with st.expander("Estimator QA & quantity reconciliation", expanded=False):
        st.markdown("#### Per-wall quantity reconciliation")
        st.dataframe(diagnostics.get("per_wall_quantity_reconciliation", []))
        with st.expander("Technical diagnostics", expanded=False):
            st.json(diagnostics)


def _render_legacy_editor_opt_in(
    app: Any,
    original_model_page: Any,
    workspace: Any,
    args: tuple,
    kwargs: dict,
) -> None:
    """Expose the historical editor only behind a developer flag and session opt-in."""
    if not legacy_editor_feature_enabled() or not callable(original_model_page):
        return

    try:
        wid_int = _workspace_id_from_context(app, workspace)
    except Exception:
        wid_int = "unknown"

    st.markdown("---")
    st.caption("Advanced developer tools are enabled for this environment.")
    enabled = st.checkbox(
        "Enable legacy manual 3D editor for this session",
        value=False,
        key=f"legacy_3d_editor_opt_in_{wid_int}",
        help=(
            "This historical editor is not the canonical source of truth and may expose "
            "legacy convenience defaults. Use it only for controlled diagnostics."
        ),
    )
    if not enabled:
        return

    st.warning(
        "LEGACY MANUAL 3D EDITOR — NON-CANONICAL / NON-TAKEOFF-AUTHORITATIVE. "
        "Do not use legacy defaults as measured geometry."
    )
    original_model_page(workspace, *args, **kwargs)


def apply(app: Any) -> None:
    """Install the canonical estimator-facing 3D page exactly once."""
    if getattr(app, "_canonical_3d_extension_installed", False):
        return

    if not hasattr(app, "model_3d_page"):
        return

    original_model_3d = getattr(app, "model_3d_page")
    setattr(app, "_legacy_model_3d_page", original_model_3d)

    def model_3d_page_canonical_wrapper(workspace: Any = None, *args, **kwargs):
        # Preserve the normal app/project header without executing the legacy 3D page.
        _render_shared_workspace_header(app, workspace)
        # Canonical review is the only default production 3D path.
        render_workspace_3d_canonical_view(app, workspace)
        # The old editor is never executed unless two explicit developer gates pass.
        _render_legacy_editor_opt_in(app, original_model_3d, workspace, args, kwargs)

    setattr(app, "model_3d_page", model_3d_page_canonical_wrapper)
    setattr(app, "_canonical_3d_extension_installed", True)
