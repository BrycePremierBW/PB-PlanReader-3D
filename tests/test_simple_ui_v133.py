from pb_simple_ui_v133 import ADVANCED_STEPS, SIMPLE_STEPS, bright_css, route_page


def test_simple_workflow_is_short_and_ordered():
    assert SIMPLE_STEPS == [
        "🏠 Overview",
        "1 · Upload plans",
        "2 · Read project",
        "3 · Review take-off",
        "4 · 3D model",
        "5 · Export",
    ]


def test_routes_keep_advanced_tools_available():
    assert route_page("1 · Upload plans") == "upload"
    assert route_page("2 · Read project") == "read"
    assert route_page("3 · Review take-off") == "review"
    assert route_page("4 · 3D model") == "model"
    assert route_page("5 · Export") == "export"
    assert route_page("Plan mapper") == "plan_mapper"
    assert route_page("Settings") == "settings"
    assert "Accuracy lab" in ADVANCED_STEPS


def test_bright_theme_replaces_dark_sidebar():
    css = bright_css()
    assert "background:#ffffff" in css
    assert "--pb-blue:#2563eb" in css
    assert "pb-stepbar" in css
