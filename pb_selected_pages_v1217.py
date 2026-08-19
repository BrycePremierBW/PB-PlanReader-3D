"""PlanReader v1.2.17 selected-page scope guard.

Take-off workflows must respect the page selection made during plan processing.
Unselected sheets remain in the drawing register, but they are excluded from the
No-AI geometry builder and from page queries made by the optional AI take-off
workflow.
"""
from __future__ import annotations

import re
import types
from typing import Any, Callable, Dict, List

import pb_no_ai_takeoff_v1216 as noai_v1216


def _has_selected_predicate(sql: str) -> bool:
    low = str(sql or "").lower()
    return bool(
        re.search(
            r"(?:coalesce\s*\([^)]*selected[^)]*\)|(?:\b\w+\.)?selected)\s*=\s*(?:1|true)",
            low,
        )
    )


def scope_takeoff_page_sql(sql: str) -> str:
    """Add the selected-page predicate to page queries used by Take-off.

    This is intentionally narrow: only SQL that reads the ``pages`` table and
    contains a workspace predicate is modified. Queries that already filter on
    selected pages are left unchanged.
    """
    text = str(sql or "")
    low = text.lower()
    if "from pages" not in low and "join pages" not in low:
        return text
    if _has_selected_predicate(text):
        return text

    # Aliased page queries used by Source & basis / drawing-register helpers.
    if re.search(r"\b(?:from|join)\s+pages\s+p\b", text, flags=re.IGNORECASE):
        scoped, count = re.subn(
            r"\bWHERE\s+p\.workspace_id\s*=\s*\?",
            "WHERE p.workspace_id=? AND COALESCE(p.selected,0)=1",
            text,
            count=1,
            flags=re.IGNORECASE,
        )
        if count:
            return scoped

    # Direct page query used by the AI page picker and similar take-off panels.
    if re.search(r"\bFROM\s+pages\b", text, flags=re.IGNORECASE):
        scoped, count = re.subn(
            r"\bWHERE\s+workspace_id\s*=\s*\?",
            "WHERE workspace_id=? AND COALESCE(selected,0)=1",
            text,
            count=1,
            flags=re.IGNORECASE,
        )
        if count:
            return scoped

    return text


def build_selected_no_ai_rows(app: Any, workspace_id: int) -> List[Dict[str, Any]]:
    """Build the deterministic draft from geometry on selected pages only."""
    zones = app.lquery(
        """SELECT z.*,p.page_label,p.page_type,p.px_per_m AS page_px_per_m
           FROM mapped_zones z
           JOIN pages p ON p.id=z.page_id AND p.workspace_id=z.workspace_id
           WHERE z.workspace_id=? AND COALESCE(p.selected,0)=1
           ORDER BY z.id""",
        (workspace_id,),
    )
    measurements = app.lquery(
        """SELECT m.*,p.page_label,p.page_type,p.px_per_m AS page_px_per_m
           FROM measurement_lines m
           JOIN pages p ON p.id=m.page_id AND p.workspace_id=m.workspace_id
           WHERE m.workspace_id=? AND COALESCE(p.selected,0)=1
             AND (m.takeoff_row_id IS NULL OR m.takeoff_row_id=0)
           ORDER BY m.id""",
        (workspace_id,),
    )

    rows: List[Dict[str, Any]] = []
    for zone in zones:
        row = noai_v1216.zone_to_takeoff_row(dict(zone))
        if row:
            rows.append(row)
    for measurement in measurements:
        row = noai_v1216.measurement_to_takeoff_row(dict(measurement))
        if row:
            rows.append(row)
    return rows


def _find_core_subscription_page(wrapper: Callable[..., Any]) -> Callable[..., Any]:
    """Find the pre-v1.2.16 Subscription Take-off function in its closure."""
    seen: set[int] = set()

    def walk(fn: Callable[..., Any]) -> Callable[..., Any] | None:
        marker = id(fn)
        if marker in seen:
            return None
        seen.add(marker)
        if getattr(fn, "__name__", "") == "subscription_takeoff_page":
            return fn
        for cell in getattr(fn, "__closure__", ()) or ():
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if callable(value):
                found = walk(value)
                if found is not None:
                    return found
        return None

    found = walk(wrapper)
    if found is None:
        raise RuntimeError("Could not locate the core Subscription Take-off page.")
    return found


def clone_selected_subscription_page(app: Any, core_page: Callable[..., Any]) -> Callable[..., Any]:
    """Clone the core page with page-query functions scoped to selected sheets.

    A cloned globals dictionary avoids temporarily monkey-patching the shared app
    module, so separate Streamlit sessions cannot interfere with one another.
    """
    base_ldf = app.ldf
    base_lquery = app.lquery

    def selected_ldf(sql: str, params=()):
        return base_ldf(scope_takeoff_page_sql(sql), params)

    def selected_lquery(sql: str, params=()):
        return base_lquery(scope_takeoff_page_sql(sql), params)

    namespace = dict(core_page.__globals__)
    namespace["ldf"] = selected_ldf
    namespace["lquery"] = selected_lquery
    clone = types.FunctionType(
        core_page.__code__,
        namespace,
        name=core_page.__name__,
        argdefs=core_page.__defaults__,
        closure=core_page.__closure__,
    )
    clone.__kwdefaults__ = getattr(core_page, "__kwdefaults__", None)
    clone.__annotations__ = dict(getattr(core_page, "__annotations__", {}))
    clone.__doc__ = core_page.__doc__
    clone.__module__ = core_page.__module__
    return clone


def apply(app: Any) -> None:
    """Install selected-page scoping after the v1.2.16 no-AI workflow."""
    if getattr(app, "_pb_selected_pages_v1217_applied", False):
        return
    app._pb_selected_pages_v1217_applied = True

    current_page = app.subscription_takeoff_page
    core_page = _find_core_subscription_page(current_page)
    selected_core_page = clone_selected_subscription_page(app, core_page)

    # The v1.2.16 panel resolves this module global at call time, so replacing it
    # updates both the UI draft and the exported helper without rewriting that
    # proven workflow.
    noai_v1216.build_no_ai_rows = build_selected_no_ai_rows
    app.build_no_ai_takeoff_rows = lambda workspace_id: build_selected_no_ai_rows(
        app, int(workspace_id)
    )
    app._pb_selected_subscription_core_v1217 = selected_core_page

    def _v1217_subscription_takeoff_page(
        workspace,
        session_api_key="",
        ai_provider="OpenAI",
    ):
        app.hero(workspace)
        workspace_id = int(workspace["id"])
        mode = app.st.radio(
            "Take-off method",
            ["No AI — measured geometry", "AI assistant — optional"],
            horizontal=True,
            key=f"takeoff_method_v1216_{workspace_id}",
            help=(
                "Only drawing pages selected during plan processing are used here. "
                "No-AI is the default; AI remains optional."
            ),
        )
        if mode.startswith("No AI"):
            noai_v1216.no_ai_takeoff_panel(app, workspace)
            return

        original_hero = app.hero
        app.hero = lambda *_args, **_kwargs: None
        try:
            selected_core_page(workspace, session_api_key, ai_provider)
        finally:
            app.hero = original_hero

    app.subscription_takeoff_page = _v1217_subscription_takeoff_page
