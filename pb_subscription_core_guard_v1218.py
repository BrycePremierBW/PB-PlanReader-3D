"""PlanReader v1.2.18 stable Subscription Take-off core-page guard.

The v1.2.17 selected-page wrapper needs access to the original Subscription
Take-off page.  Closure introspection is fragile because Python only creates
closure cells for values actually referenced by a wrapper, and wrapper names can
change across patches.  Capture the core function explicitly before v1.2.16 is
installed and teach v1.2.17's resolver to prefer that stable reference.
"""
from __future__ import annotations

from typing import Any, Callable

import pb_selected_pages_v1217 as selected_v1217


def apply(app: Any) -> None:
    """Capture the unwrapped take-off page and make v1.2.17 resolve it safely."""
    if getattr(app, "_pb_subscription_core_guard_v1218_applied", False):
        return
    app._pb_subscription_core_guard_v1218_applied = True

    core_page = getattr(app, "_pb_core_subscription_takeoff_page", None)
    if not callable(core_page):
        core_page = app.subscription_takeoff_page
        app._pb_core_subscription_takeoff_page = core_page

    fallback_finder: Callable[..., Any] = selected_v1217._find_core_subscription_page

    def _stable_core_finder(_wrapper):
        stable = getattr(app, "_pb_core_subscription_takeoff_page", None)
        if callable(stable):
            return stable
        return fallback_finder(_wrapper)

    selected_v1217._find_core_subscription_page = _stable_core_finder
