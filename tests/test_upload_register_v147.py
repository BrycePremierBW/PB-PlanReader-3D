from pathlib import Path


def test_production_entry_restores_original_sidebar_and_combines_upload_register():
    text = Path('pb_planreader_v133_app.py').read_text(encoding='utf-8')
    assert 'apply_guided_workflow_v144' not in text
    assert 'apply_upload_register_v147' in text
    assert 'apply_sidebar_viewport_guard_v146' in text
    assert 'APP_VERSION = "1.4.7"' in text


def test_combined_upload_register_wrapper_calls_both_pages():
    text = Path('pb_upload_register_v147.py').read_text(encoding='utf-8')
    assert 'original(workspace, bridge, user)' in text
    assert 'app.drawing_register_page(workspace)' in text
