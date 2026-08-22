"""PlanReader v1.4.4 guided estimator workflow.

Keeps specialist tools available, but makes the everyday journey a single guided
wizard: Upload -> Scope & Read -> Review -> 3D -> Export.  Primary navigation is
shown in the page, with AI/provider and specialist tools moved under Advanced.
"""
from __future__ import annotations

from typing import Any, Callable

VERSION = "1.4.4"
STEPS = [
    ("upload", "1 · Upload", "Upload plans"),
    ("read", "2 · Scope & Read", "Choose scope and read project"),
    ("review", "3 · Review", "Check only the items needing attention"),
    ("model", "4 · 3D", "Confirm the reconstructed building"),
    ("export", "5 · Export", "Export the checked take-off"),
]
ADVANCED = [
    ("drawing_register", "Drawing register"),
    ("plan_mapper", "Plan mapper"),
    ("accuracy", "Accuracy lab"),
    ("offline", "Offline reader"),
    ("settings", "Settings"),
]


def _suppress_hero(app: Any, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    original = app.hero
    app.hero = lambda *_a, **_k: None
    try:
        return fn(*args, **kwargs)
    finally:
        app.hero = original


def _goto(app: Any, route: str) -> None:
    app.st.session_state["pb_guided_route"] = route
    app.st.rerun()


def _counts(app: Any, wid: int) -> tuple[int, int, int]:
    docs = app.lquery("SELECT COUNT(*) AS n FROM documents WHERE workspace_id=?", (wid,))
    rows = app.lquery("SELECT COUNT(*) AS n FROM takeoff_rows WHERE workspace_id=?", (wid,))
    masses = app.lquery("SELECT COUNT(*) AS n FROM model_masses WHERE workspace_id=?", (wid,))
    return (
        int((docs[0].get("n") if docs else 0) or 0),
        int((rows[0].get("n") if rows else 0) or 0),
        int((masses[0].get("n") if masses else 0) or 0),
    )


def _suggested_route(app: Any, wid: int) -> str:
    docs, rows, _masses = _counts(app, wid)
    if docs <= 0:
        return "upload"
    if rows <= 0:
        return "read"
    return "review"


def _stepbar(app: Any, route: str) -> None:
    bits = []
    active = next((i for i, (key, _short, _long) in enumerate(STEPS) if key == route), 0)
    for i, (key, short, _long) in enumerate(STEPS):
        if i < active:
            cls = "pb-guide-step done"
        elif i == active:
            cls = "pb-guide-step active"
        else:
            cls = "pb-guide-step"
        bits.append(f'<span class="{cls}">{short}</span>')
    app.st.markdown('<div class="pb-guide-bar">' + ''.join(bits) + '</div>', unsafe_allow_html=True)


def _nav_buttons(app: Any, route: str) -> None:
    keys = [x[0] for x in STEPS]
    idx = keys.index(route)
    cols = app.st.columns([1, 3, 1])
    if idx > 0 and cols[0].button("← Back", use_container_width=True, key=f"guide_back_{route}"):
        _goto(app, keys[idx - 1])
    cols[1].caption(STEPS[idx][2])
    if idx < len(keys) - 1 and cols[2].button("Continue →", type="primary", use_container_width=True, key=f"guide_next_{route}"):
        _goto(app, keys[idx + 1])


def _home(app: Any, workspace: dict[str, Any]) -> None:
    wid = int(workspace["id"])
    docs, rows, masses = _counts(app, wid)
    suggested = _suggested_route(app, wid)
    app.hero(workspace)
    app.st.markdown("### One simple project flow")
    app.st.caption("Upload once, set the tender scope, let PlanReader read the drawings, review only exceptions, confirm the 3D model, then export.")
    c1, c2, c3 = app.st.columns(3)
    c1.metric("Documents", docs)
    c2.metric("Take-off rows", rows)
    c3.metric("Existing model items", masses)
    label = next(long for key, _short, long in STEPS if key == suggested)
    if app.st.button(f"Continue project · {label}", type="primary", use_container_width=True, key=f"guide_continue_{wid}"):
        _goto(app, suggested)
    app.st.caption("Advanced drawing tools remain available, but they are not part of the normal workflow unless something genuinely needs correction.")


def apply(app: Any) -> None:
    if getattr(app, "_pb_guided_workflow_v144_applied", False):
        return
    app._pb_guided_workflow_v144_applied = True

    old_css = app.app_css
    def _css() -> None:
        old_css()
        app.st.markdown("""
        <style>
        .pb-guide-bar{display:flex;gap:.45rem;flex-wrap:wrap;margin:.2rem 0 1rem}
        .pb-guide-step{background:#fff;border:1px solid #dce5f0;color:#687387;border-radius:999px;padding:.42rem .72rem;font-size:.82rem;font-weight:700}
        .pb-guide-step.done{background:#effcf6;border-color:#bde6d0;color:#147a51}
        .pb-guide-step.active{background:#2563eb;border-color:#2563eb;color:#fff}
        </style>
        """, unsafe_allow_html=True)
    app.app_css = _css

    def _main() -> None:
        app.st.set_page_config(page_title=app.APP_NAME, page_icon="🏗️", layout="wide")
        app.app_css()
        app.init_local_db()
        bridge = app.get_jobhub_bridge()
        user = app.st.session_state.get("planreader_user")
        if not user:
            app.login_screen(bridge)
        workspace_id = app.sidebar_workspace_selector(bridge)

        app.st.sidebar.markdown("## PlanReader")
        app.st.sidebar.caption("Guided estimating workflow")

        if not workspace_id:
            app.hero()
            app.st.info("Choose a JobHub job or create a standalone project to begin.")
            return
        workspace = app.current_workspace()
        if not workspace:
            app.st.session_state.pop("workspace_id", None)
            app.st.rerun()
        wid = int(workspace["id"])

        if "pb_guided_route" not in app.st.session_state:
            app.st.session_state["pb_guided_route"] = "home"

        # Normal users should not have to understand provider/API settings.
        with app.st.sidebar.expander("Advanced", expanded=False):
            ai_provider = app.st.selectbox(
                "AI provider", app.AI_PROVIDERS,
                index=app.AI_PROVIDERS.index(app.resolve_ai_provider()) if app.resolve_ai_provider() in app.AI_PROVIDERS else 0,
                key="guide_ai_provider",
            )
            session_api_key = app.st.text_input("AI API key (optional)", type="password", key="guide_ai_key")
            advanced_labels = [label for _key, label in ADVANCED]
            advanced_label = app.st.selectbox("Specialist tool", ["None"] + advanced_labels, key="guide_advanced_tool")
            if advanced_label != "None" and app.st.button("Open specialist tool", use_container_width=True, key="guide_open_advanced"):
                key = next(key for key, label in ADVANCED if label == advanced_label)
                app.st.session_state["pb_guided_route"] = f"advanced:{key}"
                app.st.rerun()
        if 'ai_provider' not in locals():
            ai_provider = app.resolve_ai_provider()
            session_api_key = ""

        route = str(app.st.session_state.get("pb_guided_route") or "home")
        if route == "home":
            _home(app, workspace)
            return

        if route.startswith("advanced:"):
            key = route.split(":", 1)[1]
            app.hero(workspace)
            if app.st.button("← Back to guided workflow", type="primary", key="guide_advanced_back"):
                _goto(app, _suggested_route(app, wid))
            if key == "drawing_register": app.drawing_register_page(workspace)
            elif key == "plan_mapper": app.plan_mapper_page(workspace)
            elif key == "accuracy" and hasattr(app, "accuracy_lab_page"): app.accuracy_lab_page(workspace)
            elif key == "offline": app.offline_plan_reader_page(workspace)
            else: app.settings_page(workspace, bridge, session_api_key, ai_provider)
            return

        if route not in [x[0] for x in STEPS]:
            route = _suggested_route(app, wid)
            app.st.session_state["pb_guided_route"] = route

        app.hero(workspace)
        _stepbar(app, route)

        if route == "upload":
            app.st.markdown("### Upload the current drawing set")
            app.st.caption("Drop in the plans, elevations, schedules and specification. PlanReader keeps the technical document handling behind the scenes.")
            _suppress_hero(app, app.project_documents_page, workspace, bridge, user)
        elif route == "read":
            app.st.markdown("### Scope & read project")
            app.st.caption("Choose the building/block being priced, then let PlanReader classify and measure the selected scope. For a single-building job, leave it on Whole project.")
            _suppress_hero(app, app.subscription_takeoff_page, workspace, session_api_key, ai_provider)
        elif route == "review":
            app.st.markdown("### Review what PlanReader could not prove")
            app.st.caption("The goal is exception review—not checking every line. Verified/measured items stay out of your way; ambiguous scope or dimensions remain visible for confirmation.")
            _suppress_hero(app, app.quantity_schedule_page, workspace)
        elif route == "model":
            app.st.markdown("### Confirm the building in 3D")
            app.st.caption("The model is the visual QA step. Doors/windows, wall geometry and substrates stay tied to the same measured objects used by the take-off.")
            _suppress_hero(app, app.model_3d_page, workspace, session_api_key, ai_provider)
        elif route == "export":
            app.st.markdown("### Export the checked job")
            app.st.caption("Once the take-off and 3D model look right, export the project without jumping through any extra setup pages.")
            _suppress_hero(app, app.export_page, workspace, bridge, user)

        app.st.divider()
        _nav_buttons(app, route)
        if app.st.button("Project home", use_container_width=True, key=f"guide_home_{route}"):
            _goto(app, "home")

    app.main = _main
    app.guided_workflow_version = VERSION
