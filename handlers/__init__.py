"""
handlers package — re-exports для main.py и тестов.
"""

from handlers.signal_parser import parse_and_trade, parse_signal

from handlers.commands import (
    start_trading, stop_trading,
    set_risk_command, add_note_handler,
)

from handlers.buttons import button_handler

from handlers.views_orders import view_orders, view_symbol_orders

from handlers.views_positions import check_positions

from handlers.reporting import send_report

from handlers.startup import on_startup_check

from handlers.preflight import (
    _safe_float, get_available_usd, floor_qty,
    validate_qty, clip_qty,
)
