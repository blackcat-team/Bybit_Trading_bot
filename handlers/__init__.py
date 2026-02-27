"""
handlers package — re-exports для main.py и тестов.
"""

from .signal_parser import parse_and_trade, parse_signal

from .commands import (
    start_trading, stop_trading,
    set_risk_command, add_note_handler,
)

from .buttons import button_handler

from .views_orders import view_orders, view_symbol_orders

from .views_positions import check_positions

from .reporting import send_report

from .startup import on_startup_check

from .preflight import (
    _safe_float, get_available_usd, floor_qty,
    validate_qty, clip_qty,
)

from . import callbacks  # noqa: F401 — ensure submodule is importable
