"""PlanReader v1.3.3 brighter, simplified estimator workflow.

The production app has accumulated powerful specialist tools over time. This
patch keeps every tool available, but makes the normal estimator journey a short
five-step workflow: Upload -> Read -> Review -> 3D -> Export.
"""
from __future__ import annotations

from typing import Any, Callable

VERSION = "1.3.3"

SIMPLE_STEPS = [
    "🏠 Overview",
    "1 · Upload plans",
    "2 · Read project",
    "3 · Review take-off",
    "4 · 3D model",
    "5 · Export",
]

ADVANCED_STEPS = [
    "Drawing register",
    "Plan mapper",
    "Accuracy lab",
    "Offline reader",
    "Settings",
]


def route_page(label: str) -> str:
    mapping = {
        "🏠 Overview": "overview",
        "1 · Upload plans": "upload",
        "2 · Read project": "read",
        "3 · Review take-off": "review",
        "4 · 3D model": "model",
        "5 · Export": "export",
        "Drawing register": "drawing_register",
        "Plan mapper": "plan_mapper",
        "Accuracy lab": "accuracy",
        "Offline reader": "offline",
        "Settings": "settings",
    }
    return mapping.get(str(label or ""), "overview")


def bright_css() -> str:
    return """
    <style>
    :root {
        --pb-ink:#142033;
        --pb-muted:#687387;
        --pb-blue:#2563eb;
        --pb-blue-soft:#eff6ff;
        --pb-gold:#e5a719;
        --pb-green:#16a36a;
        --pb-border:#dce5f0;
        --pb-bg:#f7faff;
        --pb-card:#ffffff;
    }
    .stApp { background:linear-gradient(180deg,#ffffff 0%,var(--pb-bg) 42%,#f4f8fd 100%); color:var(--pb-ink); }
    .block-container { padding-top:1rem; padding-bottom:3rem; max-width:1600px; }
    [data-testid="stSidebar"] { background:#ffffff; border-right:1px solid var(--pb-border); }
    [data-testid="stSidebar"] * { color:var(--pb-ink) !important; }
    [data-testid="stSidebar"] [data-baseweb="radio"] > div { gap:.42rem; }
    [data-testid="stSidebar"] label { border-radius:10px; }
    .pb-hero { background:linear-gradient(120deg,#1f5fd8 0%,#3478ef 58%,#5b92f4 100%); color:white;
        border:0; border-radius:16px; padding:1.15rem 1.3rem; margin-bottom:1rem; box-shadow:0 8px 26px rgba(37,99,235,.16); }
    .pb-hero h1 { margin:0; font-size:1.7rem; }
    .pb-hero p { color:#edf5ff; margin:.35rem 0 0; }
    .pb-card { background:var(--pb-card); border:1px solid var(--pb-border); border-radius:14px; padding:1rem;
        margin-bottom:.85rem; box-shadow:0 3px 14px rgba(25,55,95,.05); }
    .pb-note { background:#fff9e8; border:1px solid #f4dfa0; border-left:5px solid var(--pb-gold); border-radius:10px; padding:.8rem 1rem; }
    .pb-warning { background:#fff3f3; border:1px solid #f0caca; border-left:5px solid #d34848; border-radius:10px; padding:.8rem 1rem; }
    .pb-good { background:#effcf6; border:1px solid #c9eedc; border-left:5px solid var(--pb-green); border-radius:10px; padding:.8rem 1rem; }
    .pb-badge { display:inline-block; background:#fff1bf; color:#644800; font-weight:700; border-radius:999px; padding:.22rem .58rem; margin-right:.35rem; font-size:.78rem; }
    div[data-testid="stMetric"] { background:#fff; border:1px solid var(--pb-border); padding:.72rem; border-radius:12px; box-shadow:0 2px 10px rgba(25,55,95,.04); }
    div.stButton > button, div.stDownloadButton > button { border-radius:10px; min-height:2.65rem; font-weight:650; }
    div.stButton > button[kind="primary"] { background:var(--pb-blue); border-color:var(--pb-blue); }
    [data-baseweb="tab-list"] { gap:.3rem; background:#eef4fb; border-radius:12px; padding:.28rem; }
    [data-baseweb="tab"] { border-radius:9px; padding:.55rem .9rem; }
    .small-muted { color:var(--pb-muted); font-size:.86rem; }
    .pb-stepbar { display:flex; gap:.45rem; flex-wrap:wrap; margin:.2rem 0 1rem; }
    .pb-step { background:white; border:1px solid var(--pb-border); color:#536177; border-radius:999px; padding:.38rem .68rem; font-size:.82rem; font-weight:650; }
    .pb-step.active { background:var(--pb-blue); border-color:var(--pb-blue); color:white; }
    </style>
    """


