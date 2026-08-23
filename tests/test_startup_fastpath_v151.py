import sys
from pathlib import Path
from types import SimpleNamespace

import pb_persistent_login_v1229 as login
import pb_startup_bootstrap_v151 as startup


def test_offline_reader_is_lazy_until_feature_is_used(monkeypatch):
    monkeypatch.delitem(sys.modules, "pb_planreader_offline", raising=False)
    monkeypatch.setattr(startup, "_REAL_MODULE", None)
    assert startup.install() is True
    proxy = sys.modules["pb_planreader_offline"]
    assert proxy.LAZY_STARTUP_PROXY is True
    assert callable(proxy.analyze_page_offline)
    assert startup.real_module_loaded() is False


def test_production_installs_bootstrap_before_main_stack_import():
    text = Path("pb_planreader_v133_app.py").read_text(encoding="utf-8")
    install_pos = text.index("install_startup_bootstrap_v151()")
    main_import_pos = text.index("import pb_planreader_reconstruction_v139_app as base")
    assert install_pos < main_import_pos
    assert 'APP_VERSION = "1.5.1"' in text


def test_existing_remembered_session_does_not_write_to_jobhub_on_sidebar_rerun():
    class FakeSession(dict):
        pass

    class FakeSt:
        def __init__(self):
            self.session_state = FakeSession({
                "planreader_user": {"username": "nick", "role": "admin"},
                "_pb_planreader_remember_login": True,
                "_pb_planreader_auth_token": "existing-token",
            })
            self.query_params = {}
            self.form = lambda *a, **k: None
            self.set_page_config = lambda *a, **k: None

        def html(self, *args, **kwargs):
            return None

    calls = {"tables": 0, "execute": 0, "query": 0}

    class Bridge:
        def table_names(self):
            calls["tables"] += 1
            return ["app_settings"]

        def execute(self, *args, **kwargs):
            calls["execute"] += 1

        def query(self, *args, **kwargs):
            calls["query"] += 1
            return []

    st = FakeSt()
    app = SimpleNamespace(
        st=st,
        login_screen=lambda bridge: None,
        sidebar_workspace_selector=lambda bridge: 7,
        local_connect=lambda: None,
        now_stamp=lambda: "2026-08-23T00:00:00",
    )
    login.apply(app)
    assert app.sidebar_workspace_selector(Bridge()) == 7
    assert calls == {"tables": 0, "execute": 0, "query": 0}
