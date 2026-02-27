"""
Facade — keeps main.py and tests working without import changes.
All implementation lives in handlers/ subpackage.
"""

from handlers import (                          # noqa: F401
    # TG command/callback handlers (main.py needs these)
    start_trading, stop_trading, check_positions,
    send_report, add_note_handler, button_handler,
    parse_and_trade, set_risk_command, view_orders,
    on_startup_check,
    # Pure helpers (tests need these)
    _safe_float, get_available_usd, floor_qty,
    validate_qty, clip_qty,
    # Signal parser (for future tests)
    parse_signal,
)
