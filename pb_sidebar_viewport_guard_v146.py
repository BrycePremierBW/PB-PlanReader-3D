"""PlanReader v1.4.6 sidebar/dropdown viewport guard.

Keeps sidebar select boxes and BaseWeb dropdown popovers inside the visible browser
viewport, especially on narrow desktop windows, tablets and phones.
"""
from __future__ import annotations

from typing import Any

VERSION = "1.4.6"


def viewport_css() -> str:
    return """
    <style>
    /* Sidebar itself must never be wider than the visible browser. */
    [data-testid="stSidebar"] {
        max-width: min(22rem, 100vw) !important;
        overflow-x: hidden !important;
    }
    [data-testid="stSidebar"] > div,
    [data-testid="stSidebar"] [data-testid="stSidebarContent"] {
        max-width: 100% !important;
        overflow-x: hidden !important;
        box-sizing: border-box !important;
    }

    /* Select controls and their text stay within the sidebar width. */
    [data-testid="stSidebar"] [data-baseweb="select"],
    [data-testid="stSidebar"] [data-baseweb="select"] > div,
    [data-testid="stSidebar"] [role="combobox"] {
        width: 100% !important;
        max-width: 100% !important;
        min-width: 0 !important;
        box-sizing: border-box !important;
    }
    [data-testid="stSidebar"] [data-baseweb="select"] span {
        max-width: 100% !important;
        overflow: hidden !important;
        text-overflow: ellipsis !important;
        white-space: nowrap !important;
    }

    /* Streamlit/BaseWeb renders dropdown menus in a portal outside the sidebar.
       Constrain every menu/popover to the browser viewport so it cannot disappear
       off the right-hand side of a narrow screen. */
    div[data-baseweb="popover"],
    div[data-baseweb="popover"] > div,
    ul[data-baseweb="menu"] {
        max-width: calc(100vw - 1rem) !important;
        box-sizing: border-box !important;
    }
    ul[data-baseweb="menu"] {
        overflow-x: hidden !important;
        overflow-y: auto !important;
    }
    ul[data-baseweb="menu"] li,
    ul[data-baseweb="menu"] [role="option"] {
        max-width: calc(100vw - 1.5rem) !important;
        overflow-wrap: anywhere !important;
        white-space: normal !important;
    }

    @media (max-width: 768px) {
        [data-testid="stSidebar"] {
            width: min(88vw, 22rem) !important;
            min-width: 0 !important;
        }
        [data-testid="stSidebar"] .stSelectbox,
        [data-testid="stSidebar"] .stTextInput,
        [data-testid="stSidebar"] .stButton,
        [data-testid="stSidebar"] .stExpander {
            width: 100% !important;
            max-width: 100% !important;
            min-width: 0 !important;
        }
        div[data-baseweb="popover"] {
            max-width: calc(100vw - .75rem) !important;
        }
    }
    </style>
    """


def apply(app: Any) -> None:
    if getattr(app, "_pb_sidebar_viewport_guard_v146_applied", False):
        return
    app._pb_sidebar_viewport_guard_v146_applied = True

    old_css = app.app_css

    def _css() -> None:
        old_css()
        app.st.markdown(viewport_css(), unsafe_allow_html=True)

    app.app_css = _css
    app.sidebar_viewport_guard_version = VERSION
