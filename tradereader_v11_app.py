"""TradeReader 3D v1.1 production entry point.

Keeps the v1.0 multi-trade core stable while adding pluggable trade-specific
estimating modules. Plastering / Linings is the first deep trade module.
"""

from __future__ import annotations

import tradereader_app as base
import tradereader_trade_modules as trade_modules


APP_VERSION = "1.1.0"
base.APP_VERSION = APP_VERSION
base.app.APP_VERSION = APP_VERSION

# Add module-specific systems to the shared editor without changing the
# Premier Brushworks PlanReader process (TradeReader runs in a separate process).
trade_modules.prepare_module_options(base)

_original_trade_prompt = base._trade_prompt
_original_trade_takeoff_page = base.trade_takeoff_page


def _v11_trade_prompt(profile):
    prompt = _original_trade_prompt(profile)
    trade_name = str(profile.get("name") or "")
    addendum = trade_modules.prompt_addendum(trade_name)
    if addendum:
        prompt = prompt.rstrip() + "\n\n" + addendum.strip() + "\n"
    return prompt


def _v11_trade_takeoff_page(workspace, session_api_key, ai_provider="OpenAI"):
    trade_name, _profile = base.current_trade()
    if trade_modules.get_trade_module(trade_name) is None:
        return _original_trade_takeoff_page(workspace, session_api_key, ai_provider)

    common_tab, tools_tab = base.app.st.tabs(
        ["AI & common take-off", f"{trade_name} trade tools"]
    )
    with common_tab:
        _original_trade_takeoff_page(workspace, session_api_key, ai_provider)
    with tools_tab:
        trade_modules.render_trade_tools(base, workspace, trade_name)


base._trade_prompt = _v11_trade_prompt
base.trade_takeoff_page = _v11_trade_takeoff_page


if __name__ == "__main__":
    base.main()
