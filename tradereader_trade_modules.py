from __future__ import annotations

"""TradeReader specialist-module registry.

Plastering / Linings keeps its dedicated deep module. Every other built-in or
user-named trade receives the universal source-based specialist engine.
"""

from typing import Any, Dict

import tradereader_plastering as plastering
import tradereader_universal_specialist as universal
from tradereader_profiles import TRADE_OPTIONS


class _UniversalModule:
    def __init__(self, trade_name: str):
        self.trade_name = trade_name
        self.SYSTEM_TYPES = universal.system_types(trade_name)

    def prompt_addendum(self) -> str:
        return universal.prompt_addendum(self.trade_name)

    def render(self, app: Any, workspace: Dict[str, Any]) -> None:
        universal.render(app, workspace, self.trade_name)


TRADE_MODULES = {
    trade: (plastering if trade == "Plastering / Linings" else _UniversalModule(trade))
    for trade in TRADE_OPTIONS
}


def get_trade_module(trade_name: str):
    name = str(trade_name or "").strip()
    if name in TRADE_MODULES:
        return TRADE_MODULES[name]
    # current_trade() returns the actual user-entered custom name.
    if name:
        return _UniversalModule(name)
    return None


def prompt_addendum(trade_name: str) -> str:
    module = get_trade_module(trade_name)
    fn = getattr(module, "prompt_addendum", None) if module else None
    return str(fn() if callable(fn) else "")


def prepare_module_options(base) -> None:
    for module in TRADE_MODULES.values():
        for system in getattr(module, "SYSTEM_TYPES", []):
            if system not in base.GENERIC_SYSTEMS:
                base.GENERIC_SYSTEMS.append(system)
            if system not in base.app.FINISH_SYSTEMS:
                base.app.FINISH_SYSTEMS.append(system)


def render_trade_tools(base, workspace: Dict[str, Any], trade_name: str) -> bool:
    module = get_trade_module(trade_name)
    if module is None:
        return False
    render_fn = getattr(module, "render", None)
    if not callable(render_fn):
        return False
    render_fn(base.app, workspace)
    return True
