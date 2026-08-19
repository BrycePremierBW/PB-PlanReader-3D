from __future__ import annotations

import unittest
from types import SimpleNamespace

import pb_no_ai_takeoff_v1216 as noai_v1216
import pb_selected_pages_v1217 as selected_v1217
import pb_subscription_core_guard_v1218 as guard_v1218


class SubscriptionCoreGuardV1218Tests(unittest.TestCase):
    def _app(self):
        def legacy_takeoff_renderer(workspace, session_api_key="", ai_provider="OpenAI"):
            return (workspace, session_api_key, ai_provider)

        return SimpleNamespace(
            subscription_takeoff_page=legacy_takeoff_renderer,
            ldf=lambda *_args, **_kwargs: None,
            lquery=lambda *_args, **_kwargs: [],
        ), legacy_takeoff_renderer

    def test_guard_captures_core_before_wrappers(self):
        app, core = self._app()
        guard_v1218.apply(app)
        self.assertIs(app._pb_core_subscription_takeoff_page, core)
        self.assertIs(selected_v1217._find_core_subscription_page(lambda: None), core)

    def test_real_patch_order_no_longer_requires_closure_name_search(self):
        app, core = self._app()

        guard_v1218.apply(app)
        noai_v1216.apply(app)
        selected_v1217.apply(app)

        self.assertIs(app._pb_core_subscription_takeoff_page, core)
        selected_core = app._pb_selected_subscription_core_v1217
        self.assertEqual(selected_core({"id": 7}, "key", "OpenAI"), ({"id": 7}, "key", "OpenAI"))
        self.assertTrue(getattr(app, "_pb_selected_pages_v1217_applied", False))

    def test_guard_is_idempotent_and_does_not_replace_captured_core(self):
        app, core = self._app()
        guard_v1218.apply(app)
        app.subscription_takeoff_page = lambda *_args, **_kwargs: "wrapped"
        guard_v1218.apply(app)
        self.assertIs(app._pb_core_subscription_takeoff_page, core)


if __name__ == "__main__":
    unittest.main()
