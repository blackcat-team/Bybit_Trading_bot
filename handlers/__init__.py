"""
Facade package — keeps existing `from handlers import ...` and
`from handlers.xxx import ...` working after move to bybit_bot/.
"""

import sys

from bybit_bot.handlers import (                    # noqa: F401
    parse_and_trade, parse_signal,
    start_trading, stop_trading,
    set_risk_command, add_note_handler,
    button_handler,
    view_orders, view_symbol_orders,
    check_positions,
    send_report,
    on_startup_check,
    _safe_float, get_available_usd, floor_qty,
    validate_qty, clip_qty,
)

# Register submodule aliases so `from handlers.xyz import ...` resolves
# to the real modules under bybit_bot.handlers.xyz.
_SUBMODULES = [
    'buttons', 'callbacks', 'commands', 'orders', 'preflight',
    'signal_parser', 'ui', 'views_orders', 'views_positions',
    'reporting', 'startup',
]
for _name in _SUBMODULES:
    _key = f'bybit_bot.handlers.{_name}'
    if _key in sys.modules:
        sys.modules[f'handlers.{_name}'] = sys.modules[_key]
