from pathlib import Path


def test_production_entry_keeps_old_sidebar_and_fast_combined_documents_page():
    text = Path('pb_planreader_v133_app.py').read_text(encoding='utf-8')
    assert 'apply_guided_workflow_v144' not in text
    assert 'apply_upload_register_v147' in text
    assert 'apply_sidebar_viewport_guard_v146' in text
    assert 'APP_VERSION = "1.4.8"' in text


def test_upload_register_renders_only_active_heavy_view():
    text = Path('pb_upload_register_v147.py').read_text(encoding='utf-8')
    assert 'pb_documents_view_' in text
    assert 'if app.st.session_state[state_key] == "register"' in text
    assert 'return app.drawing_register_page(workspace)' in text
    assert 'return original(workspace, bridge, user)' in text
    assert 'render exactly one heavy view per rerun' in text
