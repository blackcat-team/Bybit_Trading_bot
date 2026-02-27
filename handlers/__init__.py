"""
handlers package — re-exports для main.py и тестов.
"""

from handlers.signal_parser import parse_and_trade, parse_signal

from handlers.callbacks import (
    start_trading, stop_trading, check_positions,
    send_report, add_note_handler, button_handler,
    set_risk_command, view_orders, on_startup_check,
)

from handlers.preflight import (
    _safe_float, get_available_usd, floor_qty,
    validate_qty, clip_qty,
)
