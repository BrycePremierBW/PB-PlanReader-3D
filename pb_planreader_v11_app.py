"""Production entry point for Premier Brushworks PlanReader v1.1."""
import pb_planreader_3d_app as app
from pb_takeoff_v11 import apply

apply(app)
app.APP_VERSION = "1.1.1"

# The base app intentionally makes sidebar labels white on the dark PB sidebar,
# but Streamlit text inputs/selects use light controls. The broad sidebar rule
# also made the typed value white, so it was effectively invisible. Keep labels
# white while forcing control values/placeholders back to dark text.
_base_app_css = app.app_css


def _v11_app_css() -> None:
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
        .pb-v11-live {
            margin: 0.35rem 0 0.75rem 0;
            padding: 0.55rem 0.7rem;
            border: 1px solid #D7A21B;
            border-left: 5px solid #D7A21B;
            border-radius: 8px;
            background: #262626;
            color: #ffffff;
            font-size: 0.82rem;
            line-height: 1.25rem;
        }
        .pb-v11-live strong { color: #F4C84B !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )


app.app_css = _v11_app_css

_base_sidebar_workspace_selector = app.sidebar_workspace_selector


def _v11_sidebar_workspace_selector(bridge):
    app.st.sidebar.markdown(
        "<div class='pb-v11-live'><strong>PB TAKE-OFF v1.1 ACTIVE</strong><br>Premier Brushworks estimating + JobHub sync</div>",
        unsafe_allow_html=True,
    )
    return _base_sidebar_workspace_selector(bridge)


app.sidebar_workspace_selector = _v11_sidebar_workspace_selector

if __name__ == "__main__":
    app.main()
