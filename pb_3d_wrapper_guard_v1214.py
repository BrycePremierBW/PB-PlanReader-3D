"""PlanReader v1.2.14 3D wrapper guard.

The v1.2.11 Takeoff Studio and v1.2.12 Surface Editor were both implemented as
wrappers around ``app.model_3d_page``. v1.2.13 then added a new four-tab 3D
workspace and used the already-wrapped page for its "Existing 3D Tools" tab.
Because Streamlit executes every tab body on each rerun, that caused Takeoff
Studio to be rendered twice with identical widget keys.

This patch unwraps the historical wrapper chain once, stores the original core
3D page, and rebuilds the v1.2.13 four-tab shell so each UI layer is rendered
exactly once.
"""
from __future__ import annotations

from typing import Any, Callable, Set

import pb_3d_quickstart_v1213 as quick_v1213
import pb_3d_surface_editor_v1212 as surface_v1212
import pb_takeoff_studio_v1211 as studio_v1211


def unwrap_core_model_page(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Follow the known PlanReader model-page wrapper closures to the core page.

    Each historical wrapper captured the previous page in a closure named
    ``base_model_page`` or ``base_model_3d_page``. Following only those named
    closure cells avoids accidentally unwrapping unrelated callables.
    """
    current = fn
    seen: Set[int] = set()
    while callable(current) and id(current) not in seen:
        seen.add(id(current))
        code = getattr(current, "__code__", None)
        closure = getattr(current, "__closure__", None)
        if code is None or not closure:
            break

        next_page = None
        for name, cell in zip(code.co_freevars, closure):
            if name not in {"base_model_page", "base_model_3d_page"}:
                continue
            try:
                candidate = cell.cell_contents
            except ValueError:
                continue
            if callable(candidate):
                next_page = candidate
                break

        if next_page is None or next_page is current:
            break
        current = next_page

    return current


def apply(app: Any) -> None:
    """Install the duplicate-safe v1.2.14 3D workspace."""
    if getattr(app, "_pb_3d_wrapper_guard_v1214_applied", False):
        return
    app._pb_3d_wrapper_guard_v1214_applied = True

    current_page = app.model_3d_page
    core_page = unwrap_core_model_page(current_page)
    app._pb_core_model_3d_page = core_page
    app.unwrap_core_model_page = unwrap_core_model_page

    def _v1214_model_page(workspace, session_api_key="", ai_provider="OpenAI"):
        app.hero(workspace)
        tabs = app.st.tabs([
            "⚡ Quick 3D Build",
            "🎨 3D Surface Editor",
            "📐 Takeoff Studio",
            "🧰 Existing 3D Tools",
        ])

        with tabs[0]:
            quick_v1213.quick_build_panel(app, workspace, session_api_key, ai_provider)

        with tabs[1]:
            app.st.markdown("### 3D Surface Editor")
            app.st.caption(
                "Click or select a real model face, then assign substrate, inclusion status, progress and notes. "
                "Face m² comes directly from the current 3D geometry."
            )
            surface_v1212.surface_editor_panel(app, workspace)

        with tabs[2]:
            app.st.markdown("### Takeoff Studio")
            studio_v1211._studio_panel(app, workspace)

        with tabs[3]:
            app.st.caption(
                "Original PlanReader 3D model, masses, openings, render-reading and export tools."
            )
            original_hero = app.hero
            app.hero = lambda *_args, **_kwargs: None
            try:
                core_page(workspace, session_api_key, ai_provider)
            finally:
                app.hero = original_hero

    app.model_3d_page = _v1214_model_page
