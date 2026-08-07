"""Production entry point for Premier Brushworks PlanReader v1.2.1."""
import pb_planreader_3d_app as app
from pb_takeoff_v11 import apply as apply_v11
from pb_takeoff_v12 import apply as apply_v12

apply_v11(app)
apply_v12(app)
app.APP_VERSION = "1.2.1"

# Streamlit reruns this launcher while imported modules may stay cached. Preserve
# originals once so reruns are idempotent instead of wrapping wrappers forever.
if not hasattr(app, "_pb_original_app_css"):
    app._pb_original_app_css = app.app_css
if not hasattr(app, "_pb_original_sidebar_workspace_selector"):
    app._pb_original_sidebar_workspace_selector = app.sidebar_workspace_selector
if not hasattr(app, "_pb_original_get_jobhub_bridge"):
    app._pb_original_get_jobhub_bridge = app.get_jobhub_bridge

_base_app_css = app._pb_original_app_css
_base_sidebar_workspace_selector = app._pb_original_sidebar_workspace_selector
_base_get_jobhub_bridge = app._pb_original_get_jobhub_bridge


def _v121_get_jobhub_bridge():
    """Resolve JobHub from Render/env, Streamlit secrets, or a session override."""
    session_url = str(app.st.session_state.get("jobhub_database_url") or "").strip()
    if session_url:
        return app.JobHubBridge("postgres", session_url)

    # Match JobHub itself: Streamlit secrets can carry DATABASE_URL too.
    for key in ("JOBHUB_DATABASE_URL", "DATABASE_URL"):
        try:
            value = str(app.st.secrets.get(key, "") or "").strip()
        except Exception:
            value = ""
        if value:
            return app.JobHubBridge("postgres", value)

    return _base_get_jobhub_bridge()


app.get_jobhub_bridge = _v121_get_jobhub_bridge


def _v121_app_css() -> None:
    _base_app_css()
    app.st.markdown(
        """
        <style>
        [data-testid="stSidebar"] input,
        [data-testid="stSidebar"] textarea,
        [data-testid="stSidebar"] [role="combobox"] {
            color: #171717 !important;
            -webkit-text-fill-color: #171717 !important;
            caret-color: #171717 !important;
        }
        [data-testid="stSidebar"] input::placeholder,
        [data-testid="stSidebar"] textarea::placeholder {
            color: #666666 !important;
            -webkit-text-fill-color: #666666 !important;
            opacity: 1 !important;
        }
        [data-testid="stSidebar"] [data-baseweb="input"] > div,
        [data-testid="stSidebar"] [data-baseweb="textarea"] > div,
        [data-testid="stSidebar"] [data-baseweb="select"] > div {
            background-color: #ffffff !important;
        }
        [data-testid="stSidebar"] [data-baseweb="select"] span,
        [data-testid="stSidebar"] [data-baseweb="select"] div[aria-selected="true"] {
            color: #171717 !important;
            -webkit-text-fill-color: #171717 !important;
        }
        .pb-v12-live,
        .pb-jobhub-ok,
        .pb-jobhub-bad {
            margin: 0.35rem 0 0.6rem 0;
            padding: 0.55rem 0.7rem;
            border-radius: 8px;
            background: #262626;
            color: #ffffff;
            font-size: 0.82rem;
            line-height: 1.25rem;
        }
        .pb-v12-live { border: 1px solid #D7A21B; border-left: 5px solid #D7A21B; }
        .pb-v12-live strong { color: #F4C84B !important; }
        .pb-jobhub-ok { border: 1px solid #2E8B57; border-left: 5px solid #2E8B57; }
        .pb-jobhub-ok strong { color: #8BE0A9 !important; }
        .pb-jobhub-bad { border: 1px solid #B33A3A; border-left: 5px solid #B33A3A; }
        .pb-jobhub-bad strong { color: #FF9A9A !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )


app.app_css = _v121_app_css


def _v121_sidebar_workspace_selector(bridge):
    app.st.sidebar.markdown(
        "<div class='pb-v12-live'><strong>PB TAKE-OFF v1.2.1 ACTIVE</strong><br>PB/JobHub multi-line importer + estimating sync</div>",
        unsafe_allow_html=True,
    )

    connected = False
    connection_error = ""
    table_count = 0
    if bridge is not None:
        try:
            tables = bridge.table_names()
            table_count = len(tables)
            connected = True
        except Exception as exc:
            connection_error = str(exc)

    if connected:
        app.st.sidebar.markdown(
            f"<div class='pb-jobhub-ok'><strong>JOBHUB CONNECTED</strong><br>{table_count} database tables detected</div>",
            unsafe_allow_html=True,
        )
        if app.st.session_state.get("jobhub_database_url"):
            if app.st.sidebar.button("Clear session JobHub connection", use_container_width=True):
                app.st.session_state.pop("jobhub_database_url", None)
                app.st.rerun()
    else:
        detail = "Database URL is not configured." if bridge is None else "Database credential exists but the connection failed."
        app.st.sidebar.markdown(
            f"<div class='pb-jobhub-bad'><strong>JOBHUB NOT CONNECTED</strong><br>{detail}</div>",
            unsafe_allow_html=True,
        )
        if connection_error:
            app.st.sidebar.error(f"JobHub connection error: {connection_error}")
        with app.st.sidebar.expander("Connect JobHub"):
            app.st.caption("For a permanent Render connection, set JOBHUB_DATABASE_URL to the same PostgreSQL DATABASE_URL used by JobHub. You can also test it for this browser session here.")
            session_url = app.st.text_input(
                "JobHub PostgreSQL URL (session only)",
                type="password",
                key="jobhub_database_url_entry",
                placeholder="postgresql://...",
            )
            if app.st.button("Test & connect JobHub", type="primary", use_container_width=True):
                candidate = str(session_url or "").strip()
                if not candidate:
                    app.st.error("Paste the JobHub PostgreSQL DATABASE_URL first.")
                else:
                    try:
                        probe = app.JobHubBridge("postgres", candidate)
                        probe_tables = probe.table_names()
                        if "jobs" not in set(probe_tables):
                            raise RuntimeError("Connected to PostgreSQL, but the JobHub jobs table was not found.")
                    except Exception as exc:
                        app.st.error(f"Could not connect to JobHub: {exc}")
                    else:
                        app.st.session_state["jobhub_database_url"] = candidate
                        app.st.rerun()

    workspace_id = _base_sidebar_workspace_selector(bridge if connected else None)
    if workspace_id:
        try:
            rows = app.lquery("SELECT jobhub_job_id FROM workspaces WHERE id=?", (int(workspace_id),))
            linked_id = rows[0].get("jobhub_job_id") if rows else None
            if linked_id and connected:
                app.st.sidebar.success(f"Workspace linked to JobHub job #{linked_id}")
            elif linked_id and not connected:
                app.st.sidebar.warning(f"Workspace remembers JobHub job #{linked_id}, but JobHub is offline.")
            else:
                app.st.sidebar.caption("Current workspace is standalone — it is not linked to a JobHub job.")
        except Exception:
            pass
    return workspace_id


app.sidebar_workspace_selector = _v121_sidebar_workspace_selector

if __name__ == "__main__":
    app.main()
