from __future__ import annotations

import pb_3d_workspace_integration as integration


class FakeStreamlit:
    def __init__(self, *, checkbox_value: bool = False) -> None:
        self.checkbox_value = checkbox_value
        self.markdowns: list[str] = []
        self.captions: list[str] = []
        self.warnings: list[str] = []
        self.checkbox_calls: list[dict] = []

    def markdown(self, text, **kwargs):
        self.markdowns.append(str(text))

    def caption(self, text, **kwargs):
        self.captions.append(str(text))

    def warning(self, text, **kwargs):
        self.warnings.append(str(text))

    def checkbox(self, label, **kwargs):
        self.checkbox_calls.append({"label": label, **kwargs})
        return self.checkbox_value


class FakeApp:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events if events is not None else []
        self.legacy_calls = 0
        self.hero_calls = 0

    def hero(self, workspace=None, *args, **kwargs):
        self.hero_calls += 1
        self.events.append("hero")
        return workspace

    def model_3d_page(self, workspace=None, *args, **kwargs):
        self.legacy_calls += 1
        self.events.append("legacy")
        return "legacy-result"


def test_legacy_editor_feature_flag_is_off_by_default(monkeypatch):
    monkeypatch.delenv(integration.LEGACY_EDITOR_ENV, raising=False)
    assert integration.legacy_editor_feature_enabled() is False


def test_legacy_editor_feature_flag_accepts_only_explicit_truthy_values(monkeypatch):
    for value in ("1", "true", "TRUE", " yes ", "on"):
        monkeypatch.setenv(integration.LEGACY_EDITOR_ENV, value)
        assert integration.legacy_editor_feature_enabled() is True

    for value in ("", "0", "false", "off", "enabled", "anything"):
        monkeypatch.setenv(integration.LEGACY_EDITOR_ENV, value)
        assert integration.legacy_editor_feature_enabled() is False


def test_default_commercial_3d_page_preserves_header_and_never_calls_legacy(monkeypatch):
    events: list[str] = []
    app = FakeApp(events)
    fake_st = FakeStreamlit()
    monkeypatch.setattr(integration, "st", fake_st)
    monkeypatch.delenv(integration.LEGACY_EDITOR_ENV, raising=False)
    monkeypatch.setattr(
        integration,
        "render_workspace_3d_canonical_view",
        lambda app_arg, workspace_arg=None: events.append("canonical"),
    )

    integration.apply(app)
    app.model_3d_page({"id": 101})

    assert events == ["hero", "canonical"]
    assert app.hero_calls == 1
    assert app.legacy_calls == 0
    assert fake_st.checkbox_calls == []


def test_environment_flag_alone_is_not_enough_to_run_legacy_editor(monkeypatch):
    events: list[str] = []
    app = FakeApp(events)
    fake_st = FakeStreamlit(checkbox_value=False)
    monkeypatch.setattr(integration, "st", fake_st)
    monkeypatch.setenv(integration.LEGACY_EDITOR_ENV, "1")
    monkeypatch.setattr(
        integration,
        "render_workspace_3d_canonical_view",
        lambda app_arg, workspace_arg=None: events.append("canonical"),
    )

    integration.apply(app)
    app.model_3d_page({"id": 202})

    assert events == ["hero", "canonical"]
    assert app.hero_calls == 1
    assert app.legacy_calls == 0
    assert len(fake_st.checkbox_calls) == 1
    assert fake_st.checkbox_calls[0]["key"] == "legacy_3d_editor_opt_in_202"


def test_explicit_developer_opt_in_runs_legacy_only_after_header_and_canonical(monkeypatch):
    events: list[str] = []
    app = FakeApp(events)
    fake_st = FakeStreamlit(checkbox_value=True)
    monkeypatch.setattr(integration, "st", fake_st)
    monkeypatch.setenv(integration.LEGACY_EDITOR_ENV, "true")
    monkeypatch.setattr(
        integration,
        "render_workspace_3d_canonical_view",
        lambda app_arg, workspace_arg=None: events.append("canonical"),
    )

    integration.apply(app)
    app.model_3d_page({"id": 303})

    assert events == ["hero", "canonical", "legacy"]
    assert app.hero_calls == 1
    assert app.legacy_calls == 1
    warning_text = "\n".join(fake_st.warnings)
    assert "NON-CANONICAL" in warning_text
    assert "NON-TAKEOFF-AUTHORITATIVE" in warning_text


def test_apply_is_idempotent_and_preserves_original_legacy_callable(monkeypatch):
    app = FakeApp()
    monkeypatch.delenv(integration.LEGACY_EDITOR_ENV, raising=False)
    monkeypatch.setattr(
        integration,
        "render_workspace_3d_canonical_view",
        lambda app_arg, workspace_arg=None: None,
    )

    original_bound_method = app.model_3d_page
    integration.apply(app)
    installed_wrapper = app.model_3d_page
    saved_legacy = app._legacy_model_3d_page

    integration.apply(app)

    assert app.model_3d_page is installed_wrapper
    assert saved_legacy.__self__ is original_bound_method.__self__
    assert saved_legacy.__func__ is original_bound_method.__func__


def test_apply_is_safe_when_app_has_no_legacy_model_page():
    class AppWithout3D:
        pass

    app = AppWithout3D()
    integration.apply(app)
    assert not getattr(app, "_canonical_3d_extension_installed", False)
    assert not hasattr(app, "_legacy_model_3d_page")


def test_shared_header_helper_is_safe_when_app_has_no_hero():
    class AppWithoutHero:
        pass

    integration._render_shared_workspace_header(AppWithoutHero(), {"id": 10})


def test_workspace_context_prefers_explicit_workspace_identity(monkeypatch):
    seen: list[object] = []

    def strict_workspace_id(value):
        seen.append(value)
        if value is None:
            raise ValueError("missing workspace")
        return int(value)

    monkeypatch.setattr(integration, "require_workspace_id", strict_workspace_id)

    class App:
        def current_workspace(self):
            return {"id": 999}

    app = App()
    assert integration._workspace_id_from_context(app, {"id": 77}) == 77
    assert integration._workspace_id_from_context(app, "88") == 88
    assert integration._workspace_id_from_context(app, None) == 999
    assert seen == [77, "88", 999]
