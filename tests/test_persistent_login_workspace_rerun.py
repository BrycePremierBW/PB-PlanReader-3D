from types import SimpleNamespace

import pb_persistent_login_v1229 as persistent_login


class _FakeForm:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeStreamlit:
    def __init__(self):
        self.session_state = {}
        self.query_params = {}
        self.scripts = []
        self.page_config_calls = 0

    def set_page_config(self, *args, **kwargs):
        self.page_config_calls += 1
        return None

    def html(self, html, **kwargs):
        self.scripts.append(str(html))

    def form(self, *args, **kwargs):
        return _FakeForm()

    def checkbox(self, *args, **kwargs):
        return False


def _fake_app():
    st = _FakeStreamlit()
    app = SimpleNamespace(
        st=st,
        login_screen=lambda bridge: None,
        sidebar_workspace_selector=lambda bridge: None,
    )
    persistent_login.apply(app)
    return app, st


def test_authenticated_workspace_rerun_does_not_force_browser_reload():
    _app, st = _fake_app()
    user = {"username": "bryce", "role": "admin"}
    st.session_state.update(
        {
            "planreader_user": user,
            "workspace_id": 10,
            "_pb_planreader_remember_login": True,
            "_pb_planreader_auth_token": "remembered-token",
            "_pb_planreader_auth_token_saved_this_session": True,
        }
    )

    st.set_page_config(page_title="PlanReader")
    st.session_state["workspace_id"] = 11  # Create/open another job, then rerun.
    st.set_page_config(page_title="PlanReader")

    assert st.page_config_calls == 2
    assert st.session_state["planreader_user"] == user
    assert st.session_state["_pb_planreader_auth_token"] == "remembered-token"
    assert st.session_state["_pb_planreader_remember_login"] is True
    assert st.scripts == []


def test_cold_start_without_authenticated_user_still_bootstraps_localstorage_token():
    _app, st = _fake_app()

    st.set_page_config(page_title="PlanReader")

    assert len(st.scripts) == 1
    script = st.scripts[0]
    assert "localStorage.getItem" in script
    assert "location.replace" in script
    assert "pr_auth" in script


def test_explicit_logout_clear_still_removes_browser_token_when_authenticated():
    _app, st = _fake_app()
    st.session_state.update(
        {
            "planreader_user": {"username": "bryce", "role": "admin"},
            "_pb_planreader_clear_remember": True,
        }
    )

    st.set_page_config(page_title="PlanReader")

    assert "_pb_planreader_clear_remember" not in st.session_state
    assert len(st.scripts) == 1
    script = st.scripts[0]
    assert "localStorage.removeItem" in script
    assert "location.replace" not in script
