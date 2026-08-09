from __future__ import annotations

from typing import Any, Dict, Optional

import tradereader_plastering as plastering

TRADE_MODULES = {
    "Plastering / Linings": plastering,
}


def get_trade_module(trade_name: str):
    return TRADE_MODULES.get(str(trade_name or ""))


def prompt_addendum(trade_name: str) -> str:
    module = get_trade_module(trade_name)
    if module is None:
        return ""
    fn = getattr(module, "prompt_addendum", None)
    return str(fn() if callable(fn) else "")


def prepare_module_options(base) -> None:
    """Expose trade-specific system choices in the shared take-off editor."""
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
