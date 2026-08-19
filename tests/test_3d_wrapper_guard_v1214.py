from types import SimpleNamespace

import pb_3d_quickstart_v1213 as quick_v1213
import pb_3d_surface_editor_v1212 as surface_v1212
import pb_3d_wrapper_guard_v1214 as guard_v1214
import pb_takeoff_studio_v1211 as studio_v1211


def test_unwraps_real_planreader_wrapper_chain_to_original_core_page():
    calls = []

    def core_model_page(workspace, session_api_key="", ai_provider="OpenAI"):
        calls.append((workspace, session_api_key, ai_provider))

    app = SimpleNamespace(model_3d_page=core_model_page)

    # Recreate the production patch order before v1.2.14.
    studio_v1211.apply(app)
    surface_v1212.apply(app)
    quick_v1213.apply(app)

    wrapped_v1213 = app.model_3d_page
    assert wrapped_v1213 is not core_model_page
    assert guard_v1214.unwrap_core_model_page(wrapped_v1213) is core_model_page

    guard_v1214.apply(app)

    assert app._pb_core_model_3d_page is core_model_page
    assert app.model_3d_page is not wrapped_v1213
    assert app._pb_3d_wrapper_guard_v1214_applied is True
    assert calls == []


def test_unwrap_leaves_an_unwrapped_function_unchanged():
    def core_model_page(*_args, **_kwargs):
        return None

    assert guard_v1214.unwrap_core_model_page(core_model_page) is core_model_page


def test_guard_apply_is_idempotent():
    def core_model_page(*_args, **_kwargs):
        return None

    app = SimpleNamespace(model_3d_page=core_model_page)
    guard_v1214.apply(app)
    installed = app.model_3d_page
    guard_v1214.apply(app)
    assert app.model_3d_page is installed


if __name__ == "__main__":
    test_unwraps_real_planreader_wrapper_chain_to_original_core_page()
    test_unwrap_leaves_an_unwrapped_function_unchanged()
    test_guard_apply_is_idempotent()
    print("3D wrapper guard v1.2.14 tests passed")