def _suppress_hero(app: Any, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    original = app.hero
    app.hero = lambda *_a, **_k: None
    try:
        return fn(*args, **kwargs)
    finally:
        app.hero = original


def _stepbar(app: Any, active: int) -> None:
    names = ["Upload", "Read", "Review", "3D", "Export"]
    bits = []
    for idx, name in enumerate(names, start=1):
        cls = "pb-step active" if idx == active else "pb-step"
        bits.append(f'<span class="{cls}">{idx}. {name}</span>')
    app.st.markdown('<div class="pb-stepbar">' + ''.join(bits) + '</div>', unsafe_allow_html=True)


def _goto(app: Any, label: str) -> None:
    app.st.session_state["pb_simple_route"] = label
    app.st.rerun()


def _overview(app: Any, workspace: dict[str, Any]) -> None:
    app.hero(workspace)
    wid = int(workspace["id"])
    docs = app.lquery("SELECT COUNT(*) AS n FROM documents WHERE workspace_id=?", (wid,))
    rows = app.lquery("SELECT COUNT(*) AS n FROM takeoff_rows WHERE workspace_id=?", (wid,))
    masses = app.lquery("SELECT COUNT(*) AS n FROM model_masses WHERE workspace_id=?", (wid,))
    doc_n = int((docs[0].get("n") if docs else 0) or 0)
    row_n = int((rows[0].get("n") if rows else 0) or 0)
    mass_n = int((masses[0].get("n") if masses else 0) or 0)

    app.st.markdown("### Project workflow")
    app.st.caption("The normal estimating process is now five steps. Advanced drawing and diagnostic tools are still available in the sidebar when needed.")
    c1, c2, c3 = app.st.columns(3)
    c1.metric("Documents", doc_n)
    c2.metric("Take-off rows", row_n)
    c3.metric("3D model items", mass_n)

    a, b, c = app.st.columns(3)
    with a:
        app.st.markdown("#### 1–2 · Start")
        app.st.write("Upload the drawing set, then let PlanReader classify and read the project.")
        if app.st.button("Upload plans", type="primary", use_container_width=True, key=f"ui133_upload_{wid}"):
            _goto(app, "1 · Upload plans")
    with b:
        app.st.markdown("#### 3–4 · Check")
        app.st.write("Review quantities and open the 3D model to visually confirm the building and substrates.")
        if app.st.button("Review take-off", use_container_width=True, key=f"ui133_review_{wid}"):
            _goto(app, "3 · Review take-off")
    with c:
        app.st.markdown("#### 5 · Finish")
        app.st.write("Export the checked take-off or send the project data onward when you are satisfied.")
        if app.st.button("Export project", use_container_width=True, key=f"ui133_export_{wid}"):
            _goto(app, "5 · Export")


def apply(app: Any) -> None:
    if getattr(app, "_pb_simple_ui_v133_applied", False):
        return
    app._pb_simple_ui_v133_applied = True

    def _app_css() -> None:
        app.st.markdown(bright_css(), unsafe_allow_html=True)

    app.app_css = _app_css

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
        app.st.sidebar.caption("Simple estimator workflow")
        ai_provider = app.st.sidebar.selectbox(
            "AI provider", app.AI_PROVIDERS,
            index=app.AI_PROVIDERS.index(app.resolve_ai_provider()) if app.resolve_ai_provider() in app.AI_PROVIDERS else 0,
            help="Used when AI-assisted drawing interpretation is enabled.",
        )
        session_api_key = app.st.sidebar.text_input("AI API key (optional)", type="password")
        if app.resolve_ai_key(ai_provider, session_api_key):
            app.st.sidebar.success("AI reading ready")

        if not workspace_id:
            app.hero()
            app.st.info("Choose a JobHub job or create a standalone project from the sidebar.")
            return
        workspace = app.current_workspace()
        if not workspace:
            app.st.session_state.pop("workspace_id", None)
            app.st.rerun()

        if "pb_simple_route" not in app.st.session_state or app.st.session_state["pb_simple_route"] not in SIMPLE_STEPS:
            app.st.session_state["pb_simple_route"] = SIMPLE_STEPS[0]
        selected = app.st.sidebar.radio("Workflow", SIMPLE_STEPS, key="pb_simple_route")
        route = route_page(selected)

        with app.st.sidebar.expander("Advanced tools", expanded=False):
            use_advanced = app.st.checkbox("Show advanced navigation", key="pb_ui133_advanced")
            advanced_selected = None
            if use_advanced:
                advanced_selected = app.st.radio("Advanced", ADVANCED_STEPS, key="pb_ui133_advanced_route")
        if use_advanced and advanced_selected:
            route = route_page(advanced_selected)

        if route == "overview":
            _overview(app, workspace)
        elif route == "upload":
            app.hero(workspace)
            _stepbar(app, 1)
            app.st.markdown("### Upload plans & project documents")
            app.st.caption("Add the current drawing set and specifications here. Once uploaded, go straight to Read Project.")
            _suppress_hero(app, app.project_documents_page, workspace, bridge, user)
        elif route == "read":
            app.hero(workspace)
            _stepbar(app, 2)
            app.st.markdown("### Read project")
            app.st.caption("Let PlanReader classify the drawings, extract scope evidence and build the first take-off. Review only the items it cannot prove.")
            _suppress_hero(app, app.subscription_takeoff_page, workspace, session_api_key, ai_provider)
        elif route == "review":
            app.hero(workspace)
            _stepbar(app, 3)
            app.st.markdown("### Review take-off")
            app.st.caption("Check quantities, inclusion status and confidence. Items needing attention should remain clearly provisional rather than being silently guessed.")
            _suppress_hero(app, app.quantity_schedule_page, workspace)
        elif route == "model":
            app.hero(workspace)
            _stepbar(app, 4)
            app.st.markdown("### 3D model")
            app.st.caption("Use the model as the visual QA step. Correct geometry once and keep the take-off tied to the same source geometry.")
            _suppress_hero(app, app.model_3d_page, workspace, session_api_key, ai_provider)
        elif route == "export":
            app.hero(workspace)
            _stepbar(app, 5)
            app.st.markdown("### Export checked project")
            _suppress_hero(app, app.export_page, workspace, bridge, user)
        elif route == "drawing_register":
            app.drawing_register_page(workspace)
        elif route == "plan_mapper":
            app.plan_mapper_page(workspace)
        elif route == "accuracy":
            if hasattr(app, "accuracy_lab_page"):
                app.accuracy_lab_page(workspace)
            else:
                app.st.info("Accuracy Lab is available through the take-off review tools in this build.")
        elif route == "offline":
            app.offline_plan_reader_page(workspace)
        else:
            app.settings_page(workspace, bridge, session_api_key, ai_provider)

    app.main = _main
    app.route_simple_ui_page = route_page
    app.simple_ui_steps = list(SIMPLE_STEPS)
